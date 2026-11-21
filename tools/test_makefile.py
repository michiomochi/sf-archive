#!/usr/bin/env python3
"""Makefile の配線の検査(オフライン。一時ディレクトリだけに書く)。`make test` から実行する。

- make deploy・make precheck は test を前提にしない(test を飛ばす印も使わない)。make test は依存なしで毎回全部回る。
- 入口 make ship は、recipe の中で make test → make deploy を順に呼ぶ。実物の Makefile から ship の recipe を抜き出し、
  偽の test / deploy と組んで make -k -j2 ship を回し、test が落ちたら deploy が走らないことを確かめる。
- 対照: 同じことを `ship: test deploy`(依存)で書くと、test が落ちても deploy が走る(依存で書かない理由)。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import lint_envs  # noqa: E402

MAKEFILE = (REPO / "Makefile").read_text(encoding="utf-8")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES")}
    env.update(extra)
    return env


def prereqs(target):
    m = re.search(rf"^{re.escape(target)}:([^\n#]*)", MAKEFILE, re.M)
    assert m is not None, f"Makefile に {target}: が無い"
    return m.group(1).split()


def recipe(target):
    """target: の次から続くタブ始まりの行(recipe)をそのまま返す。"""
    lines, found = [], False
    for line in MAKEFILE.splitlines():
        if found:
            if line.startswith("\t"):
                lines.append(line)
                continue
            break
        found = re.match(rf"{re.escape(target)}:(?!=)", line) is not None
    assert lines, f"Makefile の {target}: に recipe が無い"
    return lines


def plan_list(*flags):
    """test_plan.py が回すコマンド(--list)。make test の中身は Makefile ではなく tools/tests.toml なので、並びはここで見る。"""
    r = subprocess.run([sys.executable, str(REPO / "tools" / "test_plan.py"), *flags, "--list"], capture_output=True, text=True, cwd=REPO, env=clean_env())
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def make_n(target):
    r = subprocess.run(["make", "-n", "--no-print-directory", "-C", str(REPO), target], capture_output=True, text=True, env=clean_env())
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


class WiringTest(unittest.TestCase):
    def test_deploy_has_no_prerequisites(self):
        # deploy は test に依存しない。入口は require_main.sh とロックを recipe の中で持つ(tools/test_deploy_guard.py が順序を見る)。
        self.assertEqual(prereqs("deploy"), [])

    def test_test_and_precheck_have_no_prerequisites(self):
        self.assertEqual(prereqs("test"), [])
        self.assertEqual(prereqs("precheck"), [])

    def test_ship_has_no_prerequisites_and_is_phony(self):
        self.assertEqual(prereqs("ship"), [])  # 依存で書かない(下の対照を参照)
        self.assertRegex(MAKEFILE, r"(?m)^\.PHONY:.*\bship\b")

    def test_makefile_does_not_use_stamps(self):
        self.assertNotRegex(MAKEFILE, r"(?<!test_)stamp\.py")  # リハーサル用の tools/stamp.py は Makefile から呼ばない
        self.assertNotIn(".stamps", MAKEFILE)

    def test_make_n_deploy_runs_no_test(self):
        out = "\n".join(make_n("deploy"))
        self.assertIn("bash ", out)
        self.assertIn("safe_deploy.sh", out)
        for word in ("selftest", "test_install", "test_stamp", "test_precheck", "test_makefile", "test_safe_deploy"):
            self.assertNotIn(word, out)

    def test_make_n_test_runs_the_deploy_gate_tests(self):
        # tools/test_safe_deploy.sh(デプロイ前後の EPIC 行の比較、止まる検査)は make test の一部。ここに無いと誰も流さず、壊れても気づけない。
        out = "\n".join(plan_list("--all"))
        self.assertIn("bash tools/test_safe_deploy.sh", out)

    def test_make_n_ship_runs_test_before_deploy(self):
        lines = make_n("ship")
        first_test = next(i for i, ln in enumerate(lines) if "tools/test_plan.py" in ln and "--all" in ln)  # make test の中身(全部)は test_plan.py が回す
        deploys = [i for i, ln in enumerate(lines) if ln.startswith("bash ") and "/safe_deploy.sh" in ln and "--check-only" not in ln]  # test_safe_deploy.sh は数えない
        self.assertEqual(len(deploys), 1, "\n".join(lines))
        self.assertLess(first_test, deploys[0])
        self.assertLess(max(i for i, ln in enumerate(lines) if "tools/test_" in ln), deploys[0])


class TestPlanWiringTest(unittest.TestCase):
    """test・test-fast・test-paths は tools/test_plan.py を呼ぶ1行だけ(テストの表を Makefile に書かない。表は tools/tests.toml)。"""

    def test_each_target_calls_test_plan_once_with_its_mode(self):
        for target, flag in (("test", "--all"), ("test-fast", "--fast"), ("test-paths", "--paths")):
            with self.subTest(target=target):
                self.assertEqual(prereqs(target), [])
                lines = recipe(target)
                self.assertEqual(len(lines), 2 if target == "test" else 1, lines)  # test だけ、緑のあとに tested_tree.json を書く行が続く(段5)
                self.assertIn("$(TEST_PLAN)", lines[0])
                self.assertTrue(lines[0].split("$(TEST_PLAN)")[1].strip().startswith(flag), lines[0])
                self.assertRegex(MAKEFILE, rf"(?m)^\.PHONY:.*\b{target}\b")
        self.assertRegex(MAKEFILE, r"(?m)^TEST_PLAN = \$\(PY\) tools/test_plan\.py ")

    def test_test_paths_passes_PATHS_through(self):
        self.assertIn("$(PATHS)", recipe("test-paths")[0])
        out = "\n".join(make_n("test-paths"))
        self.assertIn("--paths", out)

    def test_make_n_shows_the_plan_command_not_a_table(self):
        for target in ("test", "test-fast"):
            out = make_n(target)
            self.assertEqual(len([ln for ln in out if "tools/test_plan.py" in ln]), 1, out)
            self.assertFalse([ln for ln in out if "--selftest" in ln or "tools/test_" in ln.replace("tools/test_plan.py", "")], out)

    def test_make_test_fast_does_not_run_the_heavy_ones(self):
        fast, everything = plan_list("--fast"), plan_list("--all")
        self.assertLess(len(fast), len(everything))
        self.assertFalse([ln for ln in fast if "test_safe_deploy.sh" in ln])


class TestedTreeWiringTest(unittest.TestCase):
    """段5: テストした木と出荷する木が同じか。make test が tested_tree.json を書き、make ship-tested が deploy の前に照合する。"""

    def test_ship_checks_the_tested_tree_between_test_and_deploy(self):
        # (a) make ship の順序: test → (tested_tree の記録) → ship-tested の照合 → deploy。-n は recipe を並べるだけで何も実行しない。
        lines = make_n("ship")
        idx = lambda pred: [i for i, ln in enumerate(lines) if pred(ln)]  # noqa: E731
        plan = idx(lambda ln: "tools/test_plan.py" in ln and "--all" in ln)
        write = idx(lambda ln: "scripts/tested_tree.py write" in ln)
        check = idx(lambda ln: "scripts/tested_tree.py check" in ln)
        deploy = idx(lambda ln: ln.startswith("bash ") and "/safe_deploy.sh" in ln and "--check-only" not in ln and "tools/test_" not in ln)
        self.assertEqual([len(plan), len(write), len(check), len(deploy)], [1, 1, 1, 1], "\n".join(lines))
        self.assertLess(plan[0], write[0])
        self.assertLess(write[0], check[0])
        self.assertLess(check[0], deploy[0])

    def test_ship_tested_is_check_then_deploy_and_not_a_prerequisite_form(self):
        self.assertEqual(prereqs("ship-tested"), [])  # ship と同じ理由: 依存で書くと -j / -k で順序が崩れる
        lines = recipe("ship-tested")
        self.assertIn("tested_tree.py check", lines[0])
        self.assertIn("deploy", lines[1])
        self.assertRegex(MAKEFILE, r"(?m)^\.PHONY:.*\bship-tested\b")
        self.assertEqual([ln.strip() for ln in recipe("ship")], ["@$(MAKE) --no-print-directory test", "@$(MAKE) --no-print-directory ship-tested"])

    def test_make_test_writes_the_record_only_after_the_plan(self):
        lines = recipe("test")
        self.assertIn("$(TEST_PLAN) --all", lines[0])
        self.assertIn("scripts/tested_tree.py write", lines[1])
        self.assertIn("$(TESTED_TREE)", lines[1])
        self.assertRegex(MAKEFILE, r"(?m)^TESTED_TREE \?= .*tested_tree\.json")


class ShipTestedTreeTest(unittest.TestCase):
    """実物の ship / ship-tested / test の記録の行を偽の deploy と組む(本物の git の木で、本番には触れない)。"""

    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_makefile_tt_")
        self.t = Path(self.tmpd.name).resolve()
        self.tree = self.t / "tree"
        self.tree.mkdir()
        genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        genv.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
                    GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        self.genv = genv
        for a in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(self.tree), *a], check=True, capture_output=True, env=genv)
        self.head = subprocess.run(["git", "-C", str(self.tree), "rev-parse", "HEAD"], capture_output=True, text=True, env=genv).stdout.strip()
        self.log = self.t / "log"
        self.record = self.t / "tested_tree.json"

    def tearDown(self):
        self.tmpd.cleanup()

    def real(self, lines):
        return "\n".join(lines).replace("scripts/tested_tree.py", str(REPO / "scripts" / "tested_tree.py")) + "\n"

    def make(self, target, test_writes=True, test_fails=False):
        """偽の test(log に test と書き、本物の記録の行を回す)と deploy(log に deploy と書く)。ship / ship-tested は Makefile の実物。"""
        body = ("PY = " + sys.executable + "\n.PHONY: test deploy ship ship-tested\n"
                "test:\n\t@echo test >> $(LOG)\n" + ("\t@exit 1\n" if test_fails else "") + (self.real([recipe("test")[1]]) if test_writes else "")
                + "deploy:\n\t@echo deploy >> $(LOG)\nship-tested:\n" + self.real(recipe("ship-tested")) + "ship:\n" + "\n".join(recipe("ship")) + "\n")
        (self.tree / "Makefile").write_text(body, encoding="utf-8")
        self.log.unlink(missing_ok=True)
        env = clean_env(**self.genv)
        r = subprocess.run(["make", "--no-print-directory", target, f"LOG={self.log}", f"TESTED_TREE={self.record}"], cwd=self.tree, capture_output=True, text=True, env=env)
        return r, (self.log.read_text().split() if self.log.exists() else [])

    def test_ship_runs_test_then_deploy_when_the_head_is_the_tested_one(self):
        r, ran = self.make("ship")  # Makefile は追跡外のまま(write は追跡ファイルの変更だけを見る)
        self.assertEqual((r.returncode, ran), (0, ["test", "deploy"]), r.stdout + r.stderr)
        self.assertEqual(json.loads(self.record.read_text()), {"path": str(self.tree), "head": self.head})

    def test_other_head_in_tested_tree_is_refused_and_deploy_does_not_run(self):
        # (b) 別の HEAD を TESTED_TREE に渡すと ship-tested が拒否する
        self.record.write_text(json.dumps({"path": str(self.tree), "head": "f" * 40}))
        r, ran = self.make("ship-tested")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(ran, [])  # deploy は走らない
        self.assertIn(f"tested {'f' * 12} / now {self.head[:12]}", r.stdout + r.stderr)
        self.assertIn("make test を回してから出荷する", r.stdout + r.stderr)

    def test_same_head_in_tested_tree_lets_ship_tested_deploy(self):
        self.record.write_text(json.dumps({"path": "/elsewhere", "head": self.head}))
        r, ran = self.make("ship-tested")
        self.assertEqual((r.returncode, ran), (0, ["deploy"]), r.stdout + r.stderr)

    def test_ship_refuses_when_the_test_left_a_record_of_another_head(self):
        # test が記録を書かない(または別の木の記録が残る)と、ship は test のあとで止まる。
        self.record.write_text(json.dumps({"path": "/elsewhere", "head": "e" * 40}))
        r, ran = self.make("ship", test_writes=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(ran, ["test"])

    def test_failing_test_leaves_no_record_and_no_deploy(self):
        r, ran = self.make("ship", test_fails=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(ran, ["test"])
        self.assertFalse(self.record.exists())


class EnvDefaultTest(unittest.TestCase):
    """ENV の既定は OS で決まる(uname -s が Darwin なら michiomochi-mac、それ以外は michiomochi-wsl)。偽の uname で実物の Makefile を評価する。"""

    def test_darwin_defaults_to_mac(self):
        self.assertEqual(lint_envs.makefile_default_env("Darwin"), "michiomochi-mac")

    def test_linux_defaults_to_wsl(self):
        self.assertEqual(lint_envs.makefile_default_env("Linux"), "michiomochi-wsl")

    def test_explicit_env_wins_over_the_os_default(self):
        self.assertEqual(lint_envs.makefile_default_env("Darwin", make_args=["ENV=michiomochi-wsl"]), "michiomochi-wsl")
        self.assertEqual(lint_envs.makefile_default_env("Linux", make_args=["ENV=michiomochi-mac"]), "michiomochi-mac")

    def test_default_env_file_exists_for_both_oses(self):
        for uname in ("Darwin", "Linux"):
            with self.subTest(uname=uname):
                self.assertTrue((REPO / "envs" / f"{lint_envs.makefile_default_env(uname)}.toml").is_file())


class EnvHealthWiringTest(unittest.TestCase):
    """make test が環境を横断して検査する(wsl だけ緑で mac が赤、を見逃さない)。"""

    def test_make_test_runs_the_env_checks_before_render(self):
        out = plan_list("--all")
        idx = {k: next(i for i, ln in enumerate(out) if k in ln) for k in ("lint_envs.py", "lint_dollar.py", "lint_bash32.py", "test_lint_checks.py", "render --all-envs --dry-run")}
        self.assertLess(max(idx[k] for k in ("lint_envs.py", "lint_dollar.py", "lint_bash32.py")), idx["render --all-envs --dry-run"])  # 原因の行を、render の総合エラーより先に出す

    def test_make_test_renders_every_env_in_dry_run(self):
        self.assertTrue(any("tools/factory.py render --all-envs --dry-run" in ln for ln in plan_list("--all")))


class ShipOrderTest(unittest.TestCase):
    """偽の test / deploy と、実物の ship の recipe を組んだ Makefile で順序を見る。"""

    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_makefile_")
        self.t = Path(self.tmpd.name)
        self.log = self.t / "log"

    def tearDown(self):
        self.tmpd.cleanup()

    def run_make(self, ship_rule, test_fails, flags):
        fake = ("test:\n\t@echo test >> $(LOG)\n\t@" + ("exit 1" if test_fails else "true") + "\n"
                "deploy:\n\t@echo deploy >> $(LOG)\n"
                "ship-tested:\n\t@$(MAKE) --no-print-directory deploy\n"  # 実物は照合してから deploy(照合は TestedTreeWiringTest / ShipTestedTreeTest が見る)。ここは順序だけ
                ".PHONY: test deploy ship ship-tested\n" + ship_rule)
        (self.t / "Makefile").write_text(fake, encoding="utf-8")
        self.log.unlink(missing_ok=True)
        r = subprocess.run(["make", *flags, "ship", f"LOG={self.log}"], cwd=self.t, capture_output=True, text=True, env=clean_env())
        ran = self.log.read_text().split() if self.log.exists() else []
        return r.returncode, ran

    def real_ship(self):
        return "ship:\n" + "\n".join(recipe("ship")) + "\n"

    def test_failing_test_never_runs_deploy(self):
        for flags in ([], ["-k"], ["-j2"], ["-k", "-j2"]):
            with self.subTest(flags=flags):
                rc, ran = self.run_make(self.real_ship(), True, flags)
                self.assertNotEqual(rc, 0)
                self.assertEqual(ran, ["test"])

    def test_passing_test_runs_test_then_deploy(self):
        for flags in ([], ["-k", "-j2"]):
            with self.subTest(flags=flags):
                rc, ran = self.run_make(self.real_ship(), False, flags)
                self.assertEqual(rc, 0)
                self.assertEqual(ran, ["test", "deploy"])

    def test_control_dependency_form_runs_deploy_after_test_failed(self):
        # 対照: `ship: test deploy` と書くと、-k では test が落ちても deploy を作りに行く。
        rc, ran = self.run_make("ship: test deploy\n", True, ["-k", "-j2"])
        self.assertNotEqual(rc, 0)
        self.assertIn("deploy", ran)


if __name__ == "__main__":
    unittest.main(verbosity=1)
