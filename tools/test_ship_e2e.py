#!/usr/bin/env python3
"""scripts/ship_approved.py の通し(オフライン。標準ライブラリと git だけ。make test に入っている)。

一時ディレクトリに本物の git(bare な origin、main の作業ツリー、作業ブランチ)を作り、gh と make は偽物(PATH の先頭の小さなスクリプト)に差し替える。
偽の gh は REST を JSON ファイルで真似るが、マージは本物の git で行う(squash して origin の main へ push、ブランチ削除は ref の削除)。
Notion は関数ごと差し替える。GitHub・Notion・本番・kanban には触れない。

  E1 prepare -> 承認 -> run: 下書き PR、main に重ねたテスト(作業ツリーの中身を見る)、公開、squash マージ、ブランチ削除、main の作業ツリーが早送りされ make test → (tested_tree.json の照合) → make ship-tested / check / smoke が走る
  E2 done のあとに run を回しても gh の書き込みも make も 0 回
  E3 コンフリクト(origin/main と衝突するブランチ): マージせず void、一時ワークツリーが残らない
  E4 差し戻しは下書きのまま、取り下げは PR を閉じてブランチを残す
  E5 マージ後の make が失敗: main はそのまま(戻さない)、deploy_failed、retry で続きから(マージは1回)
  E7 出荷の記録(ship_events.jsonl)は、段が変わるたびに1行(E1・E2・E5 の中で確かめる)。書けなくても出荷は止めない
  E6 別の head でテストした記録(tested_tree.json)なら、deploy(make ship-tested)の直前で拒否する(FAIL tested-tree。main は戻さない)。記録が正しければ retry で続きから
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SF_DEPLOY_", "SAFE_DEPLOY_"))}
GIT_ENV.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0")
os.environ.update({k: v for k, v in GIT_ENV.items() if k.startswith("GIT_")})

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   {name}")
    else:
        failed += 1
        print(f"FAIL {name}\n     {detail}")


def git(d, *a, check_rc=True):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, env=GIT_ENV)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"git {a}: {r.stderr}")
    return r.stdout.strip()


FAKE_GH = r'''#!__PY__
import json, os, subprocess, sys, tempfile
ST, ORIGIN = os.environ["FAKE_GH_STATE"], os.environ["FAKE_ORIGIN"]
def git(*a, cwd=None):
    return subprocess.run(["git", *(["-C", cwd] if cwd else ["-C", ORIGIN]), *a], capture_output=True, text=True)
def load():
    return json.load(open(ST)) if os.path.exists(ST) else {"prs": {}, "comments": {}, "log": []}
def save(d): json.dump(d, open(ST, "w"))
def fail(code, text):
    print(f"gh: {text} (HTTP {code})", file=sys.stderr); sys.exit(1)
d = load(); a = sys.argv[1:]
if a[:2] == ["pr", "ready"]:
    n = a[2]; d["log"].append(f"pr ready{' --undo' if '--undo' in a else ''} {n}")
    d["prs"][n]["draft"] = "--undo" in a; save(d); sys.exit(0)
method, path = a[a.index("-X") + 1], a[a.index("-X") + 2]
body = json.loads(sys.stdin.read()) if "--input" in a else None
short = path.split("?")[0].split("/", 3)[3] if path.startswith("repos/") else path
d["log"].append(f"{method} {short}")
def view(p):
    h = git("rev-parse", "--verify", "-q", f"refs/heads/{p['head']['ref']}")
    if p["state"] == "open" and h.returncode == 0: p["head"]["sha"] = h.stdout.strip()
    mt = git("merge-tree", "--write-tree", "refs/heads/main", p["head"]["sha"])
    p["mergeable"] = mt.returncode == 0
    p["changed_files"] = len([x for x in git("diff", "--name-only", f"refs/heads/main...{p['head']['sha']}").stdout.split() if x])
    return p
out = {}
if method == "GET" and short == "pulls":
    out = [view(p) for p in d["prs"].values() if p["state"] == "open" and p["head"]["ref"] in path.replace("%2F", "/")]
elif method == "POST" and short == "pulls":
    n = str(31 + len(d["prs"]))
    sha = git("rev-parse", f"refs/heads/{body['head']}").stdout.strip()
    d["prs"][n] = dict(number=int(n), html_url=f"https://github.com/o/r/pull/{n}", title=body["title"], body=body["body"], draft=body["draft"], state="open",
                       merged=False, merge_commit_sha=None, head=dict(sha=sha, ref=body["head"]), base=dict(ref=body["base"]))
    out = view(d["prs"][n])
elif short.startswith("pulls/") and short.count("/") == 1:
    p = d["prs"][short.split("/")[1]]
    if method == "PATCH": p.update({k: v for k, v in body.items() if k in ("title", "body", "state")})
    out = view(p)
elif short.startswith("pulls/") and short.endswith("/merge"):
    p = d["prs"][short.split("/")[1]]; view(p)
    if p["draft"]: fail(405, "Pull Request is still a draft")
    if body["sha"] != p["head"]["sha"]: fail(409, "Head branch was modified")
    if body["merge_method"] != "squash": fail(405, "squash only")
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-q", ORIGIN, tmp], check=True, capture_output=True)
    r = git("merge", "--squash", p["head"]["sha"], cwd=tmp)
    if r.returncode != 0: fail(405, "Pull Request is not mergeable")
    git("commit", "-q", "-m", body["commit_title"], cwd=tmp)
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=tmp)
    p["merged"], p["state"] = True, "closed"; p["merge_commit_sha"] = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
    out = dict(sha=p["merge_commit_sha"], merged=True)
elif "/check-runs" in short:
    out = dict(check_runs=[])
elif short.startswith("issues/") and short.endswith("/comments"):
    lst = d["comments"].setdefault(short.split("/")[1], [])
    if method == "POST": lst.append(body)
    out = lst
elif method == "DELETE" and short.startswith("git/refs/heads/"):
    git("update-ref", "-d", "refs/heads/" + short.split("heads/", 1)[1])
else:
    fail(404, f"unexpected {method} {path}")
save(d); print(json.dumps(out))
'''

FAKE_MAKE = r'''#!/bin/sh
args=""; tt=""
for a in "$@"; do case "$a" in TESTED_TREE=*) tt="${a#TESTED_TREE=}";; *) args="$args $a";; esac; done
args="${args# }"
echo "make $args @ $(pwd) :: $(ls | tr '\n' ' ')" >> "$FAKE_MAKE_LOG"
case " $FAKE_MAKE_FAIL " in *" $args "*) echo "boom: $args" >&2; exit 2;; esac
# 本物の make test の最後の行(scripts/tested_tree.py write)。ship_approved.py が渡した TESTED_TREE に、この木の HEAD を書く。
# FAKE_MAKE_TT_HEAD があれば、別の木でテストした記録(別の head)を書く(E6)。
if [ "$args" = test ] && [ -n "$tt" ]; then
  if [ -n "$FAKE_MAKE_TT_HEAD" ]; then printf '{"path": "/elsewhere", "head": "%s"}\n' "$FAKE_MAKE_TT_HEAD" > "$tt"
  else __PY__ __TT__ write --file "$tt" --tree "$(pwd)" >/dev/null || exit 2; fi
fi
exit 0
'''


class Sandbox:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-ship-e2e-"))
        t = self.tmp
        self.origin, self.root = t / "origin.git", t / "repos"
        self.main = self.root / "example-org" / "sf"
        self.bin = t / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH.replace("__PY__", sys.executable), encoding="utf-8")
        (self.bin / "make").write_text(FAKE_MAKE.replace("__PY__", sys.executable).replace("__TT__", str(REPO / "scripts" / "tested_tree.py")), encoding="utf-8")
        for n in ("gh", "make"):
            (self.bin / n).chmod(0o755)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, env=GIT_ENV)
        self.main.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.main)], check=True, capture_output=True, env=GIT_ENV)
        git(self.main, "checkout", "-q", "-b", "main")
        (self.main / "a.txt").write_text("one\n")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "init")
        git(self.main, "push", "-q", "origin", "main")
        self.state, self.live = t / "state", t / "live"
        self.state.mkdir()
        self.live.mkdir()
        self.gh_state, self.make_log = t / "gh.json", t / "make.log"
        base = (REPO / "tests" / "config.test.toml").read_text(encoding="utf-8")
        base = base[:base.index("[ship.repos")]
        base = re.sub(r'(?m)^repo = .*$', f'repo = "{self.main}"', base, count=1)
        base = re.sub(r'(?m)^live = .*$', f'live = "{self.live}"', base, count=1)
        base = re.sub(r'(?m)^state = .*$', f'state = "{self.state}"', base, count=1)
        (t / "config.toml").write_text(base + '\n[ship.repos."example-org/sf"]\ntest = ["make", "test"]\ndeploy = [["make", "ship"], ["make", "check"], ["make", "smoke"]]\n'
                                       'cron_paths = ["hermes/cron.toml"]\ndescribe = "本番へ反映します"\n', encoding="utf-8")
        self.config = t / "config.toml"

    def env(self):
        os.environ.update(SF_CONFIG=str(self.config), FAKE_GH_STATE=str(self.gh_state), FAKE_ORIGIN=str(self.origin), FAKE_MAKE_LOG=str(self.make_log),
                          EPIC_PIPELINE_STATE_DIR=str(self.state), PATH=f"{self.bin}:{os.environ['PATH']}")

    def branch(self, name, files, base="main"):
        """A work branch pushed to origin with the given files."""
        git(self.main, "checkout", "-q", "-B", name, f"origin/{base}")
        for f, text in files.items():
            (self.main / f).write_text(text)
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", f"work on {name}")
        git(self.main, "push", "-q", "-f", "origin", name)
        git(self.main, "checkout", "-q", "main")

    def gh(self):
        with open(self.gh_state, encoding="utf-8") as f:
            return json.load(f)

    def made(self):
        return self.make_log.read_text(encoding="utf-8").splitlines() if self.make_log.exists() else []


def events(path, key=None):
    """The lines of ship_events.jsonl (of one key)."""
    if not Path(path).exists():
        return []
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if key is None or r["key"] == key]


def main():
    sb = Sandbox()
    sb.env()
    sys.path.insert(0, str(REPO / "scripts"))
    import epic_common as C
    import ship_approved as S
    import retro_metrics as M
    status = {}
    notes = []
    C.ntn = lambda *a, data=None, **k: ({"results": [{"id": "row-" + data["filter"]["rich_text"]["equals"], "url": "u", "key": data["filter"]["rich_text"]["equals"], "properties": {}}],
                                           "has_more": False} if a[0].endswith("/query") else {})
    C.append_to_chapter = lambda pid, h, ch: notes.append((h, ch[0][ch[0]["type"]]["rich_text"][0]["text"]["content"]))
    S.status_of_row = lambda row: status.get(row["key"], "レビュー待ち")
    S.read_inputs = lambda epic, repo=None, branch=None: ("example-org/sf", BR[0], BODY)
    BR = ["michiomochi/x"]
    BODY = "タイトル：b を足す\n\n## 概要\nb.txt を足した\n## 背景\nテスト\n## 変更内容\n- b.txt\n## 検証\n- make test\n"
    try:
        sb.branch(BR[0], {"b.txt": "b\n"})
        key = "e2e00001-承認"
        res = S.prepare("E", key)
        check("E1 prepare: a draft PR that reads back", res["status"] == "ok" and sb.gh()["prs"]["31"]["draft"] is True and sb.gh()["prs"]["31"]["changed_files"] == 1, res)
        check("E1 a draft waiting for the person: run is silent and writes nothing", S.run() == ([], 0) and sb.made() == [], sb.made())
        status[key] = "承認"
        old_main = git(sb.origin, "rev-parse", "refs/heads/main")
        lines, rc = S.run()
        new_main = git(sb.origin, "rev-parse", "refs/heads/main")
        check("E1 SHIPPED", rc == 0 and len(lines) == 1 and lines[0].startswith(f"SHIPPED {key} pr=#31 merge={new_main[:12]}"), lines)
        pr = sb.gh()["prs"]["31"]
        check("E1 squash merge on origin/main: one commit on top of the old main with the title and the branch's file",
              git(sb.origin, "rev-parse", "refs/heads/main~1") == old_main and git(sb.origin, "log", "-1", "--format=%s", "refs/heads/main") == "b を足す (#31)"
              and git(sb.origin, "show", "refs/heads/main:b.txt") == "b" and pr["merged"] and pr["merge_commit_sha"] == new_main, (new_main, pr))
        check("E1 the remote branch is deleted, the local one is not", git(sb.origin, "rev-parse", "--verify", "-q", f"refs/heads/{BR[0]}", check_rc=False) == ""
              and git(sb.main, "rev-parse", "--verify", "-q", f"refs/heads/{BR[0]}", check_rc=False) != "", "branch")
        mk = sb.made()
        check("E1 make test ran on main + the branch in a temporary tree; ship / check / smoke ran in the main working tree, in order",
              len(mk) == 5 and mk[0].startswith("make test @ ") and "b.txt" in mk[0] and "a.txt" in mk[0] and str(sb.main) not in mk[0]
              and [m.split(" @ ")[0] for m in mk[1:]] == ["make test", "make ship-tested", "make check", "make smoke"] and all(f"@ {sb.main} ::" in m for m in mk[1:]), mk)
        check("E1 the main working tree is fast-forwarded to the merge commit, clean, on main", git(sb.main, "rev-parse", "HEAD") == new_main
              and git(sb.main, "rev-parse", "--abbrev-ref", "HEAD") == "main" and git(sb.main, "status", "--porcelain", "--untracked-files=no") == "", "tree")
        check("E1 no temporary worktree is left behind", len(git(sb.main, "worktree", "list").splitlines()) == 1, git(sb.main, "worktree", "list"))
        check("E1 the PR comment is public-safe", all(not re.search(r"notion|タスク|エピック", c["body"], re.I) for c in sb.gh()["comments"].get("31", [])), sb.gh()["comments"])
        ev_file = sb.state / "ship_events.jsonl"
        ev = events(ev_file, key)
        check("E1 ship_events.jsonl: one line per stage change, in order, exactly one stage=done", [e["stage"] for e in ev] == ["drafted", "tested", "readied", "merged", "deployed", "done"]
              and all(e["key"] == key and e["repo"] == "example-org/sf" and e["pr"] == 31 and e["mode"] == "human" and isinstance(e["ts"], (int, float)) for e in ev), ev)
        timed = [e for e in ev if isinstance(e.get("test_seconds"), (int, float))]
        check("E1 ship_events.jsonl: test_seconds (a number) on exactly one line, the make test on main + the branch: stage=tested, test_rc=0", len(timed) == 1 and timed[0]["stage"] == "tested"
              and timed[0]["test_rc"] == 0 and timed[0]["test_seconds"] >= 0, timed)
        now = int(time.time())
        got = M.compute(now - 3600, now + 3600, ship_events=str(ev_file), groups=("make_test",))["metrics"]["make_test.sec_p50"]
        check("E1 retro_metrics reads that file: make_test.sec_p50 is measured (n=1, n<3), not unmeasured", got["n"] == 1 and got["status"] == "n<3", got)
        gone = M.compute(now - 3600, now + 3600, ship_events=str(sb.tmp / "nothing.jsonl"), groups=("make_test",))["metrics"]["make_test.sec_p50"]
        check("E1 a path that does not exist stays unmeasured", gone["status"] == "unmeasured", gone)
        n_events = len(events(ev_file))
        before = (len(sb.made()), [x for x in sb.gh()["log"] if x.split()[0] in ("POST", "PUT", "PATCH", "DELETE") or x.startswith("pr ready")])
        for _ in range(3):
            S.run()
        after = (len(sb.made()), [x for x in sb.gh()["log"] if x.split()[0] in ("POST", "PUT", "PATCH", "DELETE") or x.startswith("pr ready")])
        check("E2 after done: three more runs make no gh write and no make call", before == after, (before, after))
        check("E2 after done: three more runs add no line to ship_events.jsonl", len(events(ev_file)) == n_events, len(events(ev_file)))

        # E3: a branch that conflicts with main
        (sb.main / "a.txt").write_text("main side\n")
        git(sb.main, "add", "-A")
        git(sb.main, "commit", "-q", "-m", "main moves")
        git(sb.main, "push", "-q", "origin", "main")
        sb.branch("michiomochi/c", {"a.txt": "branch side\n"}, base="main~1")
        BR[0] = "michiomochi/c"
        k3 = "e2e00003-承認"
        r3 = S.prepare("E", k3)
        check("E3 prepare holds a branch that conflicts with main (GitHub says mergeable=false)", r3["status"] == "hold" and r3["code"] == "conflict", r3)
        # the branch is rebased onto the commit before main moved, but approved earlier: take a branch that conflicts only at run time
        sb.branch("michiomochi/d", {"d.txt": "d\n"})
        BR[0] = "michiomochi/d"
        k4 = "e2e00004-承認"
        check("E3 a clean branch prepares", S.prepare("E", k4)["status"] == "ok", "prepare d")
        sb.branch("michiomochi/d", {"d.txt": "d\n", "a.txt": "branch side again\n"}, base="main~1")  # (force push after the approval's PR exists: head moves)
        status[k4] = "承認"
        lines, rc = S.run()
        check("E3 a branch that moved after prepare is void (head-moved): nothing merged, the PR is a draft", lines and lines[0].startswith(f"VOID {k4} head-moved")
              and git(sb.origin, "log", "-1", "--format=%s", "refs/heads/main") == "main moves"
              and sb.gh()["prs"]["32"]["draft"] is True, (lines, git(sb.origin, "log", "-1", "--format=%s", "refs/heads/main")))
        n_made = len(sb.made())

        # E4 rejection and withdrawal
        sb.branch("michiomochi/e", {"e.txt": "e\n"})
        BR[0] = "michiomochi/e"
        k5 = "e2e00005-承認"
        S.prepare("E", k5)
        status[k5] = "差し戻し"
        lines, _ = S.run()
        n5 = [n for n, p in sb.gh()["prs"].items() if p["head"]["ref"] == "michiomochi/e"][0]
        check("E4 差し戻し: the PR stays a draft and open, one comment", lines and lines[0].startswith("REJECTED") and sb.gh()["prs"][n5]["draft"] is True
              and sb.gh()["prs"][n5]["state"] == "open" and len(sb.gh()["comments"][n5]) == 1, lines)
        sb.branch("michiomochi/f", {"f.txt": "f\n"})
        BR[0] = "michiomochi/f"
        k6 = "e2e00006-承認"
        S.prepare("E", k6)
        status[k6] = "取り下げ"
        lines, _ = S.run()
        n6 = [n for n, p in sb.gh()["prs"].items() if p["head"]["ref"] == "michiomochi/f"][0]
        check("E4 取り下げ: the PR is closed and the branch stays; no merge happened", lines and lines[0].startswith("WITHDRAWN") and sb.gh()["prs"][n6]["state"] == "closed"
              and git(sb.origin, "rev-parse", "--verify", "-q", "refs/heads/michiomochi/f", check_rc=False) != "" and len(sb.made()) == n_made, lines)

        # E5 a failing deploy step after the merge: main is not put back
        git(sb.main, "pull", "-q", "--ff-only", "origin", "main")
        sb.branch("michiomochi/g", {"g.txt": "g\n"})
        BR[0] = "michiomochi/g"
        k7 = "e2e00007-承認"
        S.prepare("E", k7)
        status[k7] = "承認"
        os.environ["FAKE_MAKE_FAIL"] = "check"
        lines, rc = S.run()
        merged_main = git(sb.origin, "rev-parse", "refs/heads/main")
        check("E5 make check fails after the merge: FAIL line, deploy_failed, origin/main keeps the merge (never reverted)", rc == 1 and lines[0].startswith(f"FAIL {k7} make check rc=2")
              and C.ship_record(k7)["stage"] == "deploy_failed" and git(sb.origin, "rev-parse", "refs/heads/main") == [p for p in sb.gh()["prs"].values() if p["head"]["ref"] == "michiomochi/g"][0]["merge_commit_sha"], lines)
        os.environ["FAKE_MAKE_FAIL"] = ""
        e7 = events(ev_file, k7)
        check("E5 ship_events.jsonl: the failed deploy is one stage=deploy_failed line (and no done line yet)", [e["stage"] for e in e7].count("deploy_failed") == 1 and "done" not in [e["stage"] for e in e7], e7)
        merges = [x for x in sb.gh()["log"] if x.endswith("/merge")]
        check("E5 retry runs the deploy again and never merges again", S.cmd_retry(k7) == 0 and S.run()[0][0].startswith(f"SHIPPED {k7}")
              and [x for x in sb.gh()["log"] if x.endswith("/merge")] == merges and git(sb.origin, "rev-parse", "refs/heads/main") == merged_main, "retry")

        # E6 the tree that was tested is not the tree to ship: refused right before the deploy
        git(sb.main, "pull", "-q", "--ff-only", "origin", "main")
        sb.branch("michiomochi/h", {"h.txt": "h\n"})
        BR[0] = "michiomochi/h"
        k8 = "e2e00008-承認"
        S.prepare("E", k8)
        status[k8] = "承認"
        os.environ["FAKE_MAKE_TT_HEAD"] = "0123456789abcdef0123456789abcdef01234567"
        lines, rc = S.run()
        now = git(sb.main, "rev-parse", "HEAD")
        made = [m.split(" @ ")[0] for m in sb.made()]
        check("E6 a record of another head: FAIL tested-tree with both shas, no make ship-tested / check / smoke, deploy_failed, main keeps the merge",
              rc == 1 and lines[0].startswith(f"FAIL {k8} tested-tree rc=1") and f"tested 0123456789ab / now {now[:12]}。make test を回してから出荷する" in lines[0]
              and made[-1] == "make test" and C.ship_record(k8)["stage"] == "deploy_failed" and git(sb.origin, "rev-parse", "refs/heads/main") == now, (lines, made[-3:]))
        os.environ["FAKE_MAKE_TT_HEAD"] = ""
        before = len(sb.made())
        check("E6 the same head (a real tested_tree.py write) goes through after retry", S.cmd_retry(k8) == 0 and S.run()[0][0].startswith(f"SHIPPED {k8}")
              and [m.split(" @ ")[0] for m in sb.made()[before:]] == ["make test", "make ship-tested", "make check", "make smoke"], sb.made()[before:])

        # E7 the record is an observation: when it cannot be written, the stage still changes and one WARN goes to stderr
        os.environ["FAKE_MAKE_TT_HEAD"] = ""
        (sb.state / "ship_events.jsonl").unlink()
        (sb.state / "ship_events.jsonl").mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rec = S.update("e2e00009-承認", stage="drafted", repo="example-org/sf", pr=99)
        check("E7 ship_events.jsonl cannot be written: update still saves the stage, one WARN on stderr", rec["stage"] == "drafted" and C.ship_record("e2e00009-承認")["stage"] == "drafted"
              and err.getvalue().count("WARN cannot write ship_events.jsonl") == 1, err.getvalue())
        S.DRY = True
        (sb.state / "ship_events.jsonl").rmdir()
        S.update("e2e00009-承認", stage="tested")
        S.DRY = False
        check("E7 a dry run writes no ship_events.jsonl", not (sb.state / "ship_events.jsonl").exists(), "file exists")
    finally:
        shutil.rmtree(sb.tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
