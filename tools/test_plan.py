#!/usr/bin/env python3
"""tools/tests.toml を読んで、テストを組み立てて回す(標準ライブラリのみ)。`make test` / `make test-fast` / `make test-paths` の本体。

Why: テストの一覧と重さは Makefile にシェルで書かない(書くと登録漏れも遅さも人の目でしか見えない)。一覧は tools/tests.toml が持ち、
このスクリプトが ①登録漏れ ②fast の CPU 時間 を機械で見る。遅いからと飛ばす道は作らない(tier を変えるのは tests.toml の差分に出る)。

  test_plan.py --all                 全部(tests.toml の並びのまま)。`make test`
  test_plan.py --fast                fast だけ。`make test-fast`。最後に回していない重い検査の名前を出す
  test_plan.py --paths P [P...]      変えたパスに合う heavy だけ(fast は test-fast で回すのでここでは回さない)。合うものが無ければ「該当なし」で rc=0
  test_plan.py --check               登録の検査だけ(回さない)
  test_plan.py --list                回すコマンドを表示して終わる(回さない)

どのモードも最初に tests.toml を読み、次を検査する(落ちたら何も回さない、rc=1):
  - 読み込み: 名前の重複・tier・cpu_limit・未知のキー・heavy に paths が無い・paths の glob がどのファイルにも当たらない(存在しない名前)・コマンドが指すファイルが無い
  - 登録漏れ: tools/*.py・tools/test_*.sh・scripts/tests/*・scripts/*_selftest.py のうち、どの [[test]] の cmd にも paths にも無いもの。
    tools/test_*.py|sh は cmd に書かれていること(paths に書いただけでは「回さない登録」になるため認めない)
実行後の検査(rc=1):
  - 各テストの CPU 時間(子プロセスの user＋sys)が cpu_limit 以下
  - fast の CPU 時間の合計が FAST_CPU_BUDGET(60 秒)以下(--fast と --all)。壁時計の時間は使わない(ほかの作業と CPU を取り合った待ちを拾うため)

tests.toml: [[test]] の name / cmd(["{py}", ...]) / tier("fast"|"heavy") / cpu_limit(秒) / paths(glob: このテストが対象にするファイル。
heavy は必須。`*` は / を越えない、`**` は越える) / 任意で cwd(repo 相対) / env({名前 = 値}) / requires(無ければ skip する絶対パス。例 /proc)。
cmd と env の値の {py}・{ENV_FILE}・{TEST_CONFIG} は --py・--env-file・--test-config(Makefile が渡す)で置き換える。
Exit: 0 成功 / 1 検査の失敗・テストの失敗 / 2 引数の誤り。
"""
import argparse
import os
import re
import resource
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAST_CPU_BUDGET = 60.0  # 秒。tests.toml からは動かせない(緩めるなら、ここの差分がレビューに出る)
TIERS = ("fast", "heavy")
TEST_KEYS = {"name", "cmd", "tier", "cpu_limit", "paths", "cwd", "env", "requires"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
SKIP_DIRS = {".git", ".worktrees", "__pycache__", "build", "node_modules", ".venv"}
# tools/test_*.py のうち、テストではなく道具(テストは tools/test_test_plan.py)。cmd に書かなくてよいが、paths には載せる。
NOT_TESTS = {"tools/test_plan.py"}


class PlanError(Exception):
    pass


def glob_regex(pattern):
    """glob → 正規表現。`**` は / を越え、`*` と `?` は越えない。"""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def repo_files(root):
    """repo の中のファイル(repo 相対の POSIX パス)。git の追跡に依らない(追加前の新しいファイルも見る)。"""
    found = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            found.append((Path(d) / f).relative_to(root).as_posix())
    return sorted(found)


def inventory(files):
    """登録が要るファイル: tools/*.py、tools/test_*.sh、scripts/tests/*、scripts/*_selftest.py。"""
    inv = set()
    for f in files:
        p = Path(f)
        parts = p.parts
        if len(parts) == 2 and parts[0] == "tools" and (f.endswith(".py") or (p.name.startswith("test_") and f.endswith(".sh"))):
            inv.add(f)
        elif len(parts) == 3 and parts[:2] == ("scripts", "tests"):
            inv.add(f)
        elif len(parts) == 2 and parts[0] == "scripts" and p.name.endswith("_selftest.py"):
            inv.add(f)
    return inv


def must_be_run(f):
    """cmd に書かれていなければならないファイル(paths に書くだけでは通さない)。"""
    p = Path(f)
    return len(p.parts) == 2 and p.parts[0] == "tools" and p.name.startswith("test_") and f.endswith((".py", ".sh")) and f not in NOT_TESTS


def cmd_files(test, files):
    """cmd のうち repo のファイルを指しているもの(cwd からの相対でも repo 相対でも)。"""
    have = set(files)
    cwd = test.get("cwd") or "."
    found = set()
    for tok in test["cmd"]:
        for cand in (os.path.normpath(os.path.join(cwd, tok)), os.path.normpath(tok)):
            if cand in have:
                found.add(cand)
    return found


def load(tests_toml, root):
    """(tests, problems)。tests は dict の list(tests.toml の並び)。"""
    try:
        with open(tests_toml, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [], [f"{tests_toml} を読めません: {e}"]
    problems = []
    extra = set(raw) - {"test"}
    if extra:
        problems.append(f"tests.toml: 未知のトップレベルのキー: {', '.join(sorted(extra))}([[test]] だけ)")
    tests = raw.get("test")
    if not isinstance(tests, list) or not tests:
        return [], problems + ["tests.toml: [[test]] がありません"]
    files = repo_files(root)
    seen = set()
    for i, t in enumerate(tests):
        label = f"[[test]] #{i + 1}"
        if not isinstance(t, dict):
            problems.append(f"{label}: テーブルではありません")
            continue
        name = t.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append(f"{label}: name は英小文字・数字・_ です: {name!r}")
        else:
            label = f"[[test]] {name}"
            if name in seen:
                problems.append(f"{label}: name が重複しています")
            seen.add(name)
        unknown = set(t) - TEST_KEYS
        if unknown:
            problems.append(f"{label}: 未知のキー: {', '.join(sorted(unknown))}")
        cmd = t.get("cmd")
        if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd)):
            problems.append(f"{label}: cmd は文字列のリストです")
        if t.get("tier") not in TIERS:
            problems.append(f"{label}: tier は fast か heavy です: {t.get('tier')!r}")
        lim = t.get("cpu_limit")
        if isinstance(lim, bool) or not isinstance(lim, (int, float)) or lim <= 0:
            problems.append(f"{label}: cpu_limit(秒、正の数)が要ります: {lim!r}")
        paths = t.get("paths", [])
        if not (isinstance(paths, list) and all(isinstance(x, str) and x for x in paths)):
            problems.append(f"{label}: paths は glob の文字列のリストです")
            paths = []
        if t.get("tier") == "heavy" and not paths:
            problems.append(f"{label}: heavy には paths が要ります(どのパスの変更で回すかが無いと test-paths が選べない)")
        for pat in paths:
            if not any(glob_regex(pat).match(f) for f in files):
                problems.append(f"{label}: paths の {pat!r} がどのファイルにも当たりません(存在しない名前)")
        if "cwd" in t and not (isinstance(t["cwd"], str) and (root / t["cwd"]).is_dir()):
            problems.append(f"{label}: cwd が無いディレクトリです: {t['cwd']!r}")
        if "env" in t and not (isinstance(t["env"], dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in t["env"].items())):
            problems.append(f"{label}: env は {{名前 = 文字列}} です")
        if "requires" in t and not (isinstance(t["requires"], str) and t["requires"].startswith("/")):
            problems.append(f"{label}: requires は絶対パスの文字列です")
        if isinstance(cmd, list):
            for tok in cmd:
                if re.search(r"(^|/)[\w.-]+\.(py|sh)$", tok) and not tok.startswith("{") and not cmd_files(dict(t, cmd=[tok]), files):
                    problems.append(f"{label}: cmd のファイルがありません: {tok}(cwd = {t.get('cwd', '.')})")
    problems += unregistered(tests, files)
    return tests, problems


def unregistered(tests, files):
    """登録漏れ(問題の文のリスト)。"""
    valid = [t for t in tests if isinstance(t, dict) and isinstance(t.get("cmd"), list) and all(isinstance(x, str) for x in t["cmd"])]
    run = set()
    covered = set()
    for t in valid:
        run |= cmd_files(t, files)
        for pat in t.get("paths", []) if isinstance(t.get("paths"), list) else []:
            if isinstance(pat, str):
                rx = glob_regex(pat)
                covered |= {f for f in files if rx.match(f)}
    inv = inventory(files)
    missing = sorted(f for f in inv if f not in run and (f not in covered or must_be_run(f)))
    if not missing:
        return []
    return ["tests.toml に無いテスト: " + ", ".join(missing) + "(tools/tests.toml に [[test]] を足す。回さない登録は認めない)"]


def expand(tokens, subst):
    out = []
    for s in tokens:
        for k, v in subst.items():
            s = s.replace(k, v)
        out.append(s)
    return out


def display(test, subst):
    env = test.get("env") or {}
    parts = []
    if test.get("cwd"):
        parts.append(f"cd {shlex.quote(test['cwd'])} &&")
    parts += [f"{k}={shlex.quote(expand([v], subst)[0])}" for k, v in env.items()]
    parts.append(shlex.join(expand(test["cmd"], subst)))
    return " ".join(parts)


def children_cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def run_test(test, root, subst):
    """(rc, wall 秒, cpu 秒)。CPU は子プロセス(孫も、待たれたもの)の user＋sys。"""
    env = dict(os.environ)
    env.update({k: expand([v], subst)[0] for k, v in (test.get("env") or {}).items()})
    cwd = root / test["cwd"] if test.get("cwd") else root
    cpu0, t0 = children_cpu(), time.monotonic()
    try:
        rc = subprocess.run(expand(test["cmd"], subst), cwd=cwd, env=env).returncode
    except FileNotFoundError as e:
        print(f"test_plan: {test['name']}: 実行できません: {e}", file=sys.stderr)
        rc = 127
    return rc, time.monotonic() - t0, children_cpu() - cpu0


def select(tests, mode, paths):
    if mode == "fast":
        return [t for t in tests if t["tier"] == "fast"]
    if mode == "paths":
        return [t for t in tests if t["tier"] == "heavy" and any(glob_regex(p).match(c) for p in t["paths"] for c in paths)]
    return list(tests)


def norm_paths(paths, root):
    """引数のパスを repo 相対の POSIX にそろえる(./ や絶対パスを許す)。"""
    out = []
    for p in paths:
        q = Path(p)
        if q.is_absolute():
            try:
                q = q.resolve().relative_to(root.resolve())
            except ValueError:
                pass
        out.append(os.path.normpath(q.as_posix()).replace(os.sep, "/"))
    return out


def execute(selected, mode, root, subst, label, fast_budget):
    results, failed = [], []
    t_start, cpu_start = time.monotonic(), children_cpu()
    for t in selected:
        if t.get("requires") and not Path(t["requires"]).exists():
            print(f"skip {t['name']} ({t['requires']} が無い OS)", flush=True)
            results.append((t, None, 0.0, 0.0))
            continue
        print("+ " + display(t, subst), flush=True)
        rc, wall, cpu = run_test(t, root, subst)
        results.append((t, rc, wall, cpu))
        print(f"  -> {t['name']} [{t['tier']}] rc={rc} wall={wall:.1f}s cpu={cpu:.1f}s (上限 {t['cpu_limit']}s)", flush=True)
        if rc != 0:
            failed.append(f"{t['name']} が落ちました(rc={rc})")
            break
        if cpu > t["cpu_limit"]:
            failed.append(f"{t['name']} の CPU 時間が {cpu:.1f} 秒で、上限 {t['cpu_limit']} 秒を超えました(tests.toml の cpu_limit。重いなら tier と paths を見直す)")
            break
    wall_total, cpu_total = time.monotonic() - t_start, children_cpu() - cpu_start
    fast_cpu = sum(c for t, rc, w, c in results if t["tier"] == "fast")
    if mode in ("fast", "all") and not failed and fast_cpu > fast_budget:  # 途中で止まったときは fast が揃っていないので見ない
        failed.append(f"fast の CPU 時間が {fast_cpu:.1f} 秒で、{fast_budget:.0f} 秒を超えました(壁時計ではなく子プロセスの user＋sys。重いテストは tier = \"heavy\" と paths にする)")
    slow = sorted(((c, t["name"]) for t, rc, w, c in results if rc is not None), reverse=True)[:3]
    ran = [t["name"] for t, rc, w, c in results if rc is not None]
    print(f"{label}: {len(ran)} 件 {'失敗' if failed else 'rc=0'} wall={wall_total:.1f}s cpu={cpu_total:.1f}s"
          + (f" (fast の CPU {fast_cpu:.1f}s / 上限 {fast_budget:.0f}s)" if mode in ("fast", "all") else "")
          + ("; CPU の上位: " + ", ".join(f"{n} {c:.1f}s" for c, n in slow) if slow else ""), flush=True)
    return failed


def build_parser():
    ap = argparse.ArgumentParser(description="tools/tests.toml のテストを回す(登録漏れと fast の CPU 時間を見る)。")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="全部(make test)")
    g.add_argument("--fast", action="store_true", help="fast だけ(make test-fast)")
    g.add_argument("--paths", nargs="*", metavar="PATH", help="変えたパスに合う heavy だけ(make test-paths PATHS=...)")
    g.add_argument("--check", action="store_true", help="tests.toml の検査だけ(回さない)")
    ap.add_argument("--list", action="store_true", help="回すコマンドを表示して終わる")
    ap.add_argument("--root", default=str(REPO), help="repo の根(テスト用)")
    ap.add_argument("--tests-toml", help="tests.toml の場所(既定: <root>/tools/tests.toml)")
    ap.add_argument("--py", default="python3")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--test-config", default=None)
    ap.add_argument("--fast-cpu-budget", type=float, default=FAST_CPU_BUDGET, help="テスト用。Makefile は渡さない")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    root = Path(a.root).resolve()
    tests_toml = Path(a.tests_toml) if a.tests_toml else root / "tools" / "tests.toml"
    tests, problems = load(tests_toml, root)
    if problems:
        for p in problems:
            print(f"test_plan: {p}", file=sys.stderr)
        print(f"test_plan: tests.toml の問題 {len(problems)} 件(何も回していません)", file=sys.stderr)
        return 1
    subst = {"{py}": a.py, "{ENV_FILE}": a.env_file or str(root / "envs" / "michiomochi-wsl.toml"), "{TEST_CONFIG}": a.test_config or str(root / "tests" / "config.test.toml")}
    if a.check:
        n = {t: sum(1 for x in tests if x["tier"] == t) for t in TIERS}
        print(f"ok tests.toml: {len(tests)} 件(fast {n['fast']} / heavy {n['heavy']})、登録漏れ 0 件")
        return 0
    if a.paths is not None:
        mode, paths = "paths", norm_paths(a.paths, root)
        if not paths:
            print("test_plan: --paths にパスを1つ以上渡してください(make test-paths PATHS=\"tools/safe_deploy.sh ...\")", file=sys.stderr)
            return 2
    else:
        mode, paths = ("fast" if a.fast else "all"), []
    selected = select(tests, mode, paths)
    label = {"fast": "test-fast", "all": "test", "paths": "test-paths"}[mode]
    if a.list:
        for t in selected:
            print(display(t, subst))
        return 0
    if mode == "paths" and not selected:
        print(f"test-paths: 該当なし(heavy のテストに当たるパスが無い: {', '.join(paths)})。fast は make test-fast で回す", flush=True)
        return 0
    failed = execute(selected, mode, root, subst, label, a.fast_cpu_budget)
    if mode == "fast":
        heavy = [t["name"] for t in tests if t["tier"] == "heavy"]
        if heavy:
            print("回していない重い検査: " + ", ".join(heavy) + "(make test で全部、変えたパスに合うものは make test-paths PATHS=... で回す)", flush=True)
    for f in failed:
        print(f"test_plan: FAIL {f}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
