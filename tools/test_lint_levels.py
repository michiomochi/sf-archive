#!/usr/bin/env python3
"""tools/lint_levels.py の再現テスト(オフライン。一時ディレクトリだけに書く)。`make test` から実行する。

わざと壊した levels.toml・雛形・コードで、決めた文言で落ちることを確かめる(壊れた入力で落ちない検査は、緑でも何も保証しない)。
  - 実物は 0 件
  - L3 の行から 実装 を抜く → `L3: 実装 がどの行にも無い`／重なる → `L3: 設計 が複数の行にある`／順が違う／題が `設計-リリース判定` → `row_phases が読めない行の題`
  - L1 の行を1行にする → `L1: 行は 0 のはず`
  - コードが L1 を見ない(ゲート制御・プランナー・create_row のどれかが makes_phase_rows を呼ばない／L1 のエピックを止める旗に頼る)→ 落ちる
  - 判定書の雛形の H1 が config と違う(L2 / L3)→ `判定書の雛形の章が config と違う`。成果物のリンクを渡す道（range_note の ${links}）が無い → 落ちる
  - レビュー行の章: レベルが雛形より少なく選ぶのは通る／雛形に無い章・並びの違い・どのレベルも使わない雛形の章 → 落ちる。判定書に build_release_ask が読む章が無い → 落ちる
  - レビューの回数 max_reviews: L1 は 1、L2・L3 は 3(違えば `<レベル>: max_reviews が …`)
  - コマンドとして: 壊れたファイルは終了コード 1
"""
import copy
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
os.environ.setdefault("SF_CONFIG", str(REPO / "tests" / "config.test.toml"))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO / "scripts"))
import lint_levels  # noqa: E402


def real():
    with open(REPO / "scripts" / "levels.toml", "rb") as f:
        return tomllib.load(f)


def dump(levels):
    out = []
    for lv, node in levels.items():
        out.append(f"[{lv}]")
        for k, v in node.items():
            out.append(f"{k} = " + (str(v) if isinstance(v, int) else "[" + ", ".join(f'"{x}"' for x in v) + "]" if isinstance(v, list) else f'"{v}"'))
    return "\n".join(out) + "\n"


class Lint(unittest.TestCase):
    def problems(self, levels, **kw):
        return lint_levels.check(levels, **kw)

    def test_the_real_files_have_no_problem(self):
        self.assertEqual(self.problems(real()), [])

    def test_l3_without_the_row_that_holds_実装_fails(self):
        lv = real()
        lv["L3"]["rows"] = ["要件定義〜設計", "テスト〜リリース判定"]
        self.assertIn("L3: 実装 がどの行にも無い", self.problems(lv))

    def test_l3_rows_that_overlap_fail(self):
        lv = real()
        lv["L3"]["rows"] = ["要件定義〜設計", "設計〜リリース判定"]
        self.assertIn("L3: 設計 が複数の行にある", self.problems(lv))

    def test_items_without_a_phase_head_fail(self):
        lv = real()
        lv["L2"]["items"] = lv["L2"]["items"] + ["頭の無い項目"]
        self.assertTrue(any("頭が `<工程名>：` ではない" in p for p in self.problems(lv)))

    def test_an_item_head_the_level_does_not_work_fails(self):
        lv = real()
        lv["L2"]["phases"] = lv["L2"]["phases"][1:]
        lv["L2"]["rows"] = ["設計〜リリース判定"]
        self.assertTrue(any("項目の頭 要件定義 は、このレベルの工程" in p for p in self.problems(lv)))

    def test_a_phase_without_an_item_fails(self):
        lv = real()
        lv["L3"]["items"] = [t for t in lv["L3"]["items"] if not t.startswith("テスト：")]
        got = self.problems(lv)
        self.assertIn("L3: 工程 テスト の項目が items に無い（その工程のカードに項目が入らない）", got)

    def test_a_row_without_an_item_fails(self):
        lv = real()
        lv["L3"]["items"] = [t for t in lv["L3"]["items"] if not t.startswith(("要件定義：", "設計："))]
        self.assertTrue(any("行 '要件定義〜設計' に入る項目が items に無い" in p for p in self.problems(lv)))

    def test_items_out_of_phase_order_fail(self):
        lv = real()
        lv["L2"]["items"] = list(reversed(lv["L2"]["items"]))
        self.assertTrue(any("items が工程の順に並んでいない" in p for p in self.problems(lv)))

    def test_l1_with_items_fails(self):
        lv = real()
        lv["L1"]["items"] = ["設計：x"]
        self.assertIn("L1: 行が無いのに items がある", self.problems(lv))

    def test_l3_rows_in_the_wrong_order_fail(self):
        lv = real()
        lv["L3"]["rows"] = ["実装〜リリース判定", "要件定義〜設計"]
        self.assertTrue(any(p.startswith("L3: 行の並びが工程の順と違う") for p in self.problems(lv)), self.problems(lv))

    def test_a_row_that_names_a_phase_the_level_does_not_pass_fails(self):
        lv = real()
        lv["L2"]["phases"] = ["設計", "実装", "テスト", "リリース判定"]
        self.assertIn("L2: 要件定義 は phases に無いのに行にある", self.problems(lv))

    def test_a_title_row_phases_cannot_read_fails(self):
        for bad in ("設計-リリース判定", "リリース判定〜設計", "作業"):
            lv = real()
            lv["L2"]["rows"] = [bad]
            self.assertIn(f"L2: row_phases が読めない行の題 '{bad}'（工程名か `<最初>〜<最後>` の範囲にする）", self.problems(lv))

    def test_the_real_file_allows_one_review_for_l1_and_three_for_l2_and_l3(self):
        lv = real()
        self.assertEqual([lv[n]["max_reviews"] for n in ("L1", "L2", "L3")], [1, 3, 3])

    def test_a_max_reviews_other_than_one_for_l1_and_three_for_l2_and_l3_fails(self):
        for lv_name, wrong in (("L1", 2), ("L2", 2), ("L2", 4), ("L3", 0), ("L3", 4)):
            lv = real()
            lv[lv_name]["max_reviews"] = wrong
            got = self.problems(lv)
            self.assertTrue(any(p.startswith(f"{lv_name}: max_reviews が {wrong}") for p in got), (lv_name, wrong, got))

    def test_l1_with_a_row_fails(self):
        lv = real()
        lv["L1"]["rows"] = ["実装"]
        lv["L1"]["phases"] = ["実装"]
        self.assertIn("L1: 行は 0 のはず（L1 は工程行を作らない）", self.problems(lv))

    def test_l1_must_not_have_a_judgement_page(self):
        lv = real()
        lv["L1"]["judgement_template"] = "release_judgement.md"
        lv["L1"]["judgement_chapters"] = list(lv["L2"]["judgement_chapters"])
        self.assertIn("L1: 判定書は無いはず（工程行が無いのでリリース判定も無い）", self.problems(lv))

    def test_judgement_chapters_that_differ_from_the_template_fail(self):
        for lvl in ("L2", "L3"):
            lv = real()
            lv[lvl]["judgement_chapters"] = lv[lvl]["judgement_chapters"][:5]
            self.assertTrue(any(p.startswith(f"{lvl}: 判定書の雛形の章が config と違う") for p in self.problems(lv)), (lvl, self.problems(lv)))

    def test_a_judgement_template_that_is_not_there_fails(self):
        lv = real()
        lv["L2"]["judgement_template"] = "release_judgement_none.md"
        self.assertIn("L2: 判定書の雛形 templates/release_judgement_none.md が無い", self.problems(lv))

    def test_a_level_may_ask_for_fewer_review_chapters_than_the_template_in_the_templates_order(self):
        lv = real()
        self.assertEqual(self.problems(lv), [])  # levels.toml as it is: L2 asks for six of the nine
        self.assertLess(len(lv["L2"]["review_chapters"]), len(lv["L3"]["review_chapters"]))
        lv["L2"]["review_chapters"] = [lv["L3"]["review_chapters"][i] for i in (0, 1, 2, 6, 7, 8)]
        self.assertEqual(self.problems(lv), [])

    def test_review_chapters_that_the_template_does_not_have_or_in_another_order_fail(self):
        for bad in (lambda c: c[:6] + ["章がない"] + c[6:], lambda c: [c[1], c[0]] + c[2:]):
            lv = real()
            lv["L3"]["review_chapters"] = bad(lv["L3"]["review_chapters"])
            ps = self.problems(lv)
            self.assertTrue(any(p.startswith("L3: レビュー行の章が雛形の章を並びどおりに選んでいない") for p in ps), ps)

    def test_a_template_chapter_no_level_asks_for_fails(self):
        lv = real()
        for lvl in ("L2", "L3"):
            lv[lvl]["review_chapters"] = lv[lvl]["review_chapters"][:-1]
        self.assertIn("レビュー行の雛形の章 反映の記録 を、どのレベルの review_chapters も使っていない（templates/gate_ask.md から消すか、レベルに入れる）", self.problems(lv))

    def test_a_judgement_without_a_chapter_the_row_is_copied_from_fails(self):
        lv = real()
        lv["L2"]["judgement_chapters"] = [c for c in lv["L2"]["judgement_chapters"] if c != "未確認"]
        self.assertIn("L2: 判定書の章に 未確認 が無い（build_release_ask が名前で読む章。epic_common.JUDGEMENT_COPIED）", self.problems(lv))

    def test_the_card_gets_the_deliverable_names_from_the_links_not_from_a_copy_in_the_template(self):
        """段10: 成果物の名前は phase_card.md に並べて書かない(書き写しは skill_lint の検査7が落とす)。カードへはゲート制御が範囲の行の末尾
        (phase_card_range_note.md の ${links})で渡す。その道が無いと、カードは成果物の名前を知らない。"""
        import shutil
        import unittest.mock
        lv = real()
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for f in (REPO / "scripts" / "templates").glob("*.md"):
                shutil.copy(f, t / f.name)
            note = (t / "phase_card_range_note.md").read_text(encoding="utf-8")
            (t / "phase_card_range_note.md").write_text(note.replace("${links}", ""), encoding="utf-8")
            with unittest.mock.patch.object(lint_levels, "TEMPLATES", t):
                self.assertIn("L2: 成果物のリンク ${links} が templates/phase_card_range_note.md に無い（カードへ成果物の名前を渡す道）", self.problems(lv))

    def test_code_that_ignores_the_level_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp)
            for name in ("epic_gate_controller.py", "epic_planner_status.py", "create_row.py"):
                (scripts / name).write_text((REPO / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(self.problems(real(), scripts=scripts), [])
            ctl = scripts / "epic_gate_controller.py"
            ctl.write_text(ctl.read_text(encoding="utf-8").replace("makes_phase_rows", "always_true"), encoding="utf-8")
            self.assertIn("L1: epic_gate_controller.py が makes_phase_rows を呼んでいない（L1 は工程行を作らないと levels.toml を見て決めていない）", self.problems(real(), scripts=scripts))
            pl = scripts / "epic_planner_status.py"
            pl.write_text(pl.read_text(encoding="utf-8").replace("C.makes_phase_rows(level)", "True"), encoding="utf-8")
            self.assertTrue(any("epic_planner_status.py が makes_phase_rows を呼んでいない" in p for p in self.problems(real(), scripts=scripts)))
            cr = scripts / "create_row.py"
            cr.write_text(cr.read_text(encoding="utf-8").replace('"level": "L1", "stop_new": False', '"level": "L1", "stop_new": True'), encoding="utf-8")
            self.assertIn("L1: create_row.l1_epic が 新規作成の停止 を立てている（旗に頼らず、レベルで止める）", self.problems(real(), scripts=scripts))


    def test_levels_toml_that_is_not_deployed_fails(self):
        # every script stops at import without levels.toml: a deploy that leaves it out would stop the whole pipeline
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp) / "safe_deploy.sh"
            text = (REPO / "tools" / "safe_deploy.sh").read_text(encoding="utf-8")
            sd.write_text(text, encoding="utf-8")
            self.assertEqual(self.problems(real(), safe_deploy=sd), [])
            sd.write_text(text.replace("sfconfig.py levels.toml config.toml", "sfconfig.py config.toml"), encoding="utf-8")
            self.assertIn("levels.toml が tools/safe_deploy.sh の DEFAULT_FILES に無い（本番の各スクリプトが import で止まる）", self.problems(real(), safe_deploy=sd))
            mk = Path(tmp) / "Makefile"
            mtext = (REPO / "Makefile").read_text(encoding="utf-8")
            mk.write_text(mtext, encoding="utf-8")
            self.assertEqual(self.problems(real(), makefile=mk), [])
            mk.write_text(mtext.replace("\tcp scripts/levels.toml $(HERMES_SCRIPTS)/staging/levels.toml\n", ""), encoding="utf-8")
            self.assertIn("Makefile の stage-run が scripts/levels.toml を staging に写していない", self.problems(real(), makefile=mk))


class Command(unittest.TestCase):
    def run_cli(self, text=None):
        env = dict(os.environ)
        args = [sys.executable, str(TOOLS / "lint_levels.py")]
        with tempfile.TemporaryDirectory() as tmp:
            if text is not None:
                p = Path(tmp) / "levels.toml"
                p.write_text(text, encoding="utf-8")
                env["SF_LEVELS"] = str(p)
            return subprocess.run(args, capture_output=True, text=True, env=env)

    def test_the_real_file_exits_0_and_says_0(self):
        r = self.run_cli()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "lint_levels: 0 件"), r.stdout + r.stderr)

    def test_the_l3_without_実装_exits_1_with_the_reason(self):
        lv = real()
        lv["L3"]["rows"] = ["要件定義〜設計", "テスト〜リリース判定"]
        r = self.run_cli(dump(lv))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("L3: 実装 がどの行にも無い", r.stdout)

    def test_a_file_that_cannot_be_loaded_exits_1(self):
        r = self.run_cli("[L1\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("levels.toml", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
