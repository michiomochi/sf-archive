#!/usr/bin/env python3
"""tools/skill_lint.py の検査7「設定値を文に書き写さない」の再現テスト(オフライン。一時ディレクトリだけに書く)。`make test` から実行する。

検査そのものが壊れていないことを、わざと壊した入力で確かめる(壊れた入力で落ちない検査は、緑でも何も保証しない):
  - 章の数(`9章`・`nine chapters`)・行の題(`要件定義〜リリース判定`)・テストのコマンド(`make test-fast`)・成果物の枚数(`3枚`)・
    レビューの回数(`差し戻しは3回まで`)を文に直書きすると落ちる
  - 同じ行に `${review_chapters}` のような名前、または「levels.toml / cron.toml / Makefile を見る」があれば通る
  - 値でないもの(カード「1枚」、「第3章」)は通る。cron の周期は lint_cron の持ち物で、ここでは見ない
  - ベースライン: 既存は許し、新規・件数の悪化だけ落とす。直したら「解消」と出る。--write-baseline で書ける
  - 実物の木(skills・prompts・hermes・scripts/templates・levels.toml・Makefile)を写して、実測の2つ(スキルに「9章」→落ちる／`${review_chapters}` → 通る)
  - リポジトリの実物は、置いてあるベースラインで 0 件(`--strict-skills`)
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import skill_lint  # noqa: E402

BASELINE_REL = "tools/skill_lint_baseline.json"
LEVELS = (REPO / "scripts" / "levels.toml").read_text(encoding="utf-8")
FRONT = "---\nname: demo\ndescription: Use when testing.\n---\n"
MAKEFILE = ".PHONY: test\n\ntest:\n\techo ok\n\ntest-fast:\n\techo fast\n"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = skill_lint.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TmpRoot(unittest.TestCase):
    """最小の木: skills/demo/SKILL.md・levels.toml・Makefile。root を --root で渡す。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_skill_lint_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "sf"
        (self.root / "skills" / "demo").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        (self.root / "tools").mkdir()
        (self.root / "scripts" / "levels.toml").write_text(LEVELS, encoding="utf-8")
        (self.root / "Makefile").write_text(MAKEFILE, encoding="utf-8")
        self.skill("")

    def skill(self, body):
        (self.root / "skills" / "demo" / "SKILL.md").write_text(FRONT + body, encoding="utf-8")

    def put(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def lint(self, *flags):
        return run_main(["--root", str(self.root), *flags])

    def fails(self, body, *flags):
        self.skill(body)
        rc, out, _ = self.lint(*flags)
        return rc, out

    def baseline(self, violations):
        p = self.root / BASELINE_REL
        p.write_text(json.dumps({"violations": violations}, ensure_ascii=False), encoding="utf-8")
        return str(p)


class TestCopiesFail(TmpRoot):
    def test_clean_tree_passes(self):
        rc, out = self.fails("規則は references を見る。\n", "--strict-skills")
        self.assertEqual(rc, 0, out)

    def test_chapter_count_fails_and_points_to_the_name(self):
        rc, out = self.fails("レビュー行は9章で書く。\n", "--strict-skills")
        self.assertEqual(rc, 1, out)
        self.assertIn("SKILL.md:", out)
        self.assertIn("9章", out)
        self.assertIn("${review_chapters}", out)

    def test_english_chapter_count_fails(self):
        for text in ("The ask has nine chapters.\n", "The ask has 8 chapters.\n"):
            rc, out = self.fails(text, "--strict-skills")
            self.assertEqual(rc, 1, text + out)

    def test_row_title_fails(self):
        rc, out = self.fails("L2 の行は「要件定義〜リリース判定」の1行。\n", "--strict-skills")
        self.assertEqual(rc, 1, out)
        self.assertIn("要件定義〜リリース判定", out)
        self.assertIn("${l2_rows}", out)

    def test_row_title_with_other_separator_and_spaces_fails(self):
        for t in ("実装 ~ リリース判定", "要件定義～設計"):
            rc, out = self.fails(f"行は {t} です。\n", "--strict-skills")
            self.assertEqual(rc, 1, t + out)

    def test_phase_range_not_in_levels_fails_too(self):
        # 題は工程の範囲の形。levels.toml に無い範囲でも、書けば行の題の書き写しになる
        rc, out = self.fails("設計〜テストをまとめる。\n", "--strict-skills")
        self.assertEqual(rc, 1, out)

    def test_test_command_fails(self):
        for cmd in ("make test-fast", "make test-paths", "make test"):
            rc, out = self.fails(f"変更したら `{cmd}` を回す。\n", "--strict-skills")
            self.assertEqual(rc, 1, cmd + out)
            self.assertIn(cmd, out)
            self.assertIn("${verify}", out)

    def test_other_make_targets_pass(self):
        rc, out = self.fails("`make deploy-check` と `make testing-ground` は別。\n", "--strict-skills")
        self.assertEqual(rc, 0, out)

    def test_deliverable_count_fails_only_in_deliverable_context(self):
        rc, out = self.fails("成果物は3枚を子ページに作る。\n", "--strict-skills")
        self.assertEqual(rc, 1, out)
        self.assertIn("3枚", out)
        rc, out = self.fails("Deliver THREE deliverables.\n", "--strict-skills")
        self.assertEqual(rc, 1, out)
        rc, out = self.fails("カード1枚につき1行。\n", "--strict-skills")  # カードの枚数は値ではない
        self.assertEqual(rc, 0, out)

    def test_deliverable_name_list_fails(self):
        rc, out = self.fails("前の工程の要件書、設計書、見積もりを読む。\n", "--strict-skills")
        self.assertEqual(rc, 1, out)
        self.assertIn("${deliverables}", out)
        rc, out = self.fails("設計書を読む。\n", "--strict-skills")  # 1つの名前は普通の文
        self.assertEqual(rc, 0, out)

    def test_review_rounds_fail(self):
        for text in ("差し戻しは3回まで。\n", "reviewer は2回までレビューする。\n", "At most 3 review rounds.\n"):
            rc, out = self.fails(text, "--strict-skills")
            self.assertEqual(rc, 1, text + out)

    def test_not_values(self):
        for text in ("第3章を読む。\n", "1回目の差し戻しは全部まとめて書く。\n", "ビルドは3回まで再試行する。\n", "2分毎に回る。\n"):
            rc, out = self.fails(text, "--strict-skills")
            self.assertEqual(rc, 0, text + out)  # 周期は lint_cron の持ち物


class TestPointersPass(TmpRoot):
    def test_placeholder_on_the_same_line_passes(self):
        for text in (
            "レビュー行は${review_chapters}で書く。レビュー行は9章。\n",
            "L2 の行は ${l2_rows}（要件定義〜リリース判定）。\n",
            "回す: `make test-fast`（${verify}）。\n",
        ):
            rc, out = self.fails(text, "--strict-skills")
            self.assertEqual(rc, 0, text + out)

    def test_placeholder_on_another_line_does_not_pass(self):
        rc, out = self.fails("章の数は ${review_chapters}。\nレビュー行は9章。\n", "--strict-skills")
        self.assertEqual(rc, 1, out)

    def test_pointer_to_the_source_file_passes(self):
        for text in (
            "レビュー行は9章（scripts/levels.toml を見る）。\n",
            "行の題 要件定義〜リリース判定 は levels.toml を読む。\n",
            "テストは make test。Makefile を見る。\n",
        ):
            rc, out = self.fails(text, "--strict-skills")
            self.assertEqual(rc, 0, text + out)

    def test_source_file_name_alone_is_not_a_pointer(self):
        rc, out = self.fails("9章。levels.toml\n", "--strict-skills")
        self.assertEqual(rc, 1, out)

    def test_html_comment_in_a_template_is_skipped(self):
        self.put("scripts/templates/x.md", "<!-- 9章は levels.toml の値 -->\n本文\n")
        rc, out, _ = self.lint("--strict-skills")
        self.assertEqual(rc, 0, out)


class TestScope(TmpRoot):
    def test_prompts_soul_and_templates_are_scanned(self):
        for rel in ("prompts/p.txt", "hermes/profiles/reviewer/SOUL.md", "scripts/templates/t.md"):
            self.put(rel, "承認依頼は9章。\n")
            rc, out, _ = self.lint("--strict-skills")
            self.assertEqual(rc, 1, rel + out)
            self.assertIn(rel, out)
            (self.root / rel).unlink()
        rc, out, _ = self.lint("--strict-skills")
        self.assertEqual(rc, 0, out)

    def test_references_are_scanned(self):
        self.put("skills/demo/references/a.md", "承認依頼は9章。\n")
        self.skill("references/a.md を見る\n")
        rc, out, _ = self.lint("--strict-skills")
        self.assertEqual(rc, 1, out)
        self.assertIn("references/a.md:1", out)

    def test_warn_only_does_not_fail(self):
        rc, out = self.fails("レビュー行は9章。\n", "--warn-only")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN", out)


class TestBaseline(TmpRoot):
    KEY = "skills/demo/SKILL.md::chapters:9章"

    def test_known_violation_is_allowed(self):
        self.skill("レビュー行は9章。\n")
        rc, out, _ = self.lint("--strict-skills", "--baseline", self.baseline({self.KEY: 1}))
        self.assertEqual(rc, 0, out)
        self.assertIn("既知 1 件を許容、新規・悪化 0 件", out)

    def test_worse_count_fails(self):
        self.skill("レビュー行は9章。\nもう一度、9章。\n")
        rc, out, _ = self.lint("--strict-skills", "--baseline", self.baseline({self.KEY: 1}))
        self.assertEqual(rc, 1, out)
        self.assertIn("新規・悪化 1 件", out)

    def test_new_violation_in_another_file_fails(self):
        self.skill("レビュー行は9章。\n")
        self.put("prompts/p.txt", "9章。\n")
        rc, out, _ = self.lint("--strict-skills", "--baseline", self.baseline({self.KEY: 1}))
        self.assertEqual(rc, 1, out)
        self.assertIn("prompts/p.txt", out)

    def test_new_kind_in_a_known_file_fails(self):
        self.skill("レビュー行は9章。\n`make test-fast`\n")
        rc, out, _ = self.lint("--strict-skills", "--baseline", self.baseline({self.KEY: 1}))
        self.assertEqual(rc, 1, out)

    def test_fixed_violation_is_reported(self):
        self.skill("直した。\n")
        rc, out, _ = self.lint("--strict-skills", "--baseline", self.baseline({self.KEY: 1}))
        self.assertEqual(rc, 0, out)
        self.assertIn("解消", out)

    def test_default_baseline_is_tools_skill_lint_baseline_json(self):
        self.skill("レビュー行は9章。\n")
        self.baseline({self.KEY: 1})
        rc, out, _ = self.lint("--strict-skills")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.lint("--strict-skills", "--no-baseline")
        self.assertEqual(rc, 1, out)

    def test_write_baseline_then_pass(self):
        self.skill("レビュー行は9章。\n`make test-fast`\n")
        path = self.root / BASELINE_REL
        rc, out, _ = self.lint("--write-baseline", str(path))
        self.assertEqual(rc, 0, out)
        data = json.loads(path.read_text(encoding="utf-8"))["violations"]
        self.assertEqual(data[self.KEY], 1)
        self.assertEqual(data["skills/demo/SKILL.md::test:make test-fast"], 1)
        rc, out, _ = self.lint("--strict-skills")
        self.assertEqual(rc, 0, out)

    def test_json_output_lists_only_new_problems(self):
        self.skill("レビュー行は9章。\n")
        rc, out, _ = self.lint("--json", "--baseline", self.baseline({self.KEY: 1}))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["problems"], [])


class TestRealTree(unittest.TestCase):
    """実物の木を写して、実測の2つ。ベースラインは本物。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_skill_lint_real_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "sf"
        for rel in ("skills", "prompts", "hermes", "scripts/templates"):
            shutil.copytree(REPO / rel, self.root / rel, ignore=shutil.ignore_patterns("__pycache__"))
        for rel in ("scripts/levels.toml", "Makefile", BASELINE_REL):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.root / rel)

    def lint(self):
        return run_main(["--root", str(self.root), "--strict-skills"])

    def test_copy_of_the_real_tree_passes(self):
        rc, out, _ = self.lint()
        self.assertEqual(rc, 0, out)

    def test_measured_nine_chapters_in_a_skill_fails(self):
        p = self.root / "skills" / "product-development-process" / "SKILL.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n承認依頼は9章で書く。\n", encoding="utf-8")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("9章", out)

    def test_measured_placeholder_passes(self):
        p = self.root / "skills" / "product-development-process" / "SKILL.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n承認依頼は${review_chapters}で書く。\n", encoding="utf-8")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 0, out)


class TestRepo(unittest.TestCase):
    def test_real_repo_is_clean_with_the_baseline(self):
        self.assertTrue((REPO / BASELINE_REL).is_file(), "ベースラインが無い")
        rc, out, _ = run_main(["--strict-skills"])
        self.assertEqual(rc, 0, out)
        self.assertIn("新規・悪化 0 件", out)

    def test_baseline_is_empty_and_the_repo_has_no_copy_at_all(self):
        """段10: 設定値の書き写しの返済が済んだ。ベースラインは空で、許す既存は無い(1件でも足せば落ちる)。"""
        data = json.loads((REPO / BASELINE_REL).read_text(encoding="utf-8"))["violations"]
        self.assertEqual(data, {})
        rc, out, _ = run_main(["--strict-skills", "--no-baseline"])
        self.assertEqual(rc, 0, out)
        self.assertIn("検査7（書き写し）0 件", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
