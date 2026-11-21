#!/usr/bin/env python3
"""scripts/ops.py の検査(オフライン。gh は PATH の先頭の偽実行ファイル、git は一時ディレクトリの本物)。`make test` から実行する。

確かめること:
- gh の -R が config の [git].repo_url から組み立てられる(config に無い repo を指さない)
- pr-create は、未コミットの変更・base と同じブランチ・未 push・既存の PR を拒否する(終了コード 2、gh pr create を呼ばない)
- 本文はファイルか stdin で渡り、gh のコマンドラインに本文が入らない(バッククォートや $() が壊れない)
- pr-merge は OPEN 以外を拒否し、成功すると merged と main の先頭コミットを出す
- worktree は既存の worktree を拒否し、作ると .worktrees/<slug> と <branch_prefix><slug> を出す
- lint は 0 件以外で非 0、render-all は 1 環境でも失敗したら非 0
- どれも --dry-run は実行するコマンドを出して終わる(gh の書き込みも git の書き込みも走らない)
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ops.py"
os.environ.setdefault("SF_CONFIG", str(REPO / "envs" / "michiomochi-wsl.toml"))
sys.path.insert(0, str(REPO / "scripts"))

import epic_common as C  # noqa: E402

SLUG = C.CFG["git"]["repo_url"].removesuffix(".git").split("github.com/", 1)[1]  # owner/repo
PREFIX = C.CFG["git"]["branch_prefix"]

# 偽の gh: 引数と stdin を FAKE_LOG に1行の JSON で足し、サブコマンドごとに環境変数の値を返す。
FAKE_GH = textwrap.dedent('''\
    #!%s
    import json, os, sys
    argv = sys.argv[1:]
    stdin = sys.stdin.read() if "-" in argv and not sys.stdin.isatty() else None
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv, "stdin": stdin}) + "\\n")
    if argv[:2] == ["pr", "list"]:
        print(os.environ.get("FAKE_PR_LIST", "[]"))
    elif argv[:2] == ["pr", "view"]:
        print(os.environ.get("FAKE_PR_VIEW", '{"state": "OPEN"}'))
    elif argv[:2] == ["pr", "create"]:
        print("https://github.com/fake/fake/pull/1")
    elif argv[:2] == ["pr", "merge"]:
        pass
    else:
        sys.exit(9)
''' % sys.executable)


def sh(*cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()  # macOS の /var は /private/var への symlink
        self.bin = t / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = t / "gh.log"
        self.log.write_text("")
        # origin(bare) と、その clone。clone の main に1コミット。
        self.origin = t / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.work = t / "work"
        sh("git", "clone", "-q", str(self.origin), str(self.work))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sh("git", "config", k, v, cwd=self.work)
        (self.work / "a.txt").write_text("a\n")
        sh("git", "add", "a.txt", cwd=self.work)
        sh("git", "commit", "-q", "-m", "init", cwd=self.work)
        sh("git", "push", "-q", "origin", "main", cwd=self.work)
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log)}

    def tearDown(self):
        self.tmp.cleanup()

    def ops(self, *args, cwd=None, stdin=None, **env):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.work, input=stdin,
                              text=True, capture_output=True, env={**self.env, **env})

    def gh_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def gh_subcommands(self):
        return [" ".join(c["argv"][:2]) for c in self.gh_calls()]

    def feature_branch(self, push=True):
        sh("git", "switch", "-q", "-c", "feat/x", cwd=self.work)
        (self.work / "b.txt").write_text("b\n")
        sh("git", "add", "b.txt", cwd=self.work)
        sh("git", "commit", "-q", "-m", "feat", cwd=self.work)
        if push:
            sh("git", "push", "-q", "-u", "origin", "feat/x", cwd=self.work)

    def body(self, text="本文 `code` $(echo boom) \"quoted\"\n"):
        p = Path(self.tmp.name) / "body.md"
        p.write_text(text)
        return str(p)


class PrCreate(Base):
    def test_repo_flag_comes_from_config(self):
        self.feature_branch()
        r = self.ops("pr-create", "--title", "T", "--body-file", self.body())
        self.assertEqual(r.returncode, 0, r.stderr)
        create = [c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"]][0]
        self.assertEqual(create[create.index("-R") + 1], SLUG)
        self.assertEqual(create[create.index("--head") + 1], "feat/x")
        self.assertEqual(create[create.index("--base") + 1], "main")
        # 既存 PR の確認も同じ -R で行う
        lst = [c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["pr", "list"]][0]
        self.assertEqual(lst[lst.index("-R") + 1], SLUG)
        self.assertEqual(r.stdout.strip(), "https://github.com/fake/fake/pull/1")

    def test_body_goes_through_file_not_command_line(self):
        self.feature_branch()
        body = self.body()
        r = self.ops("pr-create", "--title", "T", "--body-file", body)
        self.assertEqual(r.returncode, 0, r.stderr)
        create = [c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"]][0]
        self.assertEqual(Path(create[create.index("--body-file") + 1]).resolve(), Path(body).resolve())  # macOS の /var と /private/var
        self.assertNotIn("--body", create)
        self.assertFalse(any("$(echo boom)" in a or "`code`" in a for a in create), create)

    def test_body_from_stdin(self):
        self.feature_branch()
        text = "stdin の本文 `x` $(y)\n"
        r = self.ops("pr-create", "--title", "T", "--body-file", "-", stdin=text)
        self.assertEqual(r.returncode, 0, r.stderr)
        call = [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"]][0]
        self.assertEqual(call["argv"][call["argv"].index("--body-file") + 1], "-")
        self.assertEqual(call["stdin"], text)
        self.assertFalse(any("`x`" in a for a in call["argv"]))

    def test_there_is_no_body_option(self):
        self.feature_branch()
        r = self.ops("pr-create", "--title", "T", "--body", "直接渡す")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.gh_subcommands(), [])

    def test_draft_and_base_are_passed(self):
        self.feature_branch()
        r = self.ops("pr-create", "--title", "T", "--body-file", self.body(), "--draft", "--base", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        create = [c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["pr", "create"]][0]
        self.assertIn("--draft", create)

    def assert_refused(self, r, needle):
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(needle, r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertNotIn("pr create", self.gh_subcommands())

    def test_uncommitted_changes_are_refused(self):
        self.feature_branch()
        (self.work / "a.txt").write_text("dirty\n")
        self.assert_refused(self.ops("pr-create", "--title", "T", "--body-file", self.body()), "未コミット")

    def test_untracked_files_are_refused(self):
        self.feature_branch()
        (self.work / "new.txt").write_text("x\n")
        self.assert_refused(self.ops("pr-create", "--title", "T", "--body-file", self.body()), "未コミット")

    def test_pr_from_main_is_refused(self):
        self.assert_refused(self.ops("pr-create", "--title", "T", "--body-file", self.body()), "base")

    def test_unpushed_branch_is_refused(self):
        self.feature_branch(push=False)
        self.assert_refused(self.ops("pr-create", "--title", "T", "--body-file", self.body()), "push")

    def test_unpushed_commits_on_pushed_branch_are_refused(self):
        self.feature_branch()
        (self.work / "c.txt").write_text("c\n")
        sh("git", "add", "c.txt", cwd=self.work)
        sh("git", "commit", "-q", "-m", "more", cwd=self.work)
        self.assert_refused(self.ops("pr-create", "--title", "T", "--body-file", self.body()), "push")

    def test_existing_open_pr_is_refused(self):
        self.feature_branch()
        r = self.ops("pr-create", "--title", "T", "--body-file", self.body(),
                     FAKE_PR_LIST='[{"number": 5, "url": "https://github.com/x/y/pull/5"}]')
        self.assert_refused(r, "既に開いている PR")
        self.assertIn("pull/5", r.stderr)

    def test_missing_body_file_is_refused(self):
        self.feature_branch()
        self.assert_refused(self.ops("pr-create", "--title", "T", "--body-file", "/no/such/file.md"), "本文")

    def test_empty_title_is_refused(self):
        self.feature_branch()
        self.assert_refused(self.ops("pr-create", "--title", "", "--body-file", self.body()), "title")

    def test_dry_run_prints_command_and_writes_nothing(self):
        self.feature_branch()
        r = self.ops("pr-create", "--title", "T", "--body-file", self.body(), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"gh pr create -R {SLUG}", r.stdout)
        self.assertNotIn("pr create", self.gh_subcommands())


class PrMerge(Base):
    def test_merges_open_pr_with_repo_flag(self):
        r = self.ops("pr-merge", "77")
        self.assertEqual(r.returncode, 0, r.stderr)
        merge = [c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["pr", "merge"]][0]
        self.assertEqual(merge[merge.index("-R") + 1], SLUG)
        self.assertIn("77", merge)
        self.assertIn("--squash", merge)
        lines = r.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith("merged"), r.stdout)
        self.assertIn("init", lines[1])  # origin/main の先頭コミット

    def test_method_can_be_changed(self):
        r = self.ops("pr-merge", "77", "--method", "rebase")
        self.assertEqual(r.returncode, 0, r.stderr)
        merge = [c["argv"] for c in self.gh_calls() if c["argv"][:2] == ["pr", "merge"]][0]
        self.assertIn("--rebase", merge)

    def test_non_open_pr_is_refused(self):
        for state in ("MERGED", "CLOSED"):
            self.log.write_text("")
            r = self.ops("pr-merge", "77", FAKE_PR_VIEW=json.dumps({"state": state}))
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(state, r.stderr)
            self.assertNotIn("pr merge", self.gh_subcommands())

    def test_dry_run_does_not_merge(self):
        r = self.ops("pr-merge", "77", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"gh pr merge 77 -R {SLUG}", r.stdout)
        self.assertNotIn("pr merge", self.gh_subcommands())


class Worktree(Base):
    def test_creates_worktree_from_origin_main(self):
        r = self.ops("worktree", "foo")
        self.assertEqual(r.returncode, 0, r.stderr)
        path = self.work / ".worktrees" / "foo"
        self.assertTrue(path.is_dir())
        self.assertIn(str(path), r.stdout)
        self.assertIn(f"{PREFIX}foo", r.stdout)
        self.assertEqual(sh("git", "branch", "--show-current", cwd=path).stdout.strip(), f"{PREFIX}foo")
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=path).stdout, sh("git", "rev-parse", "origin/main", cwd=self.work).stdout)

    def test_fetches_before_branching(self):
        # origin に先に進んだコミットがあっても、fetch してから切る
        other = Path(self.tmp.name) / "other"
        sh("git", "clone", "-q", str(self.origin), str(other))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sh("git", "config", k, v, cwd=other)
        (other / "n.txt").write_text("n\n")
        sh("git", "add", "n.txt", cwd=other)
        sh("git", "commit", "-q", "-m", "newer", cwd=other)
        sh("git", "push", "-q", "origin", "main", cwd=other)
        r = self.ops("worktree", "foo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sh("git", "log", "-1", "--format=%s", cwd=self.work / ".worktrees" / "foo").stdout.strip(), "newer")

    def test_existing_worktree_is_refused(self):
        self.assertEqual(self.ops("worktree", "foo").returncode, 0)
        r = self.ops("worktree", "foo")
        self.assertEqual(r.returncode, 2)
        self.assertIn("既に", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_works_from_inside_another_worktree(self):
        self.assertEqual(self.ops("worktree", "foo").returncode, 0)
        r = self.ops("worktree", "bar", cwd=self.work / ".worktrees" / "foo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.work / ".worktrees" / "bar").is_dir())  # 入れ子にならず、本体の .worktrees に切る

    def test_bad_slug_is_refused(self):
        for bad in ("a/b", "A B", "..", "-x", ""):
            r = self.ops("worktree", bad)
            self.assertEqual(r.returncode, 2, (bad, r.stderr))

    def test_dry_run_creates_nothing(self):
        r = self.ops("worktree", "foo", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("git fetch origin", r.stdout)
        self.assertIn(f"git worktree add {self.work / '.worktrees' / 'foo'} -B {PREFIX}foo origin/main", r.stdout)
        self.assertFalse((self.work / ".worktrees").exists())


class Lint(Base):
    def skills_repo(self, rc, out):
        d = Path(self.tmp.name) / "skills-repo"
        (d / "tools").mkdir(parents=True)
        (d / "tools" / "skill_lint.py").write_text(
            "import sys\nprint(%r)\nprint('ARGS ' + ' '.join(sys.argv[1:]))\nsys.exit(%d)\n" % (out, rc))
        return str(d)

    def test_clean_is_zero_and_args_are_fixed(self):
        r = self.ops("lint", "--skills-repo", self.skills_repo(0, "skill-lint: 0 件"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS --strict-skills --skills-dir skills", r.stdout)

    def test_findings_are_nonzero(self):
        r = self.ops("lint", "--skills-repo", self.skills_repo(1, "skill-lint: 2 件"))
        self.assertEqual(r.returncode, 1)

    def test_findings_are_nonzero_even_if_linter_exits_zero(self):
        r = self.ops("lint", "--skills-repo", self.skills_repo(0, "skill-lint: 2 件"))
        self.assertEqual(r.returncode, 1)

    def test_missing_skills_repo_is_refused(self):
        r = self.ops("lint", "--skills-repo", "/no/such/skills-repo")
        self.assertEqual(r.returncode, 2)
        self.assertIn("skill_lint.py", r.stderr)

    @unittest.skipUnless(os.path.isfile(os.path.expanduser("~/ghq/github.com/example-org/skills/tools/skill_lint.py")), "needs the separate skills repository, which is not part of this archive")
    def test_default_skills_repo_is_under_ghq(self):
        r = self.ops("lint", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.path.expanduser("~/ghq/github.com/example-org/skills/tools/skill_lint.py"), r.stdout)
        self.assertIn("--strict-skills --skills-dir skills", r.stdout)


class RenderAll(Base):
    def factory(self, rc):
        (self.work / "tools").mkdir(exist_ok=True)
        (self.work / "tools" / "factory.py").write_text(
            "import sys\nprint('ARGS ' + ' '.join(sys.argv[1:]))\nsys.exit(%d)\n" % rc)

    def test_passes_through_zero(self):
        self.factory(0)
        r = self.ops("render-all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS render --all-envs --dry-run", r.stdout)

    def test_one_failed_env_is_nonzero(self):
        self.factory(1)
        r = self.ops("render-all")
        self.assertEqual(r.returncode, 1)

    def test_dry_run_prints_command(self):
        self.factory(1)
        r = self.ops("render-all", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tools/factory.py render --all-envs --dry-run", r.stdout)
        self.assertNotIn("ARGS", r.stdout)


class Verify(Base):
    """ops.py verify: 登録の test_fast を回し、変えたパスに合う重い検査(make test-paths)を回して、作業メモに貼る1行を出す。"""
    REPO_SLUG = "o/registered"

    def setUp(self):
        super().setUp()
        self.set_origin(self.REPO_SLUG)
        self.fast("print('fast-ran')")

    def set_origin(self, slug):
        sh("git", "remote", "set-url", "origin", f"https://github.com/{slug}.git", cwd=self.work)

    def fast(self, code, registered=True):
        """設定の test_fast を、python -c <code> にした設定ファイルを作って SF_CONFIG にする(本番の設定のコピー + 偽の repo の登録)。"""
        text = (REPO / "envs" / "michiomochi-wsl.toml").read_text(encoding="utf-8")
        reg = f'\n[ship.repos."{self.REPO_SLUG}"]\ntest = ["make", "test"]\n' + (f'test_fast = ["{sys.executable}", "-c", {json.dumps(code)}]\n' if registered else "")
        p = Path(self.tmp.name) / "config.toml"
        p.write_text(text + reg, encoding="utf-8")
        self.cfg = str(p)

    def committed(self, rel, text):
        (self.work / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.work / rel).write_text(text)
        sh("git", "add", rel, cwd=self.work)
        sh("git", "commit", "-q", "-m", f"add {rel}", cwd=self.work)
        sh("git", "push", "-q", str(self.origin), "main", cwd=self.work)  # origin の URL は github.com の形に変えてあるので、実体(bare)へ直接
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.work)

    def plan_files(self, paths_rc=0):
        self.committed("tools/test_plan.py", "# fake\n")
        self.committed("Makefile", f"test-paths:\n\t@echo PATHS=$(PATHS)\n\t@echo 該当なし\n\t@exit {paths_rc}\n")

    def verify(self, *args, **kw):
        return self.ops("verify", *args, SF_CONFIG=self.cfg, **kw)

    def test_runs_the_registered_test_fast_and_prints_one_line(self):
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fast-ran", r.stdout)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("verify: ")]
        self.assertEqual(len(line), 1, r.stdout)
        self.assertRegex(line[0], r"^verify: .*-c .*rc=0 wall=[\d.]+s cpu=[\d.]+s")
        self.assertNotIn("test-paths", line[0])  # tools/test_plan.py の無い repo では test-paths 自体を回さない

    def test_failing_test_fast_is_nonzero_and_still_prints_the_line_with_its_rc(self):
        self.fast("raise SystemExit(3)")
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"verify: .*rc=3 wall=")
        self.assertIn("verify が落ちました", r.stderr)

    def test_cpu_time_is_cpu_not_wall_clock(self):
        self.fast("import time; t = time.process_time()\nwhile time.process_time() - t < 0.4: pass")
        r = self.verify()
        m1 = re.search(r"cpu=([\d.]+)s", r.stdout)
        self.assertIsNotNone(m1, r.stdout)
        cpu = float(m1.group(1))
        self.fast("import time; time.sleep(0.8)")
        r2 = self.verify()
        m2 = re.search(r"wall=([\d.]+)s cpu=([\d.]+)s", r2.stdout)
        self.assertIsNotNone(m2, r2.stdout)
        wall2, cpu2 = float(m2.group(1)), float(m2.group(2))
        self.assertGreaterEqual(cpu, 0.35)
        self.assertGreaterEqual(wall2, 0.75)
        self.assertLess(cpu2, 0.35)  # 待つだけなら CPU は少ない

    def test_runs_test_paths_with_the_changed_files_after_test_fast(self):
        self.plan_files()
        self.feature_branch(push=False)  # b.txt を足す commit
        (self.work / "c.txt").write_text("untracked\n")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(r.stdout.index("fast-ran"), r.stdout.index("PATHS="))
        self.assertIn("PATHS=b.txt c.txt", r.stdout)  # commit 済みと未追跡の両方(base は origin/main)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("verify: ")][0]
        self.assertIn("make test-paths(変更 2 件, 該当なし) rc=0", line)

    def test_failing_test_paths_is_nonzero(self):
        self.plan_files(paths_rc=4)
        self.feature_branch(push=False)
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("make test-paths(変更 1 件, 該当なし) rc=2", r.stdout)  # make が recipe の失敗を rc=2 にする
        self.assertIn("verify が落ちました", r.stderr)

    def test_no_change_runs_no_test_paths(self):
        self.plan_files()
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("PATHS=", r.stdout)
        self.assertIn("make test-paths(変更なし)", r.stdout)

    def test_unregistered_repo_says_so_and_runs_the_default_make_test(self):
        self.set_origin("o/unregistered")
        self.committed("Makefile", "test:\n\t@echo default-test-ran\n")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("default-test-ran", r.stdout)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("verify: ")][0]
        self.assertIn("o/unregistered は ship.repos に未登録", line)
        self.assertIn("既定の make test", line)
        self.assertIn("make test rc=0", line)
        self.assertNotIn("test-paths", line)  # 登録の無い repo では、paths の検査は回さない

    def test_registered_without_test_fast_falls_back_and_says_so(self):
        self.fast("print('never')", registered=False)
        self.committed("Makefile", "test:\n\t@echo default-test-ran\n")
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("never", r.stdout)
        self.assertIn("登録はあるが test_fast が無い", r.stdout)

    def test_unregistered_repo_without_a_makefile_is_refused(self):
        self.set_origin("o/unregistered")
        r = self.verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("Makefile", r.stderr)

    def test_dry_run_prints_the_commands_and_runs_nothing(self):
        self.plan_files()
        self.feature_branch(push=False)
        r = self.verify("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-c", r.stdout)
        self.assertIn("+ make test-paths PATHS=b.txt", r.stdout)
        self.assertNotIn("fast-ran", r.stdout.splitlines())  # コマンドの表示には出るが、実行した出力の行は無い
        self.assertNotIn("PATHS=b.txt", r.stdout.splitlines())
        self.assertNotIn("verify: ", r.stdout)

    def test_no_origin_is_refused(self):
        sh("git", "remote", "remove", "origin", cwd=self.work)
        r = self.verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("origin", r.stderr)

    def test_missing_base_is_refused_not_ignored(self):
        self.plan_files()
        r = self.verify("--base", "origin/no-such-branch")
        self.assertEqual(r.returncode, 2)
        self.assertIn("共通祖先", r.stderr)


class Deployed(unittest.TestCase):
    def test_ops_py_is_in_safe_deploy_default_files(self):
        # 本番に配る一覧は明示で、書き忘れると配られない(create_card.py がこれで配られなかった)。
        text = (REPO / "tools" / "safe_deploy.sh").read_text(encoding="utf-8")
        files = re.search(r'DEFAULT_FILES="([^"]*)"', text).group(1).split()
        self.assertIn("ops.py", files)


class Cli(Base):
    def test_no_subcommand_is_an_argument_error(self):
        self.assertEqual(self.ops().returncode, 2)

    def test_outside_a_git_repo_is_refused(self):
        r = self.ops("worktree", "foo", cwd=Path(self.tmp.name) / "bin")
        self.assertEqual(r.returncode, 2)
        self.assertIn("git", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
