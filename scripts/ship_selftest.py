#!/usr/bin/env python3
"""Offline checks of ship_approved.py and its connections (ask_human --release, the gate controller, review_inbox_status, sfconfig).

Registered into the suite by epic_selftest.main (register(T)); uses its helpers (sandbox, expect, fake_prepare, ...). Everything runs on a fake world:
gh / git / make answer from `ShipWorld` through C._sh, Notion is a few fake functions, the state goes to the sandbox's temporary directory.
Nothing here reaches GitHub, Notion, the network or a real repository.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import epic_common as C
import ship_approved as S

T = None  # the epic_selftest module (helpers), set by register()
CHECKS = []
REPO = "example-org/sf"   # registered in tests/config.test.toml: test / deploy (make ship = make test → 照合 → make ship-tested, check, smoke) / cron_paths
BRANCH = "michiomochi/x"
KEY = "e1e2e3e4-承認"
EPIC = "00000000-0000-4000-8000-0000000000e1"
HEAD = "a" * 40
BODY_MD = ("タイトル：機能Xを足す\n\n## 概要\n[計画](https://app.notion.com/p/abc123) を実装した\n## 背景\n理由 https://www.notion.so/zzz 参照\n## 変更内容\n- scripts/a.py\n## 検証\n- make test rc=0\n")


def check(fn):
    CHECKS.append(fn)
    return fn


def register(mod):
    """Add the checks to the suite of `mod` (the epic_selftest module) once."""
    global T
    if T is None:
        T = mod
        mod.TESTS.extend(CHECKS)


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class ShipWorld:
    """Fake GitHub (REST by `gh api`, `gh pr ready`), git, make and the review row. `calls` = every command as a short label in order."""

    def __init__(self, repo=REPO, branch=BRANCH):
        self.repo, self.branch = repo, branch
        self.prs, self.comments, self.deleted = {}, {}, []
        self.calls, self.boom = [], {}
        self.remote = {branch: HEAD}
        self.diff = ["scripts/a.py"]
        self.make_rc, self.git_rc = {}, {}
        self.draft_supported, self.http_fail, self.mergeable, self.merge_http = True, None, True, None
        self.ready_http = None    # `gh pr ready` (publish) answers with this HTTP status
        self.checks = []
        self.cur_branch, self.dirty, self.deploy_diff = "main", "", ""
        self.status = {KEY: ["レビュー待ち"]}     # review row key -> statuses returned one per read (the last repeats)
        self.notes, self.hub, self.ticks = [], [], []
        self.merged_by_world = 0
        self.writes_record, self.tested_head = True, "c" * 40   # a green `make test` in the main tree leaves tested_tree.json with this head (the fake HEAD is c*40)

    # -- fake C._sh
    def sh(self, cmd, input=None, timeout=90):
        if os.path.basename(cmd[0]) == "sh" and cmd[1:2] == ["-c"]:
            cwd, real = cmd[3], cmd[4:]
            return self.run(real, cwd, None)
        return self.run(cmd, None, input)

    def log(self, label):
        self.calls.append(label)
        if label in self.boom:
            raise self.boom.pop(label)

    def run(self, cmd, cwd, stdin):
        name = os.path.basename(cmd[0])
        if name == "gh":
            return self.gh(cmd[1:], stdin)
        if name == "git":
            return self.git(cmd[1:])
        if name == "make":
            label = "make " + " ".join(x for x in cmd[1:] if not x.startswith("TESTED_TREE=")) + ("@wt" if cwd and cwd.endswith("wt") else "")
            self.log(label)
            rc = self.make_rc.get(label, 0)
            if label == "make test" and rc == 0 and self.writes_record:   # scripts/tested_tree.py write --file $(TESTED_TREE)
                rec = next(x.split("=", 1)[1] for x in cmd if x.startswith("TESTED_TREE="))   # ship_approved names the file it will read
                C.atomic_write_json(rec, dict(path="/fake/" + self.repo, head=self.tested_head))
            return R(rc, "ok\n")
        if name == "rm":
            return R()
        raise AssertionError(f"unexpected command {cmd}")

    def err(self, status, text=""):
        return R(1, "", f"gh: {text} (HTTP {status})")

    def pr_view(self, p):
        return dict(p)

    def gh(self, a, stdin):
        if a[:2] == ["pr", "ready"]:
            n = int(a[2])
            undo = "--undo" in a
            self.log(f"gh pr ready{' --undo' if undo else ''} {n}")
            if self.ready_http and not undo:
                return self.err(self.ready_http, "forbidden")
            self.prs[n]["draft"] = undo
            return R()
        method, path = a[a.index("-X") + 1], a[a.index("-X") + 2]
        body = json.loads(stdin) if stdin else None
        short = re.sub(r"^repos/[^/]+/[^/]+/", "", path.split("?")[0])
        self.log(f"gh {method} {short}")
        if self.http_fail and method != "GET":
            return self.err(self.http_fail, "forbidden")
        if method == "GET" and short == "pulls":
            return R(0, json.dumps([p for p in self.prs.values() if p["state"] == "open" and p["head"]["ref"] in urllib.parse.unquote(path)]))
        if method == "POST" and short == "pulls":
            if body.get("draft") and not self.draft_supported:
                return self.err(422, "Draft pull requests are not supported in this repository")
            n = 31 + len(self.prs)
            self.prs[n] = dict(number=n, html_url=f"https://github.com/{self.repo}/pull/{n}", title=body["title"], body=body["body"], draft=body["draft"],
                               state="open", merged=False, merge_commit_sha=None, head=dict(sha=self.remote[body["head"]], ref=body["head"]),
                               base=dict(ref=body["base"]), mergeable=self.mergeable, changed_files=len(self.diff))
            return R(0, json.dumps(self.prs[n]))
        m = re.fullmatch(r"pulls/(\d+)", short)
        if m:
            p = self.prs[int(m.group(1))]
            if method == "PATCH":
                p.update({k: v for k, v in body.items() if k in ("title", "body", "state")})
            elif method == "GET":
                p["head"]["sha"] = self.remote.get(p["head"]["ref"], p["head"]["sha"]) if p["state"] == "open" and not p["merged"] else p["head"]["sha"]
                p["mergeable"] = self.mergeable
            return R(0, json.dumps(p))
        m = re.fullmatch(r"pulls/(\d+)/merge", short)
        if m:
            p = self.prs[int(m.group(1))]
            if self.merge_http:
                return self.err(self.merge_http, "Head branch was modified")
            if body["sha"] != p["head"]["sha"] or body["merge_method"] != "squash" or p["draft"]:
                return self.err(409, "Head branch was modified")
            p["merged"], p["state"], p["merge_commit_sha"] = True, "closed", "9" * 40
            self.merged_by_world += 1
            return R(0, json.dumps(dict(sha="9" * 40, merged=True)))
        if short.startswith("commits/") and short.endswith("/check-runs"):
            return R(0, json.dumps(dict(check_runs=self.checks)))
        m = re.fullmatch(r"issues/(\d+)/comments", short)
        if m:
            lst = self.comments.setdefault(int(m.group(1)), [])
            if method == "POST":
                lst.append(body)
            return R(0, json.dumps(lst))
        if method == "DELETE" and short.startswith("git/refs/heads/"):
            self.deleted.append(short.split("heads/", 1)[1])
            self.remote.pop(short.split("heads/", 1)[1], None)
            return R(0, "")
        raise AssertionError(f"unexpected gh call {a}")

    def git(self, a):
        a = list(a)
        while "-c" in a:
            i = a.index("-c")
            del a[i:i + 2]
        a = a[2:] if a[:1] == ["-C"] else a
        sub = a[0]
        label = "git " + " ".join(x for x in a[:3] if not x.startswith("/"))
        self.log(label)
        if label in self.git_rc:
            return R(self.git_rc[label], "", "fail")
        if sub == "ls-remote":
            br = a[-1].split("heads/", 1)[1]
            return R(0, f"{self.remote[br]}\trefs/heads/{br}\n" if br in self.remote else "")
        if sub == "diff":
            return R(0, "\n".join(self.diff) + "\n" if "..HEAD" not in a[-1] and "..." in a[-1] else self.deploy_diff)
        if sub == "show":
            return R(1, "", "not found")
        if sub == "rev-parse":
            return R(0, (self.cur_branch if "--abbrev-ref" in a else "c" * 40 if a[-1] == "HEAD" else "b" * 40) + "\n")
        if sub == "status":
            return R(0, self.dirty)
        return R(0, "")

    # -- fake Notion
    def ntn(self, *args, data=None, **kw):
        if args[0].endswith("/query"):
            key = data["filter"]["rich_text"]["equals"]
            if key not in self.status:
                return {"results": [], "has_more": False}
            return {"results": [{"id": "row-" + key, "url": "https://app.notion.com/p/row", "key": key, "properties": {}}], "has_more": False}
        if args[0].startswith("v1/pages/") and "PATCH" in args:
            self.ticks.append(data)
            return {}
        raise AssertionError(f"unexpected ntn {args}")

    def append(self, page_id, heading, children):
        text = children[0][children[0]["type"]]["rich_text"][0]["text"]["content"]
        (self.notes if heading == "反映の記録" else self.hub).append((heading, text))

    def next_status(self, row):
        """The status a read of the row gives (S.status_of_row is called once per decision read; writing a note does not consume one)."""
        seq = self.status[row["key"]]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    # -- helpers for the checks
    def writes(self):
        return [c for c in self.calls if re.match(r"gh (POST|PUT|PATCH|DELETE)|gh pr ready|make ", c)]

    def approve(self, key=KEY, *more):
        self.status[key] = ["承認", *more] if more else ["承認"]


@contextlib.contextmanager
def ship_env(w=None, body=BODY_MD):
    w = w or ShipWorld()
    with T.sandbox() as (ctl, tmp):
        saved = (C._sh, C.ntn, C.append_to_chapter, S.repo_dir, S.lock_free, S.read_inputs, S.DRY, S.status_of_row)
        C._sh, C.ntn, C.append_to_chapter = w.sh, w.ntn, w.append
        S.status_of_row = w.next_status
        S.repo_dir = lambda repo: Path("/fake/" + repo)
        S.lock_free = lambda: True
        S.read_inputs = lambda epic, repo=None, branch=None: (w.repo, w.branch, body)
        S.DRY = False
        try:
            yield w
        finally:
            C._sh, C.ntn, C.append_to_chapter, S.repo_dir, S.lock_free, S.read_inputs, S.DRY, S.status_of_row = saved


def prepared(w, key=KEY):
    res = S.prepare(EPIC, key)
    T.expect(res["status"] == "ok", res)
    return res


# ---------------------------------------------------------------- prepare

@check
def prepare_opens_a_checked_draft_pr_without_notion_words_and_never_twice():
    with ship_env() as w:
        res = prepared(w)
        pr = w.prs[31]
        T.expect(res["pr"] == 31 and res["pr_url"] == pr["html_url"] and res["head"] == HEAD, res)
        T.expect(pr["draft"] is True and pr["base"]["ref"] == "main" and pr["head"]["ref"] == BRANCH and pr["title"] == "機能Xを足す", pr)
        T.expect(pr["body"].rstrip().endswith(C.SHIP_MARK) and not re.search(r"notion|タスク DB", pr["body"], re.I) and "計画 を実装した" in pr["body"], pr["body"])
        rec = C.ship_record(KEY)
        T.expect(rec["stage"] == "drafted" and rec["pr"] == 31 and rec["head"] == HEAD and rec["repo"] == REPO and rec["branch"] == BRANCH, rec)
        n = len(w.writes())
        prepared(w)
        T.expect(len(w.writes()) == n and len(w.prs) == 1, "the second prepare writes nothing and opens no second PR")
        w.prs[31]["draft"] = False  # someone published it by hand: prepare turns it back into a draft
        prepared(w)
        T.expect(w.prs[31]["draft"] is True and "gh pr ready --undo 31" in w.calls, w.calls)


@check
def prepare_holds_for_each_of_the_eight_reasons_and_retries_a_transient_failure():
    def code(w, **kw):
        with ship_env(w, **kw):
            res = S.prepare(EPIC, KEY)
            T.expect(C.ship_record(KEY) is None, "a held prepare records nothing")
            return res["status"], res["code"]
    w = ShipWorld(); w.repo = None
    T.expect(code(w) == ("hold", "no-branch"), "no repository / branch in the pages")
    w = ShipWorld(); w.remote = {}
    T.expect(code(w) == ("hold", "branch-not-pushed"), "the branch is not on origin")
    w = ShipWorld(); w.diff = []
    T.expect(code(w) == ("hold", "no-diff"), "no difference to main")
    T.expect(code(ShipWorld(), body="") == ("hold", "no-pr-body"), "empty PR body page")
    T.expect(code(ShipWorld(), body="## 概要\nx") == ("hold", "pr-body-shape"), "no title line")
    T.expect(code(ShipWorld(), body=BODY_MD.replace("## 検証\n- make test rc=0\n", "")) == ("hold", "pr-body-shape"), "a section is missing")
    T.expect(code(ShipWorld(), body=BODY_MD.replace("- make test rc=0", "")) == ("hold", "pr-body-shape"), "a section is empty")
    w = ShipWorld(); w.mergeable = False
    T.expect(code(w) == ("hold", "conflict"), "GitHub says mergeable=false")
    w = ShipWorld(); w.http_fail = 403
    T.expect(code(w) == ("hold", "gh-permission"), "403")
    w = ShipWorld(); w.draft_supported = False
    T.expect(code(w) == ("hold", "draft-unsupported"), "the repository cannot hold draft PRs")
    w = ShipWorld(); w.http_fail = 502
    T.expect(code(w)[0] == "error", "a 5xx is transient: exit 1, no HOLD")
    with ship_env(ShipWorld()) as w:  # a PR page that still carries a private mark is refused, not published
        cfg_id = C.CFG["notion"]["task_ds"]
        S.read_inputs = lambda *a, **k: (REPO, BRANCH, BODY_MD.replace("理由", f"理由 {cfg_id}"))
        T.expect(S.prepare(EPIC, KEY)["code"] == "pr-body-shape" and not w.prs, "a data source id in the body")


@check
def the_pr_body_is_the_judgements_chapter_and_the_old_child_page_is_the_fallback():
    judg = '# 推奨 {color="yellow_bg"}\n推奨：承認\n\n# PR 本文 {color="yellow_bg"}\n' + BODY_MD + '\n# 戻し方 {color="yellow_bg"}\nなし\n'
    chap = S.pr_chapter(judg)
    T.expect(chap.startswith("タイトル：機能Xを足す") and "## 検証" in chap and "戻し方" not in chap and "推奨" not in chap, chap)
    T.expect(S.pr_chapter("# 推奨\nx\n") is None and S.pr_chapter('# PR 本文 {color="yellow_bg"}\n\n# 戻し方\nx\n') == "", "no chapter = None, an empty chapter = ''")
    saved = (C.phase_rows, C.decision_page_ref, C.ntn, C.blocks, C.evidence_text)
    page = {}
    C.phase_rows = lambda e: {"リリース判定": {"id": "REL"}}
    C.decision_page_ref = lambda p: {"id": "JUDG"}
    C.blocks = lambda i: [{"type": "child_page", "id": "OLD", "t": "PR 本文の下書き"}]
    C.evidence_text = lambda b: b.get("t", "")
    C.ntn = lambda path: {"markdown": page["judg"] if "JUDG" in path else "タイトル：旧ページ\n"}
    try:
        page["judg"] = judg
        T.expect(S.read_inputs(EPIC, REPO, BRANCH)[2] == chap, "the chapter wins and no page is needed")
        page["judg"] = "# 推奨\n推奨：承認\n"
        T.expect(S.read_inputs(EPIC, REPO, BRANCH)[2] == "タイトル：旧ページ\n", "a judgement without the chapter (L3 / an old row): the child page")
        page["judg"] = '# PR 本文 {color="yellow_bg"}\n\n'
        T.expect(S.read_inputs(EPIC, REPO, BRANCH)[2] == "タイトル：旧ページ\n", "an empty chapter: the child page")
        C.blocks = lambda i: []
        T.expect(S.read_inputs(EPIC, REPO, BRANCH)[2] is None, "nothing anywhere: no body (HOLD no-pr-body)")
    finally:
        C.phase_rows, C.decision_page_ref, C.ntn, C.blocks, C.evidence_text = saved
    with ship_env(ShipWorld(), body=S.pr_chapter(judg)) as w:
        res = S.prepare(EPIC, KEY)
        T.expect(res["status"] == "ok" and res["title"] == "機能Xを足す" and w.prs, res)
    hint = judg.replace("機能Xを足す", "〈PR のタイトル〉")
    with ship_env(ShipWorld(), body=S.pr_chapter(hint)) as w:
        res = S.prepare(EPIC, KEY)
        T.expect((res["status"], res["code"]) == ("hold", "pr-body-shape") and not w.prs, "a writing hint left in the chapter is refused, not published")


@check
def prepare_dry_run_reads_everything_and_writes_nothing():
    with ship_env() as w:
        res = S.prepare(EPIC, KEY, dry_run=True)
        T.expect(res["status"] == "ok" and w.writes() == [] and C.ship_record(KEY) is None and not S.DRY, (res, w.calls))
        T.expect(not C.state_path(S.SHIP_EVENTS_FILE).exists(), "a dry run made ship_events.jsonl")


@check
def prepare_writes_one_ship_event_per_stage_change_not_per_call():
    with ship_env() as w:
        prepared(w)
        prepared(w)  # the same stage again (drafted -> drafted): no second line
        lines = [json.loads(x) for x in C.state_path(S.SHIP_EVENTS_FILE).read_text(encoding="utf-8").splitlines()]
        T.expect(len(lines) == 1 and lines[0]["key"] == KEY and lines[0]["stage"] == "drafted" and lines[0]["mode"] == "human" and lines[0]["pr"] == 31
                 and isinstance(lines[0]["ts"], int) and "test_seconds" not in lines[0], lines)


@check
def ask_human_release_makes_no_row_when_no_draft_pr_can_be_had():
    import ask_human as ah
    saved = (C.ntn, C.collect_release_facts)
    db = T._ReviewDB()
    try:
        C.ntn = db.ntn
        C.collect_release_facts = lambda epic, key, root=None: dict(T._facts(key=key), verdict=T._verdict())
        for over, rc, word in ((dict(status="hold", code="no-pr-body", reason="空"), 3, "HOLD k-承認 no-pr-body: 空"), (dict(status="error", code="gh", reason="502"), 1, "ERROR k-承認")):
            with T.fake_prepare(**over):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    got = ah.main(["--release", "--epic", T._id(5), "--key", "k-承認"])
            T.expect(got == rc and out.getvalue().startswith(word) and db.created == [], (got, out.getvalue()))
        with T.fake_prepare() as calls:  # an ok prepare puts the PR URL into 何を出すか and chapter 1
            C.STATE_DIR, keep = Path(tempfile.mkdtemp()), C.STATE_DIR
            ah.notify = lambda *a, **k: None
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                got = ah.main(["--release", "--epic", T._id(5), "--key", "k-承認"])
            C.STATE_DIR = keep
            body = db.created[-1]["markdown"]
            T.expect(got == 0 and "https://github.com/example-org/example-app/pull/31" in body and "下書きの [PR #31]" in body and len(calls) == 1, (got, out.getvalue(), body[:300]))
    finally:
        C.ntn, C.collect_release_facts = saved


# ---------------------------------------------------------------- run: the approved ship

def ordered(calls, *labels):
    idx = [calls.index(x) for x in labels]
    return idx == sorted(idx)


@check
def run_ships_an_approved_draft_in_the_exact_order_and_a_second_run_does_nothing():
    with ship_env() as w:
        prepared(w)
        lines, rc = S.run()
        T.expect((lines, rc) == ([], 0), "a draft that waits for the person: silent")
        T.expect(w.writes() == ["gh POST pulls"], w.writes())
        w.approve()
        lines, rc = S.run()
        T.expect(rc == 0 and len(lines) == 1 and lines[0].startswith(f"SHIPPED {KEY} pr=#31 merge=999999999999 steps="), lines)
        T.expect(ordered(w.calls, "make test@wt", "gh pr ready 31", "gh PUT pulls/31/merge", f"gh DELETE git/refs/heads/{BRANCH}", "make test", "make ship-tested", "make check", "make smoke"), w.calls)
        T.expect(w.merged_by_world == 1 and w.prs[31]["merged"] and w.deleted == [BRANCH], (w.merged_by_world, w.deleted))
        T.expect(C.ship_record(KEY)["stage"] == "done" and w.ticks == [{"properties": {"反映済み": {"checkbox": True}}}], (C.ship_record(KEY), w.ticks))
        T.expect(any("make test rc=0" in t and "make ship-tested rc=0" in t and "make smoke rc=0" in t for h, t in w.notes) and any("PR #31" in t and "999999999999" in t for h, t in w.notes), w.notes)
        T.expect(any(h == "管理ログ" and "リリース承認 承認" in t for h, t in w.hub), w.hub)
        T.expect(not any("notion" in c["body"].lower() or "タスク DB" in c["body"] for c in w.comments[31]), w.comments)
        n = len(w.calls)
        for _ in range(3):
            T.expect(S.run() == ([], 0), "done: nothing to say")
        T.expect(len(w.calls) == n, "done: not a single gh / git / make call")
        # the squash commit carries the title and an empty body
        T.expect(S.SHIP_MARK in w.prs[31]["body"], "kept")


@check
def run_survives_a_crash_between_any_two_steps_without_a_second_merge():
    for crash in ("readied", "merged", "deployed"):
        with ship_env() as w:
            prepared(w)
            w.approve()
            real = S.update
            hit = []

            def boom(key, **f):
                if f.get("stage") == crash and not hit:
                    hit.append(1)
                    if crash == "merged":
                        real(key, **f)  # (the merge happened on GitHub; the record is lost with the crash: simulate it by not saving)
                        raise RuntimeError("crash after the merge")
                    raise RuntimeError("crash")
                return real(key, **f)
            S.update = boom
            try:
                lines, rc = S.run()
                T.expect(rc == 1 and lines[0].startswith("ERROR"), (crash, lines))
            finally:
                S.update = real
            lines, rc = S.run()
            T.expect(rc == 0 and lines and lines[0].startswith("SHIPPED"), (crash, lines))
            T.expect(w.merged_by_world == 1 and w.calls.count("gh PUT pulls/31/merge") <= 2, (crash, w.calls))
            T.expect(w.calls.count("make ship-tested") <= 2, f"{crash}: the deploy is repeated at most once")
            T.expect(C.ship_record(KEY)["stage"] == "done", crash)


@check
def run_stops_before_the_merge_when_the_ground_moved_and_records_it_once():
    cases = []
    w = ShipWorld(); cases.append(("head-moved", w, lambda: w.remote.__setitem__(BRANCH, "d" * 40)))
    w = ShipWorld(); cases.append(("ci-red", w, lambda: w.checks.extend([dict(name="build", status="completed", conclusion="failure")])))
    w = ShipWorld(); w.make_rc["make test@wt"] = 1; cases.append(("test-red", w, lambda: None))
    w = ShipWorld(); w.git_rc["git merge --no-ff --no-edit"] = 1; cases.append(("conflict", w, lambda: None))
    w = ShipWorld(); w.merge_http = 409; cases.append(("conflict", w, lambda: None))
    w = ShipWorld(); w.merge_http = 403; cases.append(("gh-permission", w, lambda: None))   # the merge PUT is refused after `gh pr ready`
    w = ShipWorld(); w.ready_http = 403; cases.append(("gh-permission", w, lambda: None))   # the publish itself is refused
    for code, w, change in cases:
        with ship_env(w):
            prepared(w)
            w.approve()
            change()
            w.git_rc.setdefault("git merge --no-ff --no-edit", 0)
            if code == "conflict":
                w.git_rc = {k: v for k, v in w.git_rc.items()}
            lines, rc = S.run()
            T.expect(lines and lines[0].startswith(f"VOID {KEY} {code}"), (code, lines))
            T.expect(w.merged_by_world == 0 and w.prs[31]["draft"] is True and C.ship_record(KEY)["stage"] == "void", (code, w.prs[31]["draft"], w.calls))
            T.expect(sum(1 for h, t in w.notes if "マージ前に止めました" in t) == 1 and len(w.comments.get(31, [])) == 1, (code, w.notes, w.comments))
            has_cand = any(h == "候補" for h, _ in w.hub)
            T.expect(has_cand == (code != "head-moved"), (code, w.hub))
            T.expect(S.run() == ([], 0) and len(w.comments[31]) == 1, "void is final for this key: nothing more, no second comment")
    with ship_env() as w:  # a check that is still pending, or no check at all, does not stop the ship
        prepared(w)
        w.approve()
        w.checks.append(dict(name="slow", status="in_progress", conclusion=None))
        T.expect(S.run()[0][0].startswith("SHIPPED"), "pending checks are not waited for")


@check
def run_voids_a_refused_merge_once_and_prints_a_repeating_transient_error_once():
    with ship_env() as w:  # a 403 on the merge PUT (after `gh pr ready`): void gh-permission, a draft again, one record each, then silence
        prepared(w)
        w.approve()
        w.merge_http = 403
        lines, rc = S.run()
        T.expect(len(lines) == 1 and lines[0].startswith(f"VOID {KEY} gh-permission") and rc == 0, lines)
        T.expect(w.merged_by_world == 0 and w.prs[31]["draft"] is True and C.ship_record(KEY)["stage"] == "void", (w.prs[31]["draft"], w.calls))
        T.expect(sum(1 for h, t in w.notes if "マージ前に止めました" in t) == 1 and len(w.comments[31]) == 1, (w.notes, w.comments))
        T.expect(sum(1 for h, t in w.hub if h == "管理ログ" and "gh-permission" in t) == 1, w.hub)
        n = len(w.calls)
        for _ in range(2):
            T.expect(S.run() == ([], 0), "a void ship is silent")
        T.expect(len(w.calls) == n, "and calls nothing")
    with ship_env() as w:  # a temporary 5xx: retried every run, but the same ERROR text is printed only once; it clears and ships
        prepared(w)
        w.approve()
        w.merge_http = 502
        lines, rc = S.run()
        T.expect(len(lines) == 1 and lines[0].startswith(f"ERROR {KEY}") and rc == 1, lines)
        T.expect(S.run() == ([], 0) and S.run() == ([], 0), "the same ERROR text is not repeated every 3 minutes")
        T.expect(C.ship_record(KEY)["stage"] == "readied" and w.merged_by_world == 0, C.ship_record(KEY))
        w.merge_http = None
        lines, rc = S.run()
        T.expect(len(lines) == 1 and lines[0].startswith(f"SHIPPED {KEY}") and rc == 0 and not C.ship_record(KEY).get("last_error"), lines)


@check
def run_gives_way_when_the_approval_is_taken_back_and_after_a_merge_does_not_deploy():
    for reads, expect_stage in ((["承認", "レビュー待ち"], "drafted"), (["承認", "承認", "レビュー待ち"], "drafted")):
        with ship_env() as w:
            prepared(w)
            w.status[KEY] = list(reads)
            lines, rc = S.run()
            T.expect(w.merged_by_world == 0 and w.prs[31]["draft"] is True and C.ship_record(KEY)["stage"] == expect_stage, (reads, lines, w.calls))
    with ship_env() as w:  # taken back after the merge: main is not touched, the deploy is not run, a person gets it back
        prepared(w)
        w.status[KEY] = ["承認", "承認", "承認", "レビュー待ち"]
        lines, rc = S.run()
        T.expect(lines[0].startswith(f"HALTED {KEY}") and w.merged_by_world == 1 and "make test" not in w.calls and "make ship-tested" not in w.calls, (lines, w.calls))
        T.expect(not any(re.search(r"revert|reset|push", c) for c in w.calls), "main is never put back by the script")


@check
def run_takes_over_an_outside_merge_waits_on_a_busy_deploy_lock_and_keeps_main_on_failure():
    with ship_env() as w:  # the PR was merged outside (the squash commit is main's): no merge call, the deploy goes on
        prepared(w)
        w.approve()
        w.prs[31].update(merged=True, state="closed", merge_commit_sha="7" * 40)
        lines, rc = S.run()
        T.expect(lines[0].startswith(f"SHIPPED {KEY} pr=#31 merge=777777777777") and "gh PUT pulls/31/merge" not in w.calls, (lines, w.calls))
    with ship_env() as w:  # rc 75 of the deploy's own lock = wait (silent, the stage stays merged); the next run finishes
        prepared(w)
        w.approve()
        w.make_rc["make ship-tested"] = 75
        T.expect(S.run() == ([], 0) and C.ship_record(KEY)["stage"] == "merged", C.ship_record(KEY))
        w.make_rc["make ship-tested"] = 0
        T.expect(S.run()[0][0].startswith("SHIPPED") and w.merged_by_world == 1, "finished by the next run")
    with ship_env() as w:  # .deploy.flock busy right now: wait without running anything
        prepared(w)
        w.approve()
        S.lock_free = lambda: False
        T.expect(S.run() == ([], 0) and "make test" not in w.calls and "make ship-tested" not in w.calls and C.ship_record(KEY)["stage"] == "merged", w.calls)
    with ship_env() as w:  # a failing step: main is not reverted, FAIL once, deploy_failed; retry runs it again
        prepared(w)
        w.approve()
        w.make_rc["make check"] = 2
        lines, rc = S.run()
        T.expect(rc == 1 and lines[0].startswith(f"FAIL {KEY} make check rc=2"), lines)
        T.expect(C.ship_record(KEY)["stage"] == "deploy_failed" and "make smoke" not in w.calls and w.merged_by_world == 1, w.calls)
        T.expect(not any(re.search(r"revert|reset|push", c) for c in w.calls), "no operation that puts main back")
        T.expect(S.run() == ([], 0), "no second FAIL line before the alert time")
        w.make_rc["make check"] = 0
        with contextlib.redirect_stdout(io.StringIO()):
            retried = S.cmd_retry(KEY)
        T.expect(retried == 0 and S.run()[0][0].startswith("SHIPPED") and w.merged_by_world == 1, "retry deploys again, never merges again")
    with ship_env() as w:  # a dirty main working tree: no deploy, nothing cleaned up
        prepared(w)
        w.approve()
        w.dirty = " M scripts/a.py\n"
        lines, rc = S.run()
        T.expect(lines[0].startswith(f"FAIL {KEY} main-tree") and "make test" not in w.calls and "make ship-tested" not in w.calls and C.ship_record(KEY)["stage"] == "deploy_failed", lines)
    with ship_env() as w:  # a cron definition in the diff: cron-sync dry run, wrappers, cron-sync before, cron-resume after
        prepared(w)
        w.approve()
        w.deploy_diff = "hermes/cron.toml\nscripts/a.py\n"
        T.expect(S.run()[0][0].startswith("SHIPPED"), w.calls)
        T.expect(ordered(w.calls, "make cron-sync FLAGS=--dry-run", "make wrappers", "make cron-sync", "make test", "make ship-tested", "make check", "make smoke", "make cron-resume"), w.calls)
    other = "example-org/other"
    with ship_env(ShipWorld(repo=other)) as w:  # a repository without a deploy registration: merged, never deployed
        prepared(w)
        w.approve()
        lines, rc = S.run()
        T.expect(lines[0].startswith(f"MERGED-ONLY {KEY}") and not any(c.startswith("make") for c in w.calls) and C.ship_record(KEY)["stage"] == "done", (lines, w.calls))
        T.expect(C.ship_meaning(other)["deploy"].startswith("本番反映はしません"), "chapter 1 says so before the person approves")


@check
def run_settles_a_rejection_and_a_withdrawal_without_any_merge_and_a_later_ship_rescues_a_failed_one():
    with ship_env() as w:
        prepared(w)
        w.status[KEY] = ["差し戻し"]
        lines, rc = S.run()
        T.expect(lines[0].startswith(f"REJECTED {KEY}") and w.prs[31]["draft"] is True and w.prs[31]["state"] == "open" and w.merged_by_world == 0, lines)
        T.expect(len(w.comments[31]) == 1 and "理由" not in w.comments[31][0]["body"].split("<!--")[0], w.comments)
        S.run()
        T.expect(len(w.comments[31]) == 1 and w.deleted == [], "once")
    with ship_env() as w:
        prepared(w)
        w.status[KEY] = ["取り下げ"]
        lines, rc = S.run()
        T.expect(lines[0].startswith(f"WITHDRAWN {KEY}") and w.prs[31]["state"] == "closed" and w.deleted == [] and w.merged_by_world == 0, lines)
    with ship_env() as w:  # a deploy_failed record is done when a later done ship contains its merge commit
        prepared(w)
        w.approve()
        w.make_rc["make smoke"] = 1
        S.run()
        S.update("zz-承認", repo=REPO, stage="done", merge_sha="8" * 40, pr=40)
        lines, rc = S.run()
        T.expect(lines and lines[0].startswith(f"SHIPPED {KEY} 後の出荷") and C.ship_record(KEY)["stage"] == "done", lines)


@check
def run_dry_run_writes_nothing_and_two_runs_never_overlap():
    with ship_env() as w:
        prepared(w)
        w.approve()
        n_state = json.dumps(C.ship_records(), sort_keys=True)
        before = list(w.calls)
        lines, rc = S.run(dry_run=True)
        T.expect(w.writes() == ["gh POST pulls"] and json.dumps(C.ship_records(), sort_keys=True) == n_state and not S.DRY, (w.writes(), lines))
        T.expect(any(l.startswith("WOULD ") for l in lines) and len(w.calls) > len(before), lines)
        T.expect(len(C.state_path(S.SHIP_EVENTS_FILE).read_text(encoding="utf-8").splitlines()) == 1, "a dry run that passes through update() added a line to ship_events.jsonl (only prepare's drafted line may be there)")
        lock = C.RunLock("ship_approved.lock")
        with lock:
            T.expect(lock.acquired and S.run() == ([], 0), "the second run ends silently while the first holds the lock")
            T.expect(w.writes() == ["gh POST pulls"], "and it does nothing")


@check
def deploy_is_refused_when_the_tree_that_was_tested_is_not_the_tree_to_ship():
    """段5: make test のあと、deploy(make ship-tested)の直前に tested_tree.json の head を、これから出荷する木の HEAD と照らす。"""
    with ship_env() as w:  # a record of another head (the test ran somewhere else / the tree moved): nothing is deployed, main is not put back
        prepared(w)
        w.approve()
        w.tested_head = "d" * 40
        lines, rc = S.run()
        T.expect(rc == 1 and lines[0].startswith(f"FAIL {KEY} tested-tree rc=1"), lines)
        T.expect(f"tested {'d' * 12} / now {'c' * 12}。make test を回してから出荷する" in lines[0], lines)
        T.expect(ordered(w.calls, "make test"), w.calls)
        T.expect(not any(c in w.calls for c in ("make ship-tested", "make check", "make smoke")), w.calls)
        T.expect(C.ship_record(KEY)["stage"] == "deploy_failed" and w.merged_by_world == 1, C.ship_record(KEY))
        T.expect(not any(re.search(r"revert|reset|push", c) for c in w.calls), "main is never put back by the script")
        T.expect(any("tested-tree" in t for h, t in w.notes), w.notes)
        w.tested_head = "c" * 40  # the person runs `retry` once the tree is right: the next run tests again and deploys
        with contextlib.redirect_stdout(io.StringIO()):
            T.expect(S.cmd_retry(KEY) == 0, "retry")
        T.expect(S.run()[0][0].startswith("SHIPPED") and w.calls.count("make ship-tested") == 1, w.calls)
    with ship_env() as w:  # the same head: it goes through (the normal ship is not stopped)
        prepared(w)
        w.approve()
        lines, rc = S.run()
        T.expect(rc == 0 and lines[0].startswith("SHIPPED") and ordered(w.calls, "make test", "make ship-tested", "make check", "make smoke"), (lines, w.calls))
    with ship_env() as w:  # make test left no record at all: refused (the safe side)
        prepared(w)
        w.approve()
        w.writes_record = False
        lines, rc = S.run()
        T.expect(rc == 1 and lines[0].startswith(f"FAIL {KEY} tested-tree") and "make ship-tested" not in w.calls, (lines, w.calls))
    with ship_env() as w:  # a red make test in the main tree: no gate, no deploy
        prepared(w)
        w.approve()
        w.make_rc["make test"] = 2
        lines, rc = S.run()
        T.expect(rc == 1 and lines[0].startswith(f"FAIL {KEY} make test rc=2") and "make ship-tested" not in w.calls, (lines, w.calls))
    with ship_env() as w:  # a repository whose registration has no make ship (e.g. only check / smoke) is not touched by the gate
        prepared(w)
        w.approve()
        conf = C.CFG["ship"]["repos"][REPO]
        saved = conf["deploy"]
        conf["deploy"] = [["make", "check"]]
        try:
            lines, rc = S.run()
        finally:
            conf["deploy"] = saved
        T.expect(rc == 0 and lines[0].startswith("SHIPPED") and "make test" not in w.calls, (lines, w.calls))


@check
def a_stopped_ship_alerts_after_thirty_minutes_then_rarely():
    with ship_env() as w:
        prepared(w)
        w.approve()
        w.make_rc["make ship-tested"] = 3
        S.run()
        rec = C.ship_record(KEY)
        S.update(KEY, alerted=int(rec["updated"]) - 3600)
        lines, rc = S.run()
        T.expect(rc == 1 and lines[0].startswith(f"ALERT {KEY} stage=deploy_failed"), lines)
        T.expect(S.run() == ([], 0), "announced once; the next announcement is hours away")


# ---------------------------------------------------------------- connections

@check
def the_gate_controller_turns_hold_into_one_held_line_one_candidate_and_asks_again_after_a_void():
    import epic_gate_controller as ctl
    w = ShipWorld()
    with ship_env(w):
        ctx = dict(now=1000, cards=[], asked=set(), deadline=None)
        row = {"id": T._id(9)}
        ev = {"page": {"edited": "e1"}}
        appended, comments = [], []
        saved = (C.append_to_chapter, ctl.hermes, ctl.release_card)
        C.append_to_chapter = lambda pid, h, ch: appended.append((h, ch[0][ch[0]["type"]]["rich_text"][0]["text"]["content"]))
        ctl.release_card = lambda cards, epic: {"id": "t_1"}
        ctl.hermes = lambda *a, **k: comments.append(a) or R()
        try:
            out = []
            ctl.hold_ship_ask(row, "ab" * 16, KEY, f"HOLD {KEY} no-pr-body: 空です", ev, ctx, True, out)
            T.expect(out == [f"HELD {KEY}: no-pr-body：空です"] and [h for h, _ in appended] == ["管理ログ", "候補"] and len(comments) == 1, (out, appended))
            T.expect("開始できるフェーズ：リリース判定" in appended[1][1] and "PR 本文の下書き" in appended[1][1], appended[1])
            out2 = []
            ctl.hold_ship_ask(row, "ab" * 16, KEY, f"HOLD {KEY} no-pr-body: 空です", ev, ctx, True, out2)
            T.expect(out2 == [f"HOLD {KEY} (unchanged): no-pr-body：空です"] and len(appended) == 2, "same reason: no second note, no second candidate")
            out3 = []
            ctl.hold_ship_ask(row, "ab" * 16, KEY, f"HOLD {KEY} conflict: x", ev, ctx, True, out3)
            T.expect(out3 == [f"HELD {KEY}: conflict：x"] and [h for h, _ in appended].count("候補") == 1, "a new reason is announced, the candidate is once per key")
            appended.clear()
            out4 = []
            ctl.hold_ship_ask(row, "ab" * 16, "k2-承認", "HOLD k2-承認 gh-permission: 403", ev, ctx, True, out4)
            T.expect(out4 == ["ERROR gate ask k2-承認: gh-permission：403"] and [h for h, _ in appended] == ["管理ログ"], (out4, appended))
            # the hold is looked at again after SHIP_RECHECK_SECONDS (or when the page changed), not every run
            saved_ev = (C.decision_page_ref, C.collect_ui_evidence)
            C.decision_page_ref = lambda prows: {"edited": "e1"}
            C.collect_ui_evidence = lambda prows, ref=None, root=None: "EVIDENCE"
            try:
                o = []
                T.expect(ctl.release_evidence(KEY, {}, dict(ctx, now=1000 + 60), o) is None and o[0].startswith("HOLD"), "inside the interval: unchanged")
                T.expect(ctl.release_evidence(KEY, {}, dict(ctx, now=1000 + ctl.SHIP_RECHECK_SECONDS), []) == "EVIDENCE", "after the interval: asked again")
            finally:
                C.decision_page_ref, C.collect_ui_evidence = saved_ev
        finally:
            C.append_to_chapter, ctl.hermes, ctl.release_card = saved
        # re-ask: a void round whose branch moved on -> <key>-r2; same head, a live card, or no void -> None
        epic = "cd" * 16
        base = f"{epic[-8:]}-承認"
        S.update(base, repo=REPO, branch=BRANCH, head=HEAD, pr=31, stage="void")
        asked = {base}
        T.expect(S.reask_key(base, asked) is None, "the branch has not moved")
        w.remote[BRANCH] = "e" * 40
        T.expect(S.reask_key(base, asked) == base + "-r2", S.reask_key(base, asked))
        S.update(base + "-r2", repo=REPO, branch=BRANCH, head="e" * 40, pr=31, stage="drafted")
        asked.add(base + "-r2")
        T.expect(S.reask_key(base, asked) is None, "round 2 is waiting for the person")
        S.update(base + "-r2", stage="void")
        w.remote[BRANCH] = "f" * 40
        T.expect(S.reask_key(base, asked) == base + "-r3", "round 3")
        live = {"tenant": C.tenant_of(epic), "status": "running", "key": C.phase_key(epic, "実装"), "title": "[実装] x", "id": "t"}
        T.expect(ctl.reask_key(epic, base, dict(ctx, cards=[live], asked=asked)) is None, "a live phase card: the fix is not done")
        # ask_release maps exit 3 to HOLD
        C._sh = lambda cmd, input=None, timeout=90: R(3, f"HOLD {KEY} no-diff: 差分なし\n", "")
        T.expect(ctl.ask_release(KEY, "e") == (False, f"HOLD {KEY} no-diff: 差分なし"), "exit 3 + HOLD line")


@check
def the_epic_completes_only_when_the_approved_round_has_shipped_and_the_review_inbox_leaves_shipped_approvals_to_the_script():
    import epic_gate_controller as ctl
    import review_inbox_status as rs
    epic = "cd" * 16
    base = f"{epic[-8:]}-承認"

    def page(key, st):
        return T._review_page(abs(hash(key)) % 1000 + 1, st, key=key)
    with ship_env():
        saved = C.query
        try:
            statuses = {ph: "完了" for ph in C.ORDER}
            C.query = lambda ds, flt=None, page_size=100: [page(base, "承認")]
            T.expect(ctl.release_complete(epic, statuses, {}) is True, "an approval without a ship record completes the epic as before")
            S.update(base, repo=REPO, stage="merged")
            T.expect(ctl.release_complete(epic, statuses, {}) is False, "merged but not deployed: still 承認待ち")
            S.update(base, stage="deploy_failed")
            T.expect(ctl.release_complete(epic, statuses, {}) is False, "deploy_failed: the epic stays 承認待ち")
            S.update(base, stage="done")
            T.expect(ctl.release_complete(epic, statuses, {}) is True, "done: complete")
            C.query = lambda ds, flt=None, page_size=100: [page(base, "差し戻し"), page(base + "-r2", "承認")]
            T.expect(ctl.release_complete(epic, statuses, {}) is True, "the latest round counts")
            C.query = lambda ds, flt=None, page_size=100: [page(base, "承認"), page(base + "-r2", "レビュー待ち")]
            T.expect(ctl.release_complete(epic, statuses, {}) is False, "an earlier approval does not count once a later round exists")
        finally:
            C.query = saved
        rows = [page(base, "承認"), page("zz-done", "承認"), page(base + "-r9", "差し戻し")]
        S.update(base, stage="drafted")
        got = [rs._plain(p["properties"]["キー"]) for p in rs.decided_rows(rows)]
        T.expect(got == sorted(["zz-done", base + "-r9"], key=lambda k: next(p["id"] for p in rows if rs._plain(p["properties"]["キー"]) == k)), got)


@check
def sfconfig_checks_the_ship_registration_and_it_is_optional():
    import sfconfig
    roots = [r for r in T._repo_roots() if (r / "tests" / "config.test.toml").is_file() and (r / "envs").is_dir()]
    if not roots:
        print("skip: no repo checkout (staging)")
        return
    root = roots[0]
    text = (root / "tests" / "config.test.toml").read_text(encoding="utf-8")
    base = text[:text.index("[ship.repos")]
    with tempfile.TemporaryDirectory() as d:
        def load(extra):
            p = Path(d) / "c.toml"
            p.write_text(base + extra, encoding="utf-8")
            return sfconfig.load(p)
        T.expect("ship" not in load(""), "no [ship] at all is fine")
        ok = load('[ship.repos."o/r"]\ntest = ["make", "test"]\ntest_fast = ["make", "test-fast"]\ndeploy = [["make", "ship"]]\ndescribe = "反映します"\n')
        T.expect(ok["ship"]["repos"]["o/r"]["deploy"] == [["make", "ship"]] and ok["ship"]["repos"]["o/r"]["test_fast"] == ["make", "test-fast"], ok["ship"])
        for bad, word in (('[ship.repos."nope"]\ntest = ["x"]\n', "owner/repo"), ('[ship.repos."o/r"]\ntest = "make test"\n', "test"),
                          ('[ship.repos."o/r"]\ntest_fast = "make test-fast"\n', "test_fast"),
                          ('[ship.repos."o/r"]\ndeploy = [["make"]]\n', "describe"), ('[ship.repos."o/r"]\ndeploy = ["make"]\ndescribe = "x"\n', "deploy"),
                          ('[ship.repos."o/r"]\nfoo = 1\n', "unknown keys")):
            e = T.raises(sfconfig.ConfigError, load, bad)
            T.expect(word in str(e), (bad, str(e)))
    prod = sfconfig.load(root / "envs" / "michiomochi-wsl.toml")
    T.expect(prod["ship"]["repos"]["example-org/sf"]["deploy"] == [["make", "ship"], ["make", "check"], ["make", "smoke"]], "the production registration")


@check
def the_old_no_pr_rules_are_gone_from_every_text_and_sf_ship_drafts_are_not_closed_by_the_health_prompt():
    root = Path(C.SCRIPTS_DIR).parent
    if not (root / "prompts").is_dir():
        print("skip: no repo checkout (staging)")
        return
    rx = re.compile(r"not an external-send|not even a draft|until .{0,40}instructs|explicitly instructs it")
    hits = []
    for sub in ("prompts", "skills", "hermes", "notion", "scripts/templates"):
        for f in sorted((root / sub).rglob("*")):
            if f.is_file() and f.suffix in (".md", ".txt", ".toml", ".tmpl") and rx.search(f.read_text(encoding="utf-8")):
                hits.append(str(f.relative_to(root)))
    T.expect(not hits, f"the old fence is still written in: {hits}")
    T.expect(C.SHIP_MARK in (root / "prompts" / "health.txt").read_text(encoding="utf-8"), "health.txt names <!-- sf-ship -->")
    T.expect(C.SHIP_MARK in (root / "skills" / "product-development-process" / "SKILL.md").read_text(encoding="utf-8"), "the exception for sf-ship drafts")
    T.expect(S.SHIP_MARK == "<!-- sf-ship -->", "the mark is the one the texts name")
