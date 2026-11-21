#!/usr/bin/env python3
"""配布の対象(scripts/*.py・scripts/templates/*.md・scripts/tests/*・tests/*.toml)がすべて「本番に置く」か「置かないと明示」のどちらかであることを検査する。

なぜ: tools/safe_deploy.sh の DEFAULT_FILES は明示の一覧で、新しいスクリプトを足しても
一覧に書かなければ本番に配られない（2026-11-11 に create_card.py がこれで配られず、
`~/.hermes/scripts/sf/create_card.py` が存在しないまま make deploy が「反映完了」と言った）。

規則: 追加したスクリプト・雛形・テストデータは DEFAULT_FILES に足す(雛形は templates/<名前>、テストデータは tests/<名前>)。
本番に置かないものは NOT_DEPLOYED に理由付きで書く。どちらにも無ければこのテストが落ちる（人が思い出さなくてよい）。
envs/*.toml は名前では配らない(選んだ環境の1本が config.toml として配られる)ので、DEFAULT_FILES に envs/ が出たら落とす。
"""
import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SAFE_DEPLOY = REPO / "tools" / "safe_deploy.sh"
SCRIPTS = REPO / "scripts"
# 配布の対象 → 一覧での名前(DEFAULT_FILES / NOT_DEPLOYED の書き方)。stage-run(Makefile)が写す範囲と同じ。
TEMPLATES = SCRIPTS / "templates"
TESTDATA = SCRIPTS / "tests"
ROOT_TESTS = REPO / "tests"


def _list(name, text):
    m = re.search(rf'^{name}="([^"]*)"', text, re.M)
    assert m is not None, f"tools/safe_deploy.sh に {name}= が無い"
    return set(m.group(1).split())


def _names(directory, prefix, suffixes):
    return {prefix + f for f in os.listdir(directory) if f.endswith(suffixes)}


class DeployList(unittest.TestCase):
    def setUp(self):
        self.text = SAFE_DEPLOY.read_text(encoding="utf-8")
        self.sh = {f for f in os.listdir(SCRIPTS) if f.endswith(".py")}
        self.templates = _names(TEMPLATES, "templates/", ".md")
        self.testdata = {"tests/" + f for f in os.listdir(TESTDATA) if (TESTDATA / f).is_file()}
        self.root_tests = _names(ROOT_TESTS, "tests/", ".toml")

    def test_every_script_is_classified(self):
        deployed = _list("DEFAULT_FILES", self.text)
        skipped = _list("NOT_DEPLOYED", self.text)
        missing = sorted(self.sh - deployed - skipped)
        self.assertEqual(missing, [], "本番に配られないスクリプトが一覧に無い: " + ", ".join(missing))

    def _assert_classified(self, found, what):
        deployed = _list("DEFAULT_FILES", self.text)
        skipped = _list("NOT_DEPLOYED", self.text)
        missing = sorted(found - deployed - skipped)
        self.assertEqual(missing, [], f"本番に配られない{what}が一覧(DEFAULT_FILES / NOT_DEPLOYED)に無い: " + ", ".join(missing))

    def test_every_template_is_classified(self):
        self.assertTrue(self.templates, "scripts/templates/*.md が見つからない(検査が空振りしている)")
        self._assert_classified(self.templates, "雛形")

    def test_every_test_data_file_is_classified(self):
        self.assertTrue(self.testdata, "scripts/tests/* が見つからない(検査が空振りしている)")
        self._assert_classified(self.testdata, "テストデータ")

    def test_every_config_file_is_classified(self):
        self.assertTrue(self.root_tests, "tests/*.toml が見つからない(検査が空振りしている)")
        self._assert_classified(self.root_tests, "設定ファイル")

    def test_envs_are_never_deployed_by_name(self):
        deployed = _list("DEFAULT_FILES", self.text)
        self.assertIn("config.toml", deployed)  # 選んだ環境の envs/<ENV>.toml は config.toml として配られる
        self.assertIn("levels.toml", deployed)  # scripts/levels.toml(環境で変わらないレベルの定義)は名前で配る。無いと本番の各スクリプトが import で止まる
        by_name = sorted(f for f in deployed if f.startswith("envs/") or (f.endswith(".toml") and f not in ("config.toml", "levels.toml")))
        self.assertEqual(by_name, [], "envs/*.toml は名前では配らない: " + ", ".join(by_name))

    def test_deploy_list_has_no_ghost_template_or_test_data(self):
        deployed = _list("DEFAULT_FILES", self.text)
        known = self.templates | self.testdata
        ghost = sorted(f for f in deployed if f.startswith(("templates/", "tests/")) and f not in known)
        self.assertEqual(ghost, [], "DEFAULT_FILES にあるが scripts/templates・scripts/tests に無い: " + ", ".join(ghost))

    def test_deploy_list_has_no_ghost_script(self):
        deployed = _list("DEFAULT_FILES", self.text)
        ghost = sorted(f for f in (deployed - self.sh) if f.endswith(".py"))
        self.assertEqual(ghost, [], "DEFAULT_FILES にあるが scripts/ に無い: " + ", ".join(ghost))

    def test_not_deployed_and_deployed_are_disjoint(self):
        deployed = _list("DEFAULT_FILES", self.text)
        skipped = _list("NOT_DEPLOYED", self.text)
        self.assertEqual(sorted(deployed & skipped), [], "両方に載っているファイルがある")


if __name__ == "__main__":
    unittest.main(verbosity=1)
