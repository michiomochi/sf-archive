#!/usr/bin/env python3
"""手順を覚える必要のある操作を、引数を間違えようがない形にしたもの(標準ライブラリのみ)。

Why: `gh` の -R の付け忘れ、PR 本文をシェルの "..." に直接書いてバッククォートが展開される事故、skill_lint の
パスと引数の覚え違い、worktree の切り方の手打ちは、人(と AI)の注意では防げない。repo・base・パスは設定とこのファイルが持つ。

Usage:
  ops.py pr-create --title T --body-file F|- [--base main] [--draft] [--dry-run]
  ops.py pr-merge <番号> [--method squash|merge|rebase] [--dry-run]
  ops.py worktree <slug> [--dry-run]
  ops.py lint [--skills-repo DIR] [--dry-run]
  ops.py render-all [--dry-run]
  ops.py verify [--base origin/main] [--dry-run]

  - repo は [git].repo_url(envs/<環境>.toml)から決め、gh には毎回 -R <owner/repo> を付ける。
  - PR の本文はファイルか stdin(`--body-file -`)だけ。コマンドラインで本文を渡す道は無い。
  - verify: 作業ツリーの repo(origin の owner/repo)の ship.repos の test_fast を回し、続けて変えたパス(base との差と未追跡)に合う重い検査(make test-paths)を回して、
    作業メモに貼る1行(コマンド・rc・壁時計と CPU 時間)を最後に出す。登録の無い repo は、そう書いたうえで既定の make test を回す。
  - --dry-run: 実行するコマンドを表示して終わる。git の読み取り(状態・ブランチ・ref)だけは実行して前提を確かめるが、
    gh と書き込み(fetch / worktree add / push 相当)は実行しない。

Exit: 0 成功 / 2 引数・前提の拒否(何も変えていない。日本語1行) / 1 実行したコマンドの失敗(日本語1行)。
"""
import argparse
import json
import os
import re
import resource
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import epic_common as C  # noqa: E402

DEFAULT_SKILLS_REPO = "~/ghq/github.com/example-org/skills"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MERGE_METHODS = ("squash", "merge", "rebase")


class Refused(Exception):
    """引数・前提の拒否(終了コード 2)。何も変更していない。"""


class Failed(Exception):
    """実行したコマンドの失敗(終了コード 1)。"""


def repo_slug(repo_url=None):
    """設定の [git].repo_url から owner/repo を取る(https でも ssh の形でも)。"""
    url = repo_url or C.CFG["git"]["repo_url"]
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    if not m:
        raise Refused(f"設定の [git].repo_url から owner/repo を取れません: {url}")
    return m.group(1)


class Runner:
    """コマンドの実行。kind: read = ローカルの git の読み取り(dry-run でも実行) / remote・write = dry-run では表示だけ。"""

    def __init__(self, dry_run=False, cwd=None):
        self.dry_run = dry_run
        self.cwd = cwd

    def run(self, cmd, kind="write", stdin=None, cwd=None):
        if self.dry_run:
            print("+ " + shlex.join(map(str, cmd)))
            if kind != "read":
                return subprocess.CompletedProcess(cmd, 0, "", "")
        try:
            return subprocess.run([str(c) for c in cmd], cwd=cwd or self.cwd, input=stdin, text=True, capture_output=True)
        except FileNotFoundError:
            raise Failed(f"{cmd[0]} が見つかりません(PATH を確認してください)")

    def must(self, cmd, kind="write", stdin=None, what=None, cwd=None):
        r = self.run(cmd, kind, stdin, cwd)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            raise Failed(f"{what or shlex.join(map(str, cmd[:3]))} に失敗しました(終了コード {r.returncode})"
                         + (f": {detail[-1]}" if detail else ""))
        return r


def toplevel(cwd=None):
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, text=True, capture_output=True)
    if r.returncode != 0:
        raise Refused("git の作業ツリーの中で実行してください")
    return Path(r.stdout.strip())


def main_root(cwd=None):
    """本体の作業ツリー(worktree の中から打っても、.worktrees を入れ子にしない)。"""
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd, text=True, capture_output=True)
    if r.returncode != 0:
        raise Refused("git の作業ツリーの中で実行してください")
    return Path(r.stdout.strip()).parent


# ------------------------------------------------------------------ pr-create

def read_body(path):
    """(--body-file に渡す値, stdin に流す本文 or None)。本文はファイルか stdin だけ。"""
    if path == "-":
        text = sys.stdin.read()
        if not text.strip():
            raise Refused("本文が空です(stdin に PR 本文を渡してください)")
        return "-", text
    if not path or not Path(path).is_file():
        raise Refused(f"本文のファイルがありません: {path!r}")
    if not Path(path).read_text(encoding="utf-8").strip():
        raise Refused(f"本文のファイルが空です: {path}")
    return str(Path(path).resolve()), None


def cmd_pr_create(a, rn):
    if not a.title.strip():
        raise Refused("--title が空です")
    body_arg, body_stdin = read_body(a.body_file)
    top = toplevel()
    rn.cwd = top
    if rn.run(["git", "status", "--porcelain"], "read").stdout.strip():
        raise Refused("未コミットの変更があります(commit してから PR を作ってください)")
    branch = rn.run(["git", "branch", "--show-current"], "read").stdout.strip()
    if not branch:
        raise Refused("ブランチ上にいません(detached HEAD)")
    if branch == a.base:
        raise Refused(f"ブランチ {branch} が base ({a.base}) と同じです(作業ブランチから PR を作ってください)")
    remote = rn.run(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], "read")
    if remote.returncode != 0:
        raise Refused(f"ブランチ {branch} は origin に push されていません(git push -u origin {branch})")
    head = rn.run(["git", "rev-parse", "HEAD"], "read").stdout.strip()
    if remote.stdout.strip() != head:
        raise Refused(f"origin/{branch} より新しい commit が push されていません(git push)")
    repo = repo_slug()
    existing = rn.run(["gh", "pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "number,url"], "remote")
    if existing.returncode != 0:
        raise Failed(f"gh pr list に失敗しました: {(existing.stderr or '').strip()[-200:]}")
    if not rn.dry_run:
        try:
            prs = json.loads(existing.stdout or "[]")
        except ValueError:
            raise Failed("gh pr list の出力を読めません")
        if prs:
            raise Refused(f"既に開いている PR があります: {prs[0].get('url') or '#' + str(prs[0].get('number'))}")
    cmd = ["gh", "pr", "create", "-R", repo, "--base", a.base, "--head", branch, "--title", a.title, "--body-file", body_arg]
    if a.draft:
        cmd.append("--draft")
    r = rn.must(cmd, "remote", stdin=body_stdin, what="gh pr create")
    if not rn.dry_run:
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        print(lines[-1] if lines else "")


# ------------------------------------------------------------------ pr-merge

def cmd_pr_merge(a, rn):
    repo = repo_slug()
    top = toplevel()
    rn.cwd = top
    view = rn.run(["gh", "pr", "view", str(a.number), "-R", repo, "--json", "state"], "remote")
    if view.returncode != 0:
        raise Failed(f"PR #{a.number} を読めません(gh pr view): {(view.stderr or '').strip()[-200:]}")
    if not rn.dry_run:
        try:
            state = json.loads(view.stdout)["state"]
        except (ValueError, KeyError, TypeError):
            raise Failed("gh pr view の出力を読めません")
        if state != "OPEN":
            raise Refused(f"PR #{a.number} は OPEN ではありません({state})")
    rn.must(["gh", "pr", "merge", str(a.number), "-R", repo, f"--{a.method}"], "remote", what="gh pr merge")
    rn.must(["git", "fetch", "origin", "main"], "remote", what="git fetch origin main")
    head = rn.run(["git", "log", "-1", "--oneline", "origin/main"], "read")
    if not rn.dry_run:
        print(f"merged #{a.number}")
        print(head.stdout.strip())


# ------------------------------------------------------------------ worktree

def cmd_worktree(a, rn):
    if not SLUG_RE.match(a.slug) or ".." in a.slug:
        raise Refused(f"slug は英小文字・数字・. _ - だけ(先頭は英数字)です: {a.slug!r}")
    root = main_root()
    rn.cwd = root
    path = root / ".worktrees" / a.slug
    listed = rn.run(["git", "worktree", "list", "--porcelain"], "read").stdout
    if path.exists() or f"worktree {path}" in listed.splitlines():
        raise Refused(f"worktree が既にあります: {path}")
    branch = f"{C.BRANCH_PREFIX}{a.slug}"
    rn.must(["git", "fetch", "origin"], "remote", what="git fetch origin")
    rn.must(["git", "worktree", "add", path, "-B", branch, "origin/main"], what="git worktree add")
    print(f"worktree: {path}")
    print(f"branch: {branch}")


# ------------------------------------------------------------------ lint / render-all

def cmd_lint(a, rn):
    script = Path(os.path.expanduser(a.skills_repo)) / "tools" / "skill_lint.py"
    if not script.is_file():
        raise Refused(f"skill_lint.py がありません: {script}(--skills-repo で skills repo を指定)")
    top = toplevel()
    rn.cwd = top
    r = rn.run([sys.executable, script, "--strict-skills", "--skills-dir", "skills"], "write")
    if rn.dry_run:
        return
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    found = re.findall(r"(\d+)\s*件", r.stdout)
    if r.returncode != 0 or (found and int(found[-1]) != 0):
        raise Failed("skill_lint が指摘を出しました")


def cmd_render_all(a, rn):
    top = toplevel()
    rn.cwd = top
    r = rn.run([sys.executable, "tools/factory.py", "render", "--all-envs", "--dry-run"], "write")
    if rn.dry_run:
        return
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        raise Failed("render --all-envs が失敗した環境があります")


# ------------------------------------------------------------------ verify

DEFAULT_VERIFY = ["make", "test"]


def changed_paths(rn, base):
    """base との共通祖先から今の作業ツリーまでに変えたパス(commit 済み・未 commit・未追跡。改名は旧新どちらも)。"""
    mb = rn.run(["git", "merge-base", base, "HEAD"], "read")
    if mb.returncode != 0 or not mb.stdout.strip():
        raise Refused(f"{base} との共通祖先を取れません(git fetch origin してから。別の base は --base)")
    names = set()
    for cmd in (["git", "diff", "--name-only", "--no-renames", mb.stdout.strip()], ["git", "ls-files", "--others", "--exclude-standard"]):
        names |= {ln for ln in rn.run(cmd, "read").stdout.splitlines() if ln.strip()}
    return sorted(names)


def timed(cmd, cwd, capture=False):
    """(rc, 壁時計, CPU 秒, 出力)。CPU は待たれた子孫の user＋sys(壁時計の混み具合に左右されない)。capture のときだけ出力を取って、あとで流す。"""
    cpu0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    try:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, text=True, capture_output=capture)
    except FileNotFoundError:
        raise Failed(f"{cmd[0]} が見つかりません(PATH を確認してください)")
    wall = time.monotonic() - t0
    cpu1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.returncode, wall, (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime), (r.stdout or "") if capture else ""


def cmd_verify(a, rn):
    top = toplevel()
    rn.cwd = top
    origin = rn.run(["git", "remote", "get-url", "origin"], "read")
    if origin.returncode != 0:
        raise Refused("origin が無い作業ツリーです(repo を決められません)")
    slug = repo_slug(origin.stdout.strip())
    conf = C.ship_conf(slug) or {}
    if conf.get("test_fast"):
        fast, why = list(conf["test_fast"]), ""
    else:
        if not (top / "Makefile").is_file():
            raise Refused(f"{slug} は ship.repos に test_fast が無く、既定の {shlex.join(DEFAULT_VERIFY)} を回す Makefile も無い")
        fast = list(DEFAULT_VERIFY)
        why = f"{slug} は ship.repos に" + ("登録はあるが test_fast が無い" if conf else "未登録") + f"なので、既定の {shlex.join(fast)} を回した: "
    paths_step = (top / "tools" / "test_plan.py").is_file() and (top / "Makefile").is_file() and bool(conf.get("test_fast"))
    changed = changed_paths(rn, a.base) if paths_step else []
    plan = [fast] + ([["make", "test-paths", "PATHS=" + " ".join(changed)]] if changed else [])
    if rn.dry_run:
        for cmd in plan:
            print("+ " + shlex.join(cmd))
        return
    parts, failed = [], None
    for i, cmd in enumerate(plan):
        rc, wall, cpu, out = timed(cmd, top, capture=(i == 1))
        sys.stdout.write(out)
        label = f"{shlex.join(fast)}" if i == 0 else f"make test-paths(変更 {len(changed)} 件" + (", 該当なし" if "該当なし" in out else "") + ")"
        parts.append(f"{label} rc={rc} wall={wall:.1f}s cpu={cpu:.1f}s")
        if rc != 0:
            failed = failed or label
            break
    if paths_step and not changed:
        parts.append("make test-paths(変更なし)")
    print(f"verify: {why}" + "; ".join(parts))
    if failed:
        raise Failed(f"verify が落ちました: {failed}")


# ------------------------------------------------------------------ main

def build_parser():
    ap = argparse.ArgumentParser(description="手順を覚える必要のある操作(PR・マージ・worktree・lint・render)を間違えようがなくする。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dry-run", action="store_true", help="実行するコマンドを表示して終わる(git の読み取り以外は実行しない)")
        p.set_defaults(fn=fn)
        return p

    p = add("pr-create", cmd_pr_create, "現在のブランチから PR を作る(-R は設定から)")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True, help="本文のファイル。- なら stdin")
    p.add_argument("--base", default="main")
    p.add_argument("--draft", action="store_true")
    p = add("pr-merge", cmd_pr_merge, "OPEN の PR をマージする")
    p.add_argument("number", type=int)
    p.add_argument("--method", choices=MERGE_METHODS, default="squash")
    p = add("worktree", cmd_worktree, "origin/main から .worktrees/<slug> を切る")
    p.add_argument("slug")
    p = add("lint", cmd_lint, "skills repo の skill_lint を --strict-skills で流す")
    p.add_argument("--skills-repo", default=DEFAULT_SKILLS_REPO)
    add("render-all", cmd_render_all, "全環境の render を dry-run で通す")
    p = add("verify", cmd_verify, "repo の test_fast と、変えたパスに合う重い検査を回し、作業メモに貼る1行を出す")
    p.add_argument("--base", default="origin/main", help="変えたパスを数える比べ先")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        a.fn(a, Runner(dry_run=a.dry_run))
    except Refused as e:
        print(f"ops: 拒否: {e}", file=sys.stderr)
        return 2
    except Failed as e:
        print(f"ops: 失敗: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
