#!/usr/bin/env python3
"""tools/lint_dollar.py・lint_bash32.py・lint_envs.py の再現テスト(オフライン。一時ディレクトリだけに書く)。`make test` から実行する。

検査そのものが壊れていないことを、わざと壊した入力で確かめる(壊れた入力で落ちない検査は、緑でも何も保証しない):
  - `$` のエスケープ漏れ(`$(cat file)` を `$$` にしていない)→ file:line で落ちる。`$$` なら通る
  - set -u の下の無防備な `"${arr[@]}"` → 落ちる。`+` 形式・分岐・常に非空の配列・コメント行は通る。set -u が無ければ見ない
  - envs: 必須キーの欠け、plugins の不在・重複、`${名前}` の重複、置き場の衝突(live == skills)、環境名と OS の食い違い → 落ちる
  - リポジトリの実物はどれも通る(今日の 3 つの壊れが戻れば、ここと render --all-envs が落ちる)
  - render --all-envs は、1環境でも落ちれば終了コード 1
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO / "scripts"))
import factory  # noqa: E402
import lint_bash32  # noqa: E402
import lint_dollar  # noqa: E402
import lint_envs  # noqa: E402
import skill_lint  # noqa: E402
import sfplugins  # noqa: E402


def run_main(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_lint_checks_")
        self.t = Path(self.tmpd.name)

    def tearDown(self):
        sfplugins._SOURCES.clear()
        self.tmpd.cleanup()

    def write(self, name, text):
        p = self.t / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class DollarTest(TmpCase):
    def test_unescaped_command_substitution_fails_with_file_and_line(self):
        p = self.write("ref.md", "# 見出し\n\n実行: $(cat file)\n")
        rc, _, err = run_main(lint_dollar.main, [str(p)])
        self.assertEqual(rc, 1)
        self.assertIn(f"{p}:3:", err)  # 行番号まで出す(render の総合エラーより先に原因の行が分かる)

    def test_every_bad_line_is_listed(self):
        p = self.write("ref.md", "$(a)\nok ${owner}\n$(b)\n")
        rc, _, err = run_main(lint_dollar.main, [str(p)])
        self.assertEqual(rc, 1)
        self.assertIn(":1:", err)
        self.assertIn(":3:", err)
        self.assertNotIn(":2:", err)

    def test_escaped_and_placeholders_pass(self):
        p = self.write("ref.md", "実行: $$(cat file) と $${HOME} と ${owner} と $$1\n")
        rc, out, _ = run_main(lint_dollar.main, [str(p)])
        self.assertEqual(rc, 0, out)

    def test_repo_templates_have_no_unescaped_dollar(self):
        rc, out, err = run_main(lint_dollar.main, [])
        self.assertEqual(rc, 0, err)
        self.assertGreater(int(out.split("ok lint_dollar: ")[1].split()[0]), 50)  # 全雛形を読んでいる(空の一覧で通らない)

    def test_wrapper_template_is_scanned(self):
        labels = [label for label, _ in lint_dollar.template_sources()]
        self.assertIn("hermes/wrapper.sh.tmpl", labels)
        self.assertTrue(any(label.startswith("plugins/example/") for label in labels))


class Bash32Test(TmpCase):
    def problems(self, text):
        return lint_bash32.check_text(text, "f.sh")

    def test_unguarded_empty_array_fails_with_line(self):
        pr = self.problems('#!/bin/bash\nset -u\narr=()\necho "${arr[@]}"\n')
        self.assertEqual(len(pr), 1)
        self.assertIn("f.sh:4:", pr[0])

    def test_the_real_incident_form_fails(self):
        # test_safe_deploy.sh が bash 3.2 で落ちた書き方(空配列を無防備に env へ渡す)。
        text = '#!/bin/bash\nset -u\nstub() {\n  local env_tries=(); [ -n "${2:-}" ] && env_tries=(A="$2")\n  env X=1 "${env_tries[@]}" cmd\n}\n'
        self.assertEqual(len(self.problems(text)), 1)

    def test_plus_form_passes(self):
        self.assertEqual(self.problems('set -u\narr=()\nenv ${arr[@]+"${arr[@]}"} cmd\n'), [])

    def test_branch_guard_passes(self):
        self.assertEqual(self.problems('set -u\narr=()\nif [ "${#arr[@]}" -gt 0 ]; then\n  run "${arr[@]}"\nfi\n'), [])

    def test_guard_far_above_does_not_count(self):
        text = 'set -u\narr=()\n[ "${#arr[@]}" -gt 0 ]\n:\n:\n:\n:\nrun "${arr[@]}"\n'
        self.assertEqual(len(self.problems(text)), 1)

    def test_literal_nonempty_array_passes_but_emptied_one_does_not(self):
        self.assertEqual(self.problems('set -u\nPY=(python3 -W error)\n"${PY[@]}" x\n'), [])
        self.assertEqual(len(self.problems('set -u\nPY=(python3)\nPY=()\n"${PY[@]}" x\n')), 1)

    def test_read_array_needs_declaration(self):
        text = 'set -u\nread -r -a PY <<< "$X"\n"${PY[@]}" x\n'
        self.assertEqual(len(self.problems(text)), 1)
        self.assertEqual(self.problems('set -u\nread -r -a PY <<< "${X:-python3}"  # nonempty: PY(python3 が入る)\n"${PY[@]}" x\n'), [])

    def test_star_form_is_also_checked(self):
        self.assertEqual(len(self.problems('set -u\narr=()\necho "${arr[*]}"\n')), 1)

    def test_comment_lines_and_ok_mark_are_skipped(self):
        self.assertEqual(self.problems('set -u\narr=()\n# 例: "${arr[@]}" は落ちる\n'), [])
        self.assertEqual(self.problems('set -u\narr=()\nrun "${arr[@]}"  # bash32-ok: 呼び出し側が必ず 1 つ入れる\n'), [])

    def test_without_set_u_nothing_is_checked(self):
        self.assertEqual(self.problems('arr=()\necho "${arr[@]}"\n'), [])
        self.assertEqual(self.problems('arr=()\necho "${arr[@]}"\nset -u\n'), [])  # set -u より前の行は見ない

    def test_set_u_variants_and_set_plus_u(self):
        for head in ("set -eu", "set -euo pipefail", "set -o nounset", "set -u"):
            with self.subTest(head=head):
                self.assertEqual(len(self.problems(f'{head}\narr=()\necho "${{arr[@]}}"\n')), 1)
        self.assertEqual(self.problems('set -u\narr=()\nset +u\necho "${arr[@]}"\n'), [])

    def test_length_and_indices_are_not_expansions_of_elements(self):
        self.assertEqual(self.problems('set -u\narr=()\necho "${#arr[@]}" "${!arr[@]}"\n'), [])

    def test_cli_fails_on_a_broken_fixture_file(self):
        p = self.write("broken.sh", '#!/bin/bash\nset -u\narr=()\necho "${arr[@]}"\n')
        rc, _, err = run_main(lint_bash32.main, [str(p)])
        self.assertEqual(rc, 1)
        self.assertIn("broken.sh:4:", err)
        ok = self.write("fine.sh", '#!/bin/bash\nset -u\narr=()\necho ${arr[@]+"${arr[@]}"}\n')
        self.assertEqual(run_main(lint_bash32.main, [str(ok)])[0], 0)

    def test_repo_shell_scripts_are_clean(self):
        rc, out, err = run_main(lint_bash32.main, [])
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(int(out.split("ok lint_bash32: ")[1].split()[0]), 4)  # safe_deploy / test_safe_deploy / require_main / repo_drift_check


class EnvsTest(TmpCase):
    """tmp に偽のリポジトリ(Makefile・scripts/sfconfig.py・envs/・plugins/)を作って、実物の検査を回す。"""

    def setUp(self):
        super().setUp()
        self.repo = self.t / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(REPO / "scripts" / "sfconfig.py", self.repo / "scripts" / "sfconfig.py")
        shutil.copy(REPO / "Makefile", self.repo / "Makefile")
        (self.repo / "envs").mkdir()
        self.wsl = (REPO / "envs" / "michiomochi-wsl.toml").read_text(encoding="utf-8")
        self.put_env("michiomochi-wsl", self.wsl)
        self.put_env("michiomochi-mac", (REPO / "envs" / "michiomochi-mac.toml").read_text(encoding="utf-8"))
        shutil.copytree(REPO / "plugins" / "example", self.repo / "plugins" / "example")

    def put_env(self, name, text):
        p = self.repo / "envs" / f"{name}.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def lint(self, text, name="t-wsl"):
        p = self.put_env(name, text)
        return lint_envs.lint_env(p, self.repo)

    def test_real_envs_are_healthy(self):
        rc, out, err = run_main(lint_envs.main, [])
        self.assertEqual(rc, 0, err)
        self.assertIn("ok [michiomochi-mac]", out)
        self.assertIn("ok [michiomochi-wsl]", out)

    def test_missing_required_keys_fail(self):
        for key in ("owner", "cron_profile", "task_ds", "deliver", "state"):
            with self.subTest(key=key):
                text = "\n".join(ln for ln in self.wsl.splitlines() if not ln.startswith(f"{key} =") and not ln.startswith(f"{key}="))
                self.assertNotEqual(text, self.wsl)
                problems, _ = self.lint(text)
                self.assertTrue(any(key in pr for pr in problems), problems)

    def test_empty_section_value_fails(self):
        problems, _ = self.lint(self.wsl.replace('deliver = "discord:1473768887694246154"', 'deliver = ""'))
        self.assertTrue(any("deliver" in pr for pr in problems), problems)

    def test_unknown_plugin_fails(self):
        problems, _ = self.lint(self.wsl.replace('owner = "michiomochi"', 'owner = "michiomochi"\nplugins = ["nothing"]'))
        self.assertTrue(any("nothing" in pr for pr in problems), problems)

    def test_duplicate_plugin_fails(self):
        mac = (REPO / "envs" / "michiomochi-mac.toml").read_text(encoding="utf-8")
        problems, _ = self.lint(mac.replace('plugins = ["example"]', 'plugins = ["example", "example"]'))
        self.assertTrue(any("同じ名前" in pr for pr in problems), problems)

    def test_plugin_source_without_checkout_warns_but_passes(self):
        # [plugin_sources] は表なので、先頭ではなく末尾に足す
        text = self.wsl.replace('owner = "michiomochi"', 'owner = "michiomochi"\nplugins = ["ext"]') + f'\n[plugin_sources]\next = "{self.t}/no-such-checkout"\n'
        problems, warns = self.lint(text)
        self.assertEqual(problems, [])
        self.assertTrue(any("ext" in w for w in warns), warns)

    def test_plugin_source_with_checkout_is_accepted(self):
        ext = self.t / "ext"
        ext.mkdir()
        (ext / "plugin.toml").write_text('name = "ext"\ndescription = "d"\n', encoding="utf-8")
        text = self.wsl.replace('owner = "michiomochi"', 'owner = "michiomochi"\nplugins = ["ext"]') + f'\n[plugin_sources]\next = "{ext}"\n'
        problems, warns = self.lint(text)
        self.assertEqual((problems, warns), ([], []))

    def test_duplicate_template_name_fails(self):
        # プラグインの ${名前} が本体の名前(owner)と重なる。
        ext = self.t / "ext"
        ext.mkdir()
        (ext / "plugin.toml").write_text('name = "ext"\ndescription = "d"\n[template_names]\nowner = "k"\n[defaults]\nk = "v"\n', encoding="utf-8")
        text = self.wsl.replace('owner = "michiomochi"', 'owner = "michiomochi"\nplugins = ["ext"]') + f'\n[plugin_sources]\next = "{ext}"\n'
        problems, _ = self.lint(text)
        self.assertTrue(any("重複" in pr and "owner" in pr for pr in problems), problems)

    def test_two_paths_at_one_place_fail(self):
        problems, _ = self.lint(self.wsl.replace('skills = "~/.hermes/shared/skills"', 'skills = "~/.hermes/scripts/sf"'))
        self.assertTrue(any("paths.live" in pr and "paths.skills" in pr for pr in problems), problems)

    def test_path_clash_sees_through_spelling(self):
        problems, _ = self.lint(self.wsl.replace('skills = "~/.hermes/shared/skills"', 'skills = "~/.hermes/scripts/../scripts/sf/"'))
        self.assertTrue(any("paths.live" in pr and "paths.skills" in pr for pr in problems), problems)

    def test_home_equal_to_a_path_fails(self):
        problems, _ = self.lint(self.wsl.replace('state = "~/.hermes/state"', 'state = "~/.hermes"'))
        self.assertTrue(any("hermes.home" in pr and "paths.state" in pr for pr in problems), problems)

    def test_ship_test_without_test_fast_fails(self):
        # ops.py verify が回す fast の検査。test だけあって test_fast が無い登録は落とす。
        text = self.wsl.replace('test_fast = ["make", "test-fast"]\n', "")
        self.assertNotEqual(text, self.wsl)
        problems, _ = self.lint(text)
        self.assertTrue(any("test_fast がありません" in pr and "example-org/sf" in pr for pr in problems), problems)

    def test_ship_test_fast_as_a_string_fails(self):
        text = self.wsl.replace('test_fast = ["make", "test-fast"]', 'test_fast = "make test-fast"')
        self.assertNotEqual(text, self.wsl)
        problems, _ = self.lint(text)
        self.assertTrue(any("test_fast" in pr and "list of strings" in pr for pr in problems), problems)

    def test_ship_without_test_needs_no_test_fast(self):
        text = self.wsl.replace('test = ["make", "test"]\ntest_fast = ["make", "test-fast"]\n', "")
        self.assertNotEqual(text, self.wsl)
        problems, _ = self.lint(text)
        self.assertEqual(problems, [])

    def test_env_name_without_os_fails(self):
        self.put_env("michiomochi-linux", self.wsl)
        rc, _, err = run_main(lambda argv: lint_envs.main(argv, self.repo), [])  # 引数なし = envs/ すべて + Makefile の対応
        self.assertEqual(rc, 1)
        self.assertIn("michiomochi-linux", err)

    def test_makefile_default_pointing_at_the_wrong_os_fails(self):
        mk = (self.repo / "Makefile").read_text(encoding="utf-8")
        self.assertIn(",michiomochi-mac,michiomochi-wsl)", mk)
        (self.repo / "Makefile").write_text(mk.replace(",michiomochi-mac,michiomochi-wsl)", ",michiomochi-wsl,michiomochi-mac)"), encoding="utf-8")
        found = lint_envs.check_env_os_mapping(self.repo)
        self.assertEqual(len(found.get("", [])), 2, found)  # Darwin → wsl、Linux → mac

    def test_makefile_default_pointing_at_a_missing_env_fails(self):
        mk = (self.repo / "Makefile").read_text(encoding="utf-8")
        (self.repo / "Makefile").write_text(mk.replace(",michiomochi-mac,michiomochi-wsl)", ",michiomochi-mac,michiomochi-nothing)"), encoding="utf-8")
        found = lint_envs.check_env_os_mapping(self.repo)
        self.assertTrue(any("無い環境" in m for m in found.get("", [])), found)


class RenderAllEnvsTest(TmpCase):
    def test_one_failing_env_makes_render_fail(self):
        bad = self.write("bad.toml", (REPO / "envs" / "michiomochi-wsl.toml").read_text(encoding="utf-8").replace('owner = "michiomochi"', 'owner = "michiomochi"\nplugins = ["nothing"]'))
        good = REPO / "envs" / "michiomochi-wsl.toml"
        with mock.patch.object(factory, "env_files", lambda repo=REPO: [good, bad]):
            rc, out, err = run_main(factory.main, ["render", "--all-envs", "--dry-run"])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("FAIL [bad]", err)
        self.assertIn("OK [michiomochi-wsl]", out)  # 落ちた環境のあとも、残りを回す

    def test_all_real_envs_render(self):
        rc, out, err = run_main(factory.main, ["render", "--all-envs", "--dry-run"])
        self.assertEqual(rc, 0, err)
        self.assertIn("[michiomochi-mac]", out)
        self.assertIn("[michiomochi-wsl]", out)


class CardTemplateLimitTest(TmpCase):
    """カード雛形(phase_card.md)が 3,000 字を超えたら、どの流し方でも落ちる(--strict-skills / --warn-only / 既定)。
    以前は --strict-skills が雛形の超過を警告だけにして rc=0 のまま通していた(2,999 字 → 3,001 字でも make test が緑)。"""

    def lint(self, size, *flags):
        tmpl = self.write("phase_card.md", "あ" * size)
        with mock.patch.object(skill_lint, "CARD_TEMPLATE", str(tmpl)):
            return run_main(skill_lint.main, list(flags))

    def test_over_limit_fails_in_every_mode(self):
        for flags in (("--strict-skills",), ("--warn-only",), ("--strict-skills", "--warn-only"), ()):
            rc, out, _ = self.lint(skill_lint.MAX_CARD_TEMPLATE + 1, *flags)
            self.assertEqual(rc, 1, f"{flags}: {out}")
            self.assertIn("カード雛形が 3001 字", out)

    def test_at_limit_passes_in_every_mode(self):
        for flags in (("--strict-skills",), ("--warn-only",), ()):
            rc, out, _ = self.lint(skill_lint.MAX_CARD_TEMPLATE, *flags)
            self.assertEqual(rc, 0, f"{flags}: {out}")

    def test_real_template_is_within_limit(self):
        self.assertLessEqual(len(Path(skill_lint.CARD_TEMPLATE).read_text(encoding="utf-8")), skill_lint.MAX_CARD_TEMPLATE)


if __name__ == "__main__":
    unittest.main(verbosity=1)
