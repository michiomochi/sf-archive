#!/usr/bin/env python3
"""cron の周期とジョブ名のずれを検査する(標準ライブラリのみ。`make test` から実行)。

なぜ要るか: cron の周期は hermes/cron.toml の `schedule` が正本なのに、ジョブ名(「SF 出荷（3分）」)と指示文・スキル・SOUL の文に
書き写されている。ずれても誰も気づかない。周期は1か所(cron.toml)に置き、文はテンプレート名(`${gate_every}` など。
scripts/sfconfig.py の CRON_TEMPLATE_NAMES。値は「N分」)で書く。

検査(hermes/cron.toml を読む):
  1. ジョブ名の「（N分）」「（毎時）」が schedule と合う(`every Nm` / `*/N * * * *` は N分、`* * * * *` は1分、`M * * * *` は毎時)。ベースラインで許さない
  2. 毎分のジョブは、そのスクリプト(job.script / job.monitor → [wrappers] の target → target が呼ぶスクリプト)が RunLock(tools/with_lock.py でもよい)を取る
  3. prompts/*.txt・skills/**/*.md・SOUL・scripts/templates/*.md に周期の数値(`every Nm`、`N分毎`、`毎分`、`*/N * * * *`、ジョブ名の「（N分）」)が
     直に書かれていたら、`${<ジョブ>_every}` を使えと出す(テンプレートの `<!-- -->` のコメントは除く)
  4. tools/test_install.py は周期を cron.toml から読む(`every Nm` などを直書きしたら落とす)。ベースラインで許さない

    python3 tools/lint_cron.py                                                  # 全部出す(違反があれば exit 1)
    python3 tools/lint_cron.py --baseline tools/lint_cron_baseline.json         # 既存の 2・3 は許し、新規・悪化だけ落とす
    python3 tools/lint_cron.py --write-baseline tools/lint_cron_baseline.json   # 今の違反で書き直す(直したあとにだけ。違反を足すために使わない)

ベースラインは「キー → 件数」(3 は「ファイル::書かれた周期」、2 は「hermes/cron.toml::RunLock:<ジョブ>」。行番号は持たない)。件数が増えたら悪化で落ちる。
cron.toml の値そのものを変える(例: ゲート制御を毎分にする)のはこの検査の外(値の変更は別の段)。
"""
import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import sfconfig  # noqa: E402

CRON_REL = "hermes/cron.toml"
TEST_INSTALL_REL = "tools/test_install.py"
# 周期の数値を直に書いてはいけない文(repo 内)。globs は root からの相対
# plugins/ は対象外(プラグインのジョブは自分の cron.toml を持ち、`${gate_every}` などは本体のジョブの名前)
TEXT_GLOBS = ("prompts/*.txt", "skills/**/*.md", "hermes/profiles/**/*.md", "hermes/profiles/**/*.txt", "scripts/templates/*.md")
# 周期の書き方(分の周期だけ。timeout の「（7分）」や dispatcher の `60 s` は周期ではないので対象外)
PERIOD_RES = (
    re.compile(r"\bevery\s+\d+\s*(?:m|min|minutes?)\b"),
    re.compile(r"\d+\s*分\s*(?:毎|ごと|おき|間隔)"),
    re.compile(r"毎分"),
    re.compile(r"\*/\d+ \* \* \* \*"),
    re.compile(r"(?<![\w*/])\* \* \* \* \*(?![\w*])"),
)
NAME_PERIOD_RE = re.compile(r"[（(]\s*(\d+)\s*分\s*[）)]")
HOURLY_RE = re.compile(r"^\d+ \* \* \* \*$")
LOCK_FREE_LIBS = {"epic_common.py", "with_lock.py", "sfconfig.py"}  # RunLock の定義側。ここを辿っても「取っている」ことにはならない
FILE_REF_RE = re.compile(r"[\w./-]+\.(?:py|sh)")
BASELINABLE = ("text", "lock")  # ジョブ名の不一致と test_install の直書きは、既存でも許さない
MAX_DEPTH = 4


def load_cron(root):
    with open(Path(root) / CRON_REL, "rb") as f:
        return tomllib.load(f)


def jobs_of(cron):
    return cron.get("jobs") or {}


# ---------------------------------------------------------------- 1. ジョブ名と schedule

def check_names(cron):
    """[(kind, key, 文)]。名前に「（N分）」「（毎時）」があるのに schedule が合わないジョブ。"""
    out = []
    for key, job in jobs_of(cron).items():
        name, sched = str(job.get("name", "")), str(job.get("schedule", ""))
        m = NAME_PERIOD_RE.search(name)
        minutes = sfconfig.schedule_minutes(sched)
        if m:
            if minutes is None:
                msg = f"jobs.{key}: ジョブ名の「（{m.group(1)}分）」に対し schedule {sched!r} は分の周期ではない"
            elif minutes != int(m.group(1)):
                msg = f"jobs.{key}: ジョブ名の「（{m.group(1)}分）」と schedule {sched!r}（{minutes}分）が合わない"
            else:
                continue
        elif "（毎時）" in name:
            if HOURLY_RE.match(" ".join(sched.split())):
                continue
            msg = f"jobs.{key}: ジョブ名の「（毎時）」と schedule {sched!r} が合わない（毎時は `M * * * *`）"
        else:
            continue
        out.append(("name", f"{CRON_REL}::name:{key}", f"{CRON_REL}: {msg}"))
    return out


# ---------------------------------------------------------------- 2. 毎分のジョブの RunLock

def target_path(root, wrapper):
    """[wrappers] の target(`live/x.py` = scripts/x.py、`repo/p` = repo の p)を repo 内のパスにする。"""
    target = str(wrapper.get("target", ""))
    head, _, rest = target.partition("/")
    if head == "live":
        return Path(root) / "scripts" / rest
    if head == "repo":
        return Path(root) / rest
    return None


def _scan(path):
    """(ロックを取っている, 辿るファイル名の一覧)。.py は ast で見る(docstring に書いた RunLock() は取ったことにならない)。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            refs = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    if (isinstance(f, ast.Name) and f.id == "RunLock") or (isinstance(f, ast.Attribute) and f.attr == "RunLock"):
                        return True, []
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "with_lock.py" in node.value:
                        return True, []
                    refs += FILE_REF_RE.findall(node.value) if FILE_REF_RE.fullmatch(node.value) else []
            return False, refs
    if "with_lock.py" in text:
        return True, []
    return False, FILE_REF_RE.findall(text)


def takes_lock(root, path, depth=MAX_DEPTH, seen=None):
    """path(またはそれが名前で呼ぶ scripts/・tools/ のスクリプト)が RunLock を取っていれば True。"""
    seen = set() if seen is None else seen
    if path in seen or not path.is_file():
        return False
    seen.add(path)
    locked, refs = _scan(path)
    if locked:
        return True
    if depth <= 1:
        return False
    for ref in refs:
        name = Path(ref).name
        if name in LOCK_FREE_LIBS or name == path.name:
            continue
        for cand in (Path(root) / "scripts" / name, Path(root) / "tools" / name, Path(root) / ref):
            if cand.is_file() and takes_lock(root, cand, depth - 1, seen):
                return True
    return False


def check_locks(cron, root):
    """[(kind, key, 文)]。毎分のジョブのスクリプトが RunLock を取っていない。"""
    out, wrappers = [], cron.get("wrappers") or {}
    for key, job in jobs_of(cron).items():
        if sfconfig.schedule_minutes(job.get("schedule", "")) != 1:
            continue
        script = job.get("script") or job.get("monitor")
        where = f"{CRON_REL}: jobs.{key}（毎分: schedule {job.get('schedule')!r}）"
        if not script:
            msg = f"{where}: スクリプトが無く、同時実行を防げない"
        elif script not in wrappers:
            msg = f"{where}: {script} が [wrappers] に無い"
        else:
            path = target_path(root, wrappers[script])
            if path is None or not path.is_file():
                msg = f"{where}: {script} の実体 {wrappers[script].get('target')} が見つからない"
            elif takes_lock(root, path):
                continue
            else:
                msg = f"{where}: {script} → {wrappers[script]['target']} が RunLock を取っていない（tools/with_lock.py でもよい）"
        out.append(("lock", f"{CRON_REL}::RunLock:{key}", msg))
    return out


# ---------------------------------------------------------------- 3. 文に周期の数値

def template_hint(cron, minutes=None, key=None):
    """使うべきテンプレート名の案内。minutes が合うジョブ(CRON_TEMPLATE_NAMES にあるもの)の名前、無ければ全部。"""
    by_job = {k: n for n, k in sfconfig.CRON_TEMPLATE_NAMES.items()}
    jobs = jobs_of(cron)
    if key is not None:
        names = [by_job[key]]
    else:
        names = [by_job[k] for k in by_job if k in jobs and sfconfig.schedule_minutes(jobs[k].get("schedule", "")) == minutes]
    names = names or list(sfconfig.CRON_TEMPLATE_NAMES)
    return "周期は書かず " + " / ".join("${" + n + "}" for n in names) + " を使う"


def name_stems(cron):
    """ジョブ名の「（N分）」の手前(「SF 出荷」)-> ジョブのキー。"""
    out = {}
    for key, job in jobs_of(cron).items():
        m = NAME_PERIOD_RE.search(str(job.get("name", "")))
        if m:
            out[str(job["name"])[:m.start()].strip()] = key
    return out


def scan_text(text, cron):
    """(行番号, 書かれた周期, 案内)。`<!-- -->` のコメント(複数行も)は除く。"""
    stems = name_stems(cron)
    name_res = [(re.compile(re.escape(s) + r"\s*[（(]\s*\d+\s*分\s*[）)]"), k) for s, k in stems.items()]
    found, in_comment = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if in_comment:
            in_comment = "-->" not in line
            continue
        s = line.strip()
        if s.startswith("<!--"):
            in_comment = "-->" not in s
            continue
        for rx, k in name_res:
            for m in rx.finditer(line):
                found.append((no, m.group(0), "ジョブ名の周期は書かず、数字の所を ${" + {v: n for n, v in sfconfig.CRON_TEMPLATE_NAMES.items()}.get(k, "<ジョブ>_every") + "} にする"))
        for rx in PERIOD_RES:
            for m in rx.finditer(line):
                found.append((no, m.group(0), template_hint(cron, minutes=_minutes_of(m.group(0)))))
    return found


def _minutes_of(token):
    if token == "毎分" or token == "* * * * *":
        return 1
    m = re.search(r"\d+", token)
    return int(m.group(0)) if m else None


def text_files(root):
    seen, out = set(), []
    for g in TEXT_GLOBS:
        for p in sorted(Path(root).glob(g)):
            if p.is_file() and p not in seen and "__pycache__" not in p.parts:
                seen.add(p)
                out.append(p)
    return out


def check_texts(cron, root):
    """[(kind, key, 文, ファイル, 行)]。"""
    out = []
    for p in text_files(root):
        rel = p.relative_to(root).as_posix()
        for no, token, hint in scan_text(p.read_text(encoding="utf-8"), cron):
            out.append(("text", f"{rel}::{token}", f"{rel}:{no}: {token} → {hint}"))
    return out


# ---------------------------------------------------------------- 4. test_install

def check_test_install(root):
    p = Path(root) / TEST_INSTALL_REL
    if not p.is_file():
        return []
    out = []
    for no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        for rx in (PERIOD_RES[0], NAME_PERIOD_RE, PERIOD_RES[3], PERIOD_RES[4]):
            m = rx.search(line)
            if m:
                out.append(("test_install", f"{TEST_INSTALL_REL}::{m.group(0)}",
                            f"{TEST_INSTALL_REL}:{no}: {m.group(0)} → 周期を直書きしない。hermes/cron.toml の schedule を読む"))
                break
    return out


# ---------------------------------------------------------------- 全体

def collect(root):
    cron = load_cron(root)
    return cron, check_names(cron) + check_locks(cron, root) + check_texts(cron, root) + check_test_install(root)


def counts(items):
    c = {}
    for kind, key, *_ in items:
        if kind in BASELINABLE:
            c[key] = c.get(key, 0) + 1
    return c


def main(argv=None):
    ap = argparse.ArgumentParser(description="cron のジョブ名・schedule・RunLock・文への周期の書き写しを検査する")
    ap.add_argument("--root", default=str(REPO), help="sf repo のルート(既定: この repo。テスト用)")
    ap.add_argument("--baseline", default=None, help="既知の違反の一覧(JSON)。載っているもの(2・3)は許し、新規・悪化だけ落とす")
    ap.add_argument("--write-baseline", default=None, metavar="FILE", help="今の違反(2・3)をベースラインとして書き出して終了")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        _cron, items = collect(root)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"lint-cron: {root / CRON_REL} を読めません: {e}", file=sys.stderr)
        return 1
    now = counts(items)

    if args.write_baseline:
        Path(args.write_baseline).write_text(json.dumps({"violations": dict(sorted(now.items()))}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"lint-cron: {sum(now.values())} 件をベースラインに書いた: {args.write_baseline}")
        return 0

    known = json.loads(Path(args.baseline).read_text(encoding="utf-8"))["violations"] if args.baseline else {}
    excess = {k: n - known.get(k, 0) for k, n in now.items() if n > known.get(k, 0)}  # 新規・悪化の件数
    shown = [it for it in items if it[0] not in BASELINABLE or it[1] in excess]
    for it in shown:
        print(f"FAIL {it[2]}")
    base_n = sum(1 for it in items if it[0] in BASELINABLE)
    print(f"lint-cron: {len(items)} 件（ジョブ名 {sum(1 for i in items if i[0] == 'name')} / RunLock {sum(1 for i in items if i[0] == 'lock')} / "
          f"文の周期 {sum(1 for i in items if i[0] == 'text')} / test_install {sum(1 for i in items if i[0] == 'test_install')}）")
    if args.baseline:
        print(f"lint-cron: 既知 {base_n - sum(excess.values())} 件を許容、新規・悪化 {sum(excess.values())} 件")
        for k in sorted(known):
            if now.get(k, 0) < known[k]:
                print(f"  解消: {k} （ベースラインから書き直す: python3 tools/lint_cron.py --write-baseline tools/lint_cron_baseline.json）")
    return 1 if shown else 0


if __name__ == "__main__":
    sys.exit(main())
