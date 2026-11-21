#!/usr/bin/env python3
"""tools/lint_procedures.py の再現テスト(オフライン。一時ディレクトリだけに書く)。`make test` から実行する。

検査そのものが壊れていないことを、わざと壊した入力で確かめる(壊れた入力で落ちない検査は、緑でも何も保証しない):
  - 既知のコマンド(5種)が、移し先を同じ行に書かずにスキルにある → file:line で落ちる
  - 同じ行に移し先(script / make ターゲット)がある → 通る。コマンドを含まない md・コメント(`<!-- -->`)は通る
  - コードブロック内・表の行は対象。`make precheck` を `make pr` と取り違えない
  - ベースライン: 既存の違反は許し、新規・件数の悪化だけ落とす。行がずれても同じ違反とみなす
  - skills repo が無ければ飛ばしたことを1行出す。あれば検査する
  - **わざと検出を壊す(表を空にする)と、検出のテストが落ちる**
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import lint_procedures  # noqa: E402

CASES = [  # (違反する行, 移し先を書いた行, 見つかるコマンド)
    ("gh pr create --title x", "make pr TITLE=x  # gh pr create の代わり", "gh pr create"),
    ("gh pr merge 12 --squash", "ops.py pr-merge 12  # gh pr merge は打たない", "gh pr merge"),
    ("git worktree add .worktrees/x origin/main", "make worktree NAME=x  # git worktree add の代わり", "git worktree add"),
    ("hermes kanban create --title x", "create_card.py を使う(hermes kanban create は手で打たない)", "kanban create"),
    ("python3 tools/skill_lint.py --strict-skills", "make lint-skills  # skill_lint.py --strict-skills を流す", "skill_lint.py --strict-skills"),
]


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = lint_procedures.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_lint_procedures_")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.sf = self.tmp / "sf"
        (self.sf / "skills").mkdir(parents=True)

    def md(self, rel, text, root=None):
        p = (root or self.sf) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def lint(self, *extra, skills_repo=None):
        # 既定の skills repo は tmp の下の存在しない場所(実物の ../skills を読まない)
        sr = str(skills_repo or self.tmp / "no-such-skills-repo")
        return run_main(["--root", str(self.sf), "--skills-repo", sr, *extra])


class DetectionTest(TmpCase):
    def test_violation_is_reported_with_file_line_command_and_destination(self):
        self.md("skills/a/SKILL.md", "# a\n\n```bash\ngh pr create --title x\n```\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL sf/skills/a/SKILL.md:4: gh pr create → make pr / ops.py pr-create", out)
        self.assertIn("1 件", out)
        self.assertIn("1 ファイル", out)

    def test_every_known_command_is_caught_without_destination(self):
        for bad, _ok, cmd in CASES:
            with self.subTest(cmd=cmd):
                self.md("skills/a/SKILL.md", f"# a\n\n{bad}\n")
                rc, out, _ = self.lint()
                self.assertEqual(rc, 1, out)
                self.assertIn(f": {cmd} → ", out)

    def test_destination_on_the_same_line_is_allowed(self):
        for _bad, ok, cmd in CASES:
            with self.subTest(cmd=cmd):
                self.md("skills/a/SKILL.md", f"# a\n\n{ok}\n")
                rc, out, _ = self.lint()
                self.assertEqual(rc, 0, out)
                self.assertIn("0 件", out)

    def test_destination_on_another_line_does_not_count(self):
        self.md("skills/a/SKILL.md", "# a\n\nmake pr を使う。\ngh pr create --title x\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)

    def test_unrelated_md_passes(self):
        self.md("skills/a/SKILL.md", "# a\n\n判断の基準だけ。`make test` を流す。gh pr view 3 は読むだけ。\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 0, out)

    def test_make_precheck_is_not_mistaken_for_make_pr(self):
        self.md("skills/a/SKILL.md", "# a\n\ngh pr create --title x; make precheck\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)

    def test_table_rows_and_code_blocks_are_checked(self):
        self.md("skills/a/SKILL.md", "| 操作 | コマンド |\n|---|---|\n| マージ | `gh pr merge 3` |\n\n```\ngit worktree add x\n```\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("SKILL.md:3: gh pr merge", out)
        self.assertIn("SKILL.md:6: git worktree add", out)
        self.assertIn("2 件", out)

    def test_html_comments_are_ignored(self):
        self.md("skills/a/SKILL.md", "<!-- gh pr create の例 -->\n<!--\ngh pr merge 1\n-->\n本文\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 0, out)

    def test_references_are_scanned_too(self):
        self.md("skills/a/references/x.md", "gh pr merge 3\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("sf/skills/a/references/x.md:1", out)


class BaselineTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.md("skills/a/SKILL.md", "# a\n\ngh pr create --title x\ngh pr merge 3\n")
        self.base = self.tmp / "baseline.json"

    def test_write_baseline_then_existing_violations_are_allowed(self):
        rc, out, _ = self.lint("--write-baseline", str(self.base))
        self.assertEqual(rc, 0, out)
        data = json.loads(self.base.read_text(encoding="utf-8"))["violations"]
        self.assertEqual(data, {"sf/skills/a/SKILL.md::gh pr create": 1, "sf/skills/a/SKILL.md::gh pr merge": 1})
        rc, out, _ = self.lint("--baseline", str(self.base))
        self.assertEqual(rc, 0, out)
        self.assertIn("既知 2 件を許容、新規・悪化 0 件", out)

    def test_a_new_violation_fails(self):
        self.lint("--write-baseline", str(self.base))
        self.md("skills/b/SKILL.md", "git worktree add x\n")
        rc, out, _ = self.lint("--baseline", str(self.base))
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL sf/skills/b/SKILL.md:1: git worktree add", out)
        self.assertNotIn("sf/skills/a/SKILL.md", out.split("既知")[0].split("FAIL")[-1])

    def test_more_of_the_same_command_in_a_known_file_fails(self):
        self.lint("--write-baseline", str(self.base))
        self.md("skills/a/SKILL.md", "# a\n\ngh pr create --title x\ngh pr create --title y\ngh pr merge 3\n")
        rc, out, _ = self.lint("--baseline", str(self.base))
        self.assertEqual(rc, 1, out)
        self.assertIn("新規・悪化 1 件", out)

    def test_line_shift_is_still_the_same_violation(self):
        self.lint("--write-baseline", str(self.base))
        self.md("skills/a/SKILL.md", "# a\n\n前置きを足した。\n\nさらに足した。\n\ngh pr create --title x\ngh pr merge 3\n")
        rc, out, _ = self.lint("--baseline", str(self.base))
        self.assertEqual(rc, 0, out)

    def test_fixed_violation_is_reported_but_passes(self):
        self.lint("--write-baseline", str(self.base))
        self.md("skills/a/SKILL.md", "# a\n\nmake pr TITLE=x\ngh pr merge 3\n")
        rc, out, _ = self.lint("--baseline", str(self.base))
        self.assertEqual(rc, 0, out)
        self.assertIn("解消", out)
        self.assertIn("gh pr create", out)

    def test_baseline_does_not_hide_a_violation_in_a_file_it_never_listed(self):
        self.base.write_text('{"violations": {}}\n', encoding="utf-8")
        rc, out, _ = self.lint("--baseline", str(self.base))
        self.assertEqual(rc, 1, out)


class SkillsRepoTest(TmpCase):
    def test_missing_skills_repo_is_skipped_with_one_line(self):
        self.md("skills/a/SKILL.md", "# a\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 0, out)
        self.assertIn("skills repo が無いので飛ばした", out)

    def test_skills_repo_is_scanned_when_present(self):
        sr = self.tmp / "skills-repo"
        self.md("skills/x/SKILL.md", "gh pr merge 9\n", root=sr)
        rc, out, _ = self.lint(skills_repo=sr)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL skills/skills/x/SKILL.md:1: gh pr merge", out)
        self.assertNotIn("飛ばした", out)

    def test_absent_skills_repo_does_not_break_a_baseline_that_lists_it(self):
        sr = self.tmp / "skills-repo"
        self.md("skills/x/SKILL.md", "gh pr merge 9\n", root=sr)
        base = self.tmp / "b.json"
        self.lint("--write-baseline", str(base), skills_repo=sr)
        rc, out, _ = self.lint("--baseline", str(base))  # skills repo 無し
        self.assertEqual(rc, 0, out)


class DetectionCanFailTest(unittest.TestCase):
    """わざと検出を壊すと、検出のテストが落ちること(落ちない検査は、緑でも何も保証しない)。"""

    def _run(self, case):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        return result

    def test_suite_is_green_on_the_real_lint(self):
        self.assertTrue(self._run(DetectionTest).wasSuccessful())

    def test_emptying_the_rule_table_makes_detection_tests_fail(self):
        with mock.patch.object(lint_procedures, "RULES", []):
            result = self._run(DetectionTest)
        self.assertFalse(result.wasSuccessful())
        self.assertGreaterEqual(len(result.failures), 5)

    def test_matching_nothing_makes_detection_tests_fail(self):
        with mock.patch.object(lint_procedures, "scan_text", lambda *a, **k: []):
            result = self._run(DetectionTest)
        self.assertFalse(result.wasSuccessful())

    def test_ignoring_destinations_makes_the_allow_test_fail(self):
        broken = [(cmd, ("zzz-never",), hint) for cmd, _dest, hint in lint_procedures.RULES]
        with mock.patch.object(lint_procedures, "RULES", broken):
            result = self._run(DetectionTest)
        self.assertFalse(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main(verbosity=1)
