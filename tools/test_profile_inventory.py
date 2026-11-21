#!/usr/bin/env python3
"""scripts/profile_inventory.py の共有スキル置き場の検査(オフライン。一時ディレクトリだけに書く。`make test` から実行する)。

確かめること:
- 共有スキルは <HOME>/shared/skills から読まれ、一覧に出る(旧 shared-skills を見て MISSING になっていた)
- skills.external_dirs にその置き場がある profile は「uses shared external_dir: True」になる
- external_dirs に無い profile は「not using the shared」と報告される(検査が常に緑にならない確認)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "profile_inventory.py"


@unittest.skipUnless(shutil.which("ruby"), "profile_inventory.py は YAML の読み込みに ruby を使う")
class SharedSkillsDir(unittest.TestCase):
    def run_inventory(self, external_dir):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            skill = home / "shared" / "skills" / "foo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# foo\n")
            (home / "config.yaml").write_text(f"skills:\n  external_dirs:\n    - {external_dir.format(home=home)}\n")
            r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                               env={**os.environ, "INVENTORY_HOME": str(home)})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_shared_skills_are_found_and_used(self):
        out = self.run_inventory("{home}/shared/skills")
        self.assertIn("-> ['foo']", out)
        self.assertNotIn("MISSING", out)
        self.assertIn("uses shared external_dir: True", out)
        self.assertNotIn("not using the shared", out)
        self.assertNotIn("only in", out)

    def test_profile_not_listing_the_dir_is_reported(self):
        out = self.run_inventory("{home}/elsewhere")
        self.assertIn("uses shared external_dir: False", out)
        self.assertIn("not using the shared", out)


class ReviewPairsLine(unittest.TestCase):
    """The daily standup carries check_review_pairs.py's verdict as one line, and nothing when all is well (needs no ruby: a profile whose config cannot be read is only reported)."""

    def run_inventory(self, live_script):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "live").mkdir()
            (home / "live" / "check_review_pairs.py").write_text(live_script)  # a stand-in: the real one reads Notion
            r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                               env={**os.environ, "INVENTORY_HOME": str(home), "SF_LIVE": str(home / "live")})
            self.assertEqual(r.returncode, 0, r.stderr)
            return [l for l in r.stdout.splitlines() if "review pairs" in l]

    def test_something_wrong_is_one_line(self):
        out = self.run_inventory('print("MISSING a b c"); print("review pairs: 3 missing"); print("review pairs: 1 stale"); raise SystemExit(1)')
        self.assertEqual(out, ["- review pairs: 3 missing, 1 stale (python3 check_review_pairs.py lists them)"])

    def test_nothing_wrong_is_silent(self):
        self.assertEqual(self.run_inventory('print("review pairs: 0 missing")'), [])

    def test_unreadable_notion_is_reported(self):
        self.assertEqual(self.run_inventory('print("ERROR notion down"); raise SystemExit(2)'), ["- review pairs: check failed (ERROR notion down)"])


if __name__ == "__main__":
    unittest.main()
