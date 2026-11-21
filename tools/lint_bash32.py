#!/usr/bin/env python3
"""bash 3.2(macOS 標準)で落ちる配列の書き方を見つける(標準ライブラリのみ。`make test` から実行)。

bash 3.2 は、`set -u` の下で「空の配列」を `"${arr[@]}"` / `${arr[*]}` と展開すると unbound variable で落ちる
(bash 4.4 以降は落ちない)。WSL(bash 5)では緑のまま、Mac でだけ落ちる。許す書き方は次だけ:

  - `+` 形式: `${arr[@]+"${arr[@]}"}`(空なら何も渡さない)
  - 分岐: 同じ行か直前 3 行に要素数の判定 `${#arr[@]}` がある(`if [ "${#arr[@]}" -gt 0 ]; then ...`)
  - 常に空でない配列: ファイル内の代入がすべて 1 要素以上の書き下し(`arr=(a b)`)、または
    `# nonempty: arr(理由)` のコメントで宣言したもの(`read -a` で作り、必ず 1 語以上入る場合など)
  - その行の終わりに `# bash32-ok: 理由`

対象は、`set -u`(`set -eu` / `set -euo pipefail` / `set -o nounset` を含む)を書いたファイルの、そこより後の行。
コメントだけの行は見ない。

    python3 tools/lint_bash32.py            # リポジトリの追跡中のシェルスクリプトすべて
    python3 tools/lint_bash32.py FILE...    # 指定したファイルだけ

見つけたら `file:line: ...` を出し、終了コード 1。
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SET_U = re.compile(r"^\s*set\s+(?:-[A-Za-z]*u[A-Za-z]*|-o\s+nounset)\b")
SET_PLUS_U = re.compile(r"^\s*set\s+(?:\+[A-Za-z]*u[A-Za-z]*|\+o\s+nounset)\b")
# ${arr[@]+"${arr[@]}"} と ${arr[*]:+...} の形(許す)。内側の展開を含めて丸ごと消してから探す。
PLUS_FORM = re.compile(r"\$\{(\w+)\[[@*]\]:?\+[^}]*\$\{\1\[[@*]\]\}[^}]*\}")
EXPANSION = re.compile(r"\$\{(\w+)\[[@*]\]\}")  # `${#arr[@]}` と `${!arr[@]}` は \w+ の前に # ! があるので当たらない
NONEMPTY_DECL = re.compile(r"#\s*nonempty:\s*([\w ,]+)")
ASSIGN = re.compile(r"(?<![\w$])(\w+)\+?=\(([^)]*)\)")
OK_MARK = "# bash32-ok:"
GUARD_LINES = 3


def is_shell(path, text):
    first = text.split("\n", 1)[0]
    return path.suffix == ".sh" or (first.startswith("#!") and "bash" in first)


def literal_nonempty(lines):
    """ファイル内で、代入がすべて 1 要素以上の書き下し(arr=(a b))の配列名、と `# nonempty:` で宣言した名前。"""
    seen, empty = set(), set()
    declared = set()
    for ln in lines:
        for m in NONEMPTY_DECL.finditer(ln):
            declared |= set(re.split(r"[ ,]+", m.group(1).strip()))
        for m in ASSIGN.finditer(ln):
            seen.add(m.group(1))
            if not m.group(2).strip():
                empty.add(m.group(1))
    return (seen - empty) | declared


def check_text(text, label):
    """[(label:行: 説明)]。"""
    lines = text.split("\n")
    safe = literal_nonempty(lines)
    problems, active = [], False
    for i, ln in enumerate(lines):
        if SET_PLUS_U.match(ln):
            active = False
        if SET_U.match(ln):
            active = True
            continue
        if not active or ln.lstrip().startswith("#") or OK_MARK in ln:
            continue
        body = PLUS_FORM.sub("", ln)
        for m in EXPANSION.finditer(body):
            name = m.group(1)
            if name in safe:
                continue
            window = "\n".join(lines[max(0, i - GUARD_LINES):i + 1])
            if re.search(r"\$\{#" + re.escape(name) + r"\[[@*]\]\}", window):
                continue
            problems.append(f"{label}:{i + 1}: set -u の下で `{m.group(0)}` を無防備に展開(bash 3.2 は空配列で unbound variable)。"
                            f"`${{{name}[@]+\"${{{name}[@]}}\"}}` にするか、`${{#{name}[@]}}` で分岐する")
    return problems


def tracked_shell_files(repo=REPO):
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
        paths = [repo / p for p in out if p]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts and ".worktrees" not in p.parts and "build" not in p.parts]
    found = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if is_shell(p, text):
            found.append(p)
    return found


def main(argv):
    files = [Path(a) for a in argv] if argv else tracked_shell_files()
    problems = []
    for p in files:
        label = p.relative_to(REPO).as_posix() if p.is_absolute() and REPO in p.parents else str(p)
        problems += check_text(p.read_text(encoding="utf-8"), label)
    for pr in problems:
        print(pr, file=sys.stderr)
    if problems:
        print(f"lint_bash32: {len(problems)} 件(bash 3.2 の set -u で落ちる配列の展開)", file=sys.stderr)
        return 1
    print(f"ok lint_bash32: {len(files)} 個のシェルスクリプトに無防備な配列の展開なし")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
