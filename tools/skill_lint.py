#!/usr/bin/env python3
"""スキルと指示文の規約を機械で検査する（決定論）。

なぜ要るか: スキルは「AI が気を付ける」種類の指示なので、書いても守られない。
サイズ・参照切れ・重複は機械で判定できるので、ここで落とす。
（根拠: Anthropic「Agent Skills」の 3 段階の progressive disclosure と context 予算、
 Claude Code「Best practices」の deterministic / non-deterministic の区別、
 Lauren Tan のルール集の「同じ失敗は仕組みで直す・延長して重複を増やさない」）

検査:
  1. SKILL.md のサイズ（既定 6,000 字。超えたら references へ移す）
  2. frontmatter: name（ディレクトリ名と一致）・description（120 字以下・"Use when" で始まる）
  3. references/*.md の参照切れ・孤立（SKILL.md から辿れないファイル）
  4. references のサイズ（既定 12,000 字）
  5. 規則の重複（80 字以上の同一行が別ファイルにもある）
  6. カード本文の雛形サイズ（既定 3,000 字）と golden の一致数
  7. 設定値を文に書き写さない（下）

検査7（書き写し禁止）: 値の正本は scripts/levels.toml・scripts/templates/*.md・Makefile にあり、文（skills/**・prompts/*.txt・
  hermes/profiles/**・scripts/templates/*.md）はそれを数や文字列で直書きせず、render の `${名前}` で受け取るか、参照先を書く。
  検出する書き写し（種類 → 正本）:
    rows          行の題 `要件定義〜リリース判定`・`実装〜リリース判定`（工程名〜工程名。区切りは 〜／～／~）  → levels.toml の rows（`${l2_rows}`）
    chapters      章の数 `9章`・`nine chapters`（`第3章` は除く）                                       → levels.toml の review_chapters / judgement_chapters
    deliverables  成果物の数 `3枚`・`THREE`（成果物・子ページの文脈の行）、成果物の名前を3つ以上並べた行 → levels.toml の deliverables（`${deliverables}`）
    test          テストのコマンド `make test`・`make test-fast`・`make test-paths`（Makefile の test 系のターゲット）→ Makefile（`${verify}`）
    reviews       レビューの回数 `差し戻しは3回まで`・`3 review rounds`（レビュー・差し戻しの文脈の行。`1回目` は除く）→ levels.toml の ai_reviews・max_reviews（差し戻しの上限）
  通る形: **同じ行に** `${名前}`（小文字の識別子。`${l2_rows}`・`${review_chapters}`・`${verify}`）があるか、
    「levels.toml / cron.toml / Makefile / scripts/templates/*.md を見る・読む・参照する」と書いてある（移し先を書いている）。
  `<!-- -->` のコメントは見ない（render で消える）。cron の周期（`every Nm`・`N分毎`・ジョブ名の「（N分）」）は tools/lint_cron.py の
  持ち物で、ここでは見ない（同じ違反を2つの検査で二重に数えない）。
  ベースライン tools/skill_lint_baseline.json（キー「ファイル::種類:書かれた文字列」→件数。行番号は持たない）にある既存は許し、
  新規・件数の悪化だけ落とす。直したら `--write-baseline` で書き直す（直したあとにだけ。違反を足すために使わない）。段10で返済が済み、ベースラインは空（1件でも足せば落ちる）。

使い方:
  python3 tools/skill_lint.py                  # 既定のしきい値で検査（違反があれば exit 1）
  python3 tools/skill_lint.py --warn-only      # 違反を警告だけにして exit 0（ただしカード雛形の超過は exit 1）
  python3 tools/skill_lint.py --json
  python3 tools/skill_lint.py --no-baseline    # 検査7の既存違反も全部出す（今の件数を見る）
  python3 tools/skill_lint.py --write-baseline tools/skill_lint_baseline.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")

MAX_SKILL = 6000
BUDGET = {"autonomous-task-execution": 6000, "product-development-process": 5000}
MAX_REF = 12000
MAX_DESC = 120
MAX_CARD_TEMPLATE = 3000
CARD_TEMPLATE = os.path.join(ROOT, "scripts", "templates", "phase_card.md")
DUP_LINE = 80

# ---- 検査7: 設定値の書き写し
COPY_KINDS = ("rows", "chapters", "deliverables", "test", "reviews")
BASELINE_REL = os.path.join("tools", "skill_lint_baseline.json")
LEVELS_REL = os.path.join("scripts", "levels.toml")
# 書き写しを見る文(root からの相対)。周期の検査(lint_cron)と同じ範囲に、skills/references を足した形
COPY_GLOBS = ("skills/**/*.md", "prompts/*.txt", "hermes/profiles/**/*.md", "hermes/profiles/**/*.txt", "scripts/templates/*.md")
SEP = r"\s*[〜～~]\s*"
NUM = r"[0-9０-９]+|[一二三四五六七八九十]+"
NUM_WORD = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
PLURAL_WORD = r"two|three|four|five|six|seven|eight|nine|ten"  # one は「1つの」で、数の書き写しではない
# 移し先の名前(`${名前}`)。種類ごとに、その値の名前だけを移し先と認める（`${live}` や `${epic_ds}` が同じ行にあっても `8 章` は通さない）
PLACEHOLDER_RES = {
    "rows": re.compile(r"\$\{[a-z0-9_]*rows\}"),
    "chapters": re.compile(r"\$\{[a-z0-9_]*chapters\}"),
    "deliverables": re.compile(r"\$\{[a-z0-9_]*deliverables?\}"),
    "test": re.compile(r"\$\{(?:verify|[a-z0-9_]*test[a-z0-9_]*)\}"),
    "reviews": re.compile(r"\$\{[a-z0-9_]*review[a-z0-9_]*\}"),
}
SOURCE_RE = re.compile(r"levels\.toml|cron\.toml|Makefile|templates/[\w.\-]+\.md")
LOOK_RE = re.compile(r"見る|見て|読む|読み|参照|引く|引い|正本|\bsee\b|\bread\b|\brefer")
CHAPTER_RES = (
    re.compile(rf"(?<![第0-9０-９一二三四五六七八九十])(?:{NUM})\s*章"),
    re.compile(rf"\b(?:\d+|{NUM_WORD})[\s-]+chapters?\b", re.I),
)
DELIVERABLE_CONTEXT_RE = re.compile(r"成果物|子ページ|deliverable|child page", re.I)
DELIVERABLE_COUNT_RES = (
    re.compile(rf"(?<![0-9０-９一二三四五六七八九十])(?:{NUM})\s*枚"),
    re.compile(r"\b(?:TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)\b"),
    re.compile(rf"\b(?:\d+|{PLURAL_WORD})\s+(?:deliverables|documents|child pages)\b", re.I),
)
REVIEW_CONTEXT_RE = re.compile(r"レビュー|差し戻し|review", re.I)
REVIEW_COUNT_RES = (
    re.compile(rf"(?<![0-9０-９一二三四五六七八九十第])(?:{NUM})\s*回(?!目)"),
    re.compile(rf"\b(?:\d+|{NUM_WORD})\s+(?:review\s+)?(?:rounds?|times|passes)\b", re.I),
)
LIST_MIN = 3  # 成果物の名前をこの数以上、同じ行に並べたら書き写し
TEST_TARGETS = ("test", "test-fast", "test-paths")  # 正本は Makefile。まだ無いターゲットも名指しで見る(段4で足される)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def skill_files(root=None):
    return sorted(glob.glob(os.path.join(SKILLS if root is None else os.path.join(root, "skills"), "*", "SKILL.md")))


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check(paths, card_template=None):
    problems = []
    lines_seen = {}
    for p in paths:
        text = read(p)
        d = os.path.basename(os.path.dirname(p))
        fm = frontmatter(text)
        if not fm:
            problems.append((p, "frontmatter が無い（--- name/description --- で始める）"))
        else:
            if fm.get("name") != d:
                problems.append((p, f"name '{fm.get('name')}' がディレクトリ名 '{d}' と違う"))
            desc = fm.get("description", "")
            if not desc:
                problems.append((p, "description が無い（何をするか + いつ使うか）"))
            else:
                if len(desc) > MAX_DESC:
                    problems.append((p, f"description が {len(desc)} 字（上限 {MAX_DESC}）"))
                if not desc.lower().startswith("use when"):
                    problems.append((p, "description は 'Use when …' で始める（起動の手掛かりになる）"))
        budget = BUDGET.get(d, MAX_SKILL)
        if len(text) > budget:
            problems.append((p, f"SKILL.md が {len(text)} 字（上限 {budget}）。references へ移す"))
        # references: 参照切れ・孤立・サイズ
        refdir = os.path.join(os.path.dirname(p), "references")
        refs = sorted(glob.glob(os.path.join(refdir, "*.md")))
        for r in refs:
            name = os.path.basename(r)
            if name not in text:
                problems.append((r, f"SKILL.md から参照されていない（孤立）。'{name}' を本文で指す"))
            rt = read(r)
            if len(rt) > MAX_REF:
                problems.append((r, f"references が {len(rt)} 字（上限 {MAX_REF}）。分割する"))
        # 本文が指す references が実在するか
        # references どうしの指し方（ハイフン入りの名前）も実在を見る
        all_refs = {os.path.basename(x) for x in glob.glob(os.path.join(SKILLS, "*", "references", "*.md"))}
        for r in refs:
            for name in re.findall(r"(?<![\w/.\-])([a-z]+(?:-[a-z0-9]+)+\.md)", read(r)):
                if name not in all_refs:
                    problems.append((r, f"references が指す {name} が無い"))
        # 他スキルの参照（例: autonomous-task-execution/references/x.md）は、このスキルの参照ではない
        for m in re.finditer(r"(?<![A-Za-z0-9_\-/])references/([A-Za-z0-9_.\-]+\.md)", text):
            if not os.path.exists(os.path.join(refdir, m.group(1))):
                problems.append((p, f"参照切れ: references/{m.group(1)}"))
        # 重複した規則（80 字以上の同一行）
        for line in text.splitlines():
            s = line.strip()
            if len(s) >= DUP_LINE and not s.startswith(("#", "-", "|", "```")):
                if s in lines_seen:
                    problems.append((p, f"同一の文が {os.path.basename(os.path.dirname(lines_seen[s]))} にもある（規則の持ち主を1か所に）: {s[:50]}…"))
                else:
                    lines_seen[s] = p
    # カード本文の雛形
    tmpl = card_template or CARD_TEMPLATE
    if os.path.exists(tmpl):
        t = read(tmpl)
        if len(t) > MAX_CARD_TEMPLATE:
            problems.append((tmpl, f"カード雛形が {len(t)} 字（上限 {MAX_CARD_TEMPLATE}）"))
    return problems


# ---------------------------------------------------------------- 検査7: 設定値を文に書き写さない

def load_sources(root):
    """正本から検査に要る値を読む。(行の題の一覧, 工程名の一覧, 成果物の名前の一覧, テストのターゲットの一覧)。levels.toml が無ければ空(検査しない)。"""
    rows, phases, deliverables = {}, [], []
    path = os.path.join(root, LEVELS_REL)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            levels = tomllib.load(f)
        for lv, node in levels.items():
            if not isinstance(node, dict):
                continue
            for title in node.get("rows", []):
                rows.setdefault(re.sub(r"\s*[〜～~]\s*", "〜", title), lv)
            phases += [x for x in node.get("phases", []) if x not in phases]
            deliverables += [x for x in node.get("deliverables", []) if x not in deliverables]
    targets = list(TEST_TARGETS)
    mk = os.path.join(root, "Makefile")
    if os.path.isfile(mk):
        for m in re.finditer(r"^(test[\w-]*)\s*:", read(mk), re.M):
            if m.group(1) not in targets:
                targets.append(m.group(1))
    return rows, phases, deliverables, targets


def norm(token):
    """書かれた文字列を、ベースラインのキーにする形にする(空白をつめる。英語は1つの空白、小文字)。"""
    t = " ".join(token.split()).lower()
    return re.sub(r"\s+(?=[章枚回])", "", t)


def is_pointer(line, kind):
    """同じ行に移し先がある: その値の名前の `${名前}`、または 正本のファイル名 + 見る・読む・参照する。"""
    return bool(PLACEHOLDER_RES[kind].search(line)) or bool(SOURCE_RE.search(line) and LOOK_RE.search(line))


def scan_copies(text, rows, phases, deliverables, targets):
    """[(行番号, 種類, 書かれた文字列, 案内)]。移し先のある行・`<!-- -->` のコメントは除く。"""
    range_re = None
    if len(phases) >= 2:
        alt = "|".join(re.escape(x) for x in sorted(phases, key=len, reverse=True))
        range_re = re.compile(rf"(?:{alt}){SEP}(?:{alt})")
    test_re = re.compile(r"\bmake\s+(?:" + "|".join(re.escape(x) for x in sorted(targets, key=len, reverse=True)) + r")(?![\w-])")
    found, in_comment = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if in_comment:
            in_comment = "-->" not in line
            continue
        if line.strip().startswith("<!--"):
            in_comment = "-->" not in line.strip()
            continue
        if range_re and not is_pointer(line, "rows"):
            for m in range_re.finditer(line):
                tok = re.sub(r"\s+", "", m.group(0))
                lv = rows.get(re.sub(r"[～~]", "〜", tok))
                hint = (f"levels.toml の [{lv}] rows（行の題）を書き写している。${{{lv.lower()}_rows}} を使う" if lv else
                        "工程の範囲は行の題の形。levels.toml の rows にある題は ${l2_rows} / ${l3_rows} で受け取る")
                found.append((no, "rows", tok, hint + "か、同じ行に「levels.toml を見る」と書く"))
        if not is_pointer(line, "chapters"):
            for rx in CHAPTER_RES:
                for m in rx.finditer(line):
                    found.append((no, "chapters", norm(m.group(0)),
                                  "levels.toml の review_chapters / judgement_chapters（章の数）を書き写している。数は書かず ${review_chapters} / ${judgement_chapters} を使うか、同じ行に「levels.toml を見る」と書く"))
        if not is_pointer(line, "deliverables"):
            if DELIVERABLE_CONTEXT_RE.search(line):
                for rx in DELIVERABLE_COUNT_RES:
                    for m in rx.finditer(line):
                        found.append((no, "deliverables", norm(m.group(0)),
                                      "levels.toml の deliverables（成果物の数）を書き写している。数は書かず ${deliverables} を使うか、同じ行に「levels.toml を見る」と書く"))
            names = [d for d in deliverables if d in line]
            if len(names) >= LIST_MIN:
                found.append((no, "deliverables", "成果物名の列挙",
                              f"levels.toml の deliverables（{'、'.join(names[:LIST_MIN])}…）を書き写している。${{deliverables}} を使うか、同じ行に「levels.toml を見る」と書く"))
        if not is_pointer(line, "test"):
            for m in test_re.finditer(line):
                found.append((no, "test", norm(m.group(0)),
                              "Makefile のテストのコマンドを書き写している。${verify} を使うか、同じ行に「Makefile を見る」と書く"))
        if not is_pointer(line, "reviews"):
            for sentence in re.split(r"。|(?<=[.!?])\s+", line):  # 回数は、レビューの語と同じ文にあるときだけ
                if REVIEW_CONTEXT_RE.search(sentence):
                    for rx in REVIEW_COUNT_RES:
                        for m in rx.finditer(sentence):
                            found.append((no, "reviews", norm(m.group(0)),
                                          "levels.toml の ai_reviews・max_reviews（レビューの回数・差し戻しの上限）を書き写している。回数は書かず、同じ行に「levels.toml を見る」と書く"))
    return found


def copy_files(root):
    seen, out = set(), []
    for g in COPY_GLOBS:
        for p in sorted(glob.glob(os.path.join(root, g), recursive=True)):
            if os.path.isfile(p) and p not in seen and "__pycache__" not in p:
                seen.add(p)
                out.append(p)
    return out


def check_copies(root):
    """[(path, key, 文)]。key は「相対パス::種類:書かれた文字列」(ベースラインの単位)。"""
    rows, phases, deliverables, targets = load_sources(root)
    if not os.path.isfile(os.path.join(root, LEVELS_REL)):
        return []
    out = []
    for p in copy_files(root):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        for no, kind, tok, hint in scan_copies(read(p), rows, phases, deliverables, targets):
            out.append((p, f"{rel}::{kind}:{tok}", f"{rel}:{no}: {tok} → {hint}"))
    return out


def copy_counts(items):
    c = {}
    for _p, key, _m in items:
        c[key] = c.get(key, 0) + 1
    return dict(sorted(c.items()))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--warn-only", action="store_true")
    ap.add_argument("--strict-skills", action="store_true",
                    help="スキル（skills/**）の違反で exit 1。カード雛形の超過は、このフラグの有無・--warn-only の有無に関わらず exit 1")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-skill", type=int, default=None)
    ap.add_argument("--root", default=None, help="sf repo のルート(既定: この repo。テスト用)")
    ap.add_argument("--baseline", default=None, metavar="FILE",
                    help=f"検査7 の既知の違反(JSON)。既定は <root>/{BASELINE_REL}（あれば）。載っているものは許し、新規・悪化だけ落とす")
    ap.add_argument("--no-baseline", action="store_true", help="検査7 の既存違反も全部出す（件数を見る）")
    ap.add_argument("--write-baseline", default=None, metavar="FILE",
                    help="今の検査7 の違反をベースラインとして書き出して終了（直したあとにだけ。違反を足すために使わない）")
    args = ap.parse_args(argv)
    global MAX_SKILL
    if args.max_skill:
        MAX_SKILL = args.max_skill
    root = ROOT if args.root is None else os.path.abspath(args.root)
    card = CARD_TEMPLATE if args.root is None else os.path.join(root, "scripts", "templates", "phase_card.md")

    copies = check_copies(root)
    now = copy_counts(copies)
    if args.write_baseline:
        with open(args.write_baseline, "w", encoding="utf-8") as f:
            f.write(json.dumps({"violations": now}, ensure_ascii=False, indent=1) + "\n")
        print(f"skill-lint: 検査7 {len(copies)} 件をベースラインに書いた: {args.write_baseline}")
        return 0
    base_path = args.baseline or os.path.join(root, BASELINE_REL)
    known = {}
    if not args.no_baseline and os.path.isfile(base_path):
        known = json.loads(read(base_path))["violations"]
    excess = {k: n - known.get(k, 0) for k, n in now.items() if n > known.get(k, 0)}  # 新規・悪化の件数
    copy_problems = [(p, m) for p, k, m in copies if k in excess]

    paths = skill_files(root)
    problems = check(paths, card) + copy_problems
    if args.json:
        print(json.dumps({"skills": paths, "problems": [{"file": p, "problem": m} for p, m in problems],
                          "copies": {"total": len(copies), "known": len(copies) - sum(excess.values()), "new": sum(excess.values())}},
                         ensure_ascii=False, indent=1))
    else:
        print(f"skill-lint: {len(paths)} 本の SKILL.md を検査")
        for p, m in problems:
            print(f"  {'WARN' if args.warn_only else 'FAIL'} {m if (p, m) in copy_problems else os.path.relpath(p, root) + ': ' + m}")
        print(f"skill-lint: {len(problems)} 件")
        print(f"skill-lint: 検査7（書き写し）{len(copies)} 件、既知 {len(copies) - sum(excess.values())} 件を許容、新規・悪化 {sum(excess.values())} 件")
        for k in sorted(known):
            if now.get(k, 0) < known[k]:
                print(f"  解消: {k} （ベースラインから書き直す: python3 tools/skill_lint.py --write-baseline {BASELINE_REL}）")
    # カード雛形の超過は、どの流し方でも落とす(--warn-only でも、--strict-skills でも)。
    # 雛形は毎ターン再送される前置きで、静かに太ると今日削った分が戻る。
    tmpl_problems = [q for q in problems if os.path.abspath(q[0]) == os.path.abspath(card)]
    for q in tmpl_problems:
        if args.warn_only or args.strict_skills:
            print(f"  STRICT {os.path.relpath(q[0], root)}: {q[1]}")
    if tmpl_problems:
        return 1
    if problems and not args.warn_only:
        if args.strict_skills:
            # 検査7 の新規・悪化は、prompts・SOUL・雛形の文でも落とす(スキルだけに絞らない)
            hard = [q for q in problems if (os.sep + "skills" + os.sep) in q[0] or q[0].endswith("SKILL.md")
                    or (os.sep + "references" + os.sep) in q[0] or q in copy_problems]
            if hard:
                for q in hard:
                    print(f"  STRICT {q[1] if q in copy_problems else os.path.relpath(q[0], root) + ': ' + q[1]}")
                return 1
            return 0
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
