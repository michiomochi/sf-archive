#!/usr/bin/env python3
"""tools/rename_to_sf.py のオフラインのテスト。`make test` から実行する。

本物のリポジトリ・本番(~/.hermes)・kanban・cron には触れない: 一時ディレクトリに、bare の origin、旧リポジトリ(worktree 2本)、
旧本番、偽の board DB を作り、make は呼ばれた順を記録するだけの偽物にする。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "rename_to_sf.py"

FAKE_MAKE = r'''
import json, os, sqlite3, sys
args = sys.argv[1:]
d = args[args.index("-C") + 1] if "-C" in args else os.getcwd()
targets = [a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] != "-C") and "=" not in a]
t = targets[0] if targets else ""
with open(os.environ["FAKE_MAKE_LOG"], "a") as f:
    f.write(json.dumps([os.path.realpath(d), t] + [a for a in args if "=" in a and not a.startswith("ENV_FILE=")]) + "\n")
env_file = next((a.split("=", 1)[1] for a in args if a.startswith("ENV_FILE=")), None)
dry = "FLAGS=--dry-run" in args
fail_t, _, fail_dir = os.environ.get("FAKE_MAKE_FAIL", "").partition("@")  # <ターゲット>[@<ディレクトリ名>]
if t and t == fail_t and (not fail_dir or os.path.basename(os.path.realpath(d)) == fail_dir):
    print("偽の make: 失敗させる", t)
    sys.exit(2)
if t == "check" and env_file and os.environ.get("FAKE_MAKE_ENV_SEEN"):  # ガード 6 が渡した設定の写しを残す
    open(os.environ["FAKE_MAKE_ENV_SEEN"], "a").write(open(env_file).read())
if t == "check" and os.environ.get("FAKE_MAKE_CHECK_OUT_" + os.path.basename(os.path.realpath(d))):
    print(os.environ["FAKE_MAKE_CHECK_OUT_" + os.path.basename(os.path.realpath(d))])
    sys.exit(2)


def ids_path():
    import tomllib
    return os.path.join(tomllib.loads(open(os.path.join(d, "envs/michiomochi-wsl.toml")).read())["paths"]["live"], "cron_ids.json")


# 本番の cron 台帳の代わり(FAKE_MAKE_JOBS: キー -> ジョブ ID)。FAKE_MAKE_CRON_WANT は cron.toml が定義するキー(カンマ区切り)
jobs_file, want = os.environ.get("FAKE_MAKE_JOBS"), [k for k in os.environ.get("FAKE_MAKE_CRON_WANT", "").split(",") if k]
if jobs_file and t == "cron-adopt":
    pairs = dict(p.split("=", 1) for a in args if a.startswith("IDS=") for p in a[4:].split())
    ids = json.load(open(ids_path()))
    json.dump({**ids, **pairs}, open(ids_path(), "w"))
    print(f"{len(ids) + len(pairs)} 件を保存")
if jobs_file and t == "cron-sync":
    jobs, ids = json.load(open(jobs_file)), json.load(open(ids_path()))
    clash = [k for k in want if k not in ids and k in jobs]
    if clash:  # factory.py の plan_sync と同じ文言で、何も変えずに止まる(dry-run でも)
        print("sync を中止しました(何も変更していません):", file=sys.stderr)
        for k in clash:
            print(f"  {k}: 同じ名前のジョブ {jobs[k]}「{k}」が既にあります。重複して作らず、`factory.py cron adopt {k}={jobs[k]}` で取り込んでください", file=sys.stderr)
        sys.exit(1)
    if not dry:
        for k in [k for k in want if k not in ids]:
            if k in os.environ.get("FAKE_MAKE_NEEDS_WRAPPER", "").split(",") and not os.path.exists(os.path.join(d, ".fake_wrappers_done")):
                print(f"  NG  create {k}: Failed to create job: Script file not found: sf-{k}.sh")
                sys.exit(1)
            jobs[k] = ids[k] = "new-" + k
        json.dump(jobs, open(jobs_file, "w"))
        json.dump(ids, open(ids_path(), "w"))
if jobs_file and t == "deploy":  # sync_live: 指示文を配るジョブが台帳に無いと失敗にする
    missing = [k for k in want if k not in json.load(open(jobs_file))]
    if missing:
        print(f"差分 [cron] {missing[0]}: ジョブ「{missing[0]}」が見つからない")
        print(f"反映後も差分が残っています: {missing}")
        sys.exit(2)
if t == "wrappers":
    open(os.path.join(d, ".fake_wrappers_done"), "w").write("")
if t == "render":  # 置換後の SOUL を build/ に出す(本物の make render と同じ置き場所)
    for prof in ("executor", "reviewer"):
        p = os.path.join(d, "build/michiomochi-wsl/hermes/profiles", prof, "SOUL.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(f"SOUL {prof} from {os.path.realpath(d)}\n")
if t == "cron-sync":
    print(os.environ.get("FAKE_MAKE_CRON_SYNC", "DRY-RUN 要約: 一致 7 / 変更 0 / 作成 0  (何も変更していません)"))
    if "FLAGS=--dry-run" not in args and os.environ.get("FAKE_MAKE_CRON_CREATE"):  # 本番に無いジョブを作る(cron_ids.json に足す)
        import tomllib
        live = tomllib.loads(open(os.path.join(d, "envs/michiomochi-wsl.toml")).read())["paths"]["live"]
        p = os.path.join(live, "cron_ids.json")
        ids = json.load(open(p))
        ids[os.environ["FAKE_MAKE_CRON_CREATE"]] = "newjob000001"
        json.dump(ids, open(p, "w"))
if t == "cron-pause" and os.environ.get("FAKE_MAKE_CARD_ON_PAUSE"):
    c = sqlite3.connect(os.environ["FAKE_MAKE_CARD_ON_PAUSE"])
    c.execute("insert into tasks values ('t_late', 'running', '止める前に動き出したカード')")
    c.commit()
'''

GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main",
       "-c", "advice.detachedHead=false"]


def git(*a, cwd=None):
    r = subprocess.run(GIT + list(a), cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {a}: {r.stderr}")
    return r.stdout


def env_toml(repo, live):
    home = Path(repo).parent.parent / "home"
    return (f'[hermes]\nhome = "{home}"\n\n[paths]\nrepo = "{repo}"\nlive = "{live}"\n\n'
            f'[git]\nrepo_url = "https://example.invalid/org/{Path(repo).name}"\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="test_rename_to_sf_")
        t = Path(self.tmpd.name).resolve()
        self.t = t
        self.old_repo, self.new_repo = t / "ghq/software-factory", t / "ghq/sf"
        self.old_live, self.new_live = t / "scripts/software-factory", t / "scripts/sf"
        self.board, self.state, self.mlog = t / "kanban.db", t / "state/rename.json", t / "make.log"
        origin = t / "origin.git"
        git("init", "--bare", "-q", str(origin))
        git("clone", "-q", str(origin), str(self.old_repo))
        (self.old_repo / "envs").mkdir()
        (self.old_repo / "envs/michiomochi-wsl.toml").write_text(env_toml(self.old_repo, self.old_live))
        git("add", "-A", cwd=self.old_repo)
        git("commit", "-qm", "init", cwd=self.old_repo)
        git("push", "-q", "origin", "main", cwd=self.old_repo)
        self.pre_sha = git("rev-parse", "main", cwd=self.old_repo).strip()
        with open(self.old_repo / ".git/info/exclude", "a") as f:  # 本物と同じく .worktrees/ は追跡しない
            f.write(".worktrees/\n")
        for name in ("a", "b"):
            git("worktree", "add", "-q", "-b", f"michiomochi/{name}", str(self.old_repo / ".worktrees" / name), cwd=self.old_repo)
            (self.old_repo / ".worktrees" / name / f"{name}.txt").write_text(name)  # 未追跡ファイルも一緒に動くこと
        other = t / "other"  # PR のマージ(origin/main だけが進む)
        git("clone", "-q", str(origin), str(other))
        (other / "envs/michiomochi-wsl.toml").write_text(env_toml(self.new_repo, self.new_live))
        git("commit", "-qam", "rename", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        for prof in ("executor", "reviewer"):
            (t / "home/profiles" / prof).mkdir(parents=True)
            (t / "home/profiles" / prof / "SOUL.md").write_text("old soul\n")
        self.old_live.mkdir(parents=True)
        (self.old_live / "cron_ids.json").write_text("{}")
        c = sqlite3.connect(self.board)
        c.execute("create table tasks (id text, status text, title text)")
        c.execute("insert into tasks values ('t_done', 'done', '終わったカード')")
        c.commit()
        c.close()
        fm = t / "fake_make.py"
        fm.write_text(FAKE_MAKE)
        self.make = f"{sys.executable} {fm}"
        self.env = dict(os.environ, FAKE_MAKE_LOG=str(self.mlog))
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.tmpd.cleanup()

    def run_script(self, *extra, env=None):
        cmd = [sys.executable, str(SCRIPT), *extra, "--old-repo", str(self.old_repo), "--new-repo", str(self.new_repo),
               "--old-live", str(self.old_live), "--new-live", str(self.new_live), "--board-db", str(self.board),
               "--state-file", str(self.state), "--make", self.make]
        r = subprocess.run(cmd, cwd=self.t, capture_output=True, text=True, env=env or self.env)
        return r

    def make_calls(self):
        if not self.mlog.exists():
            return []
        return [json.loads(l) for l in self.mlog.read_text().splitlines()]

    def calls_short(self):
        names = {str(self.old_repo): "old", str(self.new_repo): "new"}
        return [(names.get(d, "tmp"), t, *rest) for d, t, *rest in self.make_calls()]

    def assert_healthy(self, repo):
        out = git("worktree", "list", "--porcelain", cwd=repo)
        self.assertNotIn("prunable", out)
        paths = [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")]
        self.assertEqual(paths, [str(repo), str(repo / ".worktrees/a"), str(repo / ".worktrees/b")], out)
        for name in ("a", "b"):
            w = repo / ".worktrees" / name
            self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=w).strip(), f"michiomochi/{name}")
            self.assertTrue((w / f"{name}.txt").is_file())


class TestRenameToSf(Base):
    def test_dry_run_changes_nothing(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ガードはすべて満たしています", r.stdout)
        self.assertIn("worktree 2 本を移す", r.stdout)
        self.assertTrue(self.old_repo.is_dir() and self.old_live.is_dir())
        self.assertFalse(self.new_repo.exists() or self.new_live.exists() or self.state.exists())
        self.assertEqual(self.calls_short(), [("old", "check")])  # ガード 6 だけ

    def test_busy_board_waits(self):
        c = sqlite3.connect(self.board)
        c.execute("insert into tasks values ('t_run', 'running', '実行中')")
        c.commit()
        c.close()
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("t_run running", r.stdout)
        self.assertTrue(self.old_repo.is_dir() and not self.new_repo.exists())
        self.assertEqual(self.make_calls(), [])  # cron も止めない

    def test_process_cwd_in_worktree_waits(self):
        self.procs.append(subprocess.Popen(["sleep", "30"], cwd=self.old_repo / ".worktrees/a"))
        time.sleep(0.2)
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("cwd " + str(self.old_repo / ".worktrees/a"), r.stdout)
        self.assertTrue(self.old_repo.is_dir() and not self.new_repo.exists())

    def test_unmerged_pr_waits(self):
        git("reset", "-q", "--hard", self.pre_sha, cwd=self.old_repo)
        git("push", "-q", "--force", "origin", "main", cwd=self.old_repo)
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("5 origin/main の paths.repo", r.stdout)

    def test_card_started_before_pause_resumes_and_waits(self):
        env = dict(self.env, FAKE_MAKE_CARD_ON_PAUSE=str(self.board))
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.calls_short(), [("old", "check"), ("old", "cron-pause"), ("old", "cron-resume")])
        self.assertTrue(self.old_repo.is_dir() and not self.new_repo.exists())
        self.assertFalse(self.state.exists())

    def test_apply_then_rollback(self):
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.old_repo.exists() or self.old_live.exists())
        self.assertTrue((self.new_live / "cron_ids.json").is_file())
        self.assert_healthy(self.new_repo)
        self.assertIn(str(self.new_repo), (self.new_repo / "envs/michiomochi-wsl.toml").read_text())  # main が進んだ
        self.assertEqual(self.calls_short(), [
            ("old", "check"), ("old", "cron-pause"),
            ("new", "cron-sync", "FLAGS=--dry-run"), ("new", "wrappers"), ("new", "cron-sync"), ("new", "deploy"), ("new", "render"),
            ("new", "check"), ("new", "smoke"), ("new", "cron-sync", "FLAGS=--dry-run"), ("new", "cron-resume")])
        self.assertEqual(json.loads(self.state.read_text())["step"], 7)
        self.assertEqual((self.t / "home/profiles/executor/SOUL.md").read_text(), f"SOUL executor from {self.new_repo}\n")
        self.mlog.unlink()
        r = self.run_script("--rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.new_repo.exists() or self.new_live.exists())
        self.assertTrue((self.old_live / "cron_ids.json").is_file())
        self.assert_healthy(self.old_repo)
        calls = self.calls_short()
        self.assertEqual(calls[0], ("new", "cron-pause"))
        self.assertEqual([c[1] for c in calls[1:]], ["cron-sync", "wrappers", "cron-sync", "deploy", "render", "check", "cron-resume"])
        self.assertTrue(all(c[0] == "tmp" for c in calls[1:]), calls)  # マージ前のコミットの一時 worktree で配り直す
        self.assertEqual(git("worktree", "list", "--porcelain", cwd=self.old_repo).count("worktree "), 3)
        self.assertTrue((self.t / "home/profiles/reviewer/SOUL.md").read_text().startswith("SOUL reviewer from "))
        self.assertNotIn(str(self.new_repo), (self.t / "home/profiles/reviewer/SOUL.md").read_text())  # マージ前の写しに戻った  # 一時 worktree は消えた
        self.assertIsNone(json.loads(self.state.read_text())["step"])

    def test_failure_after_move_rolls_back_automatically(self):
        env = dict(self.env, FAKE_MAKE_FAIL="smoke")
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("戻します", r.stdout)
        self.assertFalse(self.new_repo.exists() or self.new_live.exists())
        self.assert_healthy(self.old_repo)
        short = [c[:2] for c in self.calls_short()]
        self.assertEqual(short[:9], [("old", "check"), ("old", "cron-pause"), ("new", "cron-sync"), ("new", "wrappers"),
                                     ("new", "cron-sync"), ("new", "deploy"), ("new", "render"), ("new", "check"), ("new", "smoke")])
        self.assertEqual([c[1] for c in short[9:]], ["cron-sync", "wrappers", "cron-sync", "deploy", "render", "check", "cron-resume"])
        self.assertNotIn(("new", "cron-resume"), short)  # 新しい設定のまま再開しない

    def test_pause_failure_resumes_and_clears_state(self):
        r = self.run_script("--apply", env=dict(self.env, FAKE_MAKE_FAIL="cron-pause"))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cron の停止に失敗しました", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(self.calls_short(), [("old", "check"), ("old", "cron-pause"), ("old", "cron-resume")])
        self.assertFalse(self.state.exists())  # 次の --apply を断らない
        self.assertTrue(self.old_repo.is_dir() and self.old_live.is_dir() and not self.new_repo.exists())

    def test_final_resume_failure_reports_redo_command(self):
        r = self.run_script("--apply", env=dict(self.env, FAKE_MAKE_FAIL="cron-resume"))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn(f"ERROR cron の再開に失敗しました", r.stdout)
        self.assertIn(f"`make -C {self.new_repo} cron-resume`", r.stdout)
        self.assertTrue(self.new_repo.is_dir() and self.new_live.is_dir())  # 切り替えは済んでいるので戻さない
        st = json.loads(self.state.read_text())
        self.assertEqual((st["step"], st["resume_failed"]), (6, True))
        self.assertEqual(self.calls_short()[-1], ("new", "cron-resume"))

    def test_resume_failure_after_recheck_reports_redo_command(self):
        env = dict(self.env, FAKE_MAKE_CARD_ON_PAUSE=str(self.board), FAKE_MAKE_FAIL="cron-resume")
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"`make -C {self.old_repo} cron-resume`", r.stdout)
        self.assertTrue(self.old_repo.is_dir() and not self.new_repo.exists())

    def test_staging_only_leftover_is_removed_by_apply(self):
        (self.new_live / "staging/tests").mkdir(parents=True)
        (self.new_live / "staging/epic_common.py").write_text("# make stage の写し\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("NOTE 3 新しい本番の置き場所に staging/ だけがある", r.stdout)
        self.assertTrue((self.new_live / "staging/epic_common.py").is_file())  # dry-run は消さない
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.new_live / "cron_ids.json").is_file())  # 旧本番がそのまま移った
        self.assertFalse((self.new_live / "staging/epic_common.py").exists())
        self.assertTrue(json.loads(self.state.read_text())["removed_staging_only"])

    def test_new_live_with_deployed_files_waits(self):
        (self.new_live / "staging").mkdir(parents=True)
        (self.new_live / "epic_common.py").write_text("# make deploy が新しい置き場所に配った\n")
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(f"3 新しい置き場所が既にある: {self.new_live}(staging/ 以外もある", r.stdout)
        self.assertTrue((self.new_live / "epic_common.py").is_file() and self.old_live.is_dir())
        self.assertEqual(self.make_calls(), [])

    def test_origin_main_with_only_repo_renamed_waits(self):
        # paths.repo だけが先に新しくなった main(改名の PR は未マージ)では、まだ動かない
        other = self.t / "other"
        (other / "envs/michiomochi-wsl.toml").write_text(env_toml(self.new_repo, self.old_live))
        git("commit", "-qam", "repo only", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(f"5 origin/main の paths.repo / paths.live が新しい置き場所でない(PR が未マージ): {self.new_repo} / {self.old_live}", r.stdout)

    def test_local_main_already_pulled_waits(self):
        git("fetch", "-q", "origin", cwd=self.old_repo)
        git("merge", "-q", "--ff-only", "origin/main", cwd=self.old_repo)
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("4 ローカルの main の paths.live が旧い本番でない(改名の PR まで pull 済み)", r.stdout)
        self.assertEqual(self.make_calls(), [])

    def test_guard6_compares_with_old_repo_name_when_main_already_has_new_name(self):
        # main に新しいリポジトリ名だけが先に入っている(本番はまだ旧い名前): 名前そのものは差に数えない設定の写しで比べる
        other = self.t / "other"  # 履歴は前に積むだけ(force push しない): 名前だけの main → 改名の PR
        (other / "envs/michiomochi-wsl.toml").write_text(env_toml(self.new_repo, self.old_live))
        git("commit", "-qam", "repo name only", cwd=other)
        repo_only = git("rev-parse", "HEAD", cwd=other).strip()
        (other / "envs/michiomochi-wsl.toml").write_text(env_toml(self.new_repo, self.new_live))
        git("commit", "-qam", "rename", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        git("fetch", "-q", "origin", cwd=self.old_repo)
        git("merge", "-q", "--ff-only", repo_only, cwd=self.old_repo)
        seen = self.t / "env_seen.toml"
        r = self.run_script(env=dict(self.env, FAKE_MAKE_ENV_SEEN=str(seen)))
        self.assertEqual(r.returncode, 0, r.stdout)
        txt = seen.read_text()
        self.assertIn(f'repo = "{self.old_repo}"', txt)
        self.assertIn(f'repo_url = "https://example.invalid/org/{self.old_repo.name}"', txt)
        self.assertIn(f'live = "{self.old_live}"', txt)
        self.assertEqual(r.stdout.count("ENV_FILE="), 1)  # ガード 6 だけが写しを使う

    def test_neutral_env_keeps_the_written_form_of_the_path(self):
        # 本物の設定は `~/ghq/...` の形。置換は名前の部分だけで、`~` を展開しない(展開すると雛形の置換結果が本番と違って見える)
        sys.path.insert(0, str(HERE))
        import rename_to_sf as m
        a = argparse.Namespace(
            old_repo="~/ghq/github.com/o/software-factory", new_repo="~/ghq/github.com/o/sf", old_live="~/x/software-factory",
            new_live="~/x/sf", board_db="~/b.db", state_file="~/s.json", make="make", git="git", proc="/proc")
        mig = m.Mig(a)
        (self.t / "r/envs").mkdir(parents=True)
        (self.t / "r/envs/michiomochi-wsl.toml").write_text(
            '[paths]\nrepo = "~/ghq/github.com/o/sf"\nlive = "~/x/software-factory"\n\n[git]\nrepo_url = "https://github.com/o/sf"\n')
        out = mig.neutral_env(self.t / "r", self.t).read_text()
        self.assertIn('repo = "~/ghq/github.com/o/software-factory"\n', out)
        self.assertIn('repo_url = "https://github.com/o/software-factory"\n', out)
        self.assertIn('live = "~/x/software-factory"\n', out)

    def test_guard6_additions_only_do_not_wait(self):
        out = ("差分 skills/new-skill/SKILL.md: 本番に無い: /x/SKILL.md\n"
               "差分 prompts/decision_digest.txt: ジョブ「SF 判断待ちダイジェスト」が見つからない\n差分あり: 2 件 / 9 ファイル")
        r = self.run_script(env=dict(self.env, **{"FAKE_MAKE_CHECK_OUT_" + self.old_repo.name: out}))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("NOTE 6 本番にまだ無いだけ", r.stdout)

    def test_guard6_content_diff_waits(self):
        out = ("差分 skills/software-factory-ops/SKILL.md: 内容が違う: /x/SKILL.md\n"
               "差分 skills/new-skill/SKILL.md: 本番に無い: /x/SKILL.md\n差分あり: 2 件 / 9 ファイル")
        r = self.run_script("--apply", env=dict(self.env, **{"FAKE_MAKE_CHECK_OUT_" + self.old_repo.name: out}))
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("6 旧リポジトリで make check に内容の差がある", r.stdout)
        self.assertIn("software-factory-ops/SKILL.md: 内容が違う", r.stdout)
        self.assertEqual(self.calls_short(), [("old", "check")])  # cron は止めない

    def test_jobs_made_by_cron_sync_are_left_paused_on_rollback(self):
        # 手順5の cron-sync が本番に無かったジョブを作ったあとで失敗: 戻した設定の cron-resume で動かさない
        env = dict(self.env, FAKE_MAKE_CRON_CREATE="decision_digest", FAKE_MAKE_FAIL="smoke")
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(json.loads((self.old_live / "cron_ids.json").read_text()), {})  # 切り替え前と同じ
        self.assertIn("止めたまま残した(cron_ids.json から外した。消すかは人が決める): decision_digest=newjob000001", r.stdout)
        self.assert_healthy(self.old_repo)

    def test_jobs_made_by_cron_sync_are_kept_on_success(self):
        r = self.run_script("--apply", env=dict(self.env, FAKE_MAKE_CRON_CREATE="decision_digest"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((self.new_live / "cron_ids.json").read_text()), {"decision_digest": "newjob000001"})
        self.assertEqual(json.loads(self.state.read_text())["ids_before"], {})

    def test_cron_sync_diff_after_switch_rolls_back(self):
        env = dict(self.env, FAKE_MAKE_CRON_SYNC="DRY-RUN 要約: 一致 6 / 変更 1 / 作成 0  (何も変更していません)")
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cron-sync --dry-run に差がある", r.stdout)
        self.assertFalse(self.new_repo.exists() or self.new_live.exists())
        self.assert_healthy(self.old_repo)

    # ---- 本番にだけあるジョブ(2026-11-10 の実例): cron_ids.json に無いが cron.toml と同じ名前のジョブが本番にある
    def prod_only_env(self, jobs, **extra):
        """台帳(本番の cron ジョブ)に jobs があり、cron.toml が decision_digest と profile_standup を定義する状態。
        profile_standup は main で増えたジョブで、その薄い .sh は make wrappers が置くまで無い。"""
        ledger = self.t / "jobs.json"
        ledger.write_text(json.dumps(jobs))
        return dict(self.env, FAKE_MAKE_JOBS=str(ledger), FAKE_MAKE_CRON_WANT="decision_digest,profile_standup",
                    FAKE_MAKE_NEEDS_WRAPPER="profile_standup", **extra), ledger

    def test_prod_only_job_apply_adopts_then_syncs(self):
        # --apply 経路: cron-sync が止めたキーだけを取り込み、.sh を置いてから cron-sync、そのあと deploy
        env, ledger = self.prod_only_env({"decision_digest": "d93984732486"})
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("戻すのにも失敗しました", r.stdout)
        self.assertEqual(self.calls_short()[2:8], [
            ("new", "cron-sync", "FLAGS=--dry-run"), ("new", "cron-adopt", "IDS=decision_digest=d93984732486"),
            ("new", "wrappers"), ("new", "cron-sync"), ("new", "deploy"), ("new", "render")])
        self.assertEqual(json.loads((self.new_live / "cron_ids.json").read_text()),
                         {"decision_digest": "d93984732486", "profile_standup": "new-profile_standup"})
        st = json.loads(self.state.read_text())
        self.assertEqual((st["step"], st["ids_before"], st["adopted"]), (7, {}, {"decision_digest": "d93984732486"}))
        self.assertIn("NOTE 本番にだけあったジョブを取り込んだ", r.stdout)

    def test_prod_only_job_rollback_after_late_failure(self):
        # ロールバック経路(手順6で失敗): 一時 worktree でも cron-sync を deploy の前に流す。取り込んだジョブは残し、作ったジョブは止めたまま外す
        env, ledger = self.prod_only_env({"decision_digest": "d93984732486"}, FAKE_MAKE_FAIL="smoke")
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("戻すのにも失敗しました", r.stdout)
        self.assertIn("OK 戻しました", r.stdout)
        self.assertFalse(self.new_repo.exists() or self.new_live.exists())
        self.assert_healthy(self.old_repo)
        tmp = [c[1] for c in self.calls_short() if c[0] == "tmp"]
        self.assertEqual(tmp, ["cron-sync", "wrappers", "cron-sync", "deploy", "render", "check", "cron-resume"])
        self.assertEqual(json.loads((self.old_live / "cron_ids.json").read_text()), {"decision_digest": "d93984732486"})
        self.assertIn("止めたまま残した(cron_ids.json から外した。消すかは人が決める): profile_standup=new-profile_standup", r.stdout)

    def test_rollback_syncs_cron_before_deploy(self):
        # ロールバック経路(手順5の cron-sync で失敗): マージ前の main が定義していて本番に無いジョブを、deploy の前に作る
        env, ledger = self.prod_only_env({"decision_digest": "d93984732486"}, FAKE_MAKE_FAIL="cron-sync@" + self.new_repo.name)
        (self.old_live / "cron_ids.json").write_text(json.dumps({"decision_digest": "d93984732486"}))
        r = self.run_script("--apply", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("戻すのにも失敗しました", r.stdout)
        self.assertIn("OK 戻しました", r.stdout)
        tmp = [c[1] for c in self.calls_short() if c[0] == "tmp"]
        self.assertEqual(tmp[:4], ["cron-sync", "wrappers", "cron-sync", "deploy"])
        self.assertIn("profile_standup", json.loads(ledger.read_text()))  # ロールバックの cron-sync が作った
        self.assertEqual(json.loads((self.old_live / "cron_ids.json").read_text()), {"decision_digest": "d93984732486"})

    def test_second_apply_refused_while_state_left(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"step": 3}))
        r = self.run_script("--apply")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertTrue(self.old_repo.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=1)
