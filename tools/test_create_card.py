#!/usr/bin/env python3
"""create_card.py の検査(オフライン。カードは作らない。`make test` から実行する)。

確かめること:
- board の既定は環境の設定([hermes].board)で、--board を付けなくても必ず --board が渡る
- --board は上書きとして効く
- 存在しない board は拒否され、何も作らない(終了コード 2)
- --board はサブコマンド create より前にある(hermes の全体フラグ)
- --body と --body-file の同時指定は拒否
- --dry-run は 1 行のコマンドを出して終わる(hermes を呼ばない)
- `kanban create --json` の出力から id を取り出せる(JSON と旧来のテキストの両方)
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "create_card.py"
# 環境の設定が無いと epic_common が import で落ちる。make test と同じ既定に合わせる。
os.environ.setdefault("SF_CONFIG", str(REPO / "envs" / "michiomochi-wsl.toml"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import epic_common as C  # noqa: E402
import create_card as cc  # noqa: E402


def run(*args, stdin=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin,
                          text=True, capture_output=True)


class CreateCard(unittest.TestCase):
    def test_default_board_is_the_configured_one(self):
        self.assertIn("--board", cc.build_args("x", "t"))
        r = run("--title", "probe", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--board {C.BOARD}", r.stdout)

    def test_board_flag_overrides(self):
        other = next((b for b in cc.boards() if b != C.BOARD and not b.startswith("_")), None)  # 環境ごとに board 名が違う
        if other is None:
            self.skipTest("設定の board 以外に board が無い")
        r = run("--title", "probe", "--board", other, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--board {other}", r.stdout)

    def test_unknown_board_is_refused(self):
        r = run("--title", "probe", "--board", "no-such-board-xyz", "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not exist", r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_board_is_a_global_flag_before_the_subcommand(self):
        args = cc.build_args("dev", "t")
        self.assertLess(args.index("--board"), args.index("create"),
                        "--board は hermes の全体フラグなので create より前に置く")
        self.assertEqual(args[args.index("--board") + 1], "dev")

    def test_body_and_body_file_together_are_refused(self):
        r = run("--title", "probe", "--body", "x", "--body-file", "y", "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not both", r.stderr)

    def test_dry_run_does_not_call_hermes(self):
        r = run("--title", "probe", "--assignee", "executor", "--tenant", "epic-abc", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("hermes kanban --board "), r.stdout)
        self.assertIn("--assignee executor", r.stdout)
        self.assertIn("--tenant epic-abc", r.stdout)

    def test_model_flags_come_through_build_args_and_the_controller_uses_it(self):
        args = cc.build_args("dev", "t", "executor", 20, "epic-x", "-", ("s1", "s2"), "key-1", model_args=("--model", "m", "--provider", "p"))
        self.assertEqual(args[:5], ["kanban", "--board", "dev", "create", "t"])
        self.assertEqual(args[-5:], ["--model", "m", "--provider", "p", "--json"])
        self.assertEqual(args[args.index("--idempotency-key") + 1], "key-1")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--skill"], ["s1", "s2"])
        import epic_gate_controller as ctl  # the gate controller builds its cards with it (no builder of its own)
        got = ctl.card_args("[x] h", "executor", "epic-x", "k", ["要件定義"])
        self.assertEqual(got, cc.build_args(C.BOARD, "[x] h", "executor", None, "epic-x", "-", ctl.CARD_SKILLS, "k", model_args=C.card_model_args(["要件定義"])))

    def test_parse_created_reads_json_and_text(self):
        self.assertEqual(cc.parse_created('{"id": "t_abc123", "status": "ready"}'), "t_abc123")
        self.assertEqual(cc.parse_created("Created t_def456 (ready, executor)"), "t_def456")
        self.assertIsNone(cc.parse_created("nothing here"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
