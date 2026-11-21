#!/usr/bin/env python3
"""スキルに「手順のコマンド」が直に書かれていないかを検査する(標準ライブラリのみ。`make test` から実行)。

なぜ要るか: スキルは「AI が読んで守る」種類の指示で、手順を文章で書くほど読み違える。繰り返す手順は script / make ターゲットに
移し、スキルには移し先の名前だけを書く(規則は skills repo の auditing-skills の規則9が持つ。ここには書き写さない)。
この lint は、移し先が既にある手順のコマンドが、**同じ行に移し先を書かないまま**スキルにあれば落とす。

対象: sf の `skills/**/*.md` と、あれば skills repo の `skills/**/*.md`(`--skills-repo` で場所を変える。無ければ飛ばしたと1行出す)。
コードブロック内・表の行も対象。`<!-- ... -->` のコメントは除く。

    python3 tools/lint_procedures.py                                        # 全部出す(違反があれば exit 1)
    python3 tools/lint_procedures.py --baseline tools/lint_procedures_baseline.json   # 既存は許し、新規・悪化だけ落とす
    python3 tools/lint_procedures.py --write-baseline tools/lint_procedures_baseline.json   # 今の違反で書き直す(直したあとにだけ)

ベースラインは「ファイル::コマンド → 件数」(行番号は持たない。行がずれても同じ違反とみなす)。件数が増えたら悪化で落ちる。
新しい移し先ができたら RULES に1行足す。
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (検出するコマンドの正規表現, 同じ行にあれば許す移し先の正規表現, 出力に出す移し先)
RULES = [
    (r"gh pr create", (r"\bmake pr\b", r"\bops\.py pr-create\b"), "make pr / ops.py pr-create"),
    (r"gh pr merge", (r"\bmake merge\b", r"\bops\.py pr-merge\b"), "make merge / ops.py pr-merge"),
    (r"git worktree add", (r"\bmake worktree\b", r"\bops\.py worktree\b"), "make worktree / ops.py worktree"),
    (r"kanban create", (r"\bcreate_card\.py\b",), "create_card.py"),
    (r"skill_lint\.py\s+--strict-skills", (r"\bmake lint-skills\b",), "make lint-skills"),
]


def scan_text(text, rules=None):
    """(行番号, コマンド, 移し先) を返す。コメント(`<!-- -->`、複数行も)は除く。"""
    rules = RULES if rules is None else rules
    found, in_comment = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if in_comment:
            in_comment = "-->" not in line
            continue
        s = line.strip()
        if s.startswith("<!--"):
            in_comment = "-->" not in s
            continue
        for cmd_re, dest_res, hint in rules:
            m = re.search(cmd_re, line)
            if m and not any(re.search(d, line) for d in dest_res):
                found.append((no, m.group(0), hint))
    return found


def md_files(root):
    return sorted(Path(root, "skills").rglob("*.md")) if Path(root, "skills").is_dir() else []


def default_skills_repo(root):
    """sf の隣の skills repo。worktree(<repo>/.worktrees/<名>)の中からでも本体の隣を見つける。"""
    root = Path(root)
    for base in (root.parent, root.parent.parent.parent if root.parent.name == ".worktrees" else None):
        if base and (base / "skills" / "skills").is_dir():
            return base / "skills"
    return root.parent / "skills"


def collect(targets):
    """targets: [(ラベル, repo のルート)]。(相対パス, 行, コマンド, 移し先) の一覧。"""
    hits = []
    for label, root in targets:
        for p in md_files(root):
            for no, cmd, hint in scan_text(p.read_text(encoding="utf-8")):
                hits.append((f"{label}/{p.relative_to(root).as_posix()}", no, cmd, hint))
    return hits


def counts(hits):
    c = {}
    for path, _no, cmd, _h in hits:
        c[f"{path}::{cmd}"] = c.get(f"{path}::{cmd}", 0) + 1
    return c


def main(argv=None):
    ap = argparse.ArgumentParser(description="スキルに移し先のある手順のコマンドが直に書かれていないかを検査する")
    ap.add_argument("--root", default=str(REPO), help="sf repo のルート(既定: この repo。テスト用)")
    ap.add_argument("--skills-repo", default=None, help="skills repo の場所(既定: sf の隣の skills)")
    ap.add_argument("--baseline", default=None, help="既知の違反の一覧(JSON)。載っているものは許し、新規・悪化だけ落とす")
    ap.add_argument("--write-baseline", default=None, metavar="FILE", help="今の違反をベースラインとして書き出して終了")
    args = ap.parse_args(argv)

    sf = Path(args.root)
    sr = Path(args.skills_repo).expanduser() if args.skills_repo else default_skills_repo(sf)
    targets = [("sf", sf)]
    if (sr / "skills").is_dir():
        targets.append(("skills", sr))
    else:
        print(f"lint-procedures: skills repo が無いので飛ばした: {sr}")
    hits = collect(targets)
    now = counts(hits)

    if args.write_baseline:
        Path(args.write_baseline).write_text(
            json.dumps({"violations": dict(sorted(now.items()))}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"lint-procedures: {len(hits)} 件をベースラインに書いた: {args.write_baseline}")
        return 0

    known = json.loads(Path(args.baseline).read_text(encoding="utf-8"))["violations"] if args.baseline else {}
    excess = {k: n - known.get(k, 0) for k, n in now.items() if n > known.get(k, 0)}  # 新規・悪化の件数
    bad_keys = set(excess)
    shown = [h for h in hits if f"{h[0]}::{h[2]}" in bad_keys]
    for path, no, cmd, hint in shown:
        print(f"FAIL {path}:{no}: {cmd} → {hint}")
    files = len({h[0] for h in hits})
    print(f"lint-procedures: {len(hits)} 件 / {files} ファイル")
    if args.baseline:
        print(f"lint-procedures: 既知 {len(hits) - sum(excess.values())} 件を許容、新規・悪化 {sum(excess.values())} 件")
        scanned = {t[0] for t in targets}
        for k in sorted(known):
            # 走査しなかった repo(skills repo 無し)のぶんは、解消とは言えない
            if k.split("/", 1)[0] in scanned and now.get(k, 0) < known[k]:
                print(f"  解消: {k} （ベースラインから書き直す: make lint-procedures-baseline）")
    return 1 if shown else 0


if __name__ == "__main__":
    sys.exit(main())
