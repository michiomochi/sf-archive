#!/usr/bin/env python3
"""tools/lint_cron.py と、cron.toml から引く `${gate_every}` などの名前の再現テスト(オフライン。一時ディレクトリだけに書く)。`make test` から実行する。

検査そのものが壊れていないことを、わざと壊した入力で確かめる(壊れた入力で落ちない検査は、緑でも何も保証しない):
  - ジョブ名の「（N分）」と schedule が合わなければ落ちる(every Nm と `*/N * * * *` と `* * * * *`)。合えば通る
  - 毎分のジョブは、スクリプト(薄い .sh → 実体 → 実体が呼ぶスクリプト)が RunLock を取らないと落ちる。毎分でないジョブは問わない
  - 指示文・スキル・SOUL に周期の数値を直に書くと落ち、`${<ジョブ>_every}` を使えと出る。テンプレート名で書けば通る
  - ベースライン: 既存は許し、新規・件数の悪化だけ落とす。ジョブ名の不一致と test_install の直書きはベースラインで許さない
  - 実物の hermes/cron.toml・scripts・prompts・skills を写した木で、実測の3つ(名前だけ 1分・毎分ジョブの RunLock を消す・skill に周期を直書き)が落ちる
  - `${gate_every}` が cron.toml の値から置換される(factory render)
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
sys.path.insert(0, str(REPO / "scripts"))
import factory  # noqa: E402
import lint_cron  # noqa: E402
import sfconfig  # noqa: E402

CRON = """
[defaults]
catch_up = "off"

[jobs.gate]
name = "SF ゲート制御（%(gate)s）"
schedule = "%(gate_sched)s"
no_agent = true
script = "sf-gate.sh"

[jobs.planner]
name = "SF プランナー（5分）"
schedule = "every 5m"
monitor = "sf-planner.sh"
prompt = "planner"

[jobs.drift]
name = "SF 差の確認（毎時）"
schedule = "37 * * * *"
no_agent = true
script = "sf-drift.sh"

[wrappers."sf-gate.sh"]
run = "python3"
target = "live/gate_apply.py"

[wrappers."sf-planner.sh"]
run = "python3"
target = "live/planner_status.py"

[wrappers."sf-drift.sh"]
run = "bash"
target = "repo/tools/drift.sh"
"""
APPLY = 'import os\nCONTROLLER = os.path.join(HERE, "gate_controller.py")\n'  # 薄い呼び出し側。ロックは呼ばれる側が取る
CONTROLLER_LOCKED = 'import epic_common as C\n"""RunLock() は docstring に書いただけでは取ったことにならない"""\nlock = C.RunLock()\n'
CONTROLLER_UNLOCKED = 'import epic_common as C\n"""RunLock() は docstring に書いただけでは取ったことにならない"""\nlock = None\n'


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = lint_cron.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TmpRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_lint_cron_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "sf"
        self.root.mkdir()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def mini(self, gate="2分", gate_sched="every 2m", controller=CONTROLLER_LOCKED):
        self.write("hermes/cron.toml", CRON % dict(gate=gate, gate_sched=gate_sched))
        self.write("scripts/gate_apply.py", APPLY)
        self.write("scripts/gate_controller.py", controller)
        self.write("scripts/planner_status.py", "print('status')\n")
        self.write("tools/drift.sh", "echo drift\n")

    def lint(self, *extra):
        return run_main(["--root", str(self.root), *extra])


class NameVsSchedule(TmpRoot):
    def test_matching_name_and_schedule_pass(self):
        self.mini()
        rc, out, _ = self.lint()
        self.assertEqual((rc, "FAIL" in out), (0, False), out)

    def test_name_says_1_but_schedule_is_every_2m_fails(self):
        self.mini(gate="1分", gate_sched="every 2m")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL .*gate.*1分.*every 2m")

    def test_cron_expressions_are_read(self):
        for sched, minutes, ok in (("*/3 * * * *", "3分", True), ("*/3 * * * *", "2分", False), ("* * * * *", "1分", True), ("* * * * *", "2分", False)):
            with self.subTest(sched=sched, name=minutes):
                self.mini(gate=minutes, gate_sched=sched)
                self.assertEqual(self.lint()[0], 0 if ok else 1)

    def test_hourly_name_needs_an_hourly_schedule(self):
        self.mini()
        toml = (self.root / "hermes/cron.toml").read_text(encoding="utf-8").replace('schedule = "37 * * * *"', 'schedule = "every 5m"')
        self.write("hermes/cron.toml", toml)
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL .*drift.*毎時")

    def test_a_name_mismatch_is_not_allowed_by_the_baseline(self):
        self.mini(gate="1分", gate_sched="every 2m")
        base = self.root / "base.json"
        self.assertEqual(self.lint("--write-baseline", str(base))[0], 0)
        self.assertEqual(self.lint("--baseline", str(base))[0], 1)


class RunLock(TmpRoot):
    def test_every_minute_job_with_a_lock_in_the_script_it_calls_passes(self):
        self.mini(gate="1分", gate_sched="* * * * *")  # gate_apply.py -> gate_controller.py の RunLock()
        rc, out, _ = self.lint()
        self.assertEqual((rc, "FAIL" in out), (0, False), out)

    def test_every_minute_job_without_a_lock_fails(self):
        self.mini(gate="1分", gate_sched="every 1m", controller=CONTROLLER_UNLOCKED)
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL .*gate.*RunLock")

    def test_a_job_that_is_not_every_minute_is_not_asked(self):
        self.mini(gate="2分", gate_sched="every 2m", controller=CONTROLLER_UNLOCKED)
        self.assertEqual(self.lint()[0], 0)

    def test_the_lock_may_come_from_with_lock_py(self):
        self.mini(gate="1分", gate_sched="every 1m", controller=CONTROLLER_UNLOCKED)
        self.write("scripts/gate_apply.py", 'import subprocess\nsubprocess.run(["python3", "tools/with_lock.py", "x.lock", "--", "echo"])\n')
        self.assertEqual(self.lint()[0], 0)

    def test_the_lock_check_follows_a_shell_wrapper_target(self):
        self.mini(gate="1分", gate_sched="every 1m", controller=CONTROLLER_UNLOCKED)
        toml = (self.root / "hermes/cron.toml").read_text(encoding="utf-8").replace('target = "live/gate_apply.py"', 'target = "repo/tools/gate.sh"')
        self.write("hermes/cron.toml", toml)
        self.write("tools/gate.sh", "python3 tools/with_lock.py x.lock -- python3 scripts/gate_apply.py\n")
        self.assertEqual(self.lint()[0], 0)

    def test_an_existing_missing_lock_is_allowed_by_the_baseline_but_a_new_one_is_not(self):
        self.mini(gate="1分", gate_sched="every 1m", controller=CONTROLLER_UNLOCKED)
        base = self.root / "base.json"
        self.assertEqual(self.lint("--write-baseline", str(base))[0], 0)
        rc, out, _ = self.lint("--baseline", str(base))
        self.assertEqual(rc, 0, out)
        self.assertIn("新規・悪化 0 件", out)
        toml = (self.root / "hermes/cron.toml").read_text(encoding="utf-8").replace('schedule = "every 5m"', 'schedule = "every 1m"').replace("（5分）", "（1分）")
        self.write("hermes/cron.toml", toml)  # 別のジョブが毎分になった(ロックなし)
        rc, out, _ = self.lint("--baseline", str(base))
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL .*planner.*RunLock")


class TextsUseTheTemplateNames(TmpRoot):
    def setUp(self):
        super().setUp()
        self.mini()

    def test_a_number_written_in_a_skill_fails_and_names_the_template(self):
        self.write("skills/s/SKILL.md", "プランナー（5分毎）が行を作る。\n")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL skills/s/SKILL\.md:1: 5分毎 → .*\$\{planner_every\}")

    def test_every_form_of_a_period_is_found(self):
        cases = ["every 5m", "every 5 minutes", "3分ごと", "3分毎", "3分おき", "毎分", "*/3 * * * *", "* * * * *", "SF ゲート制御（2分）", "SF ゲート制御(2分)"]
        for text in cases:
            with self.subTest(text=text):
                self.write("skills/s/SKILL.md", f"cron は {text} で走る。\n")
                self.assertEqual(self.lint()[0], 1, text)

    def test_the_template_name_passes(self):
        self.write("skills/s/SKILL.md", "プランナー（${planner_every}毎）が行を作る。cron『SF ゲート制御（${gate_every}）』。\n")
        self.assertEqual(self.lint()[0], 0)

    def test_numbers_that_are_not_a_cron_period_pass(self):
        self.write("skills/s/SKILL.md", "10 minutes 止まったら警告。dispatcher は every 60 s。timeout は 420秒（7分）。差の確認は SF 差の確認（毎時）。\n")
        self.assertEqual(self.lint()[0], 0)

    def test_prompts_soul_and_templates_are_scanned_and_comments_are_not(self):
        for rel in ("prompts/p.txt", "hermes/profiles/executor/SOUL.md", "scripts/templates/t.md"):
            with self.subTest(rel=rel):
                self.write(rel, "<!-- every 5m は書かない例 -->\nok\n")
                self.assertEqual(self.lint()[0], 0)
                self.write(rel, "cron は every 5m\n")
                rc, out, _ = self.lint()
                self.assertEqual(rc, 1, out)
                self.assertIn(rel, out)
                (self.root / rel).unlink()

    def test_baseline_allows_the_existing_and_fails_on_a_new_or_a_worse_one(self):
        self.write("skills/s/SKILL.md", "プランナー（5分毎）が行を作る。\n")
        base = self.root / "base.json"
        rc, out, _ = self.lint("--write-baseline", str(base))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(base.read_text(encoding="utf-8"))["violations"], {"skills/s/SKILL.md::5分毎": 1})
        self.write("skills/s/SKILL.md", "\n\nプランナー（5分毎）が行を作る。\n")  # 行がずれても同じ違反
        self.assertEqual(self.lint("--baseline", str(base))[0], 0)
        self.write("skills/s/SKILL.md", "プランナー（5分毎）が行を作る。\n取り込みは 5分毎。\n")  # 悪化
        self.assertEqual(self.lint("--baseline", str(base))[0], 1)
        self.write("skills/s/SKILL.md", "ok\n")
        self.write("skills/t/SKILL.md", "ゲートは 3分ごと。\n")  # 新規
        self.write("skills/s/SKILL.md", "プランナー（5分毎）が行を作る。\n")
        rc, out, _ = self.lint("--baseline", str(base))
        self.assertEqual(rc, 1, out)
        self.assertIn("skills/t/SKILL.md", out)


class TestInstallReadsCronToml(TmpRoot):
    def test_a_period_written_in_test_install_fails_and_is_not_baselined(self):
        self.mini()
        self.write("tools/test_install.py", 'x = "every 2m"\n')
        base = self.root / "base.json"
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("tools/test_install.py", out)
        self.lint("--write-baseline", str(base))
        self.assertEqual(self.lint("--baseline", str(base))[0], 1)
        self.write("tools/test_install.py", 'x = cron["jobs"]["gate"]["schedule"]\n')
        self.assertEqual(self.lint()[0], 0)


class OnTheRealFiles(unittest.TestCase):
    """実物を写した木で、実測の3つを再現する(このリポジトリの cron.toml は触らない)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_lint_cron_real_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "sf"
        for d in ("hermes", "scripts", "prompts", "skills", "tools"):
            shutil.copytree(REPO / d, self.root / d, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.base = ["--baseline", str(REPO / "tools" / "lint_cron_baseline.json")]

    def lint(self):
        return run_main(["--root", str(self.root), *self.base])

    def edit(self, rel, old, new):
        p = self.root / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text, f"{rel} に {old!r} が無い(テストの前提が変わった)")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_the_repository_as_it_is_passes_with_the_baseline(self):
        rc, out, _ = self.lint()
        self.assertEqual(rc, 0, out)

    def test_name_two_minutes_with_schedule_every_minute_fails(self):
        self.edit("hermes/cron.toml", 'name = "SF ゲート制御（1分）"', 'name = "SF ゲート制御（2分）"')  # 段9: ゲート制御は毎分(`* * * * *`)
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL .*gate.*2分.*\* \* \* \* \*")

    def test_removing_the_run_lock_from_the_every_minute_gate_fails(self):
        self.assertEqual(self.lint()[0], 0)  # 毎分でもロックがあれば通る(ゲート制御は段9から毎分)
        self.edit("scripts/epic_gate_controller.py", "lock = C.RunLock()", "lock = None")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL .*gate.*RunLock")

    def test_writing_a_period_in_a_skill_fails_and_names_the_template(self):
        self.edit("skills/software-factory-ops/SKILL.md", "（出荷は", "（ゲート制御は2分ごと。出荷は")
        rc, out, _ = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL skills/software-factory-ops/SKILL\.md:\d+: 2分ごと → .*\$\{gate_every\}")


class TemplateNames(unittest.TestCase):
    def test_every_name_comes_from_the_value_in_cron_toml(self):
        cron = factory.load_cron_toml()
        names = sfconfig.cron_template_vars(cron)
        self.assertTrue({"gate_every", "planner_every"} <= set(names))
        for name, key in sfconfig.CRON_TEMPLATE_NAMES.items():
            self.assertEqual(names[name], f"{sfconfig.schedule_minutes(cron['jobs'][key]['schedule'])}分", name)

    def test_the_value_follows_cron_toml(self):
        cron = {"jobs": {"gate": {"schedule": "* * * * *"}, "planner": {"schedule": "*/7 * * * *"}, "ship": {"schedule": "every 4m"}}}
        names = sfconfig.cron_template_vars(cron, only=("gate", "planner", "ship"))
        self.assertEqual((names["gate_every"], names["planner_every"], names["ship_every"]), ("1分", "7分", "4分"))

    def test_a_schedule_that_is_not_in_minutes_is_an_error(self):
        with self.assertRaises(sfconfig.ConfigError):
            sfconfig.cron_template_vars({"jobs": {"gate": {"schedule": "0 9 * * *"}}}, only=("gate",))

    def test_render_replaces_the_names(self):
        cfg_raw = sfconfig.load(factory.REPO / "tests" / "config.test.toml", expand=False)
        cron = factory.load_cron_toml()
        out = factory.render_text("ゲート（${gate_every}）、プランナー（${planner_every}毎）", "prompt", cfg_raw)
        self.assertEqual(out, f"ゲート（{sfconfig.cron_template_vars(cron)['gate_every']}）、プランナー（{sfconfig.cron_template_vars(cron)['planner_every']}毎）")


if __name__ == "__main__":
    unittest.main(verbosity=1)
