#!/usr/bin/env python3
"""Environment settings of the software factory (standard library only: tomllib).

Every value that differs between installations (Notion ids, paths, profile / board names, model names, chat delivery targets, branch
prefix) lives in ONE TOML file. The scripts read it through this module instead of hard-coding it.

Which file:  $SF_CONFIG when set, otherwise `config.toml` next to this file (deployed there by `make deploy`; the repo keeps the
per-environment sources in envs/<name>.toml and the offline-test one in tests/config.test.toml).

    import sfconfig
    cfg = sfconfig.get()                       # dict, `~` expanded
    cfg["notion"]["task_ds"]

A missing / unreadable file or a missing required key prints which one and exits with status 1 (never a half-working default).
Importing this module does no I/O; `get()` / `load()` read the file.

The values of the levels (L1 / L2 / L3: rows, deliverables, chapters, reviews) live in ONE environment-independent file, `levels.toml`
next to this file ($SF_LEVELS overrides it for tests): `load_levels()` / `get_levels()` below.

CLI (used by the Makefile):  python3 sfconfig.py get <section.key>   (a list prints joined with ':'; `~` expanded)
                              python3 sfconfig.py check              (validate only: the environment config and levels.toml)
"""
import os
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_ENV = "SF_CONFIG"
LEVELS_ENV = "SF_LEVELS"

# section -> {key: "str" | "list"}; owner is a top-level string.
REQUIRED = {
    "": {"owner": "str"},
    "hermes": {"home": "str", "cron_profile": "str", "board": "str", "path_prepend": "list"},
    "paths": {"repo": "str", "live": "str", "skills": "str", "state": "str"},
    "notion": {"parent_page": "str", "task_ds": "str", "epic_ds": "str", "review_ds": "str"},
    "chat": {"deliver": "str", "failure_deliver": "str"},
    "models": {"provider": "str", "worker": "str", "design": "str", "reviewer": "str", "light": "str"},
    "git": {"branch_prefix": "str", "repo_url": "str"},
}

# `${name}` placeholders of the texts (prompts, skills, SOUL, card templates) -> (section, key). Rendered by tools/factory.py.
TEMPLATE_NAMES = {
    "owner": ("", "owner"),
    "board": ("hermes", "board"),
    "cron_profile": ("hermes", "cron_profile"),
    "live": ("paths", "live"),
    "repo": ("paths", "repo"),
    "skills_dir": ("paths", "skills"),
    "state_dir": ("paths", "state"),
    "task_ds": ("notion", "task_ds"),
    "epic_ds": ("notion", "epic_ds"),
    "review_ds": ("notion", "review_ds"),
    "provider": ("models", "provider"),
    "worker_model": ("models", "worker"),
    "design_model": ("models", "design"),
    "reviewer_model": ("models", "reviewer"),
    "branch_prefix": ("git", "branch_prefix"),
    "repo_url": ("git", "repo_url"),
    "deliver": ("chat", "deliver"),                  # 配信先そのまま(`discord:<スレッド>` / `slack:<チャンネル>`、カンマ区切りで複数)。cron の --deliver に渡す形
    "failure_deliver": ("chat", "failure_deliver"),  # 失敗の通知先(同じ形)
    "task_template": ("notion", "task_template"),      # Notion DB の既定テンプレートのページ ID(任意。環境ごとに違う。無ければ空文字)
    "epic_template": ("notion", "epic_template"),
    "review_template": ("notion", "review_template"),
    "mention_user": ("chat", "mention_user"),        # 承認依頼でメンションする Discord ユーザー ID(任意。無ければ空文字)
}
OPTIONAL_TEMPLATE_NAMES = {"mention_user", "task_template", "epic_template", "review_template"}  # 設定に無くても動く名前(空文字になる)
# 配信先の1要素の形: `discord:<ID>` / `discord:<チャンネル>:<スレッド>` / `slack:<チャンネル>` / `slack:<チャンネル>:<スレッドts>`
DELIVER_TARGET_RE = re.compile(r"^(discord|slack):[^\s,:]+(:[^\s,]+)?$")
PATH_TEMPLATE_NAMES = ("live", "repo", "skills_dir", "state_dir")  # names whose value is a path (`~` may be expanded per file kind)

# levels.toml: [L1] [L2] [L3], each with exactly these keys ("strs" = a list of non-empty strings, "str" = a string, "int" = a non-negative integer).
LEVEL_NAMES = ("L1", "L2", "L3")
LEVEL_KEYS = {"rows": "strs", "phases": "strs", "items": "strs", "deliverables": "strs", "judgement_template": "str", "judgement_chapters": "strs",
              "review_chapters": "strs", "ai_reviews": "int", "max_reviews": "int"}
# `${name}` placeholders that come from levels.toml (not from the environment config) -> text. The row titles of a level joined with ／ ("なし" when none).
LEVEL_TEMPLATE_NAMES = ("l1_rows", "l2_rows", "l3_rows", "deliverables")
# `${name}` placeholders that come from hermes/cron.toml (not from the environment config) -> the job key whose period they spell ("N分", e.g. `${gate_every}` -> "2分").
# Texts write the period with these names, never as a number (tools/lint_cron.py fails on a number). Only factory render reads cron.toml (it is not deployed to <live>).
CRON_TEMPLATE_NAMES = {
    "intake_every": "intake",
    "planner_every": "planner",
    "health_every": "health",
    "review_apply_every": "review_apply",
    "gate_every": "gate",
    "ship_every": "ship",
    "task_events_every": "task_events",
}
_SCHEDULE_MINUTES = (re.compile(r"^every (\d+)m$"), re.compile(r"^\*/(\d+) \* \* \* \*$"))

_cache = {}


class ConfigError(Exception):
    pass


def config_path(path=None):
    """Explicit path, else $SF_CONFIG, else config.toml next to this file."""
    if path:
        return Path(path).expanduser()
    env = os.environ.get(CONFIG_ENV)
    return Path(env).expanduser() if env else HERE / "config.toml"


def _expand(v):
    if isinstance(v, str):
        return os.path.expanduser(v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def _validate(data, path):
    if "discord" in data:  # 互換読みはしない
        raise ConfigError(f"{path}: 古い [discord] があります。[chat] に改名してください(値はそのまま)")
    missing, wrong = [], []
    for section, keys in REQUIRED.items():
        node = data if section == "" else data.get(section)
        if section and not isinstance(node, dict):
            missing += [f"[{section}].{k}" for k in keys]
            continue
        for key, kind in keys.items():
            label = key if section == "" else f"[{section}].{key}"
            if key not in node:
                missing.append(label)
                continue
            v = node[key]
            ok = (isinstance(v, str) and v.strip() != "") if kind == "str" else (isinstance(v, list) and all(isinstance(x, str) and x for x in v))
            if not ok:
                wrong.append(f"{label} ({'a non-empty string' if kind == 'str' else 'a list of strings'})")
    for key in ("task_template", "epic_template", "review_template"):
        v = (data.get("notion") or {}).get(key)
        if v is not None and not isinstance(v, str):
            wrong.append(f"[notion].{key} (a string)")
    for key in ("mention_user", "fallback_parent"):  # 任意。あるなら文字列
        v = (data.get("chat") or {}).get(key)
        if v is not None and not isinstance(v, str):
            wrong.append(f"[chat].{key} (a string)")
    mu = (data.get("chat") or {}).get("mention_user")
    if isinstance(mu, str) and mu.strip() and not mu.strip().isdigit():
        wrong.append("[chat].mention_user (Discord のユーザー ID = 数字だけ)")
    for key in ("deliver", "failure_deliver", "fallback_parent"):
        v = (data.get("chat") or {}).get(key)
        if isinstance(v, str) and v.strip():
            bad = [x for x in v.split(",") if not DELIVER_TARGET_RE.match(x)]
            if bad:
                wrong.append(f"[chat].{key} (`discord:<ID>` / `slack:<ID>` をカンマで区切る。合わない要素: {', '.join(repr(x) for x in bad)})")
    wrong += _ship_problems(data.get("ship"))
    if missing or wrong:
        parts = []
        if missing:
            parts.append("missing keys: " + ", ".join(missing))
        if wrong:
            parts.append("wrong type / empty: " + ", ".join(wrong))
        raise ConfigError(f"{path}: " + "; ".join(parts))


def _strs(v):
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v)


def _ship_problems(ship):
    """[ship.repos."owner/repo"] (optional): test = [command…], test_fast = [command…], deploy = [[command…], …], cron_paths = [glob…], describe = "one sentence".
    Returns the problems as texts (an absent [ship] is fine: no repository is then deployed after the merge)."""
    if ship is None:
        return []
    repos = ship.get("repos", {}) if isinstance(ship, dict) else None
    if not isinstance(repos, dict):
        return ["[ship.repos] (a table of [ship.repos.\"owner/repo\"] tables)"]
    out = []
    for name, c in repos.items():
        label = f"[ship.repos.\"{name}\"]"
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", name):
            out.append(f"{label} (the table name is owner/repo)")
        if not isinstance(c, dict):
            out.append(f"{label} (a table)")
            continue
        if "test" in c and not _strs(c["test"]):
            out.append(f"{label}.test (a command as a list of strings)")
        if "test_fast" in c and not _strs(c["test_fast"]):
            out.append(f"{label}.test_fast (a command as a list of strings: the fast checks that ops.py verify runs, e.g. [\"make\", \"test-fast\"])")
        if "cron_paths" in c and not (isinstance(c["cron_paths"], list) and all(isinstance(x, str) and x for x in c["cron_paths"])):
            out.append(f"{label}.cron_paths (a list of path globs)")
        if "deploy" in c and not (isinstance(c["deploy"], list) and c["deploy"] and all(_strs(x) for x in c["deploy"])):
            out.append(f"{label}.deploy (a list of commands, each a list of strings)")
        if "describe" in c and not (isinstance(c["describe"], str) and c["describe"].strip()):
            out.append(f"{label}.describe (a sentence)")
        if c.get("deploy") and not c.get("describe"):
            out.append(f"{label}.describe (required with deploy: the sentence that chapter 1 of the review row says about the deploy)")
        unknown = set(c) - {"test", "test_fast", "deploy", "cron_paths", "describe"}
        if unknown:
            out.append(f"{label} (unknown keys: {', '.join(sorted(unknown))})")
    return out


def load(path=None, expand=True):
    """Parse + validate the config file and return a new dict. Raises ConfigError (see `get` for the exit-1 variant)."""
    p = config_path(path)
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p} (set {CONFIG_ENV}=<path> or deploy config.toml next to sfconfig.py)")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read config {p}: {e}")
    _validate(data, p)
    return _expand(data) if expand else data


def get(expand=True):
    """Cached config of the current process ($SF_CONFIG or the neighbouring config.toml). Exits with status 1 and a clear message on error."""
    key = (str(config_path()), expand)
    if key not in _cache:
        try:
            _cache[key] = load(expand=expand)
        except ConfigError as e:
            print(f"sfconfig: {e}", file=sys.stderr)
            raise SystemExit(1)
    return _cache[key]


def levels_path(path=None):
    """Explicit path, else $SF_LEVELS, else levels.toml next to this file."""
    if path:
        return Path(path).expanduser()
    env = os.environ.get(LEVELS_ENV)
    return Path(env).expanduser() if env else HERE / "levels.toml"


def _level_problems(data):
    wrong = []
    for name in sorted(set(data) - set(LEVEL_NAMES)):
        wrong.append(f"[{name}] (unknown level; the levels are {', '.join(LEVEL_NAMES)})")
    for name in LEVEL_NAMES:
        node = data.get(name)
        if not isinstance(node, dict):
            wrong.append(f"[{name}] (missing)")
            continue
        for key in sorted(set(node) - set(LEVEL_KEYS)):
            wrong.append(f"[{name}].{key} (unknown key)")
        for key, kind in LEVEL_KEYS.items():
            if key not in node:
                wrong.append(f"[{name}].{key} (missing)")
                continue
            v = node[key]
            if kind == "strs":
                ok = isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
            elif kind == "str":
                ok = isinstance(v, str)
            else:
                ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
            if not ok:
                wrong.append(f"[{name}].{key} ({'a list of strings' if kind == 'strs' else 'a string' if kind == 'str' else 'a non-negative integer'})")
        if isinstance(node.get("judgement_template"), str) and bool(node["judgement_template"]) != bool(node.get("judgement_chapters")):
            wrong.append(f"[{name}] (judgement_template and judgement_chapters are both empty or both set)")
    return wrong


def load_levels(path=None):
    """Parse + validate levels.toml -> {"L1": {rows, phases, ...}, "L2": ..., "L3": ...}. Raises ConfigError. (What the values mean against the code: tools/lint_levels.py.)"""
    p = levels_path(path)
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"levels file not found: {p} (levels.toml is deployed next to sfconfig.py; {LEVELS_ENV}=<path> overrides it)")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read levels.toml {p}: {e}")
    wrong = _level_problems(data)
    if wrong:
        raise ConfigError(f"{p}: " + "; ".join(wrong))
    return data


def get_levels():
    """Cached levels.toml of the current process. Exits with status 1 and a clear message on error (never a half-working default)."""
    key = str(levels_path())
    if key not in _cache:
        try:
            _cache[key] = load_levels()
        except ConfigError as e:
            print(f"sfconfig: {e}", file=sys.stderr)
            raise SystemExit(1)
    return _cache[key]


def level_template_vars(levels):
    """{placeholder name: text} of LEVEL_TEMPLATE_NAMES. `deliverables` is the L2 / L3 list (the levels that make phase cards)."""
    out = {f"{lv.lower()}_rows": "／".join(levels[lv]["rows"]) or "なし" for lv in LEVEL_NAMES}
    out["deliverables"] = "、".join(levels["L2"]["deliverables"])
    return out


def schedule_minutes(schedule):
    """The period in minutes of a cron.toml `schedule` (`every Nm`, `*/N * * * *`, `* * * * *` = 1), or None when it is not a minute period (hourly, daily, ...)."""
    s = " ".join(str(schedule).split())
    if s == "* * * * *":
        return 1
    for rx in _SCHEDULE_MINUTES:
        m = rx.match(s)
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


def cron_template_vars(cron, only=None):
    """{placeholder name: "N分"} of CRON_TEMPLATE_NAMES from the parsed hermes/cron.toml. `only` = the job keys to read (default: all of them).
    Raises ConfigError when a job is missing or its schedule is not a minute period (the name could not spell it)."""
    out, wrong = {}, []
    for name, key in CRON_TEMPLATE_NAMES.items():
        if only is not None and key not in only:
            continue
        job = (cron.get("jobs") or {}).get(key)
        if not job:
            wrong.append(f"${{{name}}}: cron.toml has no jobs.{key}")
            continue
        n = schedule_minutes(job.get("schedule", ""))
        if n is None:
            wrong.append(f"${{{name}}}: jobs.{key}.schedule {job.get('schedule')!r} is not a minute period (every Nm / */N * * * * / * * * * *)")
        else:
            out[name] = f"{n}分"
    if wrong:
        raise ConfigError("; ".join(wrong))
    return out


def template_vars(cfg, expand_paths=False):
    """{placeholder name: value} for the `${...}` texts. `cfg` should be the NOT expanded config (texts keep `~`, as written today);
    expand_paths=True expands the path names (for files that must hold absolute paths, e.g. the profile SOUL)."""
    out = {}
    for name, (section, key) in TEMPLATE_NAMES.items():
        node = cfg if section == "" else cfg[section]
        v = node.get(key, "") if name in OPTIONAL_TEMPLATE_NAMES else node[key]
        out[name] = os.path.expanduser(v) if expand_paths and name in PATH_TEMPLATE_NAMES else v
    out.update(level_template_vars(get_levels()))
    out["verify"] = f"python3 {out['live']}/ops.py verify"  # what a card runs to check its work (ops.py verify: the repo's fast checks, then the heavy ones for the changed paths)
    return out


def tool_search_path(cfg=None):
    """hermes.path_prepend as an os.pathsep-joined string (`~` expanded)."""
    cfg = cfg or get()
    return os.pathsep.join(cfg["hermes"]["path_prepend"])


def _main(argv):
    if len(argv) >= 2 and argv[0] == "get":
        section, _, key = argv[1].rpartition(".")
        cfg = get()
        v = cfg[key] if not section else cfg.get(section, {}).get(key)
        if v is None:
            print(f"sfconfig: no such key: {argv[1]}", file=sys.stderr)
            return 1
        print(":".join(v) if isinstance(v, list) else v)
        return 0
    if argv == ["check"]:
        get()
        get_levels()
        print(f"ok {config_path()} {levels_path()}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
