#!/usr/bin/env python3
"""sf（ソフトウェアファクトリー）の配布ツール(標準ライブラリのみ)。

  factory.py render [--all-envs] [--dry-run] [--out DIR]
      `${...}` を含む雛形(prompts/*.txt、skills/**/*.md、hermes/profiles/*/SOUL.md、scripts/templates/*.md、notion/templates/*.md)を環境の設定で置換し、
      build/<環境名>/ に同じ構成で出す。知らない名前・不正な `$`・置換残りがあれば内容を表示して終了コード 1。--dry-run は何も書かない。
  factory.py check
      雛形を置換した結果と、本番(cron の指示文、スキル、プロファイルの SOUL、カードのテンプレート)を比べる。
      差があれば要約(ファイルごとの差分の先頭)を出して終了コード 1。本番は変更しない。

  ---- インストーラ(M1 PR3)。cron.toml / wrapper.sh.tmpl / profiles / schema.json は hermes/ と notion/ にある ----
  factory.py cron adopt KEY=ID ...      <live>/cron_ids.json にキーとジョブ ID を保存。台帳(jobs.json)にない ID があれば全部やめる
  factory.py cron sync [--dry-run]      hermes/cron.toml と台帳を比べ、差は `hermes -p <cron_profile> cron edit`、無いジョブは --paused で作る
  factory.py cron pause|resume [--dry-run]   cron_ids.json のジョブだけ
  factory.py wrappers [--dry-run] [--out DIR]   cron の薄い .sh(LIVE・PATH・リポジトリのパスを埋め込み済み。実行時に設定を読まない)
  factory.py profiles [--dry-run] [--update]    executor / reviewer(あれば作らない。--update で既存の差も直す)
  factory.py board [--dry-run]          kanban ボード(あれば何もしない)と cron_profile の kanban.review_dispatch
  factory.py notion-export-schema       本番の3つのDBを読んで notion/schema.json へ(読むだけ)
  factory.py notion-bootstrap [--dry-run]   schema.json から DB を epic -> task -> review の順に作り、ds ID を envs に書き戻して git commit
  factory.py doctor                     環境の検査。足りないものを並べ、1つでもあれば終了コード 1
  factory.py smoke [--dry-run]          読み取りだけの動作確認
hermes / ntn の場所は環境変数 SF_HERMES / SF_NTN、cron_ids.json の場所は SF_CRON_IDS で差し替えられる(オフラインのテスト用)。

環境の設定の選び方: --config FILE > $SF_CONFIG > --env NAME(envs/NAME.toml) > envs/ にある唯一のファイル。
置換する名前は scripts/sfconfig.py の TEMPLATE_NAMES(owner, board, cron_profile, live, repo, skills_dir, state_dir,
task_ds, epic_ds, review_ds, task_template, epic_template, review_template(任意), provider, worker_model, design_model, reviewer_model, branch_prefix, repo_url, deliver,
failure_deliver)。
cron の周期の名前(intake_every, planner_every, health_every, review_apply_every, gate_every, ship_every, task_events_every。値は「N分」)は
hermes/cron.toml の schedule から引く(sfconfig.CRON_TEMPLATE_NAMES)。文には周期の数値を書かずこの名前で書く(tools/lint_cron.py が落とす)。
パスは設定に書いた形(`~` のまま)で入れる。例外は SOUL.md で、本番が絶対パスなので展開して入れる。
scripts/templates/*.md は実行時にコードが埋める `${hub_name}` などを残し(RUNTIME_NAMES)、環境の名前だけ置換する。
`$` をそのまま書きたい場所は `$$` と書く。
"""
import argparse
import difflib
import json
import os
import re
import shlex
import string
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import sfconfig  # noqa: E402
sys.path.insert(0, str(REPO / "tools"))
import sfplugins  # noqa: E402

# 雛形の種類 -> (リポジトリ内の glob)。順序が表示順。
KINDS = {
    "prompt": ["prompts/*.txt"],
    "skill": ["skills/**/*.md"],
    "soul": ["hermes/profiles/*/SOUL.md"],
    "card": ["scripts/templates/*.md"],
    "notion": ["notion/templates/*.md"],  # Notion の既定テンプレートの本文(UI に貼る本文は build/<環境>/ のこれ)
}
# カードの雛形で、実行時にコード(epic_gate_controller / epic_common.render)が埋める名前。置換せずそのまま残す。
# epic_selftest の templates_load_and_have_expected_fields が、雛形の名前の集合と一致することを確かめている。
RUNTIME_NAMES = {"epic_url", "hub_name", "ph", "prow_url", "parts", "parallel_note", "full", "text", "n_groups", "group_names", "g",
                 "phases", "rows", "hub_url", "gate", "why", "recommendation", "reason", "key", "evidence",
                 "judge", "recommend", "checks", "conditions", "ships", "risks", "links", "copied_at", "pr", "deploy"}
DIFF_LINES = 8  # check が1ファイルにつき出す差分の行数


class RenderError(Exception):
    pass


class SourceMissingError(RenderError):
    """プラグインの作業コピーが無い。--all-envs / テストでは、その環境を飛ばす。"""


# ------------------------------------------------------------------ render

def source_files(repo=REPO, plugins=None):
    """[(kind, repo-relative path)] of every template source. plugins=None は plugins/ にあるプラグインすべて(静的な検査用)、
    名前のリストならそのプラグインだけ(環境の設定 plugins = [...] で有効なもの)。"""
    out = []
    for kind, globs in KINDS.items():
        for g in globs:
            out += [(kind, p.relative_to(repo).as_posix()) for p in sorted(repo.glob(g)) if p.is_file() and "__pycache__" not in p.parts]
    return out + sfplugins.source_files(repo, sfplugins.available(repo) if plugins is None else plugins)


def _lineno(text, pos):
    return text.count("\n", 0, pos) + 1


def render_text(text, kind, cfg_raw, label="<text>"):
    """Strict `string.Template` substitution. Raises RenderError listing every unknown name / invalid `$` with its line."""
    names = sfconfig.template_vars(cfg_raw, expand_paths=(kind == "soul"))
    try:
        names.update(sfconfig.cron_template_vars(load_cron_toml()))  # ${gate_every} など: 周期は hermes/cron.toml の schedule から引く(文に数値を書かない)
    except sfconfig.ConfigError as e:
        raise RenderError(f"hermes/cron.toml: {e}")
    try:
        names.update(sfplugins.template_vars(cfg_raw, names, REPO))
    except sfplugins.PluginError as e:
        raise RenderError(str(e))
    problems = []
    for m in string.Template.pattern.finditer(text):
        name = m.group("named") or m.group("braced")
        if m.group("invalid") is not None:
            problems.append(f"{label}:{_lineno(text, m.start())}: 不正な `$`(そのまま書くなら `$$`): {text[m.start():m.start() + 20]!r}")
        elif name is not None and name not in names and not (kind == "card" and name in RUNTIME_NAMES):
            problems.append(f"{label}:{_lineno(text, m.start())}: 知らない名前 ${{{name}}}")
    if problems:
        raise RenderError("\n".join(problems))
    values = dict(names)
    if kind == "card":
        values.update({n: "${" + n + "}" for n in RUNTIME_NAMES})  # 実行時の名前は自分自身に置換 = 残す
    out = string.Template(text).substitute(values)
    left = [n for n in names if kind != "card" and "${" + n + "}" in out]
    if left:
        raise RenderError(f"{label}: 置換残り {left}")
    return out


def render_all(cfg_raw, repo=REPO):
    """({repo-relative path: rendered text}, {path: kind}); raises RenderError with all problems of all files."""
    out, kinds, problems = {}, {}, []
    try:
        plugins = sfplugins.enabled(cfg_raw, repo)
        missing = sfplugins.config_problems(cfg_raw, repo)
        if missing:
            raise sfplugins.PluginError("プラグインの設定が足りません: " + ", ".join(missing))
    except sfplugins.SourceMissing as e:
        raise SourceMissingError(str(e))
    except sfplugins.PluginError as e:
        raise RenderError(str(e))
    for kind, rel in source_files(repo, plugins):
        try:
            out[rel] = render_text(sfplugins.read_source(repo, rel), kind, cfg_raw, rel)
            kinds[rel] = kind
        except RenderError as e:
            problems.append(str(e))
    if problems:
        raise RenderError("\n".join(problems))
    try:  # 同名のスキル・指示文を 2 か所が作っていないか(置換後に検査)
        sfplugins.deployed_skills(out)
        sfplugins.deployed_prompts(out)
    except sfplugins.PluginError as e:
        raise RenderError(str(e))
    return out, kinds


# ------------------------------------------------------------------ environments

def env_files(repo=REPO):
    return sorted((repo / "envs").glob("*.toml"))


def pick_config(args, repo=REPO):
    if getattr(args, "config", None):
        return Path(args.config)
    if os.environ.get(sfconfig.CONFIG_ENV):
        return Path(os.environ[sfconfig.CONFIG_ENV]).expanduser()
    if getattr(args, "env", None):
        return repo / "envs" / f"{args.env}.toml"
    files = env_files(repo)
    if len(files) == 1:
        return files[0]
    raise SystemExit("factory: 環境が決められません。--env NAME か --config FILE か SF_CONFIG を指定してください(envs/: " + ", ".join(p.stem for p in files) + ")")


def load_cfg(path):
    try:
        return sfconfig.load(path, expand=False), sfconfig.load(path, expand=True)
    except sfconfig.ConfigError as e:
        raise SystemExit(f"factory: {e}")


# ------------------------------------------------------------------ render command

def cmd_render(args):
    paths = env_files() if args.all_envs else [pick_config(args)]
    if not paths:
        raise SystemExit("factory: envs/*.toml がありません")
    base = Path(args.out) if args.out else REPO / "build"
    failed = 0
    for cfg_path in paths:
        name = cfg_path.stem
        raw, _ = load_cfg(cfg_path)
        try:
            files, _ = render_all(raw)
        except SourceMissingError as e:
            print(f"SKIP [{name}] {e}")
            continue
        except RenderError as e:
            print(f"FAIL [{name}] 置換できません:\n{e}", file=sys.stderr)
            failed += 1
            continue
        if not args.dry_run:
            for rel, text in files.items():
                dst = base / name / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text, encoding="utf-8")
        print(f"{'DRY ' if args.dry_run else ''}OK [{name}] {len(files)} ファイルを置換" + ("" if args.dry_run else f" -> {base / name}"))
    return 1 if failed else 0


# ------------------------------------------------------------------ check command

def load_jobs(jobs_json):
    data = json.loads(Path(jobs_json).read_text(encoding="utf-8"))
    return data["jobs"] if isinstance(data, dict) else data


def profile_root(home, name):
    """プロファイルの場所。default プロファイルは Hermes の home そのもの(profiles/default ではない)。"""
    return home if name == "default" else home / "profiles" / name


def live_locations(cfg):
    """Where the production copies live (from the env's expanded config)."""
    home = Path(cfg["hermes"]["home"])
    return dict(live=Path(cfg["paths"]["live"]), skills=Path(cfg["paths"]["skills"]), profiles=home / "profiles",
                jobs=profile_root(home, cfg["hermes"]["cron_profile"]) / "cron" / "jobs.json")


def diff_head(have, want, n=DIFF_LINES):
    lines = list(difflib.unified_diff(have.splitlines(), want.splitlines(), "本番", "リポジトリ(置換後)", lineterm="", n=0))
    return [l[:160] for l in lines[:n]] + ([f"... (他 {len(lines) - n} 行)"] if len(lines) > n else [])


def compare(files, kinds, cfg):
    """[(path, reason, [diff lines])] — empty when production equals the rendered repo."""
    loc = live_locations(cfg)
    drift = []

    def cmp_file(rel, want, live_path, strip=False):
        if not live_path.is_file():
            drift.append((rel, f"本番に無い: {live_path}", []))
            return
        have = live_path.read_text(encoding="utf-8")
        if (have.strip() != want.strip()) if strip else (have != want):
            drift.append((rel, f"内容が違う: {live_path}", diff_head(have, want)))

    try:
        jobs = load_jobs(loc["jobs"])
    except (OSError, ValueError) as e:
        jobs = None
        drift.append(("prompts/*", f"cron の台帳を読めない: {loc['jobs']} ({e})", []))
    skills_seen = {}
    prompt_job = prompt_jobs(cfg)
    deployed = sfplugins.deployed_skills(files)
    for name, sub_texts in deployed.items():
        for sub, text in sub_texts.items():
            cmp_file(f"skills/{name}/{sub}", text, loc["skills"] / name / sub)
            skills_seen.setdefault(name, set()).add(f"{name}/{sub}")
    for rel, kind in kinds.items():
        want = files[rel]
        if kind == "prompt":
            key = Path(rel).stem
            prefix = prompt_job.get(key)
            if prefix is None:
                drift.append((rel, "PROMPT_JOBS に対応するジョブ名が無い", []))
            elif jobs is not None:
                job = next((j for j in jobs if j.get("name", "").startswith(prefix)), None)
                if job is None:
                    drift.append((rel, f"ジョブ「{prefix}」が見つからない", []))
                elif (job.get("prompt") or "").strip() != want.strip():
                    drift.append((rel, f"cron の指示文が違う(job {job['id']})", diff_head(job.get("prompt") or "", want)))
        elif kind == "skill":
            continue  # 上の deployed_skills で比べた(プラグインのスキルと SKILL.addendum.md の合成を含む)
        elif kind == "soul":
            cmp_file(rel, want, loc["profiles"] / Path(rel).relative_to("hermes/profiles"))
        elif kind == "card":
            cmp_file(rel, want, loc["live"] / "templates" / Path(rel).name)
    for name, subs in sorted(skills_seen.items()):  # 本番だけにあるスキルのファイル
        root = loc["skills"] / name
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                r = p.relative_to(loc["skills"]).as_posix()
                if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store" and r not in subs:
                    drift.append((f"skills/{r}", "リポジトリに無い(本番だけにある)", []))
    return drift


def cmd_check(args):
    cfg_path = pick_config(args)
    raw, expanded = load_cfg(cfg_path)
    try:
        files, kinds = render_all(raw)
    except RenderError as e:
        print(f"FAIL 置換できません:\n{e}", file=sys.stderr)
        return 1
    drift = compare(files, kinds, expanded)
    for rel, why, diff in drift:
        print(f"差分 {rel}: {why}")
        for l in diff:
            print("    " + l)
    n = len(files)
    if drift:
        print(f"差分あり: {len(drift)} 件 / {n} ファイル(設定 {cfg_path})")
        return 1
    print(f"差分なし: {n} ファイルを置換した結果が本番と一致(設定 {cfg_path})")
    return 0


# ------------------------------------------------------------------ install: 共通
# ここから下は「インストーラ」(cron / プロファイル / ボード / Notion DB / doctor)。
# 外部コマンドは hermes と ntn だけ。パスは環境変数 SF_HERMES / SF_NTN で差し替えられる(オフラインのテスト用)。

CRON_TOML = REPO / "hermes" / "cron.toml"
WRAPPER_TMPL = REPO / "hermes" / "wrapper.sh.tmpl"
SMART_POLICY = REPO / "hermes" / "profiles" / "smart_policy.txt"
SCHEMA_JSON = REPO / "notion" / "schema.json"
CRON_IDS_NAME = "cron_ids.json"  # <paths.live>/cron_ids.json: {"intake": "<ジョブ ID>", ...}
MIN_HERMES = (0, 22, 6)  # 動作を確かめた Hermes(cron --monitor-script / --failure-deliver / kanban boards が要る)
# プロファイル名・工程の順番・ゲートは環境差ではなくリポジトリに固定する。model は [models].<名前>。
PROFILES = {
    "executor": dict(model="worker", reasoning="high", max_turns=150,
                     description="{owner} pipeline executor (要件/設計/実装/テスト/リリース判定)"),
    "reviewer": dict(model="reviewer", reasoning="high", max_turns=150,
                     description="{owner} pipeline reviewer (独立した検証と完了ゲート)"),
}
# Notion の DB。作る順番(関係の相手が先にできている)と、設定 [notion] のキー。
DB_ORDER = ("epic", "task", "review")
DB_ENV_KEY = {"epic": "epic_ds", "task": "task_ds", "review": "review_ds"}
SIMPLE_PROP_TYPES = ("title", "rich_text", "url", "checkbox", "created_time", "last_edited_time")


def die(msg):
    raise SystemExit(f"factory: {msg}")


class Ctx:
    """1つの環境の設定と、そこから決まる場所。インストーラの各コマンドが共有する。"""

    def __init__(self, args):
        self.path = pick_config(args)
        self.raw, self.cfg = load_cfg(self.path)
        c = self.cfg
        self.home = Path(c["hermes"]["home"])
        self.cron_profile = c["hermes"]["cron_profile"]
        self.board = c["hermes"]["board"]
        self.live = Path(c["paths"]["live"])
        self.skills = Path(c["paths"]["skills"])
        self.repo = Path(c["paths"]["repo"])
        self.profiles_dir = self.home / "profiles"
        self.jobs_json = profile_root(self.home, self.cron_profile) / "cron" / "jobs.json"
        self.scripts_dir = profile_root(self.home, self.cron_profile) / "scripts"
        self.ids_path = Path(os.environ["SF_CRON_IDS"]).expanduser() if os.environ.get("SF_CRON_IDS") else self.live / CRON_IDS_NAME  # 環境変数はテスト用
        # hermes / ntn / python3 を見つけるため、設定の path_prepend を PATH の先頭に足す(Makefile を通さない呼び出しでも同じ)
        os.environ["PATH"] = os.pathsep.join(c["hermes"]["path_prepend"] + [os.environ.get("PATH", "")])

    def rendered(self):
        try:
            return render_all(self.raw)[0]
        except RenderError as e:
            die(f"置換できません:\n{e}")


def run_cmd(cmd, input_text=None, timeout=180):
    """外部コマンドを実行して CompletedProcess を返す(見つからない・時間切れも rc で返す。例外にしない)。"""
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: コマンドが見つかりません")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"時間切れ({timeout}秒): {' '.join(cmd[:4])}")


def hermes_cmd(*args, profile=None):
    return [os.environ.get("SF_HERMES", "hermes"), *(["-p", profile] if profile else []), *args]


def ntn_cmd(*args):
    return [os.environ.get("SF_NTN", "ntn"), *args]


def fmt_cmd(cmd):
    """表示用(長い引数は切る)。実行には使わない。"""
    return " ".join(shlex.quote(a if len(a) <= 120 else a[:80] + f"…({len(a)}文字)") for a in cmd)


def oneline(v, n=90):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("\n", "⏎")
    return s if len(s) <= n else s[:n - 1] + "…"


# ------------------------------------------------------------------ cron: 定義と本番の読み取り

def load_cron_toml(raw=None, path=CRON_TOML, plugins=None):
    """本体の hermes/cron.toml。raw(環境の設定)があれば有効なプラグインの cron.toml を足す(ジョブのキーは <プラグイン>.<キー>)。
    plugins=名前のリストで直接指定もできる(静的な検査用)。"""
    try:
        with open(path, "rb") as f:
            core = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"{path} を読めません: {e}")
    if raw is None and plugins is None:
        return core
    try:
        return sfplugins.merge_cron(core, raw if plugins is None else {"plugins": plugins})
    except sfplugins.PluginError as e:
        die(str(e))


def prompt_jobs(raw):
    """prompts/<ファイル>.txt -> ジョブ名の前方一致(「（」の手前まで)。環境で有効なプラグインのジョブだけを含む(check / sync_live.py が本番との照合に使う)。
    無効なプラグインのジョブを入れると、置換済みの指示文が無いまま照合して落ちる。全プラグインの静的な検査は load_cron_toml(plugins=sfplugins.available()) で別にやる。"""
    return sfplugins.prompt_jobs(load_cron_toml(raw))


def desired_jobs(cron, cfg, rendered):
    """cron.toml + 環境の設定 + 置換済みの指示文 -> {キー: 本番にあるべきジョブ}。参照の誤りは全部まとめて停止する。"""
    d, out, problems = cron.get("defaults", {}), {}, []
    wrappers = cron.get("wrappers", {})
    prompts = sfplugins.deployed_prompts(rendered)
    for key, j in cron.get("jobs", {}).items():
        def ref(section, field, default=None):
            name = j.get(field, d.get(field, default))
            if name is None or name not in cfg[section]:
                problems.append(f"jobs.{key}: {field}={name!r} が設定 [{section}] にありません")
                return None
            return cfg[section][name]
        want = dict(key=key, name=j.get("name"), schedule=j.get("schedule"), catch_up=j.get("catch_up", d.get("catch_up", "off")),
                    deliver=ref("chat", "deliver"), failure_deliver=ref("chat", "failure_deliver"), no_agent=bool(j.get("no_agent")))
        for need in ("name", "schedule"):
            if not want[need]:
                problems.append(f"jobs.{key}: {need} がありません")
        if want["no_agent"]:
            want.update(script=j.get("script"), monitor_script=None)
            if not want["script"]:
                problems.append(f"jobs.{key}: no_agent=true なのに script がありません")
        else:
            want.update(script=j.get("script"), monitor_script=j.get("monitor"), skills=list(j.get("skills", [])),
                        model=ref("models", "model"), provider=ref("models", "provider"), reasoning=j.get("reasoning"))
            if j.get("prompt") not in prompts:
                problems.append(f"jobs.{key}: prompt={j.get('prompt')!r} の prompts/{j.get('prompt')}.txt(プラグインなら plugins/<名前>/prompts/)がありません")
            else:
                want["prompt"] = prompts[j["prompt"]].strip()
        for f in ("script", "monitor_script"):
            if want.get(f) and want[f] not in wrappers:
                problems.append(f"jobs.{key}: {f}={want[f]!r} が [wrappers] にありません")
        out[key] = want
    if problems:
        die("hermes/cron.toml:\n  " + "\n  ".join(problems))
    return out


def live_view(job):
    """jobs.json の1件を、desired_jobs と比べられる形にする。"""
    return dict(name=job.get("name"), schedule=job.get("schedule_display") or (job.get("schedule") or {}).get("display"),
                catch_up={True: "on", False: "off"}.get(job.get("catch_up")), deliver=job.get("deliver"),
                failure_deliver=job.get("failure_deliver"), no_agent=bool(job.get("no_agent")), script=job.get("script") or None,
                monitor_script=job.get("monitor_script") or None, prompt=(job.get("prompt") or "").strip(),
                skills=list(job.get("skills") or []), model=job.get("model"), provider=job.get("provider"),
                reasoning=job.get("reasoning_effort"))


AGENT_ONLY = ("prompt", "skills", "model", "provider", "reasoning")
COMMON_FIELDS = ("name", "schedule", "catch_up", "deliver", "failure_deliver", "no_agent", "script", "monitor_script")
EDIT_FLAGS = dict(name="--name", schedule="--schedule", prompt="--prompt", model="--model", provider="--provider",
                  reasoning="--reasoning-effort", deliver="--deliver", failure_deliver="--failure-deliver", catch_up="--catch-up",
                  script="--script", monitor_script="--monitor-script")


def job_diffs(want, job):
    """[(項目, 本番の値, あるべき値)]。AI を使わないジョブは指示文・モデル・スキルを比べない。"""
    have = live_view(job)
    fields = COMMON_FIELDS + (() if want["no_agent"] else AGENT_ONLY)
    return [(f, have[f], want.get(f)) for f in fields if have[f] != want.get(f)]


def edit_args(diffs, want):
    args = []
    for f, _, w in diffs:
        if f == "skills":
            args += [a for s in w for a in ("--skill", s)] if w else ["--clear-skills"]
        elif f == "no_agent":
            args.append("--no-agent" if w else "--agent")
        else:
            args += [EDIT_FLAGS[f], "" if w is None else str(w)]
    return args


def create_args(want, paused_reason):
    args = [want["schedule"]] + ([want["prompt"]] if want.get("prompt") else [])
    args += ["--name", want["name"], "--deliver", want["deliver"], "--failure-deliver", want["failure_deliver"],
             "--catch-up", want["catch_up"], "--paused", "--paused-reason", paused_reason]
    if want["no_agent"]:
        args += ["--no-agent", "--script", want["script"]]
    else:
        if want.get("script"):  # AI を起こす前に実行し、出力を指示文に注入するスクリプト
            args += ["--script", want["script"]]
        if want.get("monitor_script"):
            args += ["--monitor-script", want["monitor_script"]]
        args += ["--model", want["model"], "--provider", want["provider"],
                 "--reasoning-effort", want["reasoning"]] + [a for s in want["skills"] for a in ("--skill", s)]
    return args


def read_ids(ctx, required=False):
    if not ctx.ids_path.is_file():
        if required:
            die(f"{ctx.ids_path} がありません。先に `factory.py cron adopt <キー>=<ジョブ ID> ...` を実行してください")
        return {}
    try:
        ids = json.loads(ctx.ids_path.read_text(encoding="utf-8"))
        assert isinstance(ids, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in ids.items())
        return ids
    except (ValueError, AssertionError) as e:
        die(f"{ctx.ids_path} が壊れています: {e}")


def write_ids(ctx, ids):
    ctx.ids_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.ids_path.write_text(json.dumps(dict(sorted(ids.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jobs(ctx):
    try:
        return load_jobs(ctx.jobs_json)
    except (OSError, ValueError, KeyError) as e:
        die(f"cron の台帳を読めません: {ctx.jobs_json} ({e})")


# ------------------------------------------------------------------ cron adopt / sync / pause / resume

def cmd_cron_adopt(args):
    ctx = Ctx(args)
    cron = load_cron_toml(ctx.raw)
    pairs, problems = {}, []
    for p in args.pairs:
        key, eq, jid = p.partition("=")
        if not eq or not key or not jid:
            problems.append(f"{p!r}: キー=ジョブ ID の形で書いてください")
        elif key not in cron["jobs"]:
            problems.append(f"{key}: hermes/cron.toml にないキーです(あるキー: {', '.join(cron['jobs'])})")
        else:
            pairs[key] = jid
    jobs = {j["id"]: j for j in read_jobs(ctx)}
    for key, jid in pairs.items():
        if jid not in jobs:
            problems.append(f"{key}={jid}: {ctx.jobs_json} にそのジョブ ID がありません")
    ids = {**read_ids(ctx), **pairs}
    dup = {i for i in ids.values() if list(ids.values()).count(i) > 1}
    if dup:
        problems.append(f"同じジョブ ID が複数のキーに割り当てられます: {sorted(dup)}")
    if problems:
        print("adopt を中止しました(何も書いていません):", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    write_ids(ctx, ids)
    for key, jid in pairs.items():
        want_name = cron["jobs"][key]["name"]
        note = "" if jobs[jid].get("name") == want_name else f"  ※名前が違う(本番「{jobs[jid].get('name')}」/ cron.toml「{want_name}」。sync で直ります)"
        print(f"adopt {key} = {jid}  「{jobs[jid].get('name')}」{note}")
    print(f"{len(ids)} 件を {ctx.ids_path} に保存")
    return 0


def plan_sync(ctx, desired):
    """(作成の一覧, 変更の一覧, 一致の一覧, 取り込み済みなのに cron.toml に無いキー)。矛盾があれば全部まとめて停止する(何も変更しない)。"""
    ids, jobs, problems = read_ids(ctx), read_jobs(ctx), []
    byid = {j["id"]: j for j in jobs}
    create, edit, same = [], [], []
    for key, want in desired.items():
        if key in ids:
            job = byid.get(ids[key])
            if job is None:
                problems.append(f"{key}: cron_ids.json の ID {ids[key]} が {ctx.jobs_json} にありません(消えたジョブ。cron_ids.json を直すか adopt し直す)")
                continue
            diffs = job_diffs(want, job)
            (edit if diffs else same).append((key, job, diffs) if diffs else (key, job))
        else:
            clash = [j for j in jobs if j.get("name") == want["name"]]
            if clash:
                problems.append(f"{key}: 同じ名前のジョブ {clash[0]['id']}「{want['name']}」が既にあります。重複して作らず、"
                                f"`factory.py cron adopt {key}={clash[0]['id']}` で取り込んでください")
            else:
                create.append((key, want))
    if problems:
        print("sync を中止しました(何も変更していません):", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        raise SystemExit(1)
    return create, edit, same, sorted(set(ids) - set(desired))


def show_diff(f, have, want):
    if f == "prompt":
        print(f"      prompt: {len(have.splitlines())} 行 -> {len(want.splitlines())} 行")
        for l in diff_head(have, want, 4):
            print("        " + l)
    else:
        print(f"      {f}: {oneline(have)} -> {oneline(want)}")


def cmd_cron_sync(args):
    ctx = Ctx(args)
    desired = desired_jobs(load_cron_toml(ctx.raw), ctx.cfg, ctx.rendered())
    create, edit, same, orphans = plan_sync(ctx, desired)
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}cron sync  設定 {ctx.path}  cron プロファイル {ctx.cron_profile}  台帳 {ctx.jobs_json}  ID対応 {ctx.ids_path}")
    for key, job in same:
        print(f"  = {key} ({job['id']}) 一致")
    for key, job, diffs in edit:
        print(f"  ~ {key} ({job['id']}) 差分 {len(diffs)} 件: {', '.join(f for f, _, _ in diffs)}")
        for f, have, want in diffs:
            show_diff(f, have, want)
        print("      -> " + fmt_cmd(hermes_cmd("cron", "edit", job["id"], *edit_args(diffs, desired[key]), profile=ctx.cron_profile)))
    for key, want in create:
        print(f"  + {key} 「{want['name']}」 作成します(--paused: 作ったあと `cron resume` するまで動かない)")
        print("      -> " + fmt_cmd(hermes_cmd("cron", "create", *create_args(want, "sf: cron sync で作成"), profile=ctx.cron_profile)))
    for key in orphans:
        print(f"  ? {key}: cron_ids.json にあるが hermes/cron.toml にないキー(触りません)")
    print(f"{tag}要約: 一致 {len(same)} / 変更 {len(edit)} / 作成 {len(create)}" + ("  (何も変更していません)" if args.dry_run else ""))
    if args.dry_run or not (edit or create):
        return 0
    failed = 0
    for key, job, diffs in edit:
        r = run_cmd(hermes_cmd("cron", "edit", job["id"], *edit_args(diffs, desired[key]), profile=ctx.cron_profile))
        print(f"  {'OK ' if r.returncode == 0 else 'NG '} edit {key} ({job['id']})" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:300]}"))
        failed += r.returncode != 0
    for key, want in create:
        before = {j["id"] for j in read_jobs(ctx)}
        r = run_cmd(hermes_cmd("cron", "create", *create_args(want, "sf: cron sync で作成"), profile=ctx.cron_profile))
        if r.returncode != 0:
            print(f"  NG  create {key}: {(r.stderr or r.stdout).strip()[:300]}")
            failed += 1
            continue
        new = [j for j in read_jobs(ctx) if j["id"] not in before and j.get("name") == want["name"]]
        if len(new) != 1:
            print(f"  NG  create {key}: 作成したジョブを台帳から特定できません(候補 {len(new)} 件)。cron list で確認して adopt してください")
            failed += 1
            continue
        write_ids(ctx, {**read_ids(ctx), key: new[0]["id"]})
        print(f"  OK  create {key} = {new[0]['id']} (paused。確認後 `make cron-resume`)")
    return 1 if failed else 0


def cmd_cron_toggle(args):
    """pause / resume: cron_ids.json のジョブだけを対象にする(名前の前方一致で他のジョブに触れない)。"""
    ctx = Ctx(args)
    verb = args.action
    ids = read_ids(ctx, required=True)
    byid = {j["id"]: j for j in read_jobs(ctx)}
    tag = "DRY-RUN " if args.dry_run else ""
    failed = 0
    print(f"{tag}cron {verb}  対象 {len(ids)} 件(cron_ids.json: {ctx.ids_path})")
    for key, jid in sorted(ids.items()):
        job = byid.get(jid)
        if job is None:
            print(f"  NG  {key} ({jid}): 台帳にありません")
            failed += 1
            continue
        paused = job.get("state") == "paused" or job.get("enabled") is False
        if (verb == "pause") == paused:
            print(f"  =   {key} ({jid}) 既に{'停止中' if paused else '稼働中'}")
            continue
        cmd = hermes_cmd("cron", verb, jid, profile=ctx.cron_profile)
        if args.dry_run:
            print(f"  ->  {key} ({jid}) {fmt_cmd(cmd)}")
            continue
        r = run_cmd(cmd)
        print(f"  {'OK ' if r.returncode == 0 else 'NG '} {verb} {key} ({jid})" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:300]}"))
        failed += r.returncode != 0
    return 1 if failed else 0


# ------------------------------------------------------------------ wrappers (薄い .sh)

def render_wrappers(ctx, cron):
    """{ファイル名: 本文}。LIVE・PATH・リポジトリのパスを埋め込む(実行時に設定を読まない)。"""
    tmpl = string.Template(WRAPPER_TMPL.read_text(encoding="utf-8"))
    path = ":".join(ctx.cfg["hermes"]["path_prepend"])
    out = {}
    for name, w in cron.get("wrappers", {}).items():
        where, _, rel = w["target"].partition("/")
        if where not in ("live", "repo", "plugin") or not rel:
            die(f"cron.toml [wrappers.{name}] target={w['target']!r}: 先頭は live/ か repo/ か plugin/<名前>/ にしてください")
        if where == "plugin":  # plugin/<名前>/<パス>: 外のリポジトリのプラグインの実体(絶対パスを埋め込む)
            pname, _, prel = rel.partition("/")
            if pname not in sfplugins.enabled(ctx.raw, ctx.repo) or not prel:
                die(f"cron.toml [wrappers.{name}] target={w['target']!r}: plugin/<有効なプラグイン名>/<パス> にしてください")
            full = str(sfplugins.plugin_dir(pname, ctx.repo) / prel)
            out[name] = tmpl.substitute(about=w["about"], run=w["run"], live=str(ctx.live), repo=str(ctx.repo), path=path,
                                        target=full, target_display=full)
            continue
        out[name] = tmpl.substitute(about=w["about"], run=w["run"], live=str(ctx.live), repo=str(ctx.repo), path=path,
                                    target="$" + where.upper() + "/" + rel, target_display=str((ctx.live if where == "live" else ctx.repo) / rel))
    return out


def cmd_wrappers(args):
    ctx = Ctx(args)
    files = render_wrappers(ctx, load_cron_toml(ctx.raw))
    dest = Path(args.out) if args.out else ctx.scripts_dir
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}wrappers  {len(files)} 本 -> {dest}")
    changed = 0
    for name, text in files.items():
        p = dest / name
        state = "新規" if not p.exists() else ("一致" if p.read_text(encoding="utf-8") == text and os.access(p, os.X_OK) else "差分")
        print(f"  {'=' if state == '一致' else '~'} {name}: {state}")
        if state == "一致":
            continue
        changed += 1
        if state == "差分" and args.dry_run and p.exists():
            for l in diff_head(p.read_text(encoding="utf-8"), text, 6):
                print("        " + l)
        if not args.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            p.chmod(0o755)
    print(f"{tag}要約: 一致 {len(files) - changed} / 書く {changed}")
    return 0


# ------------------------------------------------------------------ hermes の設定の読み書き

def _config_lines(out):
    return [l for l in out.splitlines() if not (l.startswith("⚠") or l.lstrip().startswith(("Did you mean", "(Use --force")))]


def config_get(profile, key):
    """`hermes -p P config get KEY` の値(文字列、リストなら list)。無ければ None。"""
    r = run_cmd(hermes_cmd("config", "get", key, profile=profile))
    if r.returncode != 0:
        return None
    lines = _config_lines(r.stdout)
    if lines and all(l.lstrip().startswith("- ") for l in lines if l.strip()):
        return [l.lstrip()[2:].strip() for l in lines if l.strip()]
    text = "\n".join(lines).strip()
    return None if text in ("", "None", "null") else text


def profile_settings(name, cfg):
    """[(設定キー, あるべき値)]。リストは list。"""
    spec, m = PROFILES[name], cfg["models"]
    return [("model.default", m[spec["model"]]), ("model.provider", m["provider"]), ("agent.reasoning_effort", spec["reasoning"]),
            ("agent.max_turns", str(spec["max_turns"])), ("approvals.smart_policy", SMART_POLICY.read_text(encoding="utf-8").strip()),
            ("skills.external_dirs", [cfg["paths"]["skills"]])]


def set_arg(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value


def profile_drift(profile, settings):
    """[(キー, 本番の値, あるべき値)]。"""
    out = []
    for key, want in settings:
        have = config_get(profile, key)
        if have != want:
            out.append((key, have, want))
    return out


def soul_path(ctx, name):
    return ctx.profiles_dir / name / "SOUL.md"


# ------------------------------------------------------------------ profiles / board

def cmd_profiles(args):
    ctx = Ctx(args)
    files = ctx.rendered()
    tag = "DRY-RUN " if args.dry_run else ""
    failed = 0
    print(f"{tag}profiles  clone 元 {ctx.cron_profile}  設定 {ctx.path}")
    for name, spec in PROFILES.items():
        settings = profile_settings(name, ctx.cfg)
        soul = files[f"hermes/profiles/{name}/SOUL.md"]
        exists = (ctx.profiles_dir / name / "config.yaml").is_file()
        if not exists:
            create = hermes_cmd("profile", "create", name, "--clone-from", ctx.cron_profile, "--no-alias",
                                "--description", spec["description"].format(owner=ctx.cfg["owner"]))
            plan = [create] + [hermes_cmd("config", "set", k, set_arg(v), profile=name) for k, v in settings]
            print(f"  + {name}: 無いので作ります")
            for c in plan:
                print("      -> " + fmt_cmd(c))
            print(f"      -> {soul_path(ctx, name)} を書く(SOUL.md {len(soul)} 文字)")
            if args.dry_run:
                continue
            for c in plan:
                r = run_cmd(c)
                if r.returncode != 0:
                    print(f"  NG  {fmt_cmd(c)}: {(r.stderr or r.stdout).strip()[:300]}")
                    failed += 1
                    break
            else:
                soul_path(ctx, name).write_text(soul, encoding="utf-8")
                print(f"  OK  {name} を作成")
            continue
        # 既にある: 作らない(冪等)。差は表示だけ。--update のときだけ直す。
        drift = profile_drift(name, settings)
        soul_have = soul_path(ctx, name).read_text(encoding="utf-8") if soul_path(ctx, name).is_file() else None
        if soul_have != soul:
            drift.append(("SOUL.md", None if soul_have is None else f"{len(soul_have)} 文字", f"{len(soul)} 文字(置換後)"))
        if not drift:
            print(f"  = {name}: あります(設定・SOUL とも一致。何もしません)")
            continue
        print(f"  ~ {name}: あります(作りません)。差分 {len(drift)} 件" + ("" if args.update else "  -> 直すなら --update"))
        for k, have, want in drift:
            print(f"      {k}: {oneline(have, 60)} -> {oneline(want, 60)}")
        if args.update and not args.dry_run:
            for k, _, _ in drift:
                if k == "SOUL.md":
                    soul_path(ctx, name).write_text(soul, encoding="utf-8")
                    continue
                r = run_cmd(hermes_cmd("config", "set", k, set_arg(dict(settings)[k]), profile=name))
                print(f"  {'OK ' if r.returncode == 0 else 'NG '} set {name} {k}" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:300]}"))
                failed += r.returncode != 0
    return 1 if failed else 0


def list_boards():
    """`hermes kanban boards list` のスラッグの一覧。取れなければ None。"""
    r = run_cmd(hermes_cmd("kanban", "boards", "list"))
    if r.returncode != 0:
        return None
    return [m.group(1) for l in r.stdout.splitlines() if (m := re.match(r"^[●\s]\s*([a-z0-9][a-z0-9-]*)\s{2,}", l)) and m.group(1) != "SLUG"]


def cmd_board(args):
    ctx = Ctx(args)
    tag = "DRY-RUN " if args.dry_run else ""
    boards = list_boards()
    if boards is None:
        die("`hermes kanban boards list` が失敗しました(hermes は動きますか)")
    failed = 0
    if ctx.board in boards:
        print(f"  = ボード {ctx.board}: あります(何もしません)")
    else:
        cmd = hermes_cmd("kanban", "boards", "create", ctx.board, "--name", f"{ctx.cfg['owner']} factory")
        print(f"  + ボード {ctx.board}: 無いので作ります\n      -> {fmt_cmd(cmd)}")
        if not args.dry_run:
            r = run_cmd(cmd)
            print(f"  {'OK ' if r.returncode == 0 else 'NG '} boards create {ctx.board}" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:300]}"))
            failed += r.returncode != 0
    have = config_get(ctx.cron_profile, "kanban.review_dispatch")
    if have == "true":
        print(f"  = {ctx.cron_profile}: kanban.review_dispatch は true です")
    else:
        cmd = hermes_cmd("config", "set", "kanban.review_dispatch", "true", profile=ctx.cron_profile)
        print(f"  ~ {ctx.cron_profile}: kanban.review_dispatch = {have!r} -> true\n      -> {fmt_cmd(cmd)}")
        if not args.dry_run:
            r = run_cmd(cmd)
            print(f"  {'OK ' if r.returncode == 0 else 'NG '} kanban.review_dispatch" + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:300]}"))
            failed += r.returncode != 0
    print(f"{tag}board 完了")
    return 1 if failed else 0


# ------------------------------------------------------------------ Notion: schema の書き出し・照合・DB の作成

class NtnError(Exception):
    pass


def ntn_api(path, method=None, body=None):
    cmd = ntn_cmd("api", path) + (["-X", method] if method else []) + (["-d", "@-"] if body is not None else [])
    r = run_cmd(cmd, input_text=json.dumps(body, ensure_ascii=False) if body is not None else None, timeout=120)
    if r.returncode != 0:
        raise NtnError(f"ntn api {path} が失敗: {(r.stderr or r.stdout).strip()[:400]}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise NtnError(f"ntn api {path}: JSON ではない応答: {r.stdout[:200]!r}")
    if isinstance(data, dict) and data.get("object") == "error":
        raise NtnError(f"ntn api {path}: {data.get('code')}: {data.get('message')}")
    return data


def ds_to_schema(ds, id_to_key):
    """data source の API 応答 -> schema.json の1つの DB(ID を含まない。関係の相手は DB のキー)。"""
    props = {}
    for name, p in ds["properties"].items():
        t = p["type"]
        e = {"type": t}
        if t in ("select", "multi_select"):
            e["options"] = [dict(name=o["name"], color=o.get("color")) for o in p[t]["options"]]
        elif t == "status":
            group_of = {oid: g["name"] for g in p[t]["groups"] for oid in g["option_ids"]}
            e["options"] = [dict(name=o["name"], color=o.get("color"), group=group_of.get(o["id"])) for o in p[t]["options"]]
        elif t == "relation":
            r = p[t]
            target = id_to_key.get(r["data_source_id"])
            if target is None:
                raise NtnError(f"プロパティ「{name}」の関係の相手 {r['data_source_id']} は設定の3つのDBのどれでもありません")
            e["relation"] = dict(target=target, mode=r["type"])
            if r["type"] == "dual_property":
                e["relation"]["synced_property_name"] = r["dual_property"]["synced_property_name"]
        elif t not in SIMPLE_PROP_TYPES:
            raise NtnError(f"プロパティ「{name}」の型 {t} は未対応です(対応: {', '.join(SIMPLE_PROP_TYPES)}, select, multi_select, status, relation)")
        props[name] = e
    return dict(properties=dict(sorted(props.items(), key=lambda kv: (kv[1]["type"] != "title", kv[0]))))


def fetch_live_schema(cfg):
    ids = {k: cfg["notion"][DB_ENV_KEY[k]] for k in DB_ORDER}
    id_to_key = {v: k for k, v in ids.items()}
    out = {}
    for key in DB_ORDER:
        out[key] = ds_to_schema(ntn_api(f"v1/data_sources/{ids[key]}"), id_to_key)
    return out


def cmd_notion_export_schema(args):
    ctx = Ctx(args)
    ids = {k: ctx.cfg["notion"][DB_ENV_KEY[k]] for k in DB_ORDER}
    try:
        dbs = {}
        for key in DB_ORDER:
            ds = ntn_api(f"v1/data_sources/{ids[key]}")
            entry = ds_to_schema(ds, {v: k for k, v in ids.items()})
            parent = ds.get("parent", {})
            if parent.get("type") == "database_id":
                db = ntn_api(f"v1/databases/{parent['database_id']}")
                entry = dict(title=db["title"][0]["plain_text"] if db.get("title") else ds["title"][0]["plain_text"],
                             icon=(db.get("icon") or {}).get("emoji"), **entry)
            dbs[key] = entry
    except NtnError as e:
        die(str(e))
    doc = dict(format=1, note="`factory.py notion-export-schema` が本番のDBから書き出した。`notion-bootstrap` はこれでDBを作り、`doctor` はこれと照合する。",
               databases=dbs)
    out = Path(args.out) if args.out else SCHEMA_JSON
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    n_props = sum(len(d["properties"]) for d in dbs.values())
    if args.dry_run:
        print(f"DRY-RUN notion-export-schema: {len(dbs)} DB / {n_props} プロパティを {out} に書く予定(何も書いていません)")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{len(dbs)} DB / {n_props} プロパティを {out} に書き出し")
    return 0


def load_schema(path=SCHEMA_JSON):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        assert set(doc["databases"]) == set(DB_ORDER)
        return doc["databases"]
    except (OSError, ValueError, KeyError, AssertionError) as e:
        die(f"{path} を読めません(`factory.py notion-export-schema` で作る): {e}")


def schema_mismatches(want, have):
    """schema.json の1つの DB と、実際の DB の差(色は比べない。人が変えてよい)。"""
    out = []
    for name, w in want["properties"].items():
        h = have["properties"].get(name)
        if h is None:
            out.append(f"プロパティ「{name}」({w['type']})がありません")
            continue
        if h["type"] != w["type"]:
            out.append(f"「{name}」の型が違う: {h['type']} (あるべき {w['type']})")
            continue
        if "options" in w:
            wo = [(o["name"], o.get("group")) for o in w["options"]]
            ho = [(o["name"], o.get("group")) for o in h["options"]]
            if sorted(wo, key=str) != sorted(ho, key=str):
                missing = [o for o in wo if o not in ho]
                extra = [o for o in ho if o not in wo]
                out.append(f"「{name}」の選択肢が違う" + (f": 足りない {missing}" if missing else "") + (f" 余分 {extra}" if extra else ""))
        if "relation" in w and h.get("relation") != w["relation"]:
            out.append(f"「{name}」の関係が違う: {h.get('relation')} (あるべき {w['relation']})")
    return out


def build_create_body(key, entry, parent_page, created):
    """schema.json の DB -> POST v1/databases の本文。created は {DBのキー: 作成済みの data source ID}。"""
    props, later = {}, DB_ORDER[DB_ORDER.index(key) + 1:]
    for name, p in entry["properties"].items():
        t = p["type"]
        if t in SIMPLE_PROP_TYPES:
            props[name] = {"type": t, t: {}}
        elif t in ("select", "multi_select"):
            props[name] = {"type": t, t: {"options": [{k: v for k, v in o.items() if v is not None} for o in p["options"]]}}
        elif t == "status":
            props[name] = {"type": t, t: {"options": [{k: v for k, v in o.items() if v is not None} for o in p["options"]]}}
        elif t == "relation":
            r = p["relation"]
            if r["target"] in later:
                if r["mode"] != "dual_property":
                    raise NtnError(f"「{key}.{name}」は後で作る {r['target']} への片側の関係です。作る順番(epic -> task -> review)では先に作れません")
                continue  # 後の DB が dual_property で作ると、この側は自動でできる(対の名前は後の DB の synced_property_name)
            if r["target"] not in created:
                raise NtnError(f"「{key}.{name}」の関係の相手 {r['target']} がまだありません")
            rel = {"data_source_id": created[r["target"]], "type": r["mode"]}
            rel[r["mode"]] = {"synced_property_name": r["synced_property_name"]} if r["mode"] == "dual_property" else {}
            props[name] = {"type": "relation", "relation": rel}
        else:
            raise NtnError(f"「{key}.{name}」: 型 {t} は未対応")
    body = {"parent": {"type": "page_id", "page_id": parent_page}, "is_inline": False,
            "title": [{"type": "text", "text": {"content": entry.get("title") or key}}],
            "initial_data_source": {"properties": props}}
    if entry.get("icon"):
        body["icon"] = {"type": "emoji", "emoji": entry["icon"]}
    return body


def check_dual_pairs(dbs):
    """前の DB から後の DB への dual_property には、後の DB 側に対になる関係(相手=前の DB、対の名前=前の DB の名前)が必要。"""
    problems = []
    for i, key in enumerate(DB_ORDER):
        for name, p in dbs[key]["properties"].items():
            r = p.get("relation")
            if not r or r["target"] not in DB_ORDER[i + 1:]:
                continue
            back = dbs[r["target"]]["properties"].get(r.get("synced_property_name"))
            ok = back and back.get("relation", {}).get("target") == key and back["relation"]["mode"] == "dual_property" \
                and back["relation"].get("synced_property_name") == name
            if not ok:
                problems.append(f"{key}.{name} -> {r['target']}.{r.get('synced_property_name')}: 対になる dual_property の関係がありません")
    return problems


def set_notion_ids(text, ids):
    """env の TOML の [notion] セクションの <名前>_ds = "..." だけを書き換える(他の行・コメントはそのまま)。"""
    lines, in_sec, done = text.split("\n"), False, set()
    for i, l in enumerate(lines):
        if re.match(r"^\s*\[", l):
            in_sec = l.strip() == "[notion]"
        elif in_sec and (m := re.match(r"^(\s*(epic_ds|task_ds|review_ds)\s*=\s*)\"[^\"]*\"(.*)$", l)) and DB_ORDER[["epic_ds", "task_ds", "review_ds"].index(m.group(2))] in ids:
            key = DB_ORDER[["epic_ds", "task_ds", "review_ds"].index(m.group(2))]
            lines[i] = f'{m.group(1)}"{ids[key]}"{m.group(3)}'
            done.add(key)
    if done != set(ids):
        die(f"設定の [notion] に {', '.join(DB_ENV_KEY[k] for k in set(ids) - done)} の行がありません")
    return "\n".join(lines)


UUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)


def cmd_notion_bootstrap(args):
    ctx = Ctx(args)
    dbs = load_schema(Path(args.schema) if args.schema else SCHEMA_JSON)
    problems = check_dual_pairs(dbs)
    if problems:
        die("schema.json:\n  " + "\n  ".join(problems))
    parent = ctx.cfg["notion"]["parent_page"]
    tag = "DRY-RUN " if args.dry_run else ""
    # 既にあるなら作らない(二重に作らない)
    for key in DB_ORDER:
        cur = ctx.cfg["notion"][DB_ENV_KEY[key]]
        if UUID_RE.match(cur):
            try:
                ntn_api(f"v1/data_sources/{cur}")
            except NtnError:
                continue
            die(f"{DB_ENV_KEY[key]} = {cur} は既に存在します。{ctx.path.name} の3つの ds ID を未作成の値(例 \"UNSET\")にしてから実行してください(作り直しはしません)")
    print(f"{tag}notion-bootstrap  親ページ {parent}  schema {args.schema or SCHEMA_JSON}  書き戻し先 {ctx.path}")
    created, text = {}, ctx.path.read_text(encoding="utf-8")
    try:
        for key in DB_ORDER:
            ph = {k: f"<{k} の data source ID>" for k in DB_ORDER if k not in created}
            body = build_create_body(key, dbs[key], parent, {**ph, **created})
            n = len(body["initial_data_source"]["properties"])
            print(f"  {'+' if not args.dry_run else '~'} {key}「{dbs[key].get('title')}」: POST v1/databases  プロパティ {n} 件"
                  f"({', '.join(body['initial_data_source']['properties'])})")
            if args.dry_run:
                created[key] = f"<{key} の data source ID>"
                continue
            res = ntn_api("v1/databases", method="POST", body=body)
            sources = res.get("data_sources") or []
            if len(sources) != 1:
                raise NtnError(f"{key}: 作成した DB の data source を特定できません: {res.get('id')} {sources}")
            created[key] = sources[0]["id"]
            text = set_notion_ids(text, {key: created[key]})
            ctx.path.write_text(text, encoding="utf-8")  # 1つ作るたびに書き戻す(途中で失敗しても ID を失わない)
            print(f"      OK data source {created[key]}")
    except NtnError as e:
        print(f"NG {e}", file=sys.stderr)
        if created:
            print(f"作成済みの ID は {ctx.path} に書き戻し済み(コミットはしていません): {created}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"{tag}書き戻し予定: {ctx.path} の [notion] epic_ds / task_ds / review_ds を作成した ID に置換し、`git commit` する(何も作っていません)")
        return 0
    # 作ったものが schema.json と一致するか読み直す
    live = fetch_live_schema({"notion": {DB_ENV_KEY[k]: created[k] for k in DB_ORDER}})
    bad = [f"{k}: {m}" for k in DB_ORDER for m in schema_mismatches(dbs[k], live[k])]
    for b in bad:
        print(f"  NG 作成後の照合: {b}", file=sys.stderr)
    print("  OK 作成後の照合: 3つの DB とも schema.json と一致" if not bad else "  作成後の照合に差があります(上記)。")
    d = ctx.path.parent
    msg = f"chore(env): {ctx.path.stem} の Notion DB を作成(data source ID を書き戻す)"
    if run_cmd(["git", "-C", str(d), "rev-parse", "--is-inside-work-tree"]).returncode != 0:
        print(f"  注意: {d} は git リポジトリではないのでコミットしません")
        return 1 if bad else 0
    r = run_cmd(["git", "-C", str(d), "add", "--", str(ctx.path)])
    r = r if r.returncode else run_cmd(["git", "-C", str(d), "commit", "-m", msg, "--", str(ctx.path)])
    print(f"  {'OK ' if r.returncode == 0 else 'NG '} git commit: {msg}" + ("" if r.returncode == 0 else f"\n{(r.stderr or r.stdout).strip()[:300]}"))
    return 1 if (bad or r.returncode) else 0


# ------------------------------------------------------------------ doctor / smoke

class Report:
    def __init__(self):
        self.rows = []

    def add(self, ok, label, detail=""):
        self.rows.append((ok, label, detail))
        print(f"{'OK ' if ok else 'NG '} {label}" + (f" — {detail}" if detail else ""))
        return ok

    def ng(self):
        return [r for r in self.rows if not r[0]]


def parse_hermes_version(text):
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def env_file_state(path, key):
    """.env にキーがあるか。"set"(値あり) / "empty"(値が空) / "missing"(キーが無い・ファイルが無い)。値は返さない。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return "missing"
    state = "missing"
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, sep, v = line.partition("=")
        if sep and k.strip() == key:
            state = "set" if v.strip().strip("\"'") else "empty"
    return state


def slack_fields(cfg):
    """配信先に `slack:` を含む [chat] のキー名(deliver / failure_deliver)。"""
    return [k for k in ("deliver", "failure_deliver") if any(x.strip().startswith("slack:") for x in cfg["chat"][k].split(","))]


def cmd_doctor(args):
    ctx = Ctx(args)
    rep = Report()
    print(f"doctor  設定 {ctx.path}  cron プロファイル {ctx.cron_profile}  ボード {ctx.board}")
    # 1. Hermes
    r = run_cmd(hermes_cmd("--version"))
    ver = parse_hermes_version(r.stdout) if r.returncode == 0 else None
    rep.add(ver is not None and ver >= MIN_HERMES, "Hermes のバージョン",
            f"v{'.'.join(map(str, ver))}" + ("" if ver and ver >= MIN_HERMES else f" (必要 v{'.'.join(map(str, MIN_HERMES))} 以上)") if ver else (r.stderr or r.stdout).strip()[:120] or "取得できない")
    # 2. ntn
    r = run_cmd(ntn_cmd("whoami"))
    rep.add(r.returncode == 0, "ntn whoami", (r.stdout.split("\t")[1] if r.returncode == 0 and "\t" in r.stdout else (r.stderr or r.stdout).strip()[:120]))
    # 3. プロファイル(cron_profile + executor / reviewer: 存在、設定、SOUL)
    files = ctx.rendered()
    rep.add((ctx.profiles_dir / ctx.cron_profile).is_dir() or ctx.cron_profile == "default", f"プロファイル {ctx.cron_profile}(cron)", str(ctx.profiles_dir / ctx.cron_profile))
    for name in PROFILES:
        if not rep.add((ctx.profiles_dir / name / "config.yaml").is_file(), f"プロファイル {name}", "" if (ctx.profiles_dir / name / "config.yaml").is_file() else "無い -> make profiles"):
            continue
        bad = [f"{k} = {oneline(h, 40)} (あるべき {oneline(w, 40)})" for k, h, w in profile_drift(name, [s for s in profile_settings(name, ctx.cfg) if s[0] != "skills.external_dirs"])]
        rep.add(not bad, f"プロファイル {name} の設定(model / reasoning / max_turns / smart_policy)", "; ".join(bad))
        soul = soul_path(ctx, name)
        rep.add(soul.is_file() and soul.read_text(encoding="utf-8") == files[f"hermes/profiles/{name}/SOUL.md"], f"プロファイル {name} の SOUL.md が置換後と一致")
    # 4. ボード + review_dispatch
    boards = list_boards()
    rep.add(boards is not None and ctx.board in boards, f"kanban ボード {ctx.board}", "" if boards and ctx.board in boards else "無い -> make board")
    rd = config_get(ctx.cron_profile, "kanban.review_dispatch")
    rep.add(rd == "true", f"{ctx.cron_profile}: kanban.review_dispatch", f"{rd!r} (あるべき true)" if rd != "true" else "true")
    # 5. MCP(executor / reviewer は Notion MCP が要る)
    for name in PROFILES:
        r = run_cmd(hermes_cmd("mcp", "list", profile=name))
        ok = r.returncode == 0 and any("notion" in l.lower() and "enabled" in l.lower() and "disabled" not in l.lower() for l in r.stdout.splitlines())
        rep.add(ok, f"{name}: Notion MCP が有効", "" if ok else "`hermes -p %s mcp list` に notion の enabled が無い" % name)
    # 6. external_dirs
    for name in [ctx.cron_profile, *PROFILES]:
        have = config_get(name, "skills.external_dirs") or []
        have = have if isinstance(have, list) else [have]
        want = ctx.cfg["paths"]["skills"]
        rep.add(want in have and ctx.skills.is_dir(), f"{name}: skills.external_dirs に {want}", "" if want in have else f"現在 {have}")
    # 7. ゲートウェイ
    r = run_cmd(hermes_cmd("gateway", "status", profile=ctx.cron_profile))
    low = (r.stdout + r.stderr).lower()
    rep.add(r.returncode == 0 and "running" in low and "not running" not in low, f"ゲートウェイ({ctx.cron_profile})が稼働中",
            "" if "running" in low and "not running" not in low else (r.stdout or r.stderr).strip().splitlines()[0][:120] if (r.stdout or r.stderr).strip() else "状態を取得できない")
    # 7b. 配信先に Slack を含むなら、cron プロファイルの .env に SLACK_BOT_TOKEN(値は見せない)
    sl = slack_fields(ctx.cfg)
    if sl:
        env_path = (ctx.home if ctx.cron_profile == "default" else ctx.profiles_dir / ctx.cron_profile) / ".env"
        st = env_file_state(env_path, "SLACK_BOT_TOKEN")
        rep.add(st == "set", f"{ctx.cron_profile}: .env に SLACK_BOT_TOKEN([chat].{' / '.join(sl)} に slack:)",
                {"set": "", "empty": f"{env_path} の SLACK_BOT_TOKEN が空", "missing": f"{env_path} に SLACK_BOT_TOKEN が無い -> INSTALL.md の Slack の手順"}[st])
    # 8. cron_mode(cron の AI は python -c / heredoc を使えない前提で指示文が書かれている)
    cm = config_get(ctx.cron_profile, "approvals.cron_mode")
    rep.add(cm == "deny", f"{ctx.cron_profile}: approvals.cron_mode", f"{cm!r} (あるべき deny)" if cm != "deny" else "deny")
    # 9. cron のジョブ(ID 対応、台帳、ラッパー)
    cron = load_cron_toml(ctx.raw)
    ids = read_ids(ctx)
    jobs = {j["id"]: j for j in load_jobs(ctx.jobs_json)} if ctx.jobs_json.is_file() else {}
    missing = [k for k in cron["jobs"] if k not in ids]
    rep.add(not missing, "cron_ids.json に全ジョブ登録済み", f"無いキー {missing} -> `cron adopt` か `cron sync`" if missing else str(ctx.ids_path))
    gone = [f"{k}={i}" for k, i in ids.items() if i not in jobs]
    rep.add(not gone, "cron_ids.json の ID が台帳にある", f"台帳にない {gone}" if gone else "")
    wr = render_wrappers(ctx, cron)
    bad_w = [n for n, t in wr.items() if not (ctx.scripts_dir / n).is_file() or (ctx.scripts_dir / n).read_text(encoding="utf-8") != t]
    rep.add(not bad_w, "cron の薄い .sh が置換後と一致", f"無い・差がある {bad_w} -> make init か `factory.py wrappers`" if bad_w else str(ctx.scripts_dir))
    # 10. Notion の3つの DB のスキーマ
    try:
        dbs = load_schema()
        live = fetch_live_schema(ctx.cfg)
        for key in DB_ORDER:
            m = schema_mismatches(dbs[key], live[key])
            rep.add(not m, f"Notion DB {key}({ctx.cfg['notion'][DB_ENV_KEY[key]][:8]}…) が schema.json と一致", "; ".join(m))
    except NtnError as e:
        rep.add(False, "Notion の3つの DB を読めた", str(e)[:200])
    except SystemExit as e:  # schema.json が無い・壊れている
        rep.add(False, "notion/schema.json を読めた", str(e).replace("factory: ", "")[:200])
    bad = rep.ng()
    print(f"\n{len(rep.rows) - len(bad)} OK / {len(bad)} 足りない")
    for _, label, detail in bad:
        print(f"  足りない: {label}" + (f" — {detail}" if detail else ""))
    return 1 if bad else 0


def smoke_commands(ctx):
    """読み取りだけの確認(Notion / kanban を変更しない): ゲート制御の試し実行(反映なし)と、監視スクリプトの出力。"""
    cmds = [("ゲート制御の試し実行(反映なし)", ["python3", str(ctx.live / "epic_gate_controller.py")])]
    for j in load_cron_toml(ctx.raw)["jobs"].values():
        if j.get("monitor"):
            cmds.append((f"監視 {j['name']}", ["sh", str(ctx.scripts_dir / j["monitor"])]))
    return cmds


def cmd_smoke(args):
    ctx = Ctx(args)
    failed = 0
    for label, cmd in smoke_commands(ctx):
        if args.dry_run:
            print(f"DRY-RUN {label}: {fmt_cmd(cmd)}")
            continue
        r = run_cmd(cmd, timeout=240)
        bad = r.returncode != 0 or any(l.startswith("ERROR") for l in r.stdout.splitlines())
        print(f"{'NG ' if bad else 'OK '} {label}: rc={r.returncode} {oneline((r.stdout or r.stderr).strip() or '(出力なし)', 100)}")
        failed += bad
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, dry_run=False, parent=sub):
        p = parent.add_parser(name, help=help_)
        p.add_argument("--env", help="envs/NAME.toml")
        p.add_argument("--config", help="設定ファイルを直接指定")
        if dry_run:
            p.add_argument("--dry-run", action="store_true", help="何も変更しない。やることだけ表示")
        p.set_defaults(fn=fn)
        return p

    add("render", cmd_render, "雛形を置換して build/<環境>/ に出す")
    p = sub.choices["render"]
    p.add_argument("--all-envs", action="store_true", help="envs/*.toml をすべて")
    p.add_argument("--dry-run", action="store_true", help="置換の検証だけ。何も書かない")
    p.add_argument("--out", help="出力先(既定 build/)")
    add("check", cmd_check, "置換した結果と本番を比べる")

    cron = sub.add_parser("cron", help="cron ジョブ: adopt / sync / pause / resume")
    cs = cron.add_subparsers(dest="action", required=True)
    p = add("adopt", cmd_cron_adopt, "キー=ジョブ ID を <live>/cron_ids.json に保存(存在しない ID は停止)", parent=cs)
    p.add_argument("pairs", nargs="+", metavar="KEY=ID")
    add("sync", cmd_cron_sync, "cron.toml と本番の jobs.json を比べて直す(無いジョブは --paused で作る)", dry_run=True, parent=cs)
    add("pause", cmd_cron_toggle, "cron_ids.json のジョブだけを一時停止", dry_run=True, parent=cs)
    add("resume", cmd_cron_toggle, "cron_ids.json のジョブだけを再開", dry_run=True, parent=cs)

    p = add("wrappers", cmd_wrappers, "cron の薄い .sh を hermes/wrapper.sh.tmpl から生成", dry_run=True)
    p.add_argument("--out", help="置き場所(既定: cron プロファイルの scripts/)")
    p = add("profiles", cmd_profiles, "executor / reviewer を作る(あれば作らない)", dry_run=True)
    p.add_argument("--update", action="store_true", help="既にあるプロファイルの設定・SOUL の差も直す")
    add("board", cmd_board, "kanban ボードと review_dispatch", dry_run=True)
    p = add("notion-export-schema", cmd_notion_export_schema, "本番の3つのDBを読んで notion/schema.json に書き出す(読むだけ)", dry_run=True)
    p.add_argument("--out", help="出力先(既定 notion/schema.json)")
    p = add("notion-bootstrap", cmd_notion_bootstrap, "schema.json から3つのDBを作り、ds ID を envs に書き戻して git commit", dry_run=True)
    p.add_argument("--schema", help="schema.json の場所(既定 notion/schema.json)")
    add("doctor", cmd_doctor, "環境の検査。足りないものを並べ、1つでもあれば終了コード 1")
    add("smoke", cmd_smoke, "読み取りだけの動作確認(ゲート制御の試し実行 + 監視スクリプト)", dry_run=True)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
