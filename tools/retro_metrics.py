#!/usr/bin/env python3
"""工場の動きの指標を計算して JSON で stdout に出す(読むだけ・決定論・保存しない。標準ライブラリのみ)。

方針(docs/retro.md の「最小保存版」): 日次ファイルも集計も残さない。元データ(kanban.db の task_runs、各 profile の state.db、
cron の出力、skill_metrics、lint)が残っているので、必要な日の値はいつでもここから再計算する。出力に時刻(いま)を入れない。
窓は JST の閉じた日付(既定: 今日 00:00 JST を終わり(排他)にした直近7日)なので、同じ入力なら同じバイト列になる。

各指標は「定義の版 v」を持つ(DEFINITIONS が唯一の正本。文を変えたら v を上げる。tools/retro_definitions.golden.json が照合する)。
サンプル(n)が 3 件未満の指標は value=null・status="n<3" で出す(0 とは書かない)。元データが無ければ status="unmeasured"、
読んで失敗すれば status="error"(どちらも value=null)。

  python3 tools/retro_metrics.py                                 # 直近7日(今日 00:00 JST まで)
  python3 tools/retro_metrics.py --end 2026-11-12 --days 7       # 窓 [11-05 00:00, 11-12 00:00) JST
  python3 tools/retro_metrics.py --since 2026-11-02T12:00 --until 2026-11-09   # 窓を直接(日付だけなら JST 0 時)
  python3 tools/retro_metrics.py --groups cards,runs --with-notion              # 一部の群だけ / 起票漏れ検査(Notion を読む)も
  python3 tools/retro_metrics.py --list-definitions              # 定義の一覧(版・取得元・最小サンプル)

群(--groups): cards runs review stall ai make_test lint skills filing。窓の指標は窓の中の出来事(カードは completed_at、run は ended_at、
セッションは started_at が窓に入るもの)。時点の指標(lint・skills・filing)は「いま」の状態で、過去には遡れない。
kanban の読み取りは tenant=smoke と題名が 【smoke】 で始まるカードを除く。
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DAY = 86400
REPO = Path(__file__).resolve().parent.parent
HOME = Path("~").expanduser()
BOARD_DB = HOME / ".hermes" / "kanban" / "boards" / "dev" / "kanban.db"
PROFILES_DIR = HOME / ".hermes" / "profiles"
SKILLS_REPO = HOME / "ghq" / "github.com" / "example-org" / "skills"
SHIP_EVENTS = HOME / ".hermes" / "state" / "ship_events.jsonl"
PROFILES = ("dev", "executor", "reviewer")
WORKERS = ("executor", "reviewer")
MIN_N = 3
FAST_SECONDS = 60  # run の所要がこれ以下(60 秒ちょうどを含む)のクラッシュ = 起動直後に落ちた
STALL_SECONDS = 900  # scripts/epic_pipeline_health.py の STALL_SECONDS と同じ(test_retro.py が照合する)
CRASH = ("crashed", "timed_out")
FAILED = ("crashed", "timed_out", "spawn_failed", "gave_up")
RETRY_AFTER = ("crashed", "timed_out", "spawn_failed", "reclaimed")
# ready に戻る(= claim を待ち始める)イベント。created は payload の status が ready のときだけ。
READY_KINDS = ("review_requested", "changes_requested", "unblocked", "rate_limited", "reclaimed", "crashed", "timed_out", "spawn_failed")
GROUPS = ("cards", "runs", "review", "stall", "ai", "make_test", "lint", "skills", "filing")
BOARD_GROUPS = ("cards", "runs", "review", "stall")
SMOKE_SQL = "COALESCE(t.tenant,'') <> 'smoke' AND t.title NOT LIKE '【smoke】%'"  # kanban を読む側(review_catches.py も)が使う、smoke のカードを除く条件


def _d(unit, better, text, src, scope="window", min_n: "int | None" = MIN_N, tol=(0.2, 1.0), v=1):
    return {"v": v, "unit": unit, "better": better, "text": text, "src": src, "scope": scope, "min_n": min_n, "tol": list(tol)}


SMOKE = "tenant=smoke と題名が【smoke】で始まるカードは除く。"
DEFINITIONS = {
    "cards.done": _d("枚", "none", f"窓の中に completed_at があるカードの枚数(n=枚数)。{SMOKE}", "kanban"),
    "cards.lead_min_p50": _d("分", "low", f"窓の中に完了したカードの created_at→completed_at の中央値(分)。{SMOKE}", "kanban"),
    "cards.lead_min_p90": _d("分", "low", f"窓の中に完了したカードの created_at→completed_at の p90(最近順位法・分)。{SMOKE}", "kanban"),
    "cards.work_min_p50": _d("分", "low", f"窓の中に完了したカードの run の所要(ended_at-started_at)の合計の中央値(分)。{SMOKE}", "kanban"),
    "runs.failed": _d("%", "low", "窓の中に終わった executor・reviewer の run のうち outcome が crashed・timed_out・spawn_failed・gave_up のものの割合(n=run 数)。", "kanban", tol=(0.2, 2.0)),
    "runs.crashed.executor": _d("%", "low", "窓の中に終わった executor の run のうち outcome が crashed・timed_out のものの割合(n=run 数)。", "kanban", tol=(0.2, 2.0)),
    "runs.crashed.reviewer": _d("%", "low", "窓の中に終わった reviewer の run のうち outcome が crashed・timed_out のものの割合(n=run 数)。", "kanban", tol=(0.2, 2.0)),
    "runs.crash_fast.executor": _d("%", "low", f"窓の中に終わった executor の run のうち、crashed・timed_out で所要が {FAST_SECONDS} 秒以下のものの割合(n=run 数)。", "kanban", tol=(0.2, 2.0)),
    "runs.crash_fast.reviewer": _d("%", "low", f"窓の中に終わった reviewer の run のうち、crashed・timed_out で所要が {FAST_SECONDS} 秒以下のものの割合(n=run 数)。", "kanban", tol=(0.2, 2.0)),
    "runs.retry_rate": _d("%", "low", "窓の中に終わった run のうち、同じカードの直前の run が crashed・timed_out・spawn_failed・reclaimed だったもの(再試行)の割合(n=run 数)。", "kanban", tol=(0.2, 2.0)),
    "stall.rate": _d("%", "low", f"窓の中の claim のうち、ready になってから claim までが {STALL_SECONDS} 秒を超えたものの割合(n=起点が分かる claim 数。起点=直前の created(ready)・review_requested・changes_requested・unblocked・rate_limited・reclaimed・crashed・timed_out・spawn_failed)。", "kanban", tol=(0.2, 2.0)),
    "review.rounds_p50": _d("回", "low", f"窓の中に完了したカード1枚あたりの reviewer run の数の中央値。{SMOKE}", "kanban", tol=(0.2, 0.5)),
    "review.changes_rate": _d("%", "low", "窓の中に終わった reviewer の run のうち outcome が changes_requested のものの割合(n=reviewer の run 数)。", "kanban", tol=(0.2, 2.0)),
    "make_test.sec_p50": _d("秒", "low", "出荷で main に重ねて流した make test の所要(秒)の中央値。~/.hermes/state/ship_events.jsonl の test_seconds(ts が窓の中)。ファイルが無ければ unmeasured。", "ship_events", tol=(0.2, 5.0)),
    "lint.skill": _d("件", "low", "sf の tools/skill_lint.py --json の problems の数(時点)。", "lint", scope="point", min_n=None),
    "lint.procedures": _d("件", "low", "sf の tools/lint_procedures.py の違反の数(ベースラインを引かない生の数・時点)。", "lint", scope="point", min_n=None),
    "lint.envs": _d("件", "low", "sf の tools/lint_envs.py の違反の数(時点)。", "lint", scope="point", min_n=None),
    "skills.count": _d("本", "none", "skills repo の tools/skill_metrics.py --json の skills(時点)。", "skills_repo", scope="point", min_n=None),
    "skills.refs": _d("本", "none", "skill_metrics.py --json の refs(時点)。", "skills_repo", scope="point", min_n=None),
    "skills.total_chars": _d("字", "low", "skill_metrics.py --json の total(スキル総量・時点)。", "skills_repo", scope="point", min_n=None),
    "skills.violations": _d("件", "low", "skill_metrics.py --json の violations(時点)。", "skills_repo", scope="point", min_n=None),
    "skills.dup": _d("件", "low", "skill_metrics.py --json の dup(時点)。", "skills_repo", scope="point", min_n=None),
    "filing.orphan_tasks": _d("件", "low", "起票漏れ検査: scripts/check_orphan_tasks.py の orphan tasks(エピックの無い開いたタスク行・時点)。Notion を読むので --with-notion を付けたときだけ測る。", "notion", scope="point", min_n=None),
}
for _p in PROFILES:
    DEFINITIONS[f"ai.calls_per_session.{_p}"] = _d("回/本", "low", f"窓の中に始まった {_p} プロファイルのセッション1本あたりの api_call_count の平均(n=api_call_count が 1 以上のセッション数。合計は sum)。空の cron セッションは分母から除く。", f"state.db:{_p}", tol=(0.2, 2.0), v=2)
    DEFINITIONS[f"ai.cache_read_per_session.{_p}"] = _d("トークン/本", "low", f"窓の中に始まった {_p} プロファイルのセッション1本あたりの cache_read_tokens の平均(n=api_call_count が 1 以上のセッション数。合計は sum)。空の cron セッションは分母から除く。", f"state.db:{_p}", tol=(0.2, 1000.0), v=2)


def group_of(key):
    return key.split(".", 1)[0]


def keys_of(groups):
    return [k for k in DEFINITIONS if group_of(k) in groups]


def definition_hash(key):
    return hashlib.sha256(DEFINITIONS[key]["text"].encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 小さな道具
def parse_when(s):
    """epoch(10桁以上の整数)・ISO 日時(offset 無しは JST)・日付だけ(JST の 0 時)→ epoch 秒。"""
    s = str(s).strip()
    if re.fullmatch(r"\d{10,}", s):
        return int(s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return int(dt.timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, JST).isoformat()


def default_window(now, days):
    """now が属する JST の日の 0 時を終わり(排他)にした直近 days 日。"""
    end = int(now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return end - days * DAY, end


def num(x):
    if isinstance(x, float):
        x = round(x, 1)
        return 0.0 if x == 0 else x
    return x


def p50(xs):
    return statistics.median(xs)


def p90(xs):
    s = sorted(xs)
    return s[math.ceil(0.9 * len(s)) - 1]


def mk(key, value, n, **extra):
    d = DEFINITIONS[key]
    out = {"v": d["v"], "unit": d["unit"], "scope": d["scope"], "n": n}
    if d["min_n"] is not None and n < d["min_n"]:
        out.update(value=None, status=f"n<{d['min_n']}")
    else:
        out.update(value=num(value), status="ok")
    out.update(extra)
    return out


def rate(key, k, n):
    return mk(key, 100.0 * k / n if n else None, n, count=k)


def missing(key, status, reason):
    d = DEFINITIONS[key]
    return {"v": d["v"], "unit": d["unit"], "scope": d["scope"], "n": None, "value": None, "status": status, "reason": reason}


def ro(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} が無い")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only=1")
    return con


# ---------------------------------------------------------------- 群ごとの計算
def board_metrics(since, until, board_db, groups):
    con = ro(board_db)
    try:
        smoke = SMOKE_SQL
        cards = con.execute(f"SELECT t.id, t.created_at, t.completed_at FROM tasks t WHERE t.completed_at >= ? AND t.completed_at < ? AND {smoke}",
                            (since, until)).fetchall()
        runs_all = con.execute(f"SELECT r.task_id, r.profile, r.started_at, r.ended_at, r.outcome FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                               f"WHERE r.ended_at IS NOT NULL AND {smoke} ORDER BY r.task_id, r.started_at, r.id").fetchall()
        events = []
        if "stall" in groups:
            marks = ",".join("?" * (len(READY_KINDS) + 2))
            events = con.execute(f"SELECT e.task_id, e.kind, e.created_at, e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
                                 f"WHERE e.kind IN ({marks}) AND {smoke} ORDER BY e.task_id, e.id", ("created", "claimed") + READY_KINDS).fetchall()
    finally:
        con.close()
    out = {}
    card_ids = {c[0] for c in cards}
    by_task = {}
    for r in runs_all:
        by_task.setdefault(r[0], []).append(r)
    if "cards" in groups:
        lead = [(c[2] - c[1]) / 60 for c in cards]
        work = [sum((r[3] - r[2]) for r in by_task.get(c[0], [])) / 60 for c in cards]
        out["cards.done"] = mk("cards.done", len(cards), len(cards))
        out["cards.lead_min_p50"] = mk("cards.lead_min_p50", p50(lead) if lead else None, len(lead))
        out["cards.lead_min_p90"] = mk("cards.lead_min_p90", p90(lead) if lead else None, len(lead))
        out["cards.work_min_p50"] = mk("cards.work_min_p50", p50(work) if work else None, len(work))
    in_win = [r for rs in by_task.values() for r in rs if since <= r[3] < until]
    if "runs" in groups:
        w = [r for r in in_win if r[1] in WORKERS]
        out["runs.failed"] = rate("runs.failed", sum(r[4] in FAILED for r in w), len(w))
        for p in WORKERS:
            pr = [r for r in w if r[1] == p]
            out[f"runs.crashed.{p}"] = rate(f"runs.crashed.{p}", sum(r[4] in CRASH for r in pr), len(pr))
            out[f"runs.crash_fast.{p}"] = rate(f"runs.crash_fast.{p}", sum(r[4] in CRASH and r[3] - r[2] <= FAST_SECONDS for r in pr), len(pr))
        retries = 0
        for rs in by_task.values():
            for prev, cur in zip(rs, rs[1:]):
                if since <= cur[3] < until and cur[1] in WORKERS and prev[4] in RETRY_AFTER:
                    retries += 1
        out["runs.retry_rate"] = rate("runs.retry_rate", retries, len(w))
    if "review" in groups:
        rounds = [sum(r[1] == "reviewer" for r in by_task.get(cid, [])) for cid in sorted(card_ids)]
        out["review.rounds_p50"] = mk("review.rounds_p50", p50(rounds) if rounds else None, len(rounds))
        rv = [r for r in in_win if r[1] == "reviewer"]
        out["review.changes_rate"] = rate("review.changes_rate", sum(r[4] == "changes_requested" for r in rv), len(rv))
    if "stall" in groups:
        ready_since, n, stalls = {}, 0, 0
        for tid, kind, at, payload in events:
            if kind == "created":
                try:
                    ok = (json.loads(payload or "{}") or {}).get("status") == "ready"
                except ValueError:
                    ok = False
                ready_since[tid] = at if ok else None
            elif kind == "claimed":
                start = ready_since.get(tid)
                if start is not None and since <= at < until:
                    n += 1
                    stalls += (at - start) > STALL_SECONDS
                ready_since[tid] = None
            else:
                ready_since[tid] = at
        out["stall.rate"] = rate("stall.rate", stalls, n)
    return out


def ai_metrics(since, until, profiles_dir):
    out = {}
    for p in PROFILES:
        ck, rk = f"ai.calls_per_session.{p}", f"ai.cache_read_per_session.{p}"
        try:
            con = ro(Path(profiles_dir) / p / "state.db")
        except FileNotFoundError as e:
            out[ck], out[rk] = missing(ck, "unmeasured", str(e)), missing(rk, "unmeasured", str(e))
            continue
        except sqlite3.Error as e:
            out[ck], out[rk] = missing(ck, "error", str(e)), missing(rk, "error", str(e))
            continue
        try:
            n, calls, cache = con.execute("SELECT COUNT(*), COALESCE(SUM(api_call_count),0), COALESCE(SUM(cache_read_tokens),0) FROM sessions "
                                          "WHERE started_at >= ? AND started_at < ? AND api_call_count > 0", (since, until)).fetchone()
        except sqlite3.Error as e:
            out[ck], out[rk] = missing(ck, "error", str(e)), missing(rk, "error", str(e))
            continue
        finally:
            con.close()
        out[ck] = mk(ck, calls / n if n else None, n, sum=calls)
        out[rk] = mk(rk, cache / n if n else None, n, sum=cache)
    return out


def make_test_metrics(since, until, ship_events):
    key = "make_test.sec_p50"
    try:
        text = Path(ship_events).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {key: missing(key, "unmeasured", f"{ship_events} が無い(出荷の記録は未実装)")}
    except OSError as e:
        return {key: missing(key, "error", str(e))}
    xs = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e.get("test_seconds"), (int, float)) and isinstance(e.get("ts"), (int, float)) and since <= e["ts"] < until:
            xs.append(e["test_seconds"])
    return {key: mk(key, p50(xs) if xs else None, len(xs))}


def _run(cmd, cwd=None, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)


def lint_metrics(repo):
    out = {}
    py = sys.executable
    try:
        r = _run([py, "tools/skill_lint.py", "--json"], cwd=repo)
        out["lint.skill"] = mk("lint.skill", len(json.loads(r.stdout)["problems"]), None)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        out["lint.skill"] = missing("lint.skill", "error", f"skill_lint.py --json を読めない: {e}")
    try:
        r = _run([py, "tools/lint_procedures.py"], cwd=repo)
        m = re.search(r"lint-procedures: (\d+) 件", r.stdout)
        out["lint.procedures"] = mk("lint.procedures", int(m.group(1)), None) if m else missing("lint.procedures", "error", "件数の行が無い")
    except (OSError, subprocess.SubprocessError) as e:
        out["lint.procedures"] = missing("lint.procedures", "error", str(e))
    try:
        r = _run([py, "tools/lint_envs.py"], cwd=repo)
        m = re.search(r"lint_envs: (\d+) 件", r.stdout + r.stderr)
        if m:
            out["lint.envs"] = mk("lint.envs", int(m.group(1)), None)
        elif "ok lint_envs" in r.stdout:
            out["lint.envs"] = mk("lint.envs", 0, None)
        else:
            out["lint.envs"] = missing("lint.envs", "error", "結果の行が無い")
    except (OSError, subprocess.SubprocessError) as e:
        out["lint.envs"] = missing("lint.envs", "error", str(e))
    return out


SKILL_FIELDS = {"skills.count": "skills", "skills.refs": "refs", "skills.total_chars": "total", "skills.violations": "violations", "skills.dup": "dup"}


def skills_metrics(skills_repo, repo):
    script = Path(skills_repo) / "tools" / "skill_metrics.py"
    if not script.exists():
        return {k: missing(k, "unmeasured", f"{script} が無い") for k in SKILL_FIELDS}
    try:
        r = _run([sys.executable, str(script), "--json", "--other", str(Path(repo) / "skills")])
        d = json.loads(r.stdout)
        return {k: mk(k, d[f], None) for k, f in SKILL_FIELDS.items()}
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        return {k: missing(k, "error", f"skill_metrics.py --json を読めない: {e}") for k in SKILL_FIELDS}


def filing_metrics(orphan_cmd):
    key = "filing.orphan_tasks"
    if not orphan_cmd:
        return {key: missing(key, "unmeasured", "Notion を読むので --with-notion を付けたときだけ測る")}
    try:
        r = _run(list(orphan_cmd), timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return {key: missing(key, "error", str(e))}
    m = re.search(r"orphan tasks: (\d+)", r.stdout)
    if r.returncode in (0, 1) and m:
        return {key: mk(key, int(m.group(1)), None)}
    last = (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or ["(出力なし)"])[-1]
    return {key: missing(key, "error", f"rc={r.returncode} {last[:120]}")}


def compute(since, until, board_db=None, profiles_dir=None, skills_repo=None, ship_events=None, repo=REPO, groups=GROUPS, orphan_cmd=None):
    """窓 [since, until)(epoch 秒)の指標。{"schema", "window", "metrics": {key: {v, unit, scope, n, value, status, ...}}}。時刻(いま)は入れない。"""
    groups = tuple(groups)
    board_db = BOARD_DB if board_db is None else board_db
    profiles_dir = PROFILES_DIR if profiles_dir is None else profiles_dir
    skills_repo = SKILLS_REPO if skills_repo is None else skills_repo
    ship_events = SHIP_EVENTS if ship_events is None else ship_events
    metrics = {}
    bg = [g for g in groups if g in BOARD_GROUPS]
    if bg:
        try:
            metrics.update(board_metrics(since, until, board_db, bg))
        except FileNotFoundError as e:
            metrics.update({k: missing(k, "unmeasured", str(e)) for k in keys_of(bg)})
        except sqlite3.Error as e:
            metrics.update({k: missing(k, "error", f"kanban.db を読めない: {e}") for k in keys_of(bg)})
    if "ai" in groups:
        metrics.update(ai_metrics(since, until, profiles_dir))
    if "make_test" in groups:
        metrics.update(make_test_metrics(since, until, ship_events))
    if "lint" in groups:
        metrics.update(lint_metrics(repo))
    if "skills" in groups:
        metrics.update(skills_metrics(skills_repo, repo))
    if "filing" in groups:
        metrics.update(filing_metrics(orphan_cmd))
    return {"schema": 1, "window": {"since": iso(since), "until": iso(until)}, "metrics": {k: metrics[k] for k in sorted(metrics)}}


def compare(key, before, after):
    """同じ指標・同じ版の2つの値を比べる。→ (verdict, 理由)。verdict: improved / worse / flat / none(判定しない)。"""
    d = DEFINITIONS[key]
    if d["scope"] == "point":
        return "none", "時点の指標は過去を再計算できない"
    if d["better"] not in ("low", "high"):
        return "none", "量の指標で、良い方向が決まっていない"
    if before.get("v") != after.get("v") or before.get("v") != d["v"]:
        return "none", f"定義の版が違う(before v{before.get('v')} / after v{after.get('v')} / 今 v{d['v']})"
    for side, m in (("before", before), ("after", after)):
        if m.get("value") is None:
            return "none", f"{side} が測れていない(status={m.get('status')} n={m.get('n')})"
    rel, absol = d["tol"]
    delta = after["value"] - before["value"]
    if abs(delta) <= max(absol, rel * abs(before["value"])):
        return "flat", "許容の幅の中"
    good = delta < 0 if d["better"] == "low" else delta > 0
    return ("improved" if good else "worse"), "許容の幅を超えて動いた"


def list_definitions():
    lines = ["key\tv\tscope\tmin_n\tunit\tbetter\tsrc\ttext"]
    for k, d in DEFINITIONS.items():
        lines.append(f"{k}\tv{d['v']}\t{d['scope']}\t{d['min_n'] if d['min_n'] is not None else '-'}\t{d['unit']}\t{d['better']}\t{d['src']}\t{d['text']}")
    return "\n".join(lines)


def add_source_args(ap):
    ap.add_argument("--board-db", default=None, help=f"kanban.db(既定 {BOARD_DB})")
    ap.add_argument("--profiles-dir", default=None, help=f"profiles/<p>/state.db の親(既定 {PROFILES_DIR})")
    ap.add_argument("--skills-repo", default=None, help=f"skills repo(既定 {SKILLS_REPO})")
    ap.add_argument("--ship-events", default=None, help=f"出荷の記録 jsonl(既定 {SHIP_EVENTS})")


def source_kwargs(a):
    return dict(board_db=a.board_db, profiles_dir=a.profiles_dir, skills_repo=a.skills_repo, ship_events=a.ship_events)


def main(argv=None):
    ap = argparse.ArgumentParser(description="工場の指標を計算して JSON で出す(保存しない)")
    ap.add_argument("--since", help="窓の始め(含む)。日付 / ISO 日時 / epoch")
    ap.add_argument("--until", help="窓の終わり(含まない)")
    ap.add_argument("--end", help="窓の終わりの日付(JST の 0 時・含まない)。既定: 今日")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--groups", default=",".join(GROUPS), help="カンマ区切り: " + " ".join(GROUPS))
    ap.add_argument("--with-notion", action="store_true", help="起票漏れ検査(Notion を読む)も流す")
    ap.add_argument("--list-definitions", action="store_true")
    ap.add_argument("--write-golden", metavar="FILE", help="定義の版と文の sha を書き出す(定義を直したあとにだけ)")
    add_source_args(ap)
    a = ap.parse_args(argv)
    if a.list_definitions:
        print(list_definitions())
        return 0
    if a.write_golden:
        Path(a.write_golden).write_text(json.dumps({k: [d["v"], definition_hash(k)] for k, d in DEFINITIONS.items()}, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    groups = [g for g in a.groups.split(",") if g]
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        ap.error(f"知らない群: {','.join(bad)}")
    try:
        if a.since or a.until:
            if not (a.since and a.until):
                ap.error("--since と --until は両方要る")
            since, until = parse_when(a.since), parse_when(a.until)
        else:
            end = parse_when(a.end) if a.end else default_window(datetime.now(JST), 0)[1]
            since, until = end - a.days * DAY, end
    except ValueError as e:
        ap.error(f"日時を読めない: {e}")
    if since >= until:
        ap.error("窓が空(--since < --until にする)")
    orphan = [sys.executable, str(REPO / "scripts" / "check_orphan_tasks.py")] if a.with_notion else None
    out = compute(since, until, groups=groups, orphan_cmd=orphan, **source_kwargs(a))
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
