#!/usr/bin/env python3
"""scripts/tested_tree.py の検査(オフライン。一時ディレクトリの本物の git だけ)。`make test` から実行する。

「テストした木と出荷する木が同じか」の道具: make test が最後に tested_tree.json(path と head)を書き、
出荷(make ship-tested / ship_approved.py)が deploy の直前に、いまの木の HEAD と照らす。
  T1 write: path(木の絶対パス)と head(HEAD の sha)だけを書く。時刻は入れない。同じ木を2回書いても同じ中身
  T2 write: 追跡ファイルに未コミットの変更がある木は書かない(HEAD の木を試したことにならない)。rc 0 のまま理由を1行出す
  T3 check: head が一致すれば rc 0(別の木・別のパスでも HEAD が同じなら通る)
  T4 check: head が違えば rc 1、メッセージは「tested <sha> / now <sha>。make test を回してから出荷する」
  T5 check: ファイルが無い・壊れている・head が無いときも拒否する(rc 1。何も出荷しない側に倒す)
  T6 verify(): ship_approved が使う関数。(ok, message) を返し、例外を投げない
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import tested_tree as TT  # noqa: E402

TOOL = REPO / "scripts" / "tested_tree.py"
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")


def git(d, *a):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, env=GIT_ENV)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def tool(*args, cwd=None):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=cwd, env=GIT_ENV)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_tested_tree_")
        self.t = Path(self.tmpd.name).resolve()
        self.tree = self.t / "tree"
        self.tree.mkdir()
        git(self.tree, "init", "-q", "-b", "main")
        (self.tree / "a.txt").write_text("one\n")
        git(self.tree, "add", "-A")
        git(self.tree, "commit", "-q", "-m", "init")
        self.head = git(self.tree, "rev-parse", "HEAD")
        self.file = self.t / "state" / "tested_tree.json"

    def tearDown(self):
        self.tmpd.cleanup()

    def commit_more(self):
        (self.tree / "b.txt").write_text("two\n")
        git(self.tree, "add", "-A")
        git(self.tree, "commit", "-q", "-m", "more")
        return git(self.tree, "rev-parse", "HEAD")


class WriteTest(Base):
    def test_writes_path_and_head_only(self):
        r = tool("write", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.file.read_text()), {"path": str(self.tree), "head": self.head})  # 時刻は入れない

    def test_same_tree_twice_gives_the_same_file(self):
        tool("write", "--file", str(self.file), "--tree", str(self.tree))
        first = self.file.read_text()
        tool("write", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(self.file.read_text(), first)

    def test_rewrite_follows_the_new_head(self):
        tool("write", "--file", str(self.file), "--tree", str(self.tree))
        new = self.commit_more()
        tool("write", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(json.loads(self.file.read_text())["head"], new)

    def test_tracked_change_is_not_recorded(self):
        (self.tree / "a.txt").write_text("edited\n")
        r = tool("write", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.file.exists())
        self.assertIn("未コミット", r.stdout + r.stderr)

    def test_untracked_file_alone_does_not_block_the_record(self):
        (self.tree / "scratch.txt").write_text("x\n")
        r = tool("write", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.file.exists())

    def test_not_a_git_tree_fails(self):
        r = tool("write", "--file", str(self.file), "--tree", str(self.t))
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.file.exists())


class CheckTest(Base):
    def record(self, head, path=None):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"path": path or str(self.tree), "head": head}))

    def test_same_head_passes(self):
        self.record(self.head)
        r = tool("check", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_same_head_from_another_path_passes(self):
        self.record(self.head, path="/somewhere/else")
        self.assertEqual(tool("check", "--file", str(self.file), "--tree", str(self.tree)).returncode, 0)

    def test_other_head_is_refused_with_both_shas(self):
        other = "f" * 40
        self.record(other)
        r = tool("check", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(r.returncode, 1)
        text = r.stdout + r.stderr
        self.assertIn(f"tested {other[:12]} / now {self.head[:12]}", text)
        self.assertIn("make test を回してから出荷する", text)

    def test_head_moved_after_the_test_is_refused(self):
        tool("write", "--file", str(self.file), "--tree", str(self.tree))
        self.commit_more()
        self.assertEqual(tool("check", "--file", str(self.file), "--tree", str(self.tree)).returncode, 1)

    def test_explicit_head_overrides_the_trees_head(self):
        self.record(self.head)
        self.assertEqual(tool("check", "--file", str(self.file), "--head", "e" * 40).returncode, 1)
        self.assertEqual(tool("check", "--file", str(self.file), "--head", self.head).returncode, 0)

    def test_missing_corrupt_and_headless_files_are_refused(self):
        r = tool("check", "--file", str(self.file), "--tree", str(self.tree))
        self.assertEqual(r.returncode, 1)
        self.assertIn("make test を回してから出荷する", r.stdout + r.stderr)
        self.file.parent.mkdir(parents=True)
        for body in ("not json", "[]", json.dumps({"path": "/x"}), json.dumps({"path": "/x", "head": ""})):
            with self.subTest(body=body):
                self.file.write_text(body)
                self.assertEqual(tool("check", "--file", str(self.file), "--tree", str(self.tree)).returncode, 1)


class VerifyTest(Base):
    def test_returns_ok_and_message_never_raises(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps({"path": str(self.tree), "head": self.head}))
        self.assertEqual(TT.verify(self.file, self.head), (True, ""))
        ok, msg = TT.verify(self.file, "d" * 40)
        self.assertFalse(ok)
        self.assertIn(f"tested {self.head[:12]} / now {'d' * 12}", msg)
        ok, msg = TT.verify(self.t / "nope.json", self.head)
        self.assertFalse(ok)
        self.assertIn("make test を回してから出荷する", msg)

    def test_default_file_is_under_the_state_dir(self):
        self.assertEqual(TT.default_file("/x/state"), Path("/x/state/tested_tree.json"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
