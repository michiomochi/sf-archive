#!/usr/bin/env python3
"""レビュー依頼・PR の前の機械検査(make precheck から呼ぶ。test は回さない。先に make test を回す)。標準ライブラリのみ。

  precheck.py [--config FILE] [--base REF] [--pr-body FILE] [--repo DIR]

検査:
  1. git diff --check <base>...HEAD(行末の空白、衝突の印の残り)
  2. <base>..HEAD の commit メッセージと、--pr-body のファイルに、非公開の記録を指す印が無いこと:
     Notion のページの URL、設定の [notion] task_ds / epic_ds / review_ds の ID(ハイフンの有無を問わない)、
     語「タスク DB」「エピック DB」「レビュー DB」「Notion タスク」(空白の有無を問わない)
差分の中身(コード)は見ない: 設定やプラグインには Notion の URL と data source の ID が正当に入っているため。
1件でもあれば並べて終了コード 1。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import sfconfig  # noqa: E402

NOTION_URL = re.compile(r"https?://(?:[\w-]+\.)?notion\.(?:so|com|site)/\S*", re.I)
WORDS = re.compile(r"(?:タスク|エピック|レビュー)\s*DB|Notion\s*タスク", re.I)


def markers(cfg):
    ids = [(cfg.get("notion") or {}).get(k, "") for k in ("task_ds", "epic_ds", "review_ds")]
    pats = [NOTION_URL, WORDS]
    for i in ids:
        if i:
            pats.append(re.compile(re.escape(i) + "|" + re.escape(i.replace("-", "")), re.I))
    return pats


def scan(label, text, pats):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for p in pats:
            m = p.search(line)
            if m:
                hits.append(f"{label}:{n}: {m.group(0)}  ({line.strip()[:120]})")
                break
    return hits


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pr-body")
    ap.add_argument("--repo", default=str(REPO))
    a = ap.parse_args(argv)
    try:
        cfg = sfconfig.load(a.config, expand=False)
    except sfconfig.ConfigError as e:
        print(f"precheck: {e}", file=sys.stderr)
        return 2
    pats = markers(cfg)
    problems = []

    r = git(a.repo, "diff", "--check", f"{a.base}...HEAD")
    if r.returncode != 0:
        if r.stdout.strip():
            problems.append("git diff --check:\n" + r.stdout.rstrip())
        else:
            print(f"precheck: git diff --check failed: {r.stderr.strip()}", file=sys.stderr)
            return 2

    r = git(a.repo, "log", "--format=%h%x00%B%x1e", f"{a.base}..HEAD")
    if r.returncode != 0:
        print(f"precheck: git log failed: {r.stderr.strip()}", file=sys.stderr)
        return 2
    n_commits = 0
    for rec in r.stdout.split("\x1e"):
        if "\x00" not in rec:
            continue
        sha, body = rec.strip("\n").split("\x00", 1)
        n_commits += 1
        problems += scan(f"commit {sha}", body, pats)

    if a.pr_body:
        try:
            problems += scan(a.pr_body, Path(a.pr_body).read_text(encoding="utf-8"), pats)
        except OSError as e:
            print(f"precheck: cannot read {a.pr_body}: {e}", file=sys.stderr)
            return 2

    if problems:
        print("precheck: NG")
        for p in problems:
            print("  " + p)
        return 1
    print(f"precheck: OK (commit {n_commits} 件" + (f"、{a.pr_body}" if a.pr_body else "") + f"、base {a.base})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
