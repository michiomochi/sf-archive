#!/usr/bin/env python3
"""プラグイン(plugins/<名前>/)の読み込みと、本体との合成(標準ライブラリのみ)。

プラグインは「本体(個人で使うフロー)に足す・上書きする、特定の組織や用途の部品」。環境の設定 envs/<環境>.toml の
`plugins = ["example"]` で有効にした環境にだけ効く。構成:

  plugins/<名前>/plugin.toml          必須。name / description / [config].required / [template_names]
  plugins/<名前>/skills/<スキル>/**.md  そのプラグインのスキル(本体と同名のスキルは作れない。同名で足したいときは SKILL.addendum.md)
  plugins/<名前>/skills/<本体のスキル>/SKILL.addendum.md   本体の同名スキルの SKILL.md の末尾に足す文章
  plugins/<名前>/prompts/<名前>.txt   cron の指示文の雛形(`${...}` を含められる。本体の指示文と同じ名前は作れない)
  plugins/<名前>/cron.toml            cron ジョブ([jobs.*] と [wrappers.*])。ジョブのキーは `<プラグイン名>.<キー>` になる
  plugins/<名前>/scripts/             cron が動かすスクリプト。wrappers の target は `repo/plugins/<名前>/scripts/...`

プラグインは別のリポジトリに置くこともできる。環境の設定に `[plugin_sources]` で 名前 -> そのリポジトリの作業コピーのパス
(直下に plugin.toml がある)を書く。構成は上と同じで、`plugins/<名前>/` の部分がそのリポジトリの直下になる。置換結果の
パスは仮想的に `plugins/<名前>/...` で表す。wrappers の target は `plugin/<名前>/scripts/...`
(sf 内のプラグインは `repo/plugins/<名前>/scripts/...`)。plugin.toml の [defaults] に既定値を持てる。

`${...}` の名前は本体の名前(sfconfig.TEMPLATE_NAMES)に加え、plugin.toml の [template_names](名前 -> [plugin.<名前>] のキー)。
"""
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
ADDENDUM = "SKILL.addendum.md"
MANIFEST = "plugin.toml"


class PluginError(Exception):
    pass


class SourceMissing(PluginError):
    """[plugin_sources] のパスにリポジトリの作業コピーが無い(まだ ghq get していない等)。"""


_SOURCES = {}  # 外のリポジトリのプラグイン: 名前 -> 作業コピーのパス(register_sources が環境の設定から入れる)


def register_sources(raw):
    """環境の設定の [plugin_sources] を読んで _SOURCES を作り直す。"""
    src = raw.get("plugin_sources", {})
    if not isinstance(src, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in src.items()):
        raise PluginError("[plugin_sources] は 名前 = \"リポジトリの作業コピーのパス\" の表")
    _SOURCES.clear()
    for n, path in src.items():
        if not NAME_RE.match(n):
            raise PluginError(f"[plugin_sources]: 不正なプラグイン名 {n!r}(小文字の英数字と - _)")
        _SOURCES[n] = Path(path).expanduser()


def plugin_dir(name, repo=REPO):
    return _SOURCES[name] if name in _SOURCES else Path(repo) / "plugins" / name


def available(repo=REPO):
    root = Path(repo) / "plugins"
    local = {p.name for p in root.iterdir() if (p / MANIFEST).is_file()} if root.is_dir() else set()
    return sorted(local | {n for n, p in _SOURCES.items() if (p / MANIFEST).is_file()})


def read_source(repo, rel):
    """置換前の雛形の本文。rel は repo 内のパス、または仮想パス plugins/<名前>/...(外のリポジトリのプラグインを含む)。"""
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "plugins" and parts[1] in _SOURCES:
        return (_SOURCES[parts[1]] / Path(*parts[2:])).read_text(encoding="utf-8")
    return (Path(repo) / rel).read_text(encoding="utf-8")


def enabled(raw, repo=REPO):
    """環境の設定(置換前の dict)で有効なプラグイン名のリスト。無いプラグイン・不正な名前は PluginError。"""
    register_sources(raw)
    names = raw.get("plugins", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise PluginError("plugins は文字列のリストで書いてください(例: plugins = [\"example\"])")
    out = []
    for n in names:
        if not NAME_RE.match(n):
            raise PluginError(f"plugins: 不正なプラグイン名 {n!r}(小文字の英数字と - _)")
        if n in _SOURCES and not (_SOURCES[n] / MANIFEST).is_file():
            raise SourceMissing(f"plugin_sources: {n!r} の作業コピーがありません({_SOURCES[n] / MANIFEST})。リポジトリを取得してパスを合わせてください")
        if not (plugin_dir(n, repo) / MANIFEST).is_file():
            raise PluginError(f"plugins: プラグイン {n!r} がありません({plugin_dir(n, repo) / MANIFEST})。ある名前: {', '.join(available(repo)) or '(なし)'}")
        if n not in out:
            out.append(n)
    return out


def load_manifest(name, repo=REPO):
    path = plugin_dir(name, repo) / MANIFEST
    try:
        with open(path, "rb") as f:
            m = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise PluginError(f"{path} を読めません: {e}")
    if m.get("name") != name:
        raise PluginError(f"{path}: name = {m.get('name')!r} がディレクトリ名 {name!r} と違います")
    if not isinstance(m.get("description"), str) or not m["description"].strip():
        raise PluginError(f"{path}: description が必要です")
    req = (m.get("config") or {}).get("required", [])
    if not isinstance(req, list) or not all(isinstance(k, str) for k in req):
        raise PluginError(f"{path}: [config].required は文字列のリスト")
    tn = m.get("template_names") or {}
    if not isinstance(tn, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tn.items()):
        raise PluginError(f"{path}: [template_names] は 名前 = \"キー\" の表")
    return m


def section(raw, name, manifest):
    """プラグインの設定値 = plugin.toml の [defaults] に、環境の設定の [plugin.<名前>] を重ねたもの。
    個人用のプラグイン(リポジトリに入れない)は、自分の ID・URL を [defaults] に持てる。"""
    return {**(manifest.get("defaults") or {}), **((raw.get("plugin") or {}).get(name) or {})}


def config_problems(raw, repo=REPO):
    """有効なプラグインが要る [plugin.<名前>] のキーが無い・空のときの一覧。"""
    out = []
    for n in enabled(raw, repo):
        m = load_manifest(n, repo)
        sect = section(raw, n, m)
        keys = set((m.get("config") or {}).get("required", [])) | set((m.get("template_names") or {}).values())
        for k in sorted(keys):
            v = sect.get(k)
            if not (isinstance(v, str) and v.strip()):
                out.append(f"[plugin.{n}].{k}")
    return out


def template_vars(raw, core_names, repo=REPO):
    """プラグインが足す `${名前}` -> 値。本体の名前と重なったら PluginError。"""
    out = {}
    for n in enabled(raw, repo):
        m = load_manifest(n, repo)
        sect = section(raw, n, m)
        for tname, key in (m.get("template_names") or {}).items():
            if tname in core_names or tname in out:
                raise PluginError(f"plugins/{n}/plugin.toml: ${{{tname}}} は既にある名前です")
            out[tname] = sect.get(key, "")
    return out


# ------------------------------------------------------------------ 雛形(prompts / skills)

def source_files(repo, names):
    """[(kind, repo 内のパス)]。kind は prompt / skill。"""
    out = []
    for n in names:
        base = plugin_dir(n, repo)
        for kind, sub, pat in (("prompt", "prompts", "*.txt"), ("skill", "skills", "**/*")):  # スキルは .md 以外(scripts/ の .py など)も含める
            out += [(kind, f"plugins/{n}/" + p.relative_to(base).as_posix()) for p in sorted((base / sub).glob(pat))
                    if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"]
    return out


def _skill_key(rel):
    """雛形のパス -> (スキル名, スキル内のパス)。スキルでなければ None。"""
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "skills":
        return parts[1], Path(*parts[2:]).as_posix()
    if len(parts) >= 5 and parts[0] == "plugins" and parts[2] == "skills":
        return parts[3], Path(*parts[4:]).as_posix()
    return None


def deployed_skills(files):
    """置換済みの {repo のパス: 本文} -> {スキル名: {スキル内のパス: 本文}}(本番に置く形)。
    プラグインの SKILL.addendum.md は本体の同名スキルの SKILL.md の末尾に足す。同じファイルを 2 か所が作ると PluginError。"""
    out, owner, addenda = {}, {}, []
    for rel, text in sorted(files.items()):
        k = _skill_key(rel)
        if k is None:
            continue
        name, sub = k
        if sub == ADDENDUM and rel.startswith("plugins/"):
            addenda.append((rel, name, text))
            continue
        if sub == ADDENDUM:
            raise PluginError(f"{rel}: {ADDENDUM} は本体ではなくプラグインのスキルに置くものです")
        if (name, sub) in owner:
            raise PluginError(f"スキル {name}/{sub} が {owner[(name, sub)]} と {rel} の両方にあります(同名のスキルは作れません。足すなら {ADDENDUM})")
        owner[(name, sub)] = rel
        out.setdefault(name, {})[sub] = text
    for rel, name, text in addenda:
        base = out.get(name, {}).get("SKILL.md")
        if base is None:
            raise PluginError(f"{rel}: 足す先の本体のスキル {name}/SKILL.md がありません")
        out[name]["SKILL.md"] = base.rstrip("\n") + "\n\n" + text.strip("\n") + "\n"
    return out


def deployed_prompts(files):
    """{prompts/<名前>.txt または plugins/<p>/prompts/<名前>.txt の本文} -> {名前: 本文}。同じ名前が2か所にあれば PluginError。"""
    out, owner = {}, {}
    for rel, text in sorted(files.items()):
        parts = Path(rel).parts
        if not ((len(parts) == 2 and parts[0] == "prompts") or (len(parts) == 4 and parts[0] == "plugins" and parts[2] == "prompts")):
            continue
        stem = Path(rel).stem
        if stem in out:
            raise PluginError(f"指示文 {stem} が {owner[stem]} と {rel} の両方にあります")
        out[stem], owner[stem] = text, rel
    return out


# ------------------------------------------------------------------ cron

def merge_cron(core, raw, repo=REPO):
    """本体の hermes/cron.toml + 有効なプラグインの cron.toml -> {defaults, jobs, wrappers}。

    - プラグインのジョブのキーは `<プラグイン名>.<キー>`。ラッパーの名前は全体で一意(重なると PluginError)。
    """
    jobs = dict(core.get("jobs", {}))
    wrappers = dict(core.get("wrappers", {}))
    for n in enabled(raw, repo):
        path = plugin_dir(n, repo) / "cron.toml"
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as f:
                pc = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise PluginError(f"{path} を読めません: {e}")
        for k, j in pc.get("jobs", {}).items():
            jobs[f"{n}.{k}"] = {**j, "_plugin": n}
        for w, spec in pc.get("wrappers", {}).items():
            if w in wrappers:
                raise PluginError(f"{path}: ラッパー {w} は既にあります")
            wrappers[w] = spec
    return {"defaults": core.get("defaults", {}), "jobs": jobs, "wrappers": wrappers}


def prompt_jobs(cron):
    """指示文の名前 -> ジョブ名の前方一致(「（」の手前まで)。"""
    return {j["prompt"]: j["name"].split("（")[0] for j in cron["jobs"].values() if "prompt" in j}
