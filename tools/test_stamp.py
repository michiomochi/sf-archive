#!/usr/bin/env python3
"""tools/stamp.py(リハーサルの段の差分実行)の再現テスト(オフライン。一時ディレクトリだけを使う)。

変わった段とその後だけが回り、共有するディレクトリは前の段の写しから戻ってから回る。
写しが無い・前の段が落ちたときは終了コード 3。STAMP_FORCE=1 なら全部回す。
(make test・make deploy が test の印を使わないことは tools/test_makefile.py が見る。)
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
STAMP = [sys.executable, str(TOOLS / "stamp.py")]


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "STAMP_FORCE", "STAMP_DIR")}
    env.update(extra)
    return env


class RunStageTest(unittest.TestCase):
    """build -> prep(写し) -> dry(状態を変えない) -> apply -> rollback を、共有ディレクトリ S で回す。"""

    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_stamp_")
        self.t = Path(self.tmpd.name)
        self.stamps = self.t / "stamps"
        self.s = self.t / "S"
        for n in ("dry", "apply", "prep"):
            (self.t / f"in_{n}.txt").write_text("v1\n")

    def tearDown(self):
        self.tmpd.cleanup()

    def stamp(self, *args, **env):
        return subprocess.run(STAMP + ["--dir", str(self.stamps), *args], capture_output=True, text=True,
                              env=clean_env(**env), cwd=self.t)

    def stage(self, name, after, cmd, *opts, **env):
        args = ["run", name] + (["--after", after] if after else []) + list(opts) + ["--", "sh", "-c", f"echo {name} >> log; {cmd}"]
        return self.stamp(*args, **env)

    def rehearse(self, build_snapshot=True, apply_cmd="echo a > S/applied", **env):
        rcs = []
        rcs.append(self.stage("build", None, "rm -rf S; mkdir S; echo b > S/build",
                              *(["--snapshot", "S"] if build_snapshot else []), **env).returncode)
        rcs.append(self.stage("prep", "build", "echo p > S/prep", "--inputs", "in_prep.txt", "--snapshot", "S", **env).returncode)
        rcs.append(self.stage("dry", "prep", "test -e S/prep && test ! -e S/rolled && test ! -e S/applied",
                              "--inputs", "in_dry.txt", "--stateless", **env).returncode)
        rcs.append(self.stage("apply", "dry", apply_cmd, "--inputs", "in_apply.txt", **env).returncode)
        rcs.append(self.stage("rollback", "apply", "rm -f S/applied; echo r > S/rolled", **env).returncode)
        return rcs

    def ran(self):
        p = self.t / "log"
        out = p.read_text().split() if p.exists() else []
        p.unlink(missing_ok=True)
        return out

    def test_second_run_skips_everything(self):
        self.assertEqual(self.rehearse(), [0] * 5)
        self.assertEqual(self.ran(), ["build", "prep", "dry", "apply", "rollback"])
        self.assertEqual(self.rehearse(), [0] * 5)
        self.assertEqual(self.ran(), [])

    def test_changed_input_reruns_that_stage_and_later_from_restored_state(self):
        self.rehearse(); self.ran()
        (self.t / "in_dry.txt").write_text("v2\n")
        self.assertEqual(self.rehearse(), [0] * 5)  # dry は S/rolled が無いことを確かめる = prep の写しから戻っている
        self.assertEqual(self.ran(), ["dry", "apply", "rollback"])

    def test_changed_earlier_stage_reruns_all_later(self):
        self.rehearse(); self.ran()
        (self.t / "in_prep.txt").write_text("v2\n")
        self.assertEqual(self.rehearse(), [0] * 5)
        self.assertEqual(self.ran(), ["prep", "dry", "apply", "rollback"])

    def test_missing_snapshot_refuses_with_exit_3(self):
        self.rehearse(build_snapshot=False); self.ran()
        (self.t / "in_prep.txt").write_text("v2\n")
        rcs = self.rehearse(build_snapshot=False)
        # build の終わりの状態が無い(S は rollback の後)ので prep は回らず、prep の古い印も消えるので後の段も回らない
        self.assertEqual(rcs, [0, 3, 3, 3, 3])
        self.assertEqual(self.ran(), [])
        self.assertEqual(self.rehearse(build_snapshot=False, STAMP_FORCE="1"), [0] * 5)  # 通しなら回る
        self.assertEqual(self.ran(), ["build", "prep", "dry", "apply", "rollback"])

    def test_failed_stage_invalidates_later_stages(self):
        self.rehearse(); self.ran()
        (self.t / "in_apply.txt").write_text("v2\n")
        rcs = self.rehearse(apply_cmd="exit 7")
        self.assertEqual(rcs, [0, 0, 0, 7, 3])  # rollback は前の段 apply の印が無いので回らない
        self.assertEqual(self.ran(), ["apply"])
        self.assertEqual(self.rehearse(), [0] * 5)
        self.assertEqual(self.ran(), ["apply", "rollback"])  # apply は prep の写しから戻ってから回る

    def test_force_runs_everything(self):
        self.rehearse(); self.ran()
        self.assertEqual(self.rehearse(STAMP_FORCE="1"), [0] * 5)
        self.assertEqual(self.ran(), ["build", "prep", "dry", "apply", "rollback"])

    def test_snapshot_outside_base_is_refused(self):
        r = self.stamp("run", "x", "--snapshot", str(Path.home()), "--", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--snapshot", r.stderr)
        r = self.stamp("run", "x", "--snapshot", str(self.t), "--", "true")
        self.assertNotEqual(r.returncode, 0)

    def test_only_run_subcommand_remains(self):
        # テストの印(begin・commit・check)は無い。argparse が未知のサブコマンドとして断る。
        for sub in ("begin", "commit", "check"):
            r = self.stamp(sub, "test")
            self.assertNotEqual(r.returncode, 0, sub)
            self.assertIn("invalid choice", r.stderr, sub)


if __name__ == "__main__":
    unittest.main(verbosity=1)
