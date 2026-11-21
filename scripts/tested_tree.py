#!/usr/bin/env python3
"""「テストした木と、出荷する木が同じか」の記録と照合(標準ライブラリのみ)。

  tested_tree.py write [--file F] [--tree DIR]           make test が最後に呼ぶ。F に {"path": 木の絶対パス, "head": HEAD の sha} だけを書く(時刻は入れない)
  tested_tree.py check [--file F] [--tree DIR | --head SHA]   deploy の直前に呼ぶ。F の head が、いまの木(--head なら指定の sha)の HEAD と同じなら rc 0、違えば rc 1

F の既定は <state>/tested_tree.json(<state> は設定の paths.state。Makefile は TESTED_TREE で渡す)。--tree の既定は cwd。
write は追跡ファイルに未コミットの変更がある木では書かない(HEAD を試したことにならないため。rc 0、理由を1行出す)。
check は、ファイルが無い・壊れている・head が無いときも拒否する(出荷しない側に倒す)。パスは照合しない(別の checkout でも HEAD が同じなら同じ中身)。
拒否のメッセージ: 「出荷を拒否します: tested <sha12> / now <sha12>。make test を回してから出荷する」
ship_approved.py は verify() を直接呼ぶ。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

FILE_NAME = "tested_tree.json"
FIX = "make test を回してから出荷する"


def default_file(state_dir=None):
    if state_dir is None:
        import sfconfig
        state_dir = sfconfig.get()["paths"]["state"]
    return Path(os.path.expanduser(str(state_dir))) / FILE_NAME


def _git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)


def head_of(tree):
    """HEAD の sha(木が git でなければ None)。"""
    r = _git(tree, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def toplevel(tree):
    r = _git(tree, "rev-parse", "--show-toplevel")
    return str(Path(r.stdout.strip()).resolve()) if r.returncode == 0 and r.stdout.strip() else None


def dirty(tree):
    """追跡ファイルに未コミットの変更があるか。"""
    return bool(_git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip())


def read(file):
    """-> (record or None, 理由)。"""
    try:
        d = json.loads(Path(file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"{file} がありません"
    except (OSError, ValueError) as e:
        return None, f"{file} を読めません({type(e).__name__})"
    if not isinstance(d, dict) or not isinstance(d.get("head"), str) or not d["head"]:
        return None, f"{file} に head がありません"
    return d, ""


def verify(file, now_head):
    """出荷する木の HEAD が、make test が記録した head と同じか。-> (ok, message)。例外は投げない。"""
    rec, why = read(file)
    if rec is None:
        return False, f"出荷を拒否します: tested なし({why}) / now {(now_head or '')[:12]}。{FIX}"
    if not now_head:
        return False, f"出荷を拒否します: tested {rec['head'][:12]} / now (HEAD を読めません)。{FIX}"
    if rec["head"] != now_head:
        return False, f"出荷を拒否します: tested {rec['head'][:12]} / now {now_head[:12]}。{FIX}"
    return True, ""


def write(file, tree):
    """-> (rc, message)。"""
    head, top = head_of(tree), toplevel(tree)
    if not head or not top:
        return 2, f"tested_tree: {tree} は git の木ではありません"
    if dirty(tree):
        return 0, f"tested_tree: {top} に未コミットの変更があるため {file} は書きません(出荷できるのはコミット済みの HEAD だけ)"
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(f".{file.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"path": top, "head": head}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, file)
    return 0, f"tested_tree: {file} に head {head[:12]} ({top}) を書きました"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("write", "check"):
        p = sub.add_parser(name)
        p.add_argument("--file")
        p.add_argument("--tree", default=os.getcwd())
        if name == "check":
            p.add_argument("--head")
    a = ap.parse_args(argv)
    file = Path(a.file) if a.file else default_file()
    if a.cmd == "write":
        rc, msg = write(file, a.tree)
        print(msg, file=sys.stderr if rc else sys.stdout)
        return rc
    ok, msg = verify(file, a.head or head_of(a.tree))
    if not ok:
        print(msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
