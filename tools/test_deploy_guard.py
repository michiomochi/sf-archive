#!/usr/bin/env python3
"""tools/require_main.sh と tools/with_lock.py と Makefile の配線の再現テスト(オフライン。標準ライブラリと git / make だけ)。

一時ディレクトリの中に本物の git(bare な origin と clone)を作って確かめる。本番・Notion・kanban には触れない。
  T1 main 以外の木: ブランチ違い・worktree・切り離し HEAD は拒否、main の木は通る
  T2 古い origin/main: fetch のあと木が古い・未 push・fetch できない・未コミットの変更は拒否
  T3 同時実行: 待つ→進む、待ち超過は終了コード 75 と持ち主つきの理由、kill でロックが外れる、再入は待たない
  T4 Makefile の配線: deploy / sync は require_main.sh がロックより先、deploy-check / stage はロックだけ。*-run を直接打つと拒否(本当に実行)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
REQUIRE = str(TOOLS / "require_main.sh")
WITH_LOCK = str(TOOLS / "with_lock.py")
PY = sys.executable

ENV = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SF_DEPLOY_"))}
ENV.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
           GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   {name}")
    else:
        failed += 1
        print(f"FAIL {name}\n     {detail}")


def run(cmd, cwd=None, env=None, timeout=60):
    r = subprocess.run(cmd, cwd=cwd, env=env or ENV, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def git(cwd, *args):
    rc, out = run(["git", *args], cwd=cwd)
    assert rc == 0, f"git {args} failed in {cwd}: {out}"
    return out


def commit(cwd, name, text):
    Path(cwd, name).write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", f"add {name}")


def require(cwd):
    return run(["bash", REQUIRE], cwd=cwd)


def fresh_repos(base):
    """origin(bare) + main の clone。main は origin/main と同期済み。"""
    origin = Path(base, "origin.git")
    work = Path(base, "work")
    git(base, "init", "-q", "--bare", "-b", "main", str(origin))
    git(base, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-B", "main")
    commit(work, "a.txt", "a\n")
    git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def t1_t2(base):
    # ---- T1 main 以外の木
    origin, work = fresh_repos(base)
    rc, out = require(work)
    check("T1 main の木・同期済みは通る", rc == 0 and out == "", f"rc={rc} out={out}")

    git(work, "checkout", "-q", "-b", "feature")
    rc, out = require(work)
    check("T1 ブランチが main でないと拒否(branch と rev を含む1行)",
          rc == 1 and out.startswith("deploy 拒否: branch=feature rev=") and "ブランチが main ではない" in out and "\n" not in out, f"rc={rc} out={out}")
    git(work, "checkout", "-q", "main")

    git(work, "checkout", "-q", "--detach")
    rc, out = require(work)
    check("T1 切り離し HEAD は branch=(detached) で拒否", rc == 1 and "branch=(detached)" in out, f"rc={rc} out={out}")
    git(work, "checkout", "-q", "main")

    # worktree: 主の木を別ブランチに載せ、worktree に main を載せる(ブランチは main だが worktree)
    wt = Path(base, "wt")
    git(work, "checkout", "-q", "-b", "other")
    git(work, "worktree", "add", "-q", str(wt), "main")
    rc, out = require(wt)
    check("T1 branch が main でも worktree からは拒否(main の作業ツリーのパスを示す)",
          rc == 1 and "worktree からは deploy できない" in out and str(work) in out, f"rc={rc} out={out}")
    git(work, "worktree", "remove", "--force", str(wt))
    git(work, "checkout", "-q", "main")

    # ---- T2 古い origin/main など
    other = Path(base, "other_clone")
    git(base, "clone", "-q", str(origin), str(other))
    commit(other, "b.txt", "b\n")
    git(other, "push", "-q", "origin", "main")
    # work の origin/main(追跡参照)はまだ古い。fetch のあとで古さが分かって拒否される
    before = git(work, "rev-parse", "origin/main")
    rc, out = require(work)
    check("T2 古い木(fetch のあとで origin/main が先)は拒否", rc == 1 and "木が古い" in out and "1 コミット先" in out, f"rc={rc} out={out}")
    check("T2 検査が fetch して追跡参照を進めた", git(work, "rev-parse", "origin/main") != before)
    git(work, "pull", "-q", "--ff-only")
    rc, out = require(work)
    check("T2 pull したあとは通る", rc == 0, f"rc={rc} out={out}")

    commit(work, "c.txt", "c\n")  # push しない
    rc, out = require(work)
    check("T2 未 push のコミットがあると拒否", rc == 1 and "origin/main に含まれない" in out and "1 コミット" in out, f"rc={rc} out={out}")
    git(work, "push", "-q", "origin", "main")

    Path(work, "a.txt").write_text("edited\n", encoding="utf-8")
    rc, out = require(work)
    check("T2 追跡ファイルの未コミットの変更は拒否", rc == 1 and "未コミットの変更" in out, f"rc={rc} out={out}")
    git(work, "checkout", "-q", "--", "a.txt")
    Path(work, "untracked.txt").write_text("x\n", encoding="utf-8")
    rc, out = require(work)
    check("T2 本番へ写さないパス(リポジトリ直下)の追跡外のファイルは通る", rc == 0, f"rc={rc} out={out}")
    Path(work, "scripts").mkdir()
    Path(work, "scripts", "tmp.py").write_text("print(1)\n", encoding="utf-8")
    rc, out = require(work)
    check("T2 本番へ写すパス(scripts/)に追跡外のファイルがあると拒否(件数とファイル名つきの1行)",
          rc == 1 and "追跡外のファイルが 1 件ある: scripts/tmp.py" in out and out.startswith("deploy 拒否: branch=main rev=") and "\n" not in out, f"rc={rc} out={out}")
    rc, out = run(["bash", REQUIRE], cwd=work, env=dict(ENV, SF_REQUIRE_MAIN_LABEL="sync"))
    check("T2 sync の名前でも同じ検査で拒否(先頭の語が sync)", rc == 1 and out.startswith("sync 拒否: "), f"rc={rc} out={out}")
    Path(work, ".gitignore").write_text("scripts/tmp.py\n", encoding="utf-8")
    git(work, "add", ".gitignore")
    git(work, "commit", "-q", "-m", "ignore")
    git(work, "push", "-q", "origin", "main")
    rc, out = require(work)
    check("T2 .gitignore で無視されているファイルは数えない", rc == 0, f"rc={rc} out={out}")
    for name in ("scripts/tmp.py", "untracked.txt"):
        Path(work, name).unlink()
    Path(work, "skills", "x").mkdir(parents=True)
    Path(work, "skills", "x", "SKILL.md").write_text("x\n", encoding="utf-8")
    rc, out = require(work)
    check("T2 skills/ の下の追跡外のファイルも拒否", rc == 1 and "skills/x/SKILL.md" in out, f"rc={rc} out={out}")
    shutil.rmtree(Path(work, "skills"))
    shutil.rmtree(Path(work, "scripts"))

    # fetch できない
    git(work, "remote", "set-url", "origin", str(Path(base, "no_such_origin.git")))
    rc, out = require(work)
    check("T2 fetch できないと拒否", rc == 1 and "git fetch origin main に失敗" in out, f"rc={rc} out={out}")

    # git のリポジトリの外
    plain = Path(base, "plain")
    plain.mkdir()
    rc, out = require(plain)
    check("T2 git の外は拒否", rc == 1 and "git のリポジトリの中ではない" in out, f"rc={rc} out={out}")
    return work


def lock_cmd(lock, *extra, cmd=("true",)):
    return [PY, WITH_LOCK, *extra, str(lock), "--", *cmd]


def wait_holder(lock, proc, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if Path(lock).exists() and Path(lock).read_text(encoding="utf-8").strip():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def t3(base, work):
    lock = Path(base, "live", ".deploy.flock")  # 親ディレクトリはまだ無い(mkdir -p されること)
    sleeper = ("sleep", "30")

    # 1本目が持つ間に2本目が待ち超過で拒否される
    holder = subprocess.Popen(lock_cmd(lock, "--label", "deploy", cmd=sleeper), cwd=work, env=ENV, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check("T3 1本目がロックを取り、持ち主の行を書く", wait_holder(lock, holder) and "pid=" in lock.read_text(encoding="utf-8"), "holder line missing")
        t0 = time.time()
        rc, out = run(lock_cmd(lock, "--wait", "1", "--label", "deploy-check"), cwd=work)
        dt = time.time() - t0
        check("T3 待ち超過は終了コード 75 と、branch・rev・実行中の持ち主つきの1行",
              rc == 75 and out.startswith("deploy-check 拒否: branch=main rev=") and "ロック待ち 1 秒を超えた" in out and "実行中: pid=" in out
              and "deploy branch=main" in out and "\n" not in out, f"rc={rc} out={out}")
        check("T3 待ち超過までは実際に待つ(1秒以上)", dt >= 0.9, f"dt={dt:.2f}")
        # 環境変数でも待ちを指定できる
        env = dict(ENV, SF_DEPLOY_LOCK_WAIT="1")
        rc, out = run([PY, WITH_LOCK, str(lock), "--", "true"], cwd=work, env=env)
        check("T3 SF_DEPLOY_LOCK_WAIT で待ちの上限を変えられる", rc == 75 and "ロック待ち 1 秒" in out, f"rc={rc} out={out}")
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()

    # 親も子も kill されたらロックは自動で外れる(掃除不要)
    t0 = time.time()
    rc, out = run(lock_cmd(lock, "--wait", "3", cmd=("echo", "got-it")), cwd=work)
    check("T3 持ち主が kill されるとロックが自動で外れて次が取れる", rc == 0 and out == "got-it" and time.time() - t0 < 3, f"rc={rc} out={out}")

    # 1本目が終わるまで待ち、そのあと進む
    lock.write_text("", encoding="utf-8")  # kill された持ち主の古い行を消してから、次の持ち主を待つ
    holder = subprocess.Popen(lock_cmd(lock, cmd=("sleep", "1.5")), cwd=work, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_holder(lock, holder)
    t0 = time.time()
    rc, out = run(lock_cmd(lock, "--wait", "20", cmd=("echo", "second")), cwd=work)
    dt = time.time() - t0
    holder.wait()
    check("T3 2本目は1本目が終わるまで待ってから進む", rc == 0 and out == "second" and dt >= 1.0, f"rc={rc} out={out} dt={dt:.2f}")

    # 再入: 内側の呼び出しは同じロックを取り直さない(自分で自分を待たない)
    inner = lock_cmd(lock, "--wait", "1", cmd=("echo", "inner"))
    t0 = time.time()
    rc, out = run(lock_cmd(lock, "--wait", "1", cmd=inner), cwd=work)
    check("T3 再入は待たない", rc == 0 and out == "inner" and time.time() - t0 < 1, f"rc={rc} out={out}")
    # 別のロックファイルなら入れ子でも独立に取る
    other = Path(base, "live", ".other.flock")
    rc, out = run(lock_cmd(lock, "--wait", "1", cmd=lock_cmd(other, "--wait", "1", cmd=("echo", "other-ok"))), cwd=work)
    check("T3 別パスのロックは独立に取れる", rc == 0 and out == "other-ok", f"rc={rc} out={out}")

    # 子の終了コードをそのまま返す
    rc, out = run(lock_cmd(lock, cmd=("sh", "-c", "exit 3")), cwd=work)
    check("T3 子の終了コードを返す", rc == 3, f"rc={rc} out={out}")


def t4(base):
    if shutil.which("make") is None:
        print("skip T4 (make が無い)")
        return
    hs = Path(base, "live4")

    def dry(target):
        env = {k: v for k, v in ENV.items()}
        return run(["make", "-n", target, f"HERMES_SCRIPTS={hs}"], cwd=REPO, env=env, timeout=120)

    def lock_at(out, label):
        """入口が取るロック(`with_lock.py --label <名> <ロックファイル> --`)の位置。*-run の先頭の --check-held の行とは区別する。"""
        return out.find(f"tools/with_lock.py --label {label} {hs}/.deploy.flock --")

    rc, out = dry("deploy")
    i_req, i_lock, i_sd = out.find("tools/require_main.sh"), lock_at(out, "deploy"), out.find("safe_deploy.sh")
    check("T4 deploy は require_main.sh がロックより先、safe_deploy.sh がロックの後",
          rc == 0 and 0 <= i_req < i_lock < i_sd, f"rc={rc}\n{out[:1500]}")
    check("T4 deploy のロックの中でも require_main.sh をもう一度呼ぶ", out.count("tools/require_main.sh") >= 2, out[:1500])

    rc, out = dry("deploy-check")
    check("T4 deploy-check はロックだけ(ブランチの検査はしない)", rc == 0 and "require_main" not in out and 0 <= lock_at(out, "deploy-check") < out.find("cp scripts/*.py"), f"rc={rc}\n{out[:1500]}")

    rc, out = dry("stage")
    check("T4 stage の cp はロックの内側", rc == 0 and 0 <= lock_at(out, "stage") < out.find("cp scripts/*.py"), f"rc={rc}\n{out[:1500]}")

    rc, out = dry("sync")
    i_req, i_lock, i_sync = out.find("tools/require_main.sh"), lock_at(out, "sync"), out.find("tools/sync_live.py")
    check("T4 sync は require_main.sh がロックより先、sync_live.py がロックの後(もう一度 require_main.sh も呼ぶ)",
          rc == 0 and 0 <= i_req < i_lock < i_sync and "SF_REQUIRE_MAIN_LABEL=sync" in out and out.count("tools/require_main.sh") >= 2, f"rc={rc}\n{out[:1500]}")

    # 本体(*-run)を直接打つ抜け道: -n ではなく本当に実行する。どれも先頭の HELD_GUARD で止まり、本番の置き場所(ここでは一時ディレクトリ)に何も書かない。
    for target in ("stage-run", "deploy-check-run", "deploy-run", "sync-run"):
        rc, out = run(["make", target, f"HERMES_SCRIPTS={hs}"], cwd=REPO, env=dict(ENV), timeout=120)
        check(f"T4 make {target} を直接打つと、ロックを持たないので拒否される(make は Error 1 を出して終わる)",
              rc != 0 and f"{target} 拒否: branch=" in out and "ロックを持たずに直接は打てない" in out and "Error 1" in out, f"rc={rc}\n{out[:1000]}")
    check("T4 直接打った *-run は staging も .deploy.flock も作らない", not Path(hs, "staging").exists(), f"{list(hs.glob('*'))}")

    # ロックを持つ親の下では通る(--check-held の正の側)。パスが違えば拒否
    lock = Path(hs, ".deploy.flock")
    rc, out = run(lock_cmd(lock, cmd=[PY, WITH_LOCK, "--check-held", "--label", "x", str(lock)]), cwd=REPO)
    check("T4 ロックを持つ親の下では --check-held が通る", rc == 0 and out == "", f"rc={rc} out={out}")
    rc, out = run(lock_cmd(lock, cmd=[PY, WITH_LOCK, "--check-held", "--label", "x", str(Path(hs, ".other.flock"))]), cwd=REPO)
    check("T4 別のロックファイルを持っているだけでは --check-held は拒否", rc == 1 and "x 拒否: branch=" in out, f"rc={rc} out={out}")


def main():
    base = tempfile.mkdtemp(prefix="deploy_guard.", dir=os.environ.get("TMPDIR") or None)
    try:
        work = t1_t2(base)
        # T3 は同期済みの main の木で。fetch できない設定を戻す
        git(work, "remote", "set-url", "origin", str(Path(base, "origin.git")))
        t3(base, work)
        t4(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"deploy guard tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
