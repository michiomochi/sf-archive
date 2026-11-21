#!/usr/bin/env python3
"""Ship an approved release: open the draft PR for the release ask (prepare), and after the person's 承認 publish, squash-merge and deploy it (run).

No LLM: the person's ステータス on the review row is the only input; every step is a fixed script (gh REST, git, make). Standard library only.

usage: ship_approved.py prepare --epic <エピック row id> --key <epic8-承認[-rN]> [--dry-run]
       ship_approved.py run [--key K] [--dry-run]      (no sub-command = run; the cron wrapper cannot pass arguments)
       ship_approved.py status [--key K]
       ship_approved.py retry --key K                  (a person: deploy_failed / halted -> merged, the next run deploys again)
       ship_approved.py adopt --key K --epic E         (a person: put a review row that was waiting before the switch on the new meaning)

prepare (called by `ask_human.py --release` before the review row exists): reads the repository and branch from the judgement / phase rows, the
PR body (the chapter 'PR 本文' of the judgement page, or on an old row / L3 the child page 'PR 本文の下書き' of the リリース判定 row; first line
`タイトル：…`, the four sections 概要 / 背景 / 変更内容 / 検証; no page is made for it), opens (or updates) a
DRAFT pull request against main and reads it back: draft=true, base=main, head = `git ls-remote`, mergeable is not false, changed_files > 0.
  PREPARED <key> pr=<url> head=<sha12> draft=yes      exit 0
  HOLD <key> <code>: <reason>                         exit 3  (no row is made; code = no-branch / branch-not-pushed / no-pr-body / pr-body-shape /
                                                              conflict / no-diff / gh-permission / draft-unsupported)
  ERROR <key> <reason>                                exit 1  (transient: the next run starts again)
run (cron 'SF 出荷', every 3 minutes): per recorded key, in creation order, reads the facts first and then does the next step; stages
  drafted -> tested -> readied -> merged -> deployed -> done; stops: rejected / withdrawn / void / halted / deploy_failed.
  1 承認: head unchanged? GitHub checks not red? `make test` on main + the branch in a temporary worktree -> `gh pr ready` -> squash merge pinned
    to the approved head SHA (a moved head is HTTP 409) -> delete the remote branch -> deploy from the main working tree with the commands of
    [ship.repos."owner/repo"] (make ship / check / smoke; `make ship` is run as make test -> compare tested_tree.json with the HEAD about to be shipped,
    FAIL tested-tree and no deploy when they differ -> make ship-tested) -> record in the review row, the PR (a public comment, no Notion words) and the hub 管理ログ.
  2 差し戻し: the PR stays a draft (one comment, no reason: a PR is public). 取り下げ: the PR is closed (the branch stays).
  3 Anything that breaks before the merge (head moved, conflict, red test, red check, a 401/403/404 on publish or merge = gh-permission) = void: nothing merged, the PR is a draft again,
    one record each in the row / the PR / the hub. A failure after the merge never reverts main: deploy_failed (a person's `retry`, or a later ship).
Output lines (only when something changed; the same ERROR text is printed once): SHIPPED / MERGED-ONLY / VOID / REJECTED / WITHDRAWN / HALTED / FAIL / ALERT / ERROR; exit 1 on FAIL / ALERT / ERROR.
Idempotent: every step reads the outside fact first (isDraft, MERGED + mergeCommit, the head SHA) and records after; two runs never overlap
(<state>/ship_approved.lock; the deploy's own lock is <live>/.deploy.flock, rc 75 = wait). gh always gets the repository (REST repos/<owner>/<repo>/…).
"""
import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import epic_common as C
import tested_tree as TT

DRY = False          # --dry-run: read everything, write nothing (gh, git, make, Notion, state)
WOULD = []           # what a dry run would have done
BASE = "main"
SHIP_MARK = C.SHIP_MARK
REQUIRED_SECTIONS = ("概要", "背景", "変更内容", "検証")
PR_CHAPTER = "PR 本文"   # the chapter of the judgement page (levels.toml judgement_chapters of L2) the draft PR is assembled from
TEST_TIMEOUT = 1200
STEP_TIMEOUT = {"ship-tested": 1800}   # the deploy itself (`make ship` is run as make test -> the tested-tree check -> make ship-tested)
PERMISSION_STATUS = (401, 403, 404)   # GitHub refuses the publish / merge itself: no retry can help (void gh-permission)
DEFAULT_STEP_TIMEOUT = 300
ALERT_AFTER = 1800   # a stopped record (deploy_failed / halted) is announced after this many seconds, then every 6 hours
ALERT_EVERY = 21600
GIT_ID = ["-c", "user.name=sf-ship", "-c", "user.email=sf-ship@localhost"]
HOLD_CODES = ("no-branch", "branch-not-pushed", "no-pr-body", "pr-body-shape", "conflict", "no-diff", "gh-permission", "draft-unsupported")


class Hold(Exception):
    """prepare cannot make a checked draft PR: the review row is not made (exit 3)."""

    def __init__(self, code, reason):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


class Void(Exception):
    """run: the approval cannot be carried out any more (nothing is merged)."""

    def __init__(self, code, reason):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


class Transient(Exception):
    """A failure that the next run can retry (network, 5xx, rate limit)."""


class GhError(Exception):
    def __init__(self, status, text):
        super().__init__(f"HTTP {status}: {text}" if status else text)
        self.status, self.text = status, text


# ---------------------------------------------------------------- command choke points (tests replace sh)

def sh(cmd, timeout=60, cwd=None):
    """Run a command (array, never a shell string) -> CompletedProcess. A timeout is rc 124. `cwd` runs it in that directory."""
    if cwd:
        cmd = ["sh", "-c", 'cd "$0" && exec "$@"', str(cwd), *cmd]
    try:
        return C._sh(cmd, timeout=timeout)
    except Exception as e:
        if type(e).__name__ == "TimeoutExpired":
            class R:
                returncode, stdout, stderr = 124, "", f"timed out after {timeout}s"
            return R()
        raise


def git(d, *args, timeout=60):
    return sh([C.which_tool("git"), "-C", str(d), *args], timeout=timeout)


def api(method, path, body=None, timeout=60):
    """One GitHub REST call through `gh api` -> parsed JSON ({} when empty). Writes are skipped in a dry run. Raises GhError / Transient."""
    if DRY and method != "GET":
        WOULD.append(f"{method} {path}")
        return {}
    cmd = [C.which_tool("gh"), "api", "-X", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    r = sh_input(cmd, json.dumps(body, ensure_ascii=False) if body is not None else None, timeout)
    if r.returncode != 0:
        err = ((r.stderr or "").strip() + " " + (r.stdout or "").strip()).strip()
        m = re.search(r"HTTP (\d{3})", err)
        status = int(m.group(1)) if m else None
        if status is None or status >= 500 or status == 429 or (status == 403 and re.search(r"rate limit|abuse", err, re.I)):
            raise Transient(f"gh {method} {path}: {err[:150]}")
        raise GhError(status, err[:200])
    out = (r.stdout or "").strip()
    try:
        return json.loads(out) if out else {}
    except ValueError:
        raise Transient(f"gh {method} {path}: not JSON: {out[:80]!r}")


def sh_input(cmd, stdin, timeout):
    """sh() with stdin (the JSON body of a gh api call)."""
    try:
        return C._sh(cmd, input=stdin, timeout=timeout)
    except Exception as e:
        if type(e).__name__ == "TimeoutExpired":
            raise Transient(f"{cmd[:3]} timed out after {timeout}s")
        raise


def gh_ready(repo, n, undo=False):
    """Publish the draft (or turn a published PR back into a draft): `gh pr ready` has no REST form."""
    if DRY:
        WOULD.append(f"gh pr ready{' --undo' if undo else ''} {repo}#{n}")
        return
    r = sh([C.which_tool("gh"), "pr", "ready", str(n), "-R", repo] + (["--undo"] if undo else []))
    if r.returncode != 0:
        err = ((r.stderr or "") + (r.stdout or "")).strip()
        if re.search(r"already|is already", err, re.I):
            return
        m = re.search(r"HTTP (\d{3})", err)
        if m and int(m.group(1)) in PERMISSION_STATUS and not re.search(r"rate limit|abuse", err, re.I):
            raise GhError(int(m.group(1)), err[:200])
        raise Transient(f"gh pr ready {repo}#{n}: {err[:150]}")


# ---------------------------------------------------------------- state (<state>/ship_approved.json)

class _StateLock:
    """Short blocking flock around a read-modify-write of the state file (prepare runs in the gate controller while run may be deploying)."""

    def __enter__(self):
        p = C.state_path("ship_approved.state.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(p, "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        return False


SHIP_EVENTS_FILE = "ship_events.jsonl"   # <state>/ship_events.jsonl: one line per change of a record's stage (read by tools/retro_metrics.py and tools/review_catches.py)
MEASURED = {}   # key -> dict(test_rc, test_seconds) of the `make test` premerge_test just ran; the next stage change of that key carries it


def log_stage_event(key, rec):
    """Append one line (key, repo, pr, stage, mode, ts, and test_rc / test_seconds when the stage change comes right after a measured `make test`)
    to ship_events.jsonl. An observation, not a condition of the ship: a failed write goes to stderr and the ship goes on. Never called in a dry run."""
    line = dict(key=key, repo=rec.get("repo"), pr=rec.get("pr"), stage=rec["stage"], mode="human", ts=int(time.time()))  # mode: a person's 承認 is the only way to ship (until the L2A level exists)
    line.update(MEASURED.pop(key, {}))
    try:
        p = C.state_path(SHIP_EVENTS_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as e:
        print(f"WARN cannot write {SHIP_EVENTS_FILE} for {key}: {type(e).__name__}: {e}"[:300], file=sys.stderr)


def update(key, **fields):
    """Merge fields into the record of `key` (created when missing) and save atomically. Returns the record. A dry run only returns it.
    When the stage changes, one line is appended to ship_events.jsonl (log_stage_event)."""
    if DRY:
        rec = dict(C.ship_record(key) or {})
        rec.update(fields)
        WOULD.append(f"state {key} {fields}")
        return rec
    with _StateLock():
        d = C.ship_records()
        rec = d.setdefault(key, dict(created=int(time.time()), steps=[]))
        before = rec.get("stage")
        rec.update(fields)
        rec["updated"] = int(time.time())
        C.atomic_write_json(C.state_path(C.SHIP_STATE_FILE), d)
        if fields.get("stage") and fields["stage"] != before:
            log_stage_event(key, rec)
        return rec


def repo_dir(repo):
    return C.repos_root() / repo


def sha12(s):
    return (s or "")[:12]


# ---------------------------------------------------------------- the PR body (public: no Notion words)

_NOTION_URL = re.compile(r"https?://(?:[\w-]+\.)?notion\.(?:so|com|site)/\S*", re.I)
_PRIVATE_WORDS = re.compile(r"(?:タスク|エピック|レビュー)\s*DB|Notion\s*タスク", re.I)


def public_text(md):
    """The PR body from the Notion page markdown: link text kept, link target and mentions removed, bare Notion URLs removed."""
    t = re.sub(r"\A---\n.*?\n---\n", "", md or "", flags=re.S)
    t = re.sub(r"<unknown[^>]*/>", "", t)
    t = re.sub(r"<mention-[a-z]+\s+url=\"[^\"]*\"\s*/>", "", t)
    t = re.sub(r"<(?:mention-[a-z]+|page)\s+url=\"[^\"]*\">(.*?)</(?:mention-[a-z]+|page)>", r"\1", t, flags=re.S)
    t = re.sub(r"\[([^\]\n]*)\]\((?:" + _NOTION_URL.pattern + r")\)", r"\1", t, flags=re.I)
    t = _NOTION_URL.sub("", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def private_marks():
    """Compiled patterns of what must not be in a public PR: the data source ids of the SSOT and the DB words."""
    cfg = C.CFG.get("notion") or {}
    pats = [_PRIVATE_WORDS]
    for k in ("task_ds", "epic_ds", "review_ds"):
        i = cfg.get(k)
        if i:
            pats.append(re.compile(re.escape(i) + "|" + re.escape(i.replace("-", "")), re.I))
    return pats


def headings(md):
    return [re.sub(r"\s*\{[^}]*\}\s*$", "", m.group(2)).strip() for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", md or "", re.M)]


def section_text(md, name):
    m = re.search(r"^#{1,6}\s+" + re.escape(name) + r"\s*(?:\{[^}]*\})?\s*$\n(.*?)(?=^#{1,6}\s|\Z)", md or "", re.M | re.S)
    return (m.group(1).strip() if m else None)


def split_title(md):
    """('title', rest) when the first non-empty line is `タイトル：…`, else (None, md)."""
    lines = (md or "").lstrip("\n").split("\n")
    m = re.match(r"^タイトル\s*[：:]\s*(.+?)\s*$", lines[0]) if lines else None
    return (m.group(1)[:200], "\n".join(lines[1:]).strip()) if m else (None, (md or "").strip())


def template_sections(d):
    """Headings of the repository's PULL_REQUEST_TEMPLATE on origin/main (the section names the PR must have), else the four of CONTRIBUTING."""
    for p in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md"):
        r = git(d, "show", f"origin/{BASE}:{p}")
        if r.returncode == 0 and headings(r.stdout):
            return headings(r.stdout)
    return list(REQUIRED_SECTIONS)


def pr_chapter(md):
    """The text of the chapter 'PR 本文' of a judgement page's markdown (the PR body of a level whose judgement carries it), '' when the chapter
    is empty, None when the page has no such chapter. The sub-headings (## 概要 …) stay inside the chapter."""
    found = C._sections(md, (PR_CHAPTER,)).get(PR_CHAPTER)
    return found[0] if found else None


def build_pr(md, d):
    """(title, body) of the draft PR from the PR body markdown (the judgement's 'PR 本文' chapter, or the old child page); raises Hold(no-pr-body / pr-body-shape)."""
    if not (md or "").strip():
        raise Hold("no-pr-body", "「PR 本文」（判定書の章。旧形は子ページ「PR 本文の下書き」）が空です")
    if C.PLACEHOLDER_RE.search(md):
        raise Hold("pr-body-shape", "「PR 本文」にひな形の書き置き〈…〉が残っています")
    title, rest = split_title(public_text(md))
    if not title:
        raise Hold("pr-body-shape", "「PR 本文」の1行目が『タイトル：…』ではありません")
    need = template_sections(d)
    have = headings(rest)
    missing = [n for n in need if n not in have]
    empty = [n for n in need if n in have and not (section_text(rest, n) or "").strip()]
    if missing or empty:
        raise Hold("pr-body-shape", "節が足りないか空です：" + "・".join(missing + empty) + "（必要：" + "・".join(need) + "）")
    left = [p.pattern[:30] for p in private_marks() if p.search(title + "\n" + rest)]
    if left:
        raise Hold("pr-body-shape", "非公開の印（Notion の DB 名・ID）が残っています")
    return title, rest + "\n\n" + SHIP_MARK + "\n"


# ---------------------------------------------------------------- prepare

def read_inputs(epic_row_id, repo=None, branch=None):
    """(repo, branch, PR draft page markdown or None) from the epic's phase rows (read-only)."""
    prows = C.phase_rows(epic_row_id)
    ref = C.decision_page_ref(prows)
    if not (repo and branch):
        texts = ["\n".join(C.evidence_text(b) for b in C.iter_blocks(ref["id"]))] if ref else []
        r, b = C.repo_and_branch(texts)
        for pr in C.distinct_rows(prows, ("実装", "リリース判定", "設計")):  # a row of a range works several of them: read it once (iter_blocks is up to 30 calls)
            if r and b:
                break
            texts.append("\n".join(C.evidence_text(x) for x in C.iter_blocks(pr["id"])))
            r, b = C.repo_and_branch(texts)
        repo, branch = repo or r, branch or b
    md = None
    if ref:  # the PR body is assembled from the chapter 'PR 本文' of the judgement (no page of its own)
        try:
            md = pr_chapter((C.ntn(f"v1/pages/{ref['id']}/markdown") or {}).get("markdown", "")) or None
        except C.PipelineError:
            md = None
    rel = prows.get("リリース判定")
    if not md and rel:  # an old row / L3: the child page 'PR 本文の下書き'
        kids = [x for x in C.blocks(rel["id"]) if x.get("type") == "child_page" and re.search(r"PR\s*本文", C.evidence_text(x))]
        if kids:
            md = (C.ntn(f"v1/pages/{kids[-1]['id']}/markdown") or {}).get("markdown", "")
    return repo, branch, md


def remote_head(d, branch):
    r = git(d, "ls-remote", "origin", f"refs/heads/{branch}")
    if r.returncode != 0:
        raise Transient(f"git ls-remote: {((r.stderr or r.stdout) or '').strip()[:120]}")
    for line in (r.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}":
            return sha.strip()
    return None


def open_pr(repo, branch):
    owner = repo.split("/")[0]
    hits = api("GET", f"repos/{repo}/pulls?head={urllib.parse.quote(owner + ':' + branch, safe=':')}&state=open&per_page=5") or []
    return hits[0] if hits else None


def pr_get(repo, n):
    return api("GET", f"repos/{repo}/pulls/{n}")


def prepare(epic_row_id, key, dry_run=False, repo=None, branch=None):
    """-> dict(status 'ok'|'hold'|'error', code, reason, pr, pr_url, head, title). Never raises."""
    global DRY
    saved, DRY = DRY, bool(dry_run) or DRY
    try:
        try:
            return _prepare(epic_row_id, key, repo, branch)
        except Hold as h:
            return dict(status="hold", code=h.code, reason=h.reason)
        except GhError as e:
            if e.status in (401, 403, 404):
                return dict(status="hold", code="gh-permission", reason=f"GitHub が拒否しました（{e}）")
            return dict(status="error", code="gh", reason=str(e)[:200])
        except Exception as e:
            return dict(status="error", code=type(e).__name__, reason=str(e)[:200])
    finally:
        DRY = saved


def _prepare(epic_row_id, key, repo, branch):
    repo, branch, md = read_inputs(epic_row_id, repo, branch)
    if not (repo and branch):
        raise Hold("no-branch", "対象のリポジトリかブランチが判定書・工程の行の本文に見つかりません")
    d = repo_dir(repo)
    head = remote_head(d, branch)
    if not head:
        raise Hold("branch-not-pushed", f"{branch} が origin にありません（push されていません）")
    r = git(d, "fetch", "--quiet", "origin", BASE, branch, timeout=120)
    if r.returncode != 0:
        raise Transient(f"git fetch: {((r.stderr or r.stdout) or '').strip()[:120]}")
    diff = git(d, "diff", "--name-only", f"origin/{BASE}...origin/{branch}")
    if diff.returncode != 0:
        raise Transient(f"git diff: {((diff.stderr or diff.stdout) or '').strip()[:120]}")
    if not (diff.stdout or "").strip():
        raise Hold("no-diff", f"{branch} は origin/{BASE} と差がありません")
    title, body = build_pr(md, d)
    try:
        pr = open_pr(repo, branch)
        if pr is None:
            pr = api("POST", f"repos/{repo}/pulls", dict(title=title, head=branch, base=BASE, body=body, draft=True)) or {}
        else:
            if pr.get("title") != title or (pr.get("body") or "").strip() != body.strip():
                api("PATCH", f"repos/{repo}/pulls/{pr['number']}", dict(title=title, body=body))
            if not pr.get("draft"):
                gh_ready(repo, pr["number"], undo=True)
    except GhError as e:
        if e.status == 422 and "draft" in e.text.lower():
            raise Hold("draft-unsupported", "この repo では下書き PR を作れません（" + e.text[:80] + "）")
        raise
    if DRY:
        return dict(status="ok", code="", reason="", pr=pr.get("number") or 0, pr_url=pr.get("html_url") or f"(dry-run: {repo} {branch})", head=head, title=title)
    n = pr["number"]
    for attempt in range(3):  # `mergeable` is computed in the background: null for a moment after a push
        pr = pr_get(repo, n)
        if pr.get("mergeable") is not None:
            break
        C._sleep(2)
    problems = []
    if not pr.get("draft"):
        problems.append("draft=false")
    if (pr.get("base") or {}).get("ref") != BASE:
        problems.append(f"base={(pr.get('base') or {}).get('ref')}")
    if (pr.get("head") or {}).get("sha") != head:
        problems.append("head が git ls-remote と違う")
    if pr.get("changed_files", 1) <= 0:
        raise Hold("no-diff", "PR の changed_files が 0 です")
    if pr.get("mergeable") is False:
        raise Hold("conflict", f"{branch} は origin/{BASE} とコンフリクトしています")
    if problems:
        raise Transient("下書き PR の読み返しが合いません：" + "、".join(problems))
    update(key, epic=epic_row_id, repo=repo, branch=branch, pr=n, pr_url=pr["html_url"], head=head, title=title, stage="drafted", hold=None)
    return dict(status="ok", code="", reason="", pr=n, pr_url=pr["html_url"], head=head, title=title)


# ---------------------------------------------------------------- Notion writes (best effort: the state file is the truth of the steps)

def row_of(key):
    hits = C.query(C.REVIEW_DS, {"property": "キー", "rich_text": {"equals": key}})
    return hits[0] if hits else None


def say(rec, text):
    """One paragraph in the review row's 反映の記録 chapter. Never raises (the line goes to stderr instead)."""
    line = f"出荷 {time.strftime('%Y-%m-%d %H:%M')} {text}"
    if DRY:
        WOULD.append("row: " + line)
        return
    try:
        row = row_of(rec["key"])
        if row:
            C.append_to_chapter(row["id"], "反映の記録", [C.text_block("paragraph", line)])
    except Exception as e:
        print(f"WARN cannot write the review row of {rec['key']}: {type(e).__name__}: {e}"[:300], file=sys.stderr)


def hub_log(rec, text, candidate=None):
    if DRY:
        WOULD.append("hub: " + text)
        return
    try:
        C.append_to_chapter(rec["epic"], "管理ログ", [C.text_block("paragraph", f"{time.strftime('%Y-%m-%d')} {text}")])
        if candidate:
            C.append_to_chapter(rec["epic"], "候補", [C.text_block("to_do", candidate, checked=False)])
    except Exception as e:
        print(f"WARN cannot write the hub of {rec['key']}: {type(e).__name__}: {e}"[:300], file=sys.stderr)


def comment_once(rec, kind, text):
    """A public PR comment, once per (kind, key): the hidden marker is looked up first. Never raises."""
    mark = f"<!-- sf-ship:{kind}:{rec['key']} -->"
    try:
        if not DRY:
            old = api("GET", f"repos/{rec['repo']}/issues/{rec['pr']}/comments?per_page=100") or []
            if any(mark in (c.get("body") or "") for c in old):
                return
        api("POST", f"repos/{rec['repo']}/issues/{rec['pr']}/comments", dict(body=f"{text}\n\n{mark}"))
    except Exception as e:
        print(f"WARN cannot comment on the PR of {rec['key']}: {type(e).__name__}: {e}"[:300], file=sys.stderr)


def candidate_text(reason):
    return (f"打ち手：出荷が止まった理由を直し、ブランチを push する（{reason}）。origin/{BASE} を取り込んで衝突やテストの失敗を直す"
            f" ／ 判断の宣言：直して出し直すか（承認を受けた SHA が変わるので、リリース承認の依頼を新しい番号で出し直す）が決まる"
            f" ／ 感度の見積もり：止まった出荷 1件→0件 ／ 開始できるフェーズ：実装")


# ---------------------------------------------------------------- run

def status_of_row(row):
    return C.status_of(row, default="レビュー待ち")


def red_checks(repo, sha):
    """Names of the completed GitHub checks of `sha` that failed (failure / cancelled / timed_out / action_required). Pending or none = no problem."""
    data = api("GET", f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}
    bad = ("failure", "cancelled", "timed_out", "action_required")
    return [c.get("name", "?") for c in data.get("check_runs", []) if c.get("status") == "completed" and c.get("conclusion") in bad]


def premerge_test(rec, conf):
    """`make test` on origin/main with the approved head merged in, in a temporary worktree. -> (main sha, log text). Raises Void(conflict / test-red)."""
    d = repo_dir(rec["repo"])
    r = git(d, "fetch", "--quiet", "origin", BASE, rec["branch"], timeout=120)
    if r.returncode != 0:
        raise Transient(f"git fetch: {((r.stderr or r.stdout) or '').strip()[:120]}")
    base_sha = git(d, "rev-parse", f"origin/{BASE}").stdout.strip()
    if DRY:
        WOULD.append(f"test: {conf['test']} on {sha12(base_sha)} + {sha12(rec['head'])}")
        return base_sha, ""
    tmp = tempfile.mkdtemp(prefix="sf-ship-", dir=str(C.state_path("ship")) if C.state_path("ship").is_dir() else None)
    wt = os.path.join(tmp, "wt")
    try:
        r = git(d, "worktree", "add", "--detach", wt, f"origin/{BASE}", timeout=120)
        if r.returncode != 0:
            raise Transient(f"git worktree add: {((r.stderr or r.stdout) or '').strip()[:120]}")
        m = sh(["git", "-C", wt, *GIT_ID, "merge", "--no-ff", "--no-edit", rec["head"]], timeout=120)
        if m.returncode != 0:
            sh(["git", "-C", wt, "merge", "--abort"])
            raise Void("conflict", f"{rec['branch']} を origin/{BASE} に重ねるとコンフリクトします")
        t0 = time.time()
        t = sh(list(conf["test"]), timeout=TEST_TIMEOUT, cwd=wt)
        MEASURED[rec["key"]] = dict(test_rc=t.returncode, test_seconds=round(time.time() - t0, 1))
        log = ((t.stdout or "") + (t.stderr or ""))
        save_log(rec["key"], log)
        if t.returncode != 0:
            raise Void("test-red", f"main に重ねた {' '.join(conf['test'])} が rc={t.returncode} でした：" + (log.strip().splitlines() or [""])[-1][:100])
        return base_sha, log
    finally:
        git(d, "worktree", "remove", "--force", wt)
        git(d, "worktree", "prune")
        sh(["rm", "-rf", tmp])


def save_log(key, text):
    try:
        p = C.state_path("ship") / "logs" / f"{key}-test.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text[-20000:], encoding="utf-8")
    except OSError:
        pass


def void(rec, code, reason, lines):
    """Stop an approval before the merge: nothing merged, the PR is a draft again, one record each in the row / the PR / the hub."""
    try:
        pr = pr_get(rec["repo"], rec["pr"])
        if not pr.get("draft") and pr.get("state") == "open":
            gh_ready(rec["repo"], rec["pr"], undo=True)
    except Exception as e:
        print(f"WARN cannot turn {rec['key']} back into a draft: {e}"[:200], file=sys.stderr)
    say(rec, f"マージ前に止めました（{code}）：{reason}。PR は下書きに戻しました。マージは行っていません。")
    comment_once(rec, "void", f"sf: マージを止めました（{code}）。PR は下書きに戻します。")
    hub_log(rec, f"リリース承認の出荷を止めました（{code}：{reason}）。マージ・本番反映はしていません。{rec['key']}",
            candidate=None if code == "head-moved" else candidate_text(f"{code}：{reason}"))
    update(rec["key"], stage="void", hold=code)
    lines.append(f"VOID {rec['key']} {code}: {reason}")


def fresh_status(key):
    row = row_of(key)
    return (status_of_row(row) if row else None), row


def step_ship(rec, conf, lines):
    """Advance an approved record as far as it goes. Mutates the state file after every stage. Returns True when it ended in a stop/finish."""
    key, repo = rec["key"], rec["repo"]
    stage = rec["stage"]
    if stage in ("drafted", "tested", "readied"):
        pr = pr_get(repo, rec["pr"])
        if pr.get("merged"):  # merged outside: take its commit and go on with the deploy
            update(key, stage="merged", merge_sha=pr.get("merge_commit_sha"), outside=True)
            say(rec, f"PR #{rec['pr']} は外で MERGED でした。マージはとばし、{sha12(pr.get('merge_commit_sha'))} から続けます。")
            return step_ship(C.ship_record(key) or rec, conf, lines)
        if pr.get("state") != "open":
            raise Void("closed", f"PR #{rec['pr']} が閉じられています")
        if (pr.get("head") or {}).get("sha") != rec["head"] or remote_head(repo_dir(repo), rec["branch"]) != rec["head"]:
            raise Void("head-moved", f"ブランチの先頭が承認された {sha12(rec['head'])} から動きました")
    if stage == "drafted":
        red = red_checks(repo, rec["head"])
        if red:
            raise Void("ci-red", "GitHub のチェックが失敗しています：" + "、".join(red[:3]))
        if conf and conf.get("test"):
            base_sha, _ = premerge_test(rec, conf)
            say(rec, f"マージ前の検査：{' '.join(conf['test'])} rc=0（origin/{BASE} {sha12(base_sha)} に重ねて）")
            update(key, stage="tested", base_tested=base_sha)
        else:
            say(rec, "マージ前の検査：この repo は検査の登録がないため省きました。")
            update(key, stage="tested")
        rec = C.ship_record(key) or rec if not DRY else rec
        stage = "tested"
    if stage == "tested":
        st, _ = fresh_status(key)
        if st != "承認":
            return back_from_cancel(rec, st, lines)
        pr = pr_get(repo, rec["pr"])
        if pr.get("draft"):
            try:
                gh_ready(repo, rec["pr"])
            except GhError as e:
                if e.status in PERMISSION_STATUS:
                    raise Void("gh-permission", f"GitHub が PR の公開を拒否しました（{e}）")
                raise
        update(key, stage="readied")
        stage = "readied"
    if stage == "readied":
        st, _ = fresh_status(key)
        if st != "承認":
            return back_from_cancel(rec, st, lines)
        try:
            res = api("PUT", f"repos/{repo}/pulls/{rec['pr']}/merge",
                      dict(merge_method="squash", sha=rec["head"], commit_title=f"{rec['title']} (#{rec['pr']})", commit_message=""))
        except GhError as e:
            if e.status in (405, 409):
                pr = pr_get(repo, rec["pr"])
                if pr.get("merged"):
                    res = dict(sha=pr.get("merge_commit_sha"))
                elif (pr.get("head") or {}).get("sha") != rec["head"]:
                    raise Void("head-moved", "マージの直前にブランチの先頭が動きました")
                else:
                    raise Void("conflict", f"PR #{rec['pr']} をマージできません（{e.text[:80]}）")
            elif e.status in PERMISSION_STATUS:
                raise Void("gh-permission", f"GitHub がマージを拒否しました（{e}）")
            else:
                raise
        merge_sha = res.get("sha") or ""
        update(key, stage="merged", merge_sha=merge_sha)
        try:
            api("DELETE", f"repos/{repo}/git/refs/heads/{urllib.parse.quote(rec['branch'], safe='/')}")
            gone = f"、ブランチ {rec['branch']} を削除"
        except GhError:
            gone = f"（ブランチ {rec['branch']} は削除できませんでした）"
        say(rec, f"公開とマージ：PR #{rec['pr']} → squash {sha12(merge_sha)}{gone}")
        comment_once(rec, "merged", f"sf: 承認によりマージしました（squash {sha12(merge_sha)}）。")
        stage = "merged"
        rec = dict(rec, merge_sha=merge_sha, stage="merged")
    if stage == "merged":
        return deploy(C.ship_record(key) or rec if not DRY else rec, conf, lines)
    return False


def back_from_cancel(rec, st, lines):
    """The approval was taken back before the merge: a draft again, the record goes back to waiting (or ends with the new decision)."""
    try:
        if not pr_get(rec["repo"], rec["pr"]).get("draft"):
            gh_ready(rec["repo"], rec["pr"], undo=True)
    except Exception as e:
        print(f"WARN cannot turn {rec['key']} back into a draft: {e}"[:200], file=sys.stderr)
    say(rec, f"承認が取り消されました（いまのステータス：{st}）。マージ前だったので、PR を下書きに戻しました。")
    update(rec["key"], stage="drafted", base_tested=None)
    return True


def changed_cron(d, merge_sha, conf):
    """True when the merge commit touches a cron definition (cron_paths globs of the registration), or when that cannot be read (then the
    cron-sync dry run is run to be safe). The range is the squash commit itself, so a tree that was already pulled by someone else is not missed."""
    pats = conf.get("cron_paths") or []
    if not pats:
        return False
    r = git(d, "diff", "--name-only", f"{merge_sha}^", merge_sha)
    if r.returncode != 0:
        return True
    import fnmatch
    return any(fnmatch.fnmatch(f, p) for f in (r.stdout or "").split() for p in pats)


def lock_free():
    """True when <live>/.deploy.flock can be taken right now (a deploy or sync is not running). Taken and released at once."""
    p = Path(C.CFG["paths"]["live"]) / ".deploy.flock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        f.close()


def tail_of(r):
    """Last non-empty line of a command's output (for the FAIL line)."""
    rows = [x for x in ((r.stderr or "") + "\n" + (r.stdout or "")).splitlines() if x.strip()]
    return rows[-1].strip() if rows else ""


def fail(rec, step, rc, tail, lines, stage="deploy_failed"):
    update(rec["key"], stage=stage, hold=f"{step}:{rc}", alerted=int(time.time()))
    say(rec, f"本番反映が止まりました：{step} rc={rc}（{tail[:100]}）。main は戻していません。人の retry か後の出荷で回収します。")
    hub_log(rec, f"リリース承認の出荷：マージ済み（{sha12(rec.get('merge_sha'))}）、本番反映が {step} rc={rc} で止まりました。エピックは承認待ちのままです。{rec['key']}")
    lines.append(f"FAIL {rec['key']} {step} rc={rc}: {tail[:120]}")
    return True


def deploy(rec, conf, lines):
    """merged -> deployed -> done. Without a registered deploy the merge is the end (MERGED-ONLY)."""
    key = rec["key"]
    if not (conf and conf.get("deploy")):
        update(key, stage="done", merged_only=True)
        tick_applied(rec, f"PR #{rec['pr']} をマージしました（{sha12(rec.get('merge_sha'))}）。この repo は本番反映の登録がないため、マージまでです。")
        lines.append(f"MERGED-ONLY {key} pr=#{rec['pr']} merge={sha12(rec.get('merge_sha'))}")
        return True
    st, _ = fresh_status(key)
    if st != "承認":
        update(key, stage="halted", hold="revoked-after-merge")
        say(rec, f"マージのあとに承認が取り消されました（{st}）。本番反映は止めました。main は戻していません。")
        lines.append(f"HALTED {key} 承認が取り消されました（マージ済み {sha12(rec.get('merge_sha'))}）。本番反映は止めました")
        return True
    d = repo_dir(rec["repo"])
    if not lock_free():
        return False  # a deploy / sync is running: wait, the stage stays merged
    if DRY:
        WOULD.append(f"deploy {conf['deploy']} in {d}")
        return False
    br = git(d, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = git(d, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if br != BASE or dirty:
        return fail(rec, "main-tree", 1, f"branch={br} " + ("未コミットの変更があります" if dirty else ""), lines)
    f = git(d, "fetch", "--quiet", "origin", timeout=120)
    ff = git(d, "merge", "--ff-only", f"origin/{BASE}", timeout=120)
    has = git(d, "merge-base", "--is-ancestor", rec["merge_sha"], "HEAD")
    if f.returncode != 0 or ff.returncode != 0 or has.returncode != 0:
        return fail(rec, "main-tree", 1, "origin/main を早送りできないか、マージのコミットを含みません", lines)
    cron = changed_cron(d, rec["merge_sha"], conf)
    runs = []

    def run_step(name, cmd, timeout):
        t0 = time.time()
        r = sh(list(cmd), timeout=timeout, cwd=d)
        runs.append(f"{name} rc={r.returncode}({int(time.time() - t0)}秒)")
        return r

    if cron:
        for name, cmd in (("cron-sync --dry-run", ["make", "cron-sync", "FLAGS=--dry-run"]), ("wrappers", ["make", "wrappers"]), ("cron-sync", ["make", "cron-sync"])):
            r = run_step(name, cmd, DEFAULT_STEP_TIMEOUT)
            if r.returncode == 75:
                return False
            if r.returncode != 0:
                return fail(rec, name, r.returncode, tail_of(r), lines)
    for cmd in conf["deploy"]:
        if list(cmd) == ["make", "ship"]:
            # `make ship` = make test -> make ship-tested. Run apart so the tree that is about to be shipped is compared with the tree that was just tested,
            # right before the deploy: tested_tree.json (written by a green make test) must carry this tree's HEAD, else nothing is deployed.
            # The record's path is passed to make so that what make test writes and what is read here cannot be two different files.
            record = C.state_path(TT.FILE_NAME)
            r = run_step("make test", ["make", "test", f"TESTED_TREE={record}"], TEST_TIMEOUT)
            if r.returncode != 0:
                return fail(rec, "make test", r.returncode, tail_of(r), lines)
            ok, why = TT.verify(record, git(d, "rev-parse", "HEAD").stdout.strip())
            if not ok:
                return fail(rec, "tested-tree", 1, why, lines)
            cmd = ["make", "ship-tested", f"TESTED_TREE={record}"]
        name = " ".join(x for x in cmd if not x.startswith("TESTED_TREE="))
        r = run_step(name, cmd, STEP_TIMEOUT.get(cmd[1] if len(cmd) > 1 else "", DEFAULT_STEP_TIMEOUT))
        if r.returncode == 75:  # .deploy.flock was busy for the whole wait: try again next run
            return False
        if r.returncode != 0:
            return fail(rec, name, r.returncode, tail_of(r), lines)
    if cron:
        r = run_step("cron-resume", ["make", "cron-resume"], DEFAULT_STEP_TIMEOUT)
        if r.returncode != 0:
            return fail(rec, "cron-resume", r.returncode, "", lines)
    update(key, stage="deployed")
    say(rec, "本番反映：" + "／".join(runs))
    update(key, stage="done")
    tick_applied(rec, f"PR #{rec['pr']} をマージし（{sha12(rec.get('merge_sha'))}）、本番へ反映しました（{'／'.join(runs)}）。")
    lines.append(f"SHIPPED {key} pr=#{rec['pr']} merge={sha12(rec.get('merge_sha'))} steps=" + ",".join(re.sub(r"\(.*", "", x).replace(" rc=", ":") for x in runs))
    return True


def tick_applied(rec, text):
    """The last words of a finished ship: the 反映結果 line, the hub 管理ログ and the 反映済み checkbox of the row."""
    say(rec, f"反映結果({time.strftime('%Y-%m-%d')}): {text}")
    row = None if DRY else row_of(rec["key"])
    url = (row or {}).get("url") or ""
    hub_log(rec, f"リリース承認 承認 {url}（{text}）".replace("  ", " "))
    if DRY:
        return
    try:
        if row:
            C.ntn(f"v1/pages/{row['id']}", "-X", "PATCH", data={"properties": {"反映済み": {"checkbox": True}}})
    except Exception as e:
        print(f"WARN cannot tick 反映済み of {rec['key']}: {e}"[:200], file=sys.stderr)


def settle_decided(rec, st, lines):
    """差し戻し / 取り下げ (before the merge): the PR stays a draft + one comment, or it is closed. -> True when the record ended."""
    key = rec["key"]
    if rec["stage"] in ("merged", "deployed"):
        lines.append(f"HALTED {key} マージ後に {st} になりました。何もしません（人が判断してください）")
        update(key, stage="halted", hold=f"{st}-after-merge")
        return True
    if st == "差し戻し":
        pr = pr_get(rec["repo"], rec["pr"])
        if not pr.get("draft") and pr.get("state") == "open":
            gh_ready(rec["repo"], rec["pr"], undo=True)
        comment_once(rec, "rejected", "sf: 今回の出荷は差し戻されました。PR は下書きのまま残します。")
        say(rec, "差し戻されました。PR は下書きのまま残しました（コメント1回）。")
        update(key, stage="rejected")
        lines.append(f"REJECTED {key} pr=#{rec['pr']} (draft のまま)")
    else:
        comment_once(rec, "withdrawn", "sf: 取り下げられたため、この PR を閉じます。ブランチは残します。")
        api("PATCH", f"repos/{rec['repo']}/pulls/{rec['pr']}", dict(state="closed"))
        say(rec, "取り下げられました。PR を閉じました（ブランチは残しています）。")
        update(key, stage="withdrawn")
        lines.append(f"WITHDRAWN {key} pr=#{rec['pr']} (closed, branch kept)")
    return True


def covered_by_later(rec, d):
    """A deploy_failed record is done when another record of the same repository is done with a merge commit that contains this one."""
    for k, o in C.ship_records().items():
        if k != rec["key"] and o.get("repo") == rec["repo"] and o.get("stage") == "done" and o.get("merge_sha") and rec.get("merge_sha"):
            if git(d, "merge-base", "--is-ancestor", rec["merge_sha"], o["merge_sha"]).returncode == 0:
                return k
    return None


def process(rec, lines):
    key, stage = rec["key"], rec["stage"]
    row = row_of(key)
    if row is None:
        return  # the review row is not made yet (prepare ran first): nothing to decide
    st = status_of_row(row)
    conf = C.ship_conf(rec["repo"])
    if stage in ("deploy_failed", "halted"):
        if st == "承認" and stage == "deploy_failed":
            k = covered_by_later(rec, repo_dir(rec["repo"]))
            if k:
                update(key, stage="done")
                tick_applied(rec, f"後の出荷（{k}）に含まれて本番へ反映済みです。")
                lines.append(f"SHIPPED {key} 後の出荷 {k} で反映済み")
                return
        age = time.time() - rec.get("alerted", rec.get("updated", 0))
        if age > ALERT_AFTER and time.time() - rec.get("alert_sent", 0) > ALERT_EVERY:
            update(key, alert_sent=int(time.time()))
            lines.append(f"ALERT {key} stage={stage} {int((time.time() - rec.get('updated', time.time())) / 60)}分止まっている（{rec.get('hold')}）。人の retry を待っています")
        return
    if st == "承認":
        try:
            step_ship(rec, conf, lines)
        except Void as v:
            void(C.ship_record(key) or rec, v.code, v.reason, lines)
        return
    if st in ("差し戻し", "取り下げ"):
        settle_decided(rec, st, lines)
        return
    if stage in ("tested", "readied"):  # an approval taken back: a draft again, waiting
        back_from_cancel(rec, st, lines)


def run(only_key=None, dry_run=False):
    """-> (lines, rc). One pass over the live records."""
    global DRY
    saved, DRY = DRY, bool(dry_run)
    try:
        return _run(only_key)
    finally:
        DRY = saved


def _run(only_key):
    WOULD.clear()
    lines = []
    lock = C.RunLock("ship_approved.lock")
    with lock:
        if not lock.acquired:
            return [], 0
        for key, rec in sorted(C.ship_records().items(), key=lambda kv: kv[1].get("created", 0)):
            if only_key and key != only_key:
                continue
            if rec.get("stage") in ("done", "rejected", "withdrawn", "void"):
                continue
            rec = dict(rec, key=key)
            err = None
            try:
                process(rec, lines)
            except (Transient, GhError) as e:
                err = f"ERROR {key} {e}"[:300]
            except Exception as e:
                err = f"ERROR {key} {type(e).__name__}: {e}"[:300]
            if err:  # the same error text is printed once and again only when it changes (a stuck ship must not repeat every 3 minutes)
                if rec.get("last_error") != err:
                    lines.append(err)
                    update(key, last_error=err)
            elif rec.get("last_error"):
                update(key, last_error=None)
    lines += [f"WOULD {w}" for w in WOULD]
    return lines, (1 if any(l.startswith(("FAIL", "ALERT", "ERROR")) for l in lines) else 0)


# ---------------------------------------------------------------- re-ask after a void (used by the gate controller)

def reask_key(base_key, asked):
    """The key of the next round when the latest round of `base_key` ended in void because the branch moved on: <base>-r(N+1), else None.
    Round 1 is the base key, round N>1 is <base>-rN."""
    n = 1
    while f"{base_key}-r{n + 1}" in asked:
        n += 1
    latest = base_key if n == 1 else f"{base_key}-r{n}"
    rec = C.ship_record(latest)
    if not rec or rec.get("stage") != "void" or latest not in asked:
        return None
    try:
        head = remote_head(repo_dir(rec["repo"]), rec["branch"])
    except Transient:
        return None
    return f"{base_key}-r{n + 1}" if head and head != rec.get("head") else None


# ---------------------------------------------------------------- commands for people

def cmd_status(only_key):
    out = []
    for key, rec in sorted(C.ship_records().items(), key=lambda kv: kv[1].get("created", 0)):
        if only_key and key != only_key:
            continue
        live = ""
        try:
            pr = pr_get(rec["repo"], rec["pr"])
            live = f" github: state={pr.get('state')} draft={pr.get('draft')} merged={pr.get('merged')}"
        except Exception as e:
            live = f" github: {type(e).__name__}"
        out.append(f"{key} stage={rec.get('stage')} pr=#{rec.get('pr')} head={sha12(rec.get('head'))} merge={sha12(rec.get('merge_sha'))} hold={rec.get('hold')}{live}")
    print("\n".join(out) if out else "no records")
    return 0


def cmd_retry(key):
    rec = C.ship_record(key)
    if not rec or rec.get("stage") not in ("deploy_failed", "halted"):
        print(f"ERROR {key} is not deploy_failed / halted ({(rec or {}).get('stage')})")
        return 2
    update(key, stage="merged", hold=None, alerted=None, alert_sent=None)
    print(f"RETRY {key}: stage merged, the next run deploys again")
    return 0


OLD_MEANING = ("- 承認すると：このエピックが完了になります。PR の作成・マージと本番反映は行いません。それらは、あなたが別に指示したときだけ行います。\n"
               "- 差し戻すと：理由を読み、直す作業を足して、もう一度この依頼を出します。\n- 取り下げると：何も変えません。")
OLD_NO_PR = "PR は未作成です（人の指示のあとに作ります）。"


def cmd_adopt(key, epic):
    """A review row made before the switch: prepare the draft PR, then rewrite chapter 1's fixed sentences and the PR line of 何を出すか."""
    res = prepare(epic, key, dry_run=DRY)
    if res["status"] != "ok":
        print(f"{'HOLD' if res['status'] == 'hold' else 'ERROR'} {key} {res['code']}: {res['reason']}")
        return 3 if res["status"] == "hold" else 1
    row = row_of(key)
    if not row:
        print(f"ERROR {key} no review row with this key")
        return 1
    repo = (C.ship_record(key) or {}).get("repo")
    new = C.release_meaning(**C.ship_meaning(repo, res["pr_url"], res["pr"]))
    ups = [dict(old_str=OLD_MEANING, new_str=new), dict(old_str=OLD_NO_PR, new_str=f"PR：[{res['pr_url']}]({res['pr_url']})")]
    if not DRY:
        C.ntn(f"v1/pages/{row['id']}/markdown", "-X", "PATCH", data={"type": "update_content", "update_content": {"content_updates": ups}})
    print(f"ADOPTED {key} pr={res['pr_url']}" + (" (dry-run)" if DRY else ""))
    return 0


def main(argv=None):
    global DRY
    for k in [k for k in os.environ if k.startswith(("SAFE_DEPLOY_", "SYNC_LIVE_"))]:  # a deploy started by another process must not leak into ours
        del os.environ[k]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("prepare")
    p.add_argument("--epic", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--key")
    r.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--key")
    t = sub.add_parser("retry")
    t.add_argument("--key", required=True)
    a_ = sub.add_parser("adopt")
    a_.add_argument("--key", required=True)
    a_.add_argument("--epic", required=True)
    a_.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    cmd = a.cmd or "run"
    try:
        if cmd == "prepare":
            res = prepare(a.epic, a.key, dry_run=a.dry_run)
            if res["status"] == "ok":
                print(f"{'WOULD-PREPARE' if a.dry_run else 'PREPARED'} {a.key} pr={res['pr_url']} head={sha12(res['head'])} draft=yes")
                return 0
            print(f"{'HOLD' if res['status'] == 'hold' else 'ERROR'} {a.key} {res['code']}: {res['reason']}")
            return 3 if res["status"] == "hold" else 1
        if cmd == "run":
            lines, rc = run(getattr(a, "key", None), getattr(a, "dry_run", False))
            if lines:
                print("\n".join(lines))
            return rc
        if cmd == "status":
            return cmd_status(a.key)
        if cmd == "retry":
            return cmd_retry(a.key)
        if cmd == "adopt":
            DRY = a.dry_run
            return cmd_adopt(a.key, a.epic)
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}"[:400])
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
