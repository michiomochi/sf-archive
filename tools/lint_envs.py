#!/usr/bin/env python3
"""envs/*.toml の健全性を検査する(標準ライブラリのみ。`make test` から実行)。

環境ごとの壊れは、その環境でだけ起きる(wsl で緑でも mac は赤)。render --all-envs(Makefile の test)に先立って、原因を環境名つきで出す。
検査(環境ごと):
  1. 必須キー: owner / hermes.home・cron_profile・board・path_prepend / paths.repo・live・skills・state / notion.parent_page・*_ds /
     chat.deliver・failure_deliver / models / git(scripts/sfconfig.py の REQUIRED と型・配信先の形)
  2. plugins の各名前が plugins/<名前>/plugin.toml か [plugin_sources] の作業コピーにある(作業コピーが無いだけなら警告。render も飛ばす)。
     plugins の重複も落とす
  3. `${名前}` の重複: 本体の名前(sfconfig.TEMPLATE_NAMES)とプラグインの [template_names]、プラグイン同士
  4. 置き場の衝突: hermes.home / paths.repo / paths.live / paths.skills / paths.state のうち 2 つが同じ場所を指していない(`~` を展開、`..` と末尾の / を畳んで比べる)
  5. 環境名と OS: Makefile の ENV の既定(uname -s が Darwin なら何、それ以外なら何)が、実在する環境で、名前の末尾が OS と合う
     (-mac は Darwin、-wsl は Darwin 以外)。envs/ に OS の決まらない名前があれば落とす
  6. ship の登録: [ship.repos."owner/repo"] に test があるのに test_fast が無い → 落とす(ops.py verify が回す fast の検査。文字列で書いた場合も落ちる: 1 の型の検査)

    python3 tools/lint_envs.py              # envs/*.toml すべて
    python3 tools/lint_envs.py FILE...      # 指定した環境の設定だけ(テスト用)

見つけたら `[環境名] 説明` を出し、終了コード 1。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tools"))
import sfconfig  # noqa: E402
import sfplugins  # noqa: E402

PATH_KEYS = [("hermes", "home"), ("paths", "repo"), ("paths", "live"), ("paths", "skills"), ("paths", "state")]
# 環境名の末尾 -> その環境が既定になる OS(uname -s)。新しい OS を足すときはここと Makefile の ENV を一緒に直す。
ENV_SUFFIX_OS = {"-mac": "Darwin", "-wsl": "Linux"}


def norm_path(p):
    """同じ場所かを比べる形: `~` 展開、`.` と `..` と末尾の / を畳む(ファイルには触れない)。"""
    return os.path.normpath(os.path.expanduser(p))


def check_required(raw, path):
    try:
        sfconfig._validate(raw, path)
    except sfconfig.ConfigError as e:
        return [str(e).split(": ", 1)[1] if ": " in str(e) else str(e)]
    return []


def check_plugins(raw, repo=REPO):
    """([問題], [警告])"""
    problems, warns = [], []
    names = raw.get("plugins", [])
    if isinstance(names, list):
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            problems.append("plugins に同じ名前が複数: " + ", ".join(dup))
    try:
        enabled = sfplugins.enabled(raw, repo)
    except sfplugins.SourceMissing as e:
        warns.append(str(e))
        return problems, warns
    except sfplugins.PluginError as e:
        problems.append(str(e))
        return problems, warns
    for n in enabled:
        try:
            sfplugins.load_manifest(n, repo)
        except sfplugins.PluginError as e:
            problems.append(str(e))
    try:
        missing = sfplugins.config_problems(raw, repo)
        if missing:
            problems.append("プラグインの設定が足りません: " + ", ".join(missing))
    except sfplugins.PluginError as e:
        problems.append(str(e))
    return problems, warns


def check_template_names(raw, repo=REPO):
    try:
        names = sfconfig.template_vars(raw)
    except KeyError:
        return []  # 必須キーの欠けは check_required が出す
    names.update(dict.fromkeys(sfconfig.CRON_TEMPLATE_NAMES, ""))  # cron の周期の名前(値は hermes/cron.toml から。ここでは重複だけ見る)
    try:
        sfplugins.template_vars(raw, names, repo)
    except sfplugins.SourceMissing:
        return []
    except sfplugins.PluginError as e:
        return ["`${名前}` が重複しています: " + str(e)]
    return []


def check_ship_test_fast(raw):
    """[ship.repos."owner/repo"] に test があるのに test_fast が無い。ops.py verify が回す fast の検査が無いと、カードと reviewer は make test(重い)を回すしかなくなる。型(文字列でなくリスト)は check_required が見る。"""
    repos = (raw.get("ship") or {}).get("repos") if isinstance(raw.get("ship"), dict) else None
    if not isinstance(repos, dict):
        return []
    return [f'[ship.repos."{name}"] に test があるのに test_fast がありません(ops.py verify が回す fast の検査。例: test_fast = ["make", "test-fast"])'
            for name, c in repos.items() if isinstance(c, dict) and "test" in c and "test_fast" not in c]


def check_path_clash(raw):
    seen, problems = {}, []
    for section, key in PATH_KEYS:
        v = (raw.get(section) or {}).get(key)
        if not isinstance(v, str) or not v.strip():
            continue  # 欠けは check_required が出す
        p = norm_path(v)
        label = f"{section}.{key}"
        if p in seen:
            problems.append(f"{seen[p]} と {label} が同じ場所です({p})")
        else:
            seen[p] = label
    return problems


# ---------------------------------------------------------------- 環境名と OS(Makefile の ENV の既定)

def makefile_default_env(uname, repo=REPO, make_args=()):
    """uname -s が `uname` のとき、Makefile の ENV が何になるか(偽の uname を PATH の先頭に置いて、実物の Makefile に評価させる)。
    make_args に `ENV=...` を渡すと、明示した値が既定に勝つことも見られる。"""
    tmp = Path(tempfile.mkdtemp(prefix="lint_envs_"))
    try:
        fake = tmp / "uname"
        fake.write_text(f"#!/bin/sh\necho {uname}\n", encoding="utf-8")
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "ENV")}
        env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
        # macOS 標準の make 3.81 は --eval を知らないので、Makefile を include する一時 makefile で ENV を出す。
        wrapper = tmp / "print_env.mk"
        wrapper.write_text(f"include {repo / 'Makefile'}\nlint-print-env:\n\t@echo $(ENV)\n", encoding="utf-8")
        r = subprocess.run(["make", "--no-print-directory", "-s", "-C", str(repo), "-f", str(wrapper), *make_args, "lint-print-env"],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"make が ENV の既定を出せません: {r.stderr.strip()}")
        return r.stdout.strip().splitlines()[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_env_os_mapping(repo=REPO):
    """環境名(ファイル名)の末尾と OS の対応、と Makefile の ENV の既定の一致。{環境名: [問題]}(環境に属さないものは "" )。"""
    out = {}
    names = [p.stem for p in sorted((repo / "envs").glob("*.toml"))]

    def add(key, msg):
        out.setdefault(key, []).append(msg)

    for n in names:
        if not any(n.endswith(s) for s in ENV_SUFFIX_OS):
            add(n, f"環境名の末尾 {'/'.join(ENV_SUFFIX_OS)} のどれでもなく、どの OS の既定か決まりません(tools/lint_envs.py の ENV_SUFFIX_OS と Makefile の ENV を直す)")
    for uname in ("Darwin", "Linux"):
        try:
            got = makefile_default_env(uname, repo)
        except RuntimeError as e:
            add("", f"uname -s = {uname}: {e}")
            continue
        if got not in names:
            add("", f"Makefile の ENV の既定が envs/ に無い環境です(uname -s = {uname} → {got})")
            continue
        want = [s for s, os_name in ENV_SUFFIX_OS.items() if (os_name == "Darwin") == (uname == "Darwin")]
        if not any(got.endswith(s) for s in want):
            add("", f"Makefile の ENV の既定が OS と合いません(uname -s = {uname} → {got}。期待する名前の末尾: {'/'.join(want)})")
    return out


# ---------------------------------------------------------------- 実行

def lint_env(path, repo=REPO):
    """([問題], [警告])。path は envs/<名前>.toml。"""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [f"読めません: {e}"], []
    problems = check_required(raw, path)
    p2, warns = check_plugins(raw, repo)
    problems += p2
    problems += check_template_names(raw, repo)
    problems += check_path_clash(raw)
    problems += check_ship_test_fast(raw)
    sfplugins._SOURCES.clear()
    return problems, warns


def main(argv, repo=REPO):
    if argv:
        paths, mapping = [Path(a) for a in argv], {}
    else:
        paths = sorted((repo / "envs").glob("*.toml"))
        mapping = check_env_os_mapping(repo)
        if not paths:
            print("lint_envs: envs/*.toml がありません", file=sys.stderr)
            return 1
    failed = 0
    for p in paths:
        problems, warns = lint_env(p, repo)
        problems += mapping.get(p.stem, [])
        for w in warns:
            print(f"WARN [{p.stem}] {w}")
        for pr in problems:
            print(f"FAIL [{p.stem}] {pr}", file=sys.stderr)
        failed += len(problems)
        if not problems:
            print(f"ok [{p.stem}]")
    for pr in mapping.get("", []):
        print(f"FAIL [Makefile] {pr}", file=sys.stderr)
        failed += 1
    if failed:
        print(f"lint_envs: {failed} 件", file=sys.stderr)
        return 1
    print(f"ok lint_envs: {len(paths)} 環境")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
