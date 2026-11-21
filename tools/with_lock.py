#!/usr/bin/env python3
"""with_lock.py - ロックファイルの flock を取ってからコマンドを実行する(標準ライブラリのみ。macOS にも無い flock(1) の代わり)。

使い方: with_lock.py [--wait 秒] [--label 名] ロックファイル -- コマンド [引数...]
        with_lock.py --check-held [--label 名] ロックファイル     (ロックを持つ親の下で呼ばれたかの検査)

- 取れるまで --wait 秒(既定 600。環境変数 SF_DEPLOY_LOCK_WAIT でも指定可)待つ。超えたら
  `<名> 拒否: branch=<名前> rev=<短縮> (ロック待ち N 秒を超えた。実行中: <持ち主の行>)` を出して終了コード 75。
- 取れたら、持ち主の行(pid・branch・rev・名・開始時刻)をロックファイルに書き、子に SF_DEPLOY_LOCK_HELD=<絶対パス> を渡して
  コマンドを実行し、その終了コードで終わる。同じパスで既に取っている子孫からの呼び出しは取り直さない(自己デッドロックの防止)。
- --check-held: 環境変数 SF_DEPLOY_LOCK_HELD が同じパスのときだけ終了コード 0。違えば `<名> 拒否: branch=… rev=… (…)` を出して 1
  (本体の `*-run` を直接打って、ロックの外で本番の置き場所に書くのを止める)。
- flock(2) なので、プロセスが落ちるとロックは自動で外れる(掃除は要らない)。
"""
import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time

HELD_ENV = "SF_DEPLOY_LOCK_HELD"
EX_TEMPFAIL = 75


def git(*args):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def where():
    return git("symbolic-ref", "--short", "-q", "HEAD") or "(detached)", git("rev-parse", "--short", "HEAD") or "?"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait", type=float, default=float(os.environ.get("SF_DEPLOY_LOCK_WAIT", "600")))
    ap.add_argument("--label", default="deploy")
    ap.add_argument("--check-held", action="store_true", help="ロックは取らず、親がこのロックを持っているかだけ確かめる")
    ap.add_argument("lockfile")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    path = os.path.abspath(a.lockfile)
    branch, rev = where()

    if a.check_held:
        if os.environ.get(HELD_ENV) == path:
            return 0
        print(f"{a.label} 拒否: branch={branch} rev={rev} (ロックを持たずに直接は打てない。make stage / deploy-check / deploy / sync から呼ぶ)", file=sys.stderr)
        return 1
    if not cmd:
        ap.error("コマンドがありません(-- の後に書く)")

    if os.environ.get(HELD_ENV) == path:  # 親が同じロックを持っている
        return subprocess.call(cmd)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + a.wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                holder = os.pread(fd, 400, 0).decode("utf-8", "replace").strip() or "不明"
                print(f"{a.label} 拒否: branch={branch} rev={rev} (ロック待ち {a.wait:g} 秒を超えた。実行中: {holder})", file=sys.stderr)
                return EX_TEMPFAIL
            time.sleep(0.2)
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"pid={os.getpid()} {a.label} branch={branch} rev={rev} since={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode(), 0)

    env = dict(os.environ, **{HELD_ENV: path})
    child = subprocess.Popen(cmd, env=env, pass_fds=(fd,))  # 子にも fd を渡す: 親だけ kill されても子が終わるまでロックは残る

    def forward(signum, _frame):
        child.send_signal(signum)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, forward)
    rc = child.wait()  # シグナルは子へ forward 済み。子の終了を待つ
    os.ftruncate(fd, 0)  # 持ち主の行を消す(kill された場合は残るが、次に取った側が上書きする)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
