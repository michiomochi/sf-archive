#!/usr/bin/env python3
"""tools/test_plan.py(と実物の tools/tests.toml)の検査(オフライン。一時ディレクトリの偽 repo と、実物の repo の読み取りだけ)。`make test` から実行する。

確かめること:
- 登録漏れ: tools/test_new.py を tests.toml に登録せずに置くと落ちる(何も回さない)。paths に書くだけの「回さない登録」も落とす
- 読み込み: paths に存在しない名前、名前の重複、tier・cpu_limit の誤り、heavy に paths が無い、cmd のファイルが無い → 落ちる
- --fast は fast だけ回し、回していない重い検査の名前を出す
- --paths は変えたパスに合う heavy だけ回す。合うものが無ければ「該当なし」で rc=0(何も回さない)
- CPU 時間: fast の合計が上限を超えたら落ちる。待つだけで壁時計が長いテストは落ちない(壁時計は見ない)。テスト単位の cpu_limit も見る
- 実物: tests.toml は登録漏れ 0 件で、上限は 60 秒のまま。PATHS=tools/safe_deploy.sh は deploy_guard と safe_deploy だけを選ぶ
"""
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLAN = REPO / "tools" / "test_plan.py"
sys.path.insert(0, str(REPO / "tools"))
import test_plan  # noqa: E402

MARK = "import pathlib; pathlib.Path('ran_{n}').write_text('x')"


def plan(root, *args, toml=None):
    cmd = [sys.executable, str(PLAN), "--root", str(root), "--py", sys.executable, *args]
    if toml:
        cmd += ["--tests-toml", str(toml)]
    return subprocess.run(cmd, capture_output=True, text=True)


def entry(name, tier="fast", cmd=None, cpu: float = 30, paths=None, extra=""):
    cmd = cmd or ["{py}", "-c", MARK.format(n=name)]
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731  (JSON の文字列は TOML の基本文字列でもある)
    out = f'[[test]]\nname = "{name}"\ncmd = [{", ".join(q(c) for c in cmd)}]\ntier = "{tier}"\ncpu_limit = {cpu}\n'
    if paths is not None:
        out += "paths = [" + ", ".join(q(p) for p in paths) + "]\n"
    return out + extra + "\n"


class FakeRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test_test_plan_")
        self.root = Path(self.tmp.name)
        for d in ("tools", "scripts/tests", "docs"):
            (self.root / d).mkdir(parents=True)
        (self.root / "README.md").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="print('ok')\n"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def toml(self, *entries):
        (self.root / "tools" / "tests.toml").write_text("".join(entries), encoding="utf-8")

    def ran(self, name):
        return (self.root / f"ran_{name}").exists()


class RegistrationTest(FakeRepo):
    def setUp(self):
        super().setUp()
        self.write("tools/test_a.py", MARK.format(n="a") + "\n")
        self.toml(entry("a", cmd=["{py}", "tools/test_a.py"]))

    def test_registered_is_clean(self):
        r = plan(self.root, "--check")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_new_test_file_that_is_not_registered_fails_and_runs_nothing(self):
        self.write("tools/test_new.py", MARK.format(n="new") + "\n")
        for mode in ("--check", "--fast", "--all"):
            with self.subTest(mode=mode):
                r = plan(self.root, mode)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("tests.toml に無いテスト: tools/test_new.py", r.stderr)
                self.assertFalse(self.ran("a"), "登録漏れがあるのに、ほかのテストを回してしまった")
                self.assertFalse(self.ran("new"))

    def test_listing_a_test_only_in_paths_is_not_a_registration(self):
        # paths に書くだけで「登録した」ことにすると、回さない登録が通ってしまう。
        self.write("tools/test_new.py")
        self.toml(entry("a", cmd=["{py}", "tools/test_a.py"], paths=["tools/test_new.py"]))
        r = plan(self.root, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tools/test_new.py", r.stderr)

    def test_unregistered_tool_and_selftest_and_testdata_fail(self):
        for rel in ("tools/helper.py", "scripts/foo_selftest.py", "scripts/tests/data.json", "tools/test_x.sh"):
            with self.subTest(rel=rel):
                self.write(rel)
                r = plan(self.root, "--check")
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"tests.toml に無いテスト: {rel}", r.stderr)
                (self.root / rel).unlink()

    def test_tool_covered_by_paths_is_registered(self):
        self.write("tools/helper.py")
        self.toml(entry("a", cmd=["{py}", "tools/test_a.py"], paths=["tools/helper.py"]))
        self.assertEqual(plan(self.root, "--check").returncode, 0)

    def test_files_outside_the_inventory_need_no_registration(self):
        self.write("docs/note.md")
        self.write("scripts/epic_common.py")
        self.assertEqual(plan(self.root, "--check").returncode, 0)


class LoadTest(FakeRepo):
    def assert_fails(self, text, word):
        self.toml(text)
        r = plan(self.root, "--fast")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(word, r.stderr)
        self.assertFalse(self.ran("a"), "tests.toml が壊れているのにテストを回した")

    def test_path_that_does_not_exist_fails_at_load(self):
        self.assert_fails(entry("a", paths=["tools/no_such_name.py"]), "tools/no_such_name.py")
        self.assert_fails(entry("a", paths=["tools/no_such_dir/*"]), "どのファイルにも当たりません")

    def test_duplicate_name(self):
        self.assert_fails(entry("a") + entry("a"), "name が重複")

    def test_bad_tier(self):
        self.assert_fails(entry("a").replace('tier = "fast"', 'tier = "slow"'), "tier は fast か heavy")

    def test_missing_or_bad_cpu_limit(self):
        self.assert_fails(entry("a").replace("cpu_limit = 30\n", ""), "cpu_limit")
        self.assert_fails(entry("a").replace("cpu_limit = 30", "cpu_limit = 0"), "cpu_limit")

    def test_heavy_needs_paths(self):
        self.assert_fails(entry("a", tier="heavy"), "heavy には paths が要ります")

    def test_unknown_key(self):
        self.assert_fails(entry("a", extra='skip = true\n'), "未知のキー: skip")

    def test_command_file_that_does_not_exist(self):
        self.assert_fails(entry("a", cmd=["{py}", "tools/test_gone.py"]), "cmd のファイルがありません: tools/test_gone.py")

    def test_unreadable_toml(self):
        (self.root / "tools" / "tests.toml").write_text("[[test]\n", encoding="utf-8")
        r = plan(self.root, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("読めません", r.stderr)


class SelectionTest(FakeRepo):
    def setUp(self):
        super().setUp()
        for n in ("deploy", "other"):
            self.write(f"tools/{n}.sh", "x\n")
        self.toml(entry("quick"), entry("deploy", tier="heavy", paths=["tools/deploy.sh"]), entry("other", tier="heavy", paths=["tools/other.sh"]))

    def test_fast_runs_fast_only_and_names_the_heavy_ones_it_skipped(self):
        r = plan(self.root, "--fast")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ran("quick"))
        self.assertFalse(self.ran("deploy") or self.ran("other"))
        self.assertIn("回していない重い検査: deploy, other", r.stdout)

    def test_all_runs_everything_in_file_order(self):
        r = plan(self.root, "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ran("quick") and self.ran("deploy") and self.ran("other"))

    def test_paths_with_no_match_is_not_applicable_and_rc0(self):
        r = plan(self.root, "--paths", "README.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("該当なし", r.stdout)
        self.assertFalse(self.ran("quick") or self.ran("deploy") or self.ran("other"))

    def test_paths_runs_only_the_matching_heavy_and_not_fast(self):
        r = plan(self.root, "--paths", "tools/deploy.sh", "README.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ran("deploy"))
        self.assertFalse(self.ran("quick") or self.ran("other"))

    def test_paths_accepts_dot_slash_and_absolute_forms(self):
        for p in ("./tools/deploy.sh", str(self.root / "tools" / "deploy.sh")):
            with self.subTest(p=p):
                (self.root / "ran_deploy").unlink(missing_ok=True)
                self.assertEqual(plan(self.root, "--paths", p).returncode, 0)
                self.assertTrue(self.ran("deploy"))

    def test_paths_without_a_path_is_a_usage_error(self):
        self.assertEqual(plan(self.root, "--paths").returncode, 2)

    def test_list_prints_commands_and_runs_nothing(self):
        r = plan(self.root, "--fast", "--list")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertFalse(self.ran("quick"))

    def test_failing_test_stops_the_run_and_fails(self):
        self.toml(entry("a", cmd=["{py}", "-c", "raise SystemExit(3)"]), entry("b"))
        r = plan(self.root, "--all")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a が落ちました(rc=3)", r.stderr)
        self.assertFalse(self.ran("b"))

    def test_requires_skips_when_the_path_is_missing(self):
        self.toml(entry("a", extra='requires = "/no/such/path"\n'), entry("b"))
        r = plan(self.root, "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skip a", r.stdout)
        self.assertFalse(self.ran("a"))
        self.assertTrue(self.ran("b"))


class CpuTest(FakeRepo):
    BURN = "import time; t = time.process_time()\nwhile time.process_time() - t < 0.6: pass"
    SLEEP = "import time; time.sleep(1.0)"

    def test_fast_total_over_the_budget_fails(self):
        self.toml(entry("burn", cmd=["{py}", "-c", self.BURN]))
        r = plan(self.root, "--fast", "--fast-cpu-budget", "0.3")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("fast の CPU 時間が", r.stderr)
        self.assertIn("0 秒を超えました", r.stderr)
        r = plan(self.root, "--all", "--fast-cpu-budget", "0.3")
        self.assertEqual(r.returncode, 1)

    def test_waiting_is_wall_clock_not_cpu_and_passes(self):
        self.toml(entry("wait", cmd=["{py}", "-c", self.SLEEP]))
        r = plan(self.root, "--fast", "--fast-cpu-budget", "0.3")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cpu_of_grandchildren_is_counted(self):
        sub = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {self.BURN!r}])"
        self.toml(entry("nested", cmd=["{py}", "-c", sub]))
        self.assertEqual(plan(self.root, "--fast", "--fast-cpu-budget", "0.3").returncode, 1)

    def test_budget_is_not_checked_for_heavy_or_paths(self):
        self.write("tools/h.sh", "x\n")
        self.toml(entry("burn", tier="heavy", cmd=["{py}", "-c", self.BURN], paths=["tools/h.sh"]))
        self.assertEqual(plan(self.root, "--paths", "tools/h.sh", "--fast-cpu-budget", "0.3").returncode, 0)

    def test_per_test_cpu_limit_fails(self):
        self.toml(entry("burn", cmd=["{py}", "-c", self.BURN], cpu=0.5))
        r = plan(self.root, "--fast")
        self.assertEqual(r.returncode, 1)
        self.assertIn("burn の CPU 時間", r.stderr)
        self.assertIn("上限 0.5 秒", r.stderr)

    def test_summary_line_reports_wall_and_cpu(self):
        self.toml(entry("a"))
        r = plan(self.root, "--fast")
        self.assertRegex(r.stdout, r"test-fast: 1 件 rc=0 wall=[\d.]+s cpu=[\d.]+s \(fast の CPU [\d.]+s / 上限 60s\)")


class RealRepoTest(unittest.TestCase):
    def test_budget_is_60_seconds_and_cannot_be_set_from_tests_toml(self):
        self.assertEqual(test_plan.FAST_CPU_BUDGET, 60.0)
        raw = tomllib.loads((REPO / "tools" / "tests.toml").read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"test"})  # 設定の置き場が無い(上限を tests.toml から動かせない)

    def test_real_tests_toml_loads_with_nothing_unregistered(self):
        tests, problems = test_plan.load(REPO / "tools" / "tests.toml", REPO)
        self.assertEqual(problems, [])
        self.assertTrue(any(t["tier"] == "fast" for t in tests) and any(t["tier"] == "heavy" for t in tests))

    def test_every_script_of_the_inventory_is_registered(self):
        files = test_plan.repo_files(REPO)
        self.assertIn("tools/test_plan.py", test_plan.inventory(files))
        tests, _ = test_plan.load(REPO / "tools" / "tests.toml", REPO)
        self.assertEqual(test_plan.unregistered(tests, files), [])

    def test_safe_deploy_selects_deploy_guard_and_safe_deploy_only(self):
        r = subprocess.run([sys.executable, str(PLAN), "--paths", "tools/safe_deploy.sh", "--list"], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertIn("tools/test_deploy_guard.py", lines[0])
        self.assertIn("tools/test_safe_deploy.sh", lines[1])

    def test_readme_matches_no_heavy_test(self):
        r = subprocess.run([sys.executable, str(PLAN), "--paths", "README.md"], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("該当なし", r.stdout)

    def test_all_covers_every_command_make_test_ran_before(self):
        # 段4より前の make test が回していた 31 工程が、すべて tests.toml にある(件数と、主な名前)。
        tests, _ = test_plan.load(REPO / "tools" / "tests.toml", REPO)
        names = {t["name"] for t in tests}
        for must in ("epic_gate_controller_selftest", "epic_pipeline_health_selftest", "test_safe_deploy_sh", "test_ship_e2e", "test_install",
                     "test_ops", "skill_lint_strict", "render_all_envs", "render_test_config", "lint_cron", "lint_procedures"):
            self.assertIn(must, names)
        self.assertGreaterEqual(len(tests), 31)

    def test_fast_test_files_do_not_hide_in_heavy(self):
        # 飛ばす道を作らない: heavy は paths に合えば test-paths が回し、make test は全部回す。fast を heavy に動かして「速くした」ことにしない。
        tests, _ = test_plan.load(REPO / "tools" / "tests.toml", REPO)
        self.assertEqual(sorted(t["name"] for t in tests if t["tier"] == "heavy"), sorted(HEAVY))


# 段4の時点で heavy にしてよいもの。増やすときはこの一覧(レビューに出る)も直す。
HEAVY = ["test_deploy_guard", "test_rename_to_sf", "test_retro", "test_safe_deploy_sh"]

if __name__ == "__main__":
    unittest.main(verbosity=1)
