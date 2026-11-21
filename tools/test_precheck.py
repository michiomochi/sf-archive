#!/usr/bin/env python3
"""tools/precheck.py の再現テスト(オフライン。一時ディレクトリの git リポジトリだけを使う)。

commit メッセージと PR 本文の下書きにある非公開の印(Notion の URL、設定の data source ID、DB 名)と、
空白の誤りで落ちること。差分の中身にある Notion の URL では落ちないこと(設定やプラグインに正当に入っているため)。
"""
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
CONFIG = REPO / "tests" / "config.test.toml"
with open(CONFIG, "rb") as _f:
    TASK_DS = tomllib.load(_f)["notion"]["task_ds"]


def env():
    e = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL")}
    e.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return e


class PrecheckTest(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_precheck_")
        self.t = Path(self.tmpd.name)
        self.repo = self.t / "r"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def tearDown(self):
        self.tmpd.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, env=env())

    def commit(self, msg, name="b.txt", content="b\n"):
        (self.repo / name).write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def run_pc(self, *extra):
        return subprocess.run([sys.executable, str(TOOLS / "precheck.py"), "--config", str(CONFIG), "--repo", str(self.repo), *extra],
                              capture_output=True, text=True, env=env())

    def test_clean_branch_passes(self):
        self.commit("feat: 印で test を飛ばす")
        r = self.run_pc()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("commit 1 件", r.stdout)

    def test_notion_url_in_commit_message_fails(self):
        self.commit("fix: 詳しくは https://app.notion.com/p/0123456789abcdef0123456789abcdef")
        r = self.run_pc()
        self.assertEqual(r.returncode, 1)
        self.assertIn("app.notion.com", r.stdout)

    def test_db_name_in_commit_message_fails(self):
        self.commit("docs: タスクDBの行を直す")
        self.assertEqual(self.run_pc().returncode, 1)

    def test_data_source_id_without_hyphens_fails(self):
        self.commit("chore: " + TASK_DS.replace("-", ""))
        self.assertEqual(self.run_pc().returncode, 1)

    def test_pr_body_is_scanned(self):
        self.commit("feat: ok")
        body = self.t / "pr.md"
        body.write_text("## 背景\nエピック DB の行で決めた\n")
        r = self.run_pc("--pr-body", str(body))
        self.assertEqual(r.returncode, 1)
        self.assertIn("pr.md:2", r.stdout)
        body.write_text("## 背景\nmake deploy の test が2回走る(実測 14.2 秒)\n")
        self.assertEqual(self.run_pc("--pr-body", str(body)).returncode, 0)

    def test_trailing_whitespace_fails(self):
        self.commit("feat: ok", content="b   \n")
        r = self.run_pc()
        self.assertEqual(r.returncode, 1)
        self.assertIn("git diff --check", r.stdout)

    def test_notion_url_in_file_content_is_not_scanned(self):
        self.commit("feat: プラグインの設定", name="plugin.toml", content='url = "https://example.notion.site/p/8310bf43cc8ef953fbf8e30a9c9ed6f5"\n')
        self.assertEqual(self.run_pc().returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
