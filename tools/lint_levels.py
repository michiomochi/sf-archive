#!/usr/bin/env python3
"""scripts/levels.toml(レベルごとの値の唯一の置き場所)と、実装(コード・雛形)のずれを検査する。ずれたら落ちる(終了コード 1)。

  1. 行が工程を抜けも重なりも無くつなぐ: 各レベルの rows の工程を順につなぐと phases と同じ(`L3: 実装 がどの行にも無い`・`設計 が複数の行にある`・並びの違い)
  2. 行の題が epic_common.row_phases で読める(`設計-リリース判定` は読めない)
  3. 判定書の雛形(scripts/templates/<judgement_template>)の H1 が judgement_chapters と同じ。コード(epic_common.judgement_chapters)は levels.toml を読む(selftest が確かめる)。
     build_release_ask が名前で読む章(epic_common.JUDGEMENT_COPIED)は、どのレベルの judgement_chapters にも入っている
  4. レビュー行の雛形(templates/gate_ask.md)の H1 を、各レベルの review_chapters が並びを変えずに選んでいる(同じでも、少なくてもよい。ask_human は
     epic_common.render_review でその章だけを作る)。雛形の章はどれかのレベルが使う
  5. 成果物のリンクをカードへ渡す道(templates/phase_card_range_note.md の ${links})がある。名前そのものは雛形に書き写さない(skill_lint の検査7)
  5b. 行のチェックリストの項目(items)の頭が `<工程名>：` で、そのレベルの工程の順に並び、どの工程にも1つ以上あり、どの行にも1つ以上入る(ゲート制御が行を作るときに書く)
  6. L1 は工程行を作らない: levels.toml で rows=[]・phases=[]・判定書なし。コード(ゲート制御・プランナー)が makes_phase_rows を呼んでレベルで決める。
     create_row の L1 のエピックは 新規作成の停止 を立てない(旗に頼らない)
  7. 配布: levels.toml が safe_deploy.sh の DEFAULT_FILES にあり、Makefile の stage-run が staging に写す(無いと本番の各スクリプトが import で止まる)
  8. レビューの回数 max_reviews: L1 は 1 回、L2・L3 は 3 回(EXPECTED_MAX_REVIEWS。ゲート制御が上限を超えた request_review を block する値。違えば `<レベル>: max_reviews が …`)

usage: python3 tools/lint_levels.py     ($SF_LEVELS で levels.toml を差し替えられる。設定は $SF_CONFIG、無ければ tests/config.test.toml)
Exit 0 = 0 件、1 = 件あり(または levels.toml が読めない)。
"""
import ast
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("SF_CONFIG", str(REPO / "tests" / "config.test.toml"))  # epic_common は環境の設定を読む。この検査は値を使わない
sys.path.insert(0, str(REPO / "scripts"))
import sfconfig  # noqa: E402

TEMPLATES = REPO / "scripts" / "templates"
CODE_READS_THE_LEVEL = ("epic_gate_controller.py", "epic_planner_status.py")  # L1 のエピックに工程行・カードを作らない判断をする場所
EXPECTED_MAX_REVIEWS = {"L1": 1, "L2": 3, "L3": 3}  # 決めた方針(差し戻しは3回まで。L1 は1回で終える作業)。変えるときは levels.toml・この表・テストを一緒に直す
H1 = re.compile(r"^# (.+?)(?: \{[^}]*\})?[ \t]*$", re.M)


def h1_of(path):
    return H1.findall(path.read_text(encoding="utf-8"))


def _calls(path, name):
    """True when the file calls `name` (as `x.name(...)` or `name(...)`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, ast.Call) and (getattr(n.func, "attr", None) == name or getattr(n.func, "id", None) == name) for n in ast.walk(tree))


def _l1_epic_holds_with_the_flag(path):
    """True when create_row.l1_epic builds its spec with `"stop_new": True`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "l1_epic"):
        for d in (n for n in ast.walk(fn) if isinstance(n, ast.Dict)):
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and k.value == "stop_new" and isinstance(v, ast.Constant) and v.value is True:
                    return True
    return False


def rows_problems(lv, node, C):
    out = []
    rows, phases = node["rows"], node["phases"]
    if [p for p in phases if p not in C.ORDER] or phases != [p for p in C.ORDER if p in phases]:
        out.append(f"{lv}: phases {phases} は epic_common.ORDER {list(C.ORDER)} の部分列（順どおり）ではない")
    covered = []
    for title in rows:
        ph = C.row_phases(title)
        if ph is None:
            out.append(f"{lv}: row_phases が読めない行の題 '{title}'（工程名か `<最初>〜<最後>` の範囲にする）")
        else:
            covered += list(ph)
    if any(C.row_phases(t) is None for t in rows):
        return out  # 読めない題があると、つながりは言えない
    for p in C.ORDER:
        n = covered.count(p)
        if p in phases and n == 0:
            out.append(f"{lv}: {p} がどの行にも無い")
        elif n > 1:
            out.append(f"{lv}: {p} が複数の行にある")
        elif p not in phases and n == 1:
            out.append(f"{lv}: {p} は phases に無いのに行にある")
    if not any(o.startswith(f"{lv}: ") and ("がどの行にも無い" in o or "複数の行" in o or "phases に無い" in o) for o in out):
        if covered != phases:
            out.append(f"{lv}: 行の並びが工程の順と違う（行をつなぐと {covered}、phases は {phases}）")
    return out


def items_problems(lv, node, C):
    """items(ゲート制御が行に書くチェックリスト)が、頭の工程・順・行への入り方で破れていないか。"""
    out, heads = [], []
    for t in node["items"]:
        m = C._HEAD_RE.match(t)
        if not m or not m.group(2).strip():
            out.append(f"{lv}: 項目 '{t[:30]}' の頭が `<工程名>：` ではない（または中身が空）")
        elif m.group(1) not in node["phases"]:
            out.append(f"{lv}: 項目の頭 {m.group(1)} は、このレベルの工程 {node['phases']} に無い")
        else:
            heads.append(m.group(1))
    if heads != sorted(heads, key=C.ORDER.index):
        out.append(f"{lv}: items が工程の順に並んでいない（頭の並び {heads}）")
    for p in node["phases"]:
        if p not in heads:
            out.append(f"{lv}: 工程 {p} の項目が items に無い（その工程のカードに項目が入らない）")
    if not node["rows"] and node["items"]:
        out.append(f"{lv}: 行が無いのに items がある")
    for title in node["rows"]:
        if C.row_phases(title) is not None and not [t for t in node["items"] if C.item_head(t) in C.row_phases(title)]:
            out.append(f"{lv}: 行 '{title}' に入る項目が items に無い（項目の無い行はゲート制御がキャンセルする）")
    return out


def is_subsequence(want, got):
    it = iter(got)
    return all(n in it for n in want)


def chapters_problem(lv, what, template, want, subset=False):
    """雛形の H1 と config の章のずれ。subset=True は、config が雛形の章を並びを変えずに選んでいればよい(少なくてもよい)。"""
    out = []
    path = TEMPLATES / template
    if not path.is_file():
        return [f"{lv}: {what}の雛形 templates/{template} が無い"]
    got = h1_of(path)
    if subset and not is_subsequence(list(want), got):
        out.append(f"{lv}: {what}の章が雛形の章を並びどおりに選んでいない（templates/{template}: {got}、config: {list(want)}）")
    elif not subset and got != list(want):
        out.append(f"{lv}: {what}の雛形の章が config と違う（templates/{template}: {got}、config: {list(want)}）")
    return out


def check(levels, scripts=None, safe_deploy=None, makefile=None):
    """levels.toml の内容(dict)と実装のずれ -> 問題の文の一覧。scripts / safe_deploy / makefile: 読む場所(テストで差し替える)。"""
    import epic_common as C
    scripts = Path(scripts) if scripts else REPO / "scripts"
    out = []
    for lv in sfconfig.LEVEL_NAMES:
        node = levels[lv]
        out += rows_problems(lv, node, C)
        out += items_problems(lv, node, C)
        if node["judgement_template"]:
            out += chapters_problem(lv, "判定書", node["judgement_template"], node["judgement_chapters"])
            out += [f"{lv}: 判定書の章に {n} が無い（build_release_ask が名前で読む章。epic_common.JUDGEMENT_COPIED）"
                    for n in C.JUDGEMENT_COPIED if n not in node["judgement_chapters"]]
        if node["review_chapters"]:
            out += chapters_problem(lv, "レビュー行", "gate_ask.md", node["review_chapters"], subset=True)
        # 成果物の名前は phase_card.md に書き写さない(skill_lint 検査7)。カードへはゲート制御が範囲の行の末尾の ${links} で渡す。
        if node["deliverables"] and "${links}" not in (TEMPLATES / "phase_card_range_note.md").read_text(encoding="utf-8"):
            out.append(f"{lv}: 成果物のリンク ${{links}} が templates/phase_card_range_note.md に無い（カードへ成果物の名前を渡す道）")
        if len(set(node["deliverables"])) != len(node["deliverables"]):
            out.append(f"{lv}: 成果物の名前が重なっている")
        if node["max_reviews"] != EXPECTED_MAX_REVIEWS[lv]:
            out.append(f"{lv}: max_reviews が {node['max_reviews']}（L1 は {EXPECTED_MAX_REVIEWS['L1']} 回、L2・L3 は {EXPECTED_MAX_REVIEWS['L2']} 回）")
    used = {n for lv in sfconfig.LEVEL_NAMES for n in levels[lv]["review_chapters"]}
    out += [f"レビュー行の雛形の章 {n} を、どのレベルの review_chapters も使っていない（templates/gate_ask.md から消すか、レベルに入れる）"
            for n in h1_of(TEMPLATES / "gate_ask.md") if n not in used]
    l1 = levels["L1"]
    if l1["rows"] or l1["phases"]:
        out.append("L1: 行は 0 のはず（L1 は工程行を作らない）")
    if l1["judgement_template"] or l1["judgement_chapters"]:
        out.append("L1: 判定書は無いはず（工程行が無いのでリリース判定も無い）")
    for name in CODE_READS_THE_LEVEL:
        if not _calls(scripts / name, "makes_phase_rows"):
            out.append(f"L1: {name} が makes_phase_rows を呼んでいない（L1 は工程行を作らないと levels.toml を見て決めていない）")
    if _l1_epic_holds_with_the_flag(scripts / "create_row.py"):
        out.append("L1: create_row.l1_epic が 新規作成の停止 を立てている（旗に頼らず、レベルで止める）")
    m = re.search(r'^DEFAULT_FILES="([^"]*)"', Path(safe_deploy or REPO / "tools" / "safe_deploy.sh").read_text(encoding="utf-8"), re.M)
    if not m or "levels.toml" not in m.group(1).split():
        out.append("levels.toml が tools/safe_deploy.sh の DEFAULT_FILES に無い（本番の各スクリプトが import で止まる）")
    if "cp scripts/levels.toml $(HERMES_SCRIPTS)/staging/levels.toml" not in Path(makefile or REPO / "Makefile").read_text(encoding="utf-8"):
        out.append("Makefile の stage-run が scripts/levels.toml を staging に写していない")
    return out


def main(argv=None):
    try:
        levels = sfconfig.load_levels()
    except sfconfig.ConfigError as e:
        print(f"lint_levels: {e}")
        return 1
    problems = check(levels)
    for p in problems:
        print(p)
    print(f"lint_levels: {len(problems)} 件")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
