#!/usr/bin/env python3
"""リハーサルの段の差分実行。同じ入力で通った段を飛ばす。標準ライブラリのみ。

     stamp.py run NAME [--after PREV] [--inputs PATH ...] [--snapshot DIR | --stateless] -- CMD ...
   段の値 = 段の名前 + コマンド + --inputs の中身 + 前の段の値。値が印と一致すれば飛ばす。前の段が回り直すと後の段も回り直す。
   印は --dir(既定 build/.stamps。build/ は git の無視対象)に置く。
   共有するディレクトリ(サンドボックス)の状態は、--dir の .state に「今どの段の終わりの状態か」を書いて扱う。
   回す必要がある段は、前の段の終わりの状態が要る。違えば前の段の --snapshot の写しから戻し、写しが無ければ終了コード 3。
   --stateless の段(dry-run・棚卸しのように状態を変えない段)は、その前の段の状態を引き継ぐ。

STAMP_FORCE=1 なら run は常に回す(最後の通しの1回に使う)。

make test・make deploy・make precheck はこの印を使わない(test は毎回全部回る。テスト → デプロイの順序は make ship)。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXIT_NO_STATE = 3


def say(msg):
    print(f"[stamp] {msg}", flush=True)


def forced():
    return os.environ.get("STAMP_FORCE", "") not in ("", "0")


def file_digest(p):
    h = hashlib.sha256()
    if p.is_symlink():
        h.update(b"link:" + os.readlink(p).encode())
    elif p.is_file():
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    elif p.exists():
        h.update(b"other")
    else:
        return "deleted"
    return h.hexdigest()


def inputs_digest(paths):
    h = hashlib.sha256()
    for given in paths:
        p = Path(given)
        h.update(given.encode() + b"\0")
        if p.is_dir() and not p.is_symlink():
            for q in sorted(p.rglob("*")):
                if any(part in ("__pycache__", ".git") for part in q.relative_to(p).parts) or q.is_dir():
                    continue
                h.update(str(q.relative_to(p)).encode() + b"\0" + file_digest(q).encode() + b"\0")
        else:
            h.update(file_digest(p).encode() + b"\0")
    return h.hexdigest()


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_atomic(p, text):
    p = Path(p)
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def ok_path(d, name):
    return Path(d) / f"{name}.ok"


# ---- 段の差分実行
def state_chain(d, name):
    """name の段の終わりの状態と同じ状態の段の名前(--stateless を前へたどる)と、たどり着いた状態を持つ段の記録。"""
    names, rec = [], read_json(ok_path(d, name))
    while rec is not None:
        names.append(name)
        if not rec.get("stateless"):
            return names, name, rec
        name = rec.get("after")
        if not name:
            return names, None, None
        rec = read_json(ok_path(d, name))
    return names, name, None


def check_snapshot_path(d, target):
    t, base = Path(target).resolve(), Path(d).resolve().parent
    home = Path.home().resolve()
    if t in (Path("/"), home) or t == base or base not in t.parents or t in Path(d).resolve().parents or t == Path(d).resolve():
        raise SystemExit(f"[stamp] ERROR: --snapshot は {base} の中のディレクトリだけ(印の置き場所を含まないもの): {t}")
    return t


def restore(d, src_name, rec):
    target = Path(rec["snapshot"])
    snap = Path(d) / f"{src_name}.snap"
    check_snapshot_path(d, target)
    if target.exists() or target.is_symlink():
        shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
    shutil.copytree(snap, target, symlinks=True)
    write_atomic(Path(d) / ".state", src_name)
    say(f"{src_name} の終わりの状態に {target} を戻した")


def cmd_run(a):
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    if not a.cmd:
        raise SystemExit("[stamp] ERROR: run には -- の後にコマンドが要る")
    snapshot = str(check_snapshot_path(d, a.snapshot)) if a.snapshot else None
    prev = read_json(ok_path(d, a.after)) if a.after else None
    prev_key = (prev or {}).get("key", "missing") if a.after else ""
    key = hashlib.sha256(json.dumps([a.name, a.cmd, inputs_digest(a.inputs), prev_key, bool(a.stateless)]).encode()).hexdigest()
    mine = read_json(ok_path(d, a.name))
    if not forced() and mine and mine.get("key") == key:
        say(f"skip {a.name}: 印が一致 ({mine.get('time')})")
        return 0
    why = "STAMP_FORCE" if forced() else ("印が無い" if not mine else "入力か前の段が変わった")
    # 回すと決めた時点で古い印を消す(回せずに終わっても、後の段がこの段を「変わっていない」と読まないように)
    ok_path(d, a.name).unlink(missing_ok=True)
    if a.after:
        if prev is None:
            say(f"ERROR {a.name}: 前の段 {a.after} の印が無い(落ちたか未実行)。STAMP_FORCE=1 で通しで回す")
            return EXIT_NO_STATE
        same, holder, hrec = state_chain(d, a.after)
        cur = (d / ".state").read_text(encoding="utf-8").strip() if (d / ".state").exists() else ""
        if holder is not None and cur not in same:
            if hrec and hrec.get("snapshot") and (d / f"{holder}.snap").is_dir():
                restore(d, holder, hrec)
            else:
                say(f"ERROR {a.name}: {holder} の終わりの状態が無い(今は {cur or '不明'}、{holder} に --snapshot が無い)。STAMP_FORCE=1 で通しで回す")
                return EXIT_NO_STATE
    say(f"run {a.name}: {why}")
    rc = subprocess.run(a.cmd, check=False).returncode
    if rc != 0:
        if not a.stateless:
            write_atomic(d / ".state", "")
        say(f"{a.name}: 終了コード {rc}。印を書かない")
        return rc
    if snapshot:
        snap = d / f"{a.name}.snap"
        if snap.exists():
            shutil.rmtree(snap)
        shutil.copytree(snapshot, snap, symlinks=True)
    write_atomic(ok_path(d, a.name), json.dumps({
        "key": key, "after": a.after, "stateless": bool(a.stateless), "snapshot": snapshot,
        "time": time.strftime("%Y-%m-%d %H:%M:%S")}))
    write_atomic(d / ".state", a.name)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("STAMP_DIR", "build/.stamps"))
    sub = ap.add_subparsers(dest="cmd_name", required=True)
    p = sub.add_parser("run")
    p.add_argument("name")
    p.add_argument("--after")
    p.add_argument("--inputs", nargs="*", default=[])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--snapshot")
    g.add_argument("--stateless", action="store_true")
    p.set_defaults(func=cmd_run)
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:  # run のコマンドは -- の後ろ(argparse に渡すとオプションと混ざる)
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.cmd = cmd
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
