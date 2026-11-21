#!/usr/bin/env python3
"""tools/retro_metrics.py・retro_findings.py・retro_signal.py のオフライン検査(偽の kanban.db / state.db を一時ディレクトリに作る。本物は読まない)。

守ること: 決定論(2回で同じ出力・時刻なし) / n<3 は null で 0 と偽らない / 元データ無しは unmeasured /
指摘は行 URL 必須・同じ指標の未返済は1件だけ・サンプル不足は判定しない / signal に生の値を出さない。
"""
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import retro_metrics as M  # noqa: E402
import retro_findings as F  # noqa: E402
import retro_signal as S  # noqa: E402

JST = timezone(timedelta(hours=9))
DAY = 86400
END = int(datetime(2026, 11, 12, tzinfo=JST).timestamp())  # 窓の終わり(排他)
ROW = "https://www.notion.so/442ffb98fc956a22b90aa5695194b553"
ROW2 = "https://app.notion.com/p/0123456789abcdef0123456789abcdef"


class World:
    """偽の kanban.db(tasks / task_runs / task_events)と profiles/<p>/state.db(sessions)。"""

    def __init__(self, tmp):
        self.dir = Path(tmp)
        self.board = self.dir / "kanban.db"
        self.profiles = self.dir / "profiles"
        con = sqlite3.connect(self.board)
        con.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at INTEGER, completed_at INTEGER, tenant TEXT);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, started_at INTEGER, ended_at INTEGER, outcome TEXT);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        """)
        con.commit()
        con.close()
        self.n = 0

    def db(self):
        return sqlite3.connect(self.board)

    def card(self, created, completed, tenant=None, title="card", reviews=1):
        self.n += 1
        tid = f"t_{self.n:04d}"
        con = self.db()
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)", (tid, title, "done" if completed else "ready", created, completed, tenant))
        t = created
        for _ in range(reviews):
            con.execute("INSERT INTO task_runs (task_id, profile, started_at, ended_at, outcome) VALUES (?,?,?,?,?)",
                        (tid, "reviewer", t, t + 100, "completed"))
            t += 100
        con.commit()
        con.close()
        return tid

    def run(self, tid, profile, started, ended, outcome):
        con = self.db()
        con.execute("INSERT INTO task_runs (task_id, profile, started_at, ended_at, outcome) VALUES (?,?,?,?,?)",
                    (tid, profile, started, ended, outcome))
        con.commit()
        con.close()

    def event(self, tid, kind, at, payload=None):
        con = self.db()
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                    (tid, None, kind, json.dumps(payload) if payload else None, at))
        con.commit()
        con.close()

    def session(self, profile, started, calls, cache):
        p = self.profiles / profile
        p.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p / "state.db")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, started_at REAL, api_call_count INTEGER, cache_read_tokens INTEGER)")
        con.execute("INSERT INTO sessions (started_at, api_call_count, cache_read_tokens) VALUES (?,?,?)", (started, calls, cache))
        con.commit()
        con.close()


def compute(w, since, until, groups=("cards", "runs", "review", "stall", "ai"), **kw):
    return M.compute(since, until, board_db=w.board, profiles_dir=w.profiles, groups=groups, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = World(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def three_cards(self, leads=(600, 1200, 1800), at=END - DAY):
        for lead in leads:
            self.w.card(at - lead, at)


class Metrics(Base):
    def test_cards_p50_p90_in_minutes(self):
        self.three_cards((600, 1200, 1800))
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["cards.done"]["value"], 3)
        self.assertEqual(m["cards.lead_min_p50"]["value"], 20.0)
        self.assertEqual(m["cards.lead_min_p90"]["value"], 30.0)
        self.assertEqual(m["cards.lead_min_p50"]["n"], 3)
        self.assertEqual(m["cards.lead_min_p50"]["v"], 1)

    def test_two_samples_is_null_not_zero(self):
        self.three_cards((600, 1200))
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        for k in ("cards.done", "cards.lead_min_p50", "cards.lead_min_p90", "cards.work_min_p50"):
            self.assertIsNone(m[k]["value"], k)
            self.assertEqual(m[k]["status"], "n<3", k)
            self.assertEqual(m[k]["n"], 2, k)

    def test_no_cards_is_null_with_n_zero(self):
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertIsNone(m["cards.lead_min_p50"]["value"])
        self.assertEqual(m["cards.lead_min_p50"]["n"], 0)

    def test_missing_kanban_is_unmeasured(self):
        m = M.compute(END - 7 * DAY, END, board_db=Path(self.tmp.name) / "none.db", profiles_dir=self.w.profiles,
                      groups=("cards", "runs", "review", "stall"))["metrics"]
        for k in ("cards.done", "runs.failed", "review.rounds_p50", "stall.rate"):
            self.assertIsNone(m[k]["value"], k)
            self.assertEqual(m[k]["status"], "unmeasured", k)

    def test_smoke_cards_excluded(self):
        self.three_cards((600, 1200, 1800))
        self.w.card(END - DAY - 60, END - DAY, tenant="smoke")
        self.w.card(END - DAY - 60, END - DAY, title="【smoke】probe")
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["cards.done"]["value"], 3)

    def test_card_outside_window_not_counted(self):
        self.three_cards((600, 1200, 1800))
        self.w.card(END - 9 * DAY, END - 8 * DAY)
        self.w.card(END - 100, END + 10)  # 窓の外(排他の端)
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["cards.done"]["value"], 3)

    def crash_runs(self, durations, profile="reviewer"):
        tid = self.w.card(END - DAY, END - DAY + 10, reviews=0)
        t = END - DAY
        for d in durations:
            self.w.run(tid, profile, t, t + d, "crashed")
            t += d + 10
        return tid

    def test_crash_fast_includes_exactly_60s(self):
        self.crash_runs([30, 60, 61, 500])
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["runs.crashed.reviewer"]["count"], 4)
        self.assertEqual(m["runs.crash_fast.reviewer"]["count"], 2)  # 30 と 60(61 は含めない)
        self.assertEqual(m["runs.crash_fast.reviewer"]["value"], 50.0)
        self.assertEqual(m["runs.crash_fast.reviewer"]["n"], 4)
        self.assertEqual(m["runs.crash_fast.executor"]["status"], "n<3")  # executor の run が0件 → 0 と偽らない

    def test_run_counted_in_window_it_ended(self):
        tid = self.w.card(END - 3 * DAY, END - 2 * DAY, reviews=0)
        for _ in range(3):  # 窓の前日 23:59 に始まり、窓の中で終わる run
            self.w.run(tid, "executor", END - 7 * DAY - 60, END - 7 * DAY + 60, "crashed")
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["runs.crashed.executor"]["n"], 3)
        m2 = compute(self.w, END - 14 * DAY, END - 7 * DAY)["metrics"]
        self.assertEqual(m2["runs.crashed.executor"]["n"], 0)

    def test_retry_rate_counts_run_after_crash(self):
        tid = self.w.card(END - DAY, END - DAY + 10, reviews=0)
        t = END - DAY
        for oc in ("crashed", "completed", "crashed", "review_requested"):
            self.w.run(tid, "executor", t, t + 100, oc)
            t += 110
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["runs.retry_rate"]["count"], 2)  # crashed の次の run が2回
        self.assertEqual(m["runs.retry_rate"]["n"], 4)

    def test_review_changes_rate_and_rounds(self):
        for reviews in (1, 2, 3):
            self.w.card(END - DAY - 2000, END - DAY, reviews=reviews)
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["review.rounds_p50"]["value"], 2.0)
        self.assertEqual(m["review.rounds_p50"]["n"], 3)
        tid = self.w.card(END - DAY, END - DAY + 5, reviews=0)
        for oc in ("changes_requested", "completed", "changes_requested"):
            self.w.run(tid, "reviewer", END - DAY, END - DAY + 5, oc)
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["review.changes_rate"]["n"], 9)  # 6 + 3
        self.assertEqual(m["review.changes_rate"]["count"], 2)

    def test_stall_counts_ready_wait_over_900s(self):
        base = END - DAY
        for wait in (60, 901, 5000):
            tid = self.w.card(base, base + 99999, reviews=0)
            self.w.event(tid, "created", base, {"status": "ready"})
            self.w.event(tid, "claimed", base + wait)
        tid = self.w.card(base, base + 99999, reviews=0)  # ready ではない作成は起点にしない
        self.w.event(tid, "created", base, {"status": "todo"})
        self.w.event(tid, "claimed", base + 99999)
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["stall.rate"]["n"], 3)
        self.assertEqual(m["stall.rate"]["count"], 2)

    def test_stall_threshold_matches_source(self):
        src = (REPO / "scripts" / "epic_pipeline_health.py").read_text(encoding="utf-8")
        self.assertEqual(int(re.search(r"^STALL_SECONDS = (\d+)", src, re.M).group(1)), M.STALL_SECONDS)

    def test_ai_calls_and_cache_read_per_session(self):
        for calls, cache in ((10, 1000), (20, 3000), (30, 5000)):
            self.w.session("executor", END - DAY, calls, cache)
        self.w.session("executor", END - 8 * DAY, 999, 999)  # 窓の外
        m = compute(self.w, END - 7 * DAY, END)["metrics"]
        self.assertEqual(m["ai.calls_per_session.executor"]["value"], 20.0)
        self.assertEqual(m["ai.calls_per_session.executor"]["sum"], 60)
        self.assertEqual(m["ai.cache_read_per_session.executor"]["value"], 3000.0)
        self.assertEqual(m["ai.calls_per_session.reviewer"]["status"], "unmeasured")  # state.db が無い

    def test_make_test_seconds_unmeasured_without_source_then_measured(self):
        m = M.compute(END - 7 * DAY, END, ship_events=Path(self.tmp.name) / "ship_events.jsonl", groups=("make_test",))["metrics"]
        self.assertEqual(m["make_test.sec_p50"]["status"], "unmeasured")
        self.assertIsNone(m["make_test.sec_p50"]["value"])
        p = Path(self.tmp.name) / "ship_events.jsonl"
        p.write_text("\n".join(json.dumps({"stage": "test", "ts": END - DAY, "test_seconds": s}) for s in (100, 200, 300)) + "\n")
        m = M.compute(END - 7 * DAY, END, ship_events=p, groups=("make_test",))["metrics"]
        self.assertEqual(m["make_test.sec_p50"]["value"], 200.0)

    def test_skills_from_skill_metrics_and_missing_repo(self):
        sk = Path(self.tmp.name) / "skills_repo"
        (sk / "tools").mkdir(parents=True)
        (sk / "tools" / "skill_metrics.py").write_text(
            "import json;print(json.dumps(dict(skills=59, refs=146, total=667676, violations=0, dup=0, nested=0)))\n")
        m = M.compute(END - 7 * DAY, END, skills_repo=sk, groups=("skills",))["metrics"]
        self.assertEqual(m["skills.total_chars"]["value"], 667676)
        self.assertEqual(m["skills.count"]["value"], 59)
        m = M.compute(END - 7 * DAY, END, skills_repo=Path(self.tmp.name) / "nope", groups=("skills",))["metrics"]
        self.assertEqual(m["skills.total_chars"]["status"], "unmeasured")

    def test_lint_counts_on_this_repo(self):
        m = M.compute(END - 7 * DAY, END, groups=("lint",))["metrics"]
        self.assertEqual(m["lint.skill"]["value"], 0)
        self.assertEqual(m["lint.envs"]["value"], 0)
        self.assertIsInstance(m["lint.procedures"]["value"], int)

    def test_filing_check_opt_in_and_exit_codes(self):
        m = M.compute(END - 7 * DAY, END, groups=("filing",))["metrics"]
        self.assertEqual(m["filing.orphan_tasks"]["status"], "unmeasured")  # Notion を読むので明示したときだけ
        fake = Path(self.tmp.name) / "orphans.py"
        fake.write_text("print('ORPHAN x');print('orphan tasks: 2');raise SystemExit(1)\n")
        m = M.compute(END - 7 * DAY, END, groups=("filing",), orphan_cmd=[sys.executable, str(fake)])["metrics"]
        self.assertEqual(m["filing.orphan_tasks"]["value"], 2)
        fake.write_text("print('ERROR boom');raise SystemExit(2)\n")
        m = M.compute(END - 7 * DAY, END, groups=("filing",), orphan_cmd=[sys.executable, str(fake)])["metrics"]
        self.assertEqual(m["filing.orphan_tasks"]["status"], "error")
        self.assertIsNone(m["filing.orphan_tasks"]["value"])


class Definitions(unittest.TestCase):
    GOLDEN = TOOLS / "retro_definitions.golden.json"

    def test_text_change_requires_version_bump(self):
        golden = json.loads(self.GOLDEN.read_text(encoding="utf-8"))
        now = {k: [d["v"], hashlib.sha256(d["text"].encode()).hexdigest()[:16]] for k, d in M.DEFINITIONS.items()}
        for k, (v, sha) in now.items():
            self.assertIn(k, golden, f"{k}: golden に無い(tools/retro_metrics.py --write-golden)")
            gv, gsha = golden[k]
            if sha != gsha:
                self.assertGreater(v, gv, f"{k}: 定義の文を変えたのに v を上げていない")
        for k in golden:
            self.assertIn(k, now, f"{k}: golden にあるが定義が無い(消したなら golden も直す)")

    def test_every_metric_in_output_has_a_definition_and_version(self):
        w = World(tempfile.mkdtemp())
        out = M.compute(END - 7 * DAY, END, board_db=w.board, profiles_dir=w.profiles, groups=M.GROUPS)
        for k, m in out["metrics"].items():
            self.assertIn(k, M.DEFINITIONS, k)
            self.assertEqual(m["v"], M.DEFINITIONS[k]["v"], k)


class Deterministic(Base):
    def test_two_runs_byte_identical_and_no_clock(self):
        self.three_cards()
        self.w.session("executor", END - DAY, 10, 100)
        args = [sys.executable, str(TOOLS / "retro_metrics.py"), "--board-db", str(self.w.board), "--profiles-dir", str(self.w.profiles),
                "--since", "2026-11-05", "--until", "2026-11-12", "--groups", "cards,runs,review,stall,ai,make_test,filing",
                "--ship-events", str(Path(self.tmp.name) / "none.jsonl")]
        a = subprocess.run(args, capture_output=True, text=True)
        time.sleep(1.1)
        b = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(a.stdout, b.stdout)
        json.loads(a.stdout)
        self.assertFalse(list(Path(self.tmp.name).glob("*.json")), "保存してはいけない")

    def test_default_window_is_closed_days_not_now(self):
        w = M.default_window(datetime(2026, 11, 12, 15, 30, tzinfo=JST), 7)
        self.assertEqual(w, (END - 7 * DAY, END))


class Compare(unittest.TestCase):
    def m(self, key, value, n=5):
        return {"value": value, "n": n, "status": "ok" if value is not None else "n<3", "v": M.DEFINITIONS[key]["v"]}

    def test_directions_and_flat(self):
        k = "runs.crash_fast.reviewer"  # 小さいほど良い
        self.assertEqual(M.compare(k, self.m(k, 40.0), self.m(k, 10.0))[0], "improved")
        self.assertEqual(M.compare(k, self.m(k, 10.0), self.m(k, 40.0))[0], "worse")
        self.assertEqual(M.compare(k, self.m(k, 10.0), self.m(k, 10.5))[0], "flat")

    def test_null_side_is_none(self):
        k = "cards.lead_min_p50"
        self.assertEqual(M.compare(k, self.m(k, None, 2), self.m(k, 10.0))[0], "none")
        self.assertEqual(M.compare(k, self.m(k, 10.0), self.m(k, None, 2))[0], "none")

    def test_quantity_metric_has_no_direction(self):
        self.assertEqual(M.compare("cards.done", self.m("cards.done", 5), self.m("cards.done", 50))[0], "none")

    def test_version_mismatch_is_none(self):
        k = "cards.lead_min_p50"
        b = self.m(k, 10.0)
        b["v"] = 0
        self.assertEqual(M.compare(k, b, self.m(k, 50.0))[0], "none")

    def test_bucket_1_3_7(self):
        self.assertEqual([S.bucket(i) for i in (1, 2, 3, 6, 7, 20)], [1, 1, 3, 3, 7, 7])


def cli(module, *args, **kw):
    return subprocess.run([sys.executable, str(TOOLS / module), *args], capture_output=True, text=True, **kw)


class Findings(Base):
    def setUp(self):
        super().setUp()
        self.fj = Path(self.tmp.name) / "retro" / "findings.json"

    def fcli(self, *args):
        return cli("retro_findings.py", "--file", str(self.fj), *args)

    def add(self, metric="runs.crash_fast.reviewer", row=ROW, note="reviewer が60秒で落ちる"):
        return self.fcli("add", "--metric", metric, "--row", row, "--note", note, "--today", "2026-11-12")

    def test_add_writes_small_json_with_def_version(self):
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(self.fj.read_text())
        self.assertEqual(len(d["findings"]), 1)
        f = d["findings"][0]
        self.assertEqual((f["metric"], f["row"], f["opened"], f["def_v"]), ("runs.crash_fast.reviewer", ROW, "2026-11-12", 1))
        self.assertLess(len(self.fj.read_text().splitlines()), 40)

    def test_second_open_finding_for_same_metric_is_rejected(self):
        self.assertEqual(self.add().returncode, 0)
        before = self.fj.read_text()
        r = self.add(row=ROW2, note="別の指摘")
        self.assertEqual(r.returncode, 2)
        self.assertIn("未返済", r.stderr)
        self.assertEqual(self.fj.read_text(), before)

    def test_other_metric_is_accepted(self):
        self.assertEqual(self.add().returncode, 0)
        self.assertEqual(self.add(metric="cards.lead_min_p50", row=ROW2).returncode, 0)

    def test_missing_or_non_notion_row_is_rejected(self):
        r = self.fcli("add", "--metric", "cards.lead_min_p50", "--note", "x")
        self.assertEqual(r.returncode, 2)
        r = self.add(row="https://example.com/x")
        self.assertEqual(r.returncode, 2)
        r = self.add(row="")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.fj.exists())

    def test_unknown_metric_is_rejected(self):
        r = self.add(metric="no.such.metric")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.fj.exists())

    def test_close_removes_and_list_shows_open_only(self):
        self.add()
        self.add(metric="cards.lead_min_p50", row=ROW2)
        out = self.fcli("list").stdout
        self.assertIn("runs.crash_fast.reviewer", out)
        self.assertIn(ROW, out)
        self.assertEqual(self.fcli("close", "--metric", "runs.crash_fast.reviewer").returncode, 0)
        out = self.fcli("list").stdout
        self.assertNotIn("runs.crash_fast.reviewer", out)
        self.assertIn("cards.lead_min_p50", out)
        self.assertEqual(self.fcli("close", "--metric", "runs.crash_fast.reviewer").returncode, 2)
        self.assertEqual(self.add(row=ROW2).returncode, 0, "返済したあとは同じ指標に足せる")

    # check: マージ前7日 vs 後7日
    def fill(self, since, before_rates, after_rates, n_per_day=3):
        """reviewer の run を日ごとに n_per_day 本。rate は 1日あたりの crash_fast の本数(0..n_per_day)。"""
        tid = self.w.card(since - 20 * DAY, since - 19 * DAY, reviews=0)
        for rates, start in ((before_rates, since - 7 * DAY), (after_rates, since)):
            for d, k in enumerate(rates):
                t = start + d * DAY + 3600
                for i in range(n_per_day):
                    self.w.run(tid, "reviewer", t + i * 200, t + i * 200 + (30 if i < k else 500), "crashed" if i < k else "completed")

    def ccli(self, since, metric="runs.crash_fast.reviewer", now=None):
        return self.fcli("check", "--metric", metric, "--since", since, "--board-db", str(self.w.board), "--profiles-dir", str(self.w.profiles),
                         "--now", now or "2026-12-31")

    def test_check_improved_with_enough_samples(self):
        since = END - 10 * DAY
        self.fill(since, [3] * 7, [0] * 7)
        r = self.ccli("2026-11-02")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verdict=improved", r.stdout)

    def test_check_worse_and_flat(self):
        since = int(datetime(2026, 11, 2, tzinfo=JST).timestamp())
        self.fill(since, [0] * 7, [3] * 7)
        self.assertIn("verdict=worse", self.ccli("2026-11-02").stdout)
        w2 = World(tempfile.mkdtemp())
        self.w = w2
        self.fill(since, [1] * 7, [1] * 7)
        self.assertIn("verdict=flat", self.ccli("2026-11-02").stdout)

    def test_check_with_two_samples_does_not_judge(self):
        since = int(datetime(2026, 11, 2, tzinfo=JST).timestamp())
        tid = self.w.card(since - 20 * DAY, since - 19 * DAY, reviews=0)
        self.w.run(tid, "reviewer", since - DAY, since - DAY + 30, "crashed")
        self.w.run(tid, "reviewer", since - DAY + 100, since - DAY + 130, "crashed")
        for i in range(2):
            self.w.run(tid, "reviewer", since + DAY + i * 100, since + DAY + i * 100 + 500, "completed")
        r = self.ccli("2026-11-02")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("判定しない", r.stdout)
        self.assertIn("verdict=none", r.stdout)
        self.assertNotRegex(r.stdout, r"verdict=(improved|worse|flat)")
        self.assertIn("n=2", r.stdout)

    def test_check_after_window_is_clamped_to_now(self):
        since = int(datetime(2026, 11, 2, tzinfo=JST).timestamp())
        self.fill(since, [3] * 7, [0] * 7)
        r = self.ccli("2026-11-02", now="2026-11-02T00:30")  # 後が30分しかない(切らなければ7日ぶんの run が入る)
        self.assertIn("判定しない", r.stdout)

    def test_check_point_metric_cannot_be_judged(self):
        r = self.fcli("check", "--metric", "skills.total_chars", "--since", "2026-11-02")
        self.assertEqual(r.returncode, 0)
        self.assertIn("判定しない", r.stdout)

    def test_check_records_merge_base_and_values_only_when_judged(self):
        self.add()
        since = int(datetime(2026, 11, 2, tzinfo=JST).timestamp())
        self.fill(since, [3] * 7, [0] * 7)
        self.ccli("2026-11-02")
        f = json.loads(self.fj.read_text())["findings"][0]
        self.assertEqual(f["fix"]["merged_at"], "2026-11-02T00:00:00+09:00")
        self.assertEqual(f["fix"]["verdict"], "improved")
        self.assertEqual(f["fix"]["before"]["n"], 21)
        self.assertEqual(f["fix"]["after"]["n"], 21)

    def test_check_refuses_when_definition_version_differs(self):
        self.add()
        d = json.loads(self.fj.read_text())
        d["findings"][0]["def_v"] = 0
        self.fj.write_text(json.dumps(d))
        since = int(datetime(2026, 11, 2, tzinfo=JST).timestamp())
        self.fill(since, [3] * 7, [0] * 7)
        r = self.ccli("2026-11-02")
        self.assertIn("判定しない", r.stdout)
        self.assertIn("定義の版", r.stdout)


class Signal(Base):
    def setUp(self):
        super().setUp()
        self.fj = Path(self.tmp.name) / "findings.json"
        for p in M.PROFILES:  # state.db がある(測れている)状態にする。セッションは窓の外
            self.w.session(p, END - 100 * DAY, 1, 1)

    def build(self, crash_per_day_old, crash_per_day_new, new_days=7, per_day=4, shift_value=0):
        """reviewer を毎日 per_day 本。END から new_days 日分が new、それ以前が old の crash_fast 本数。"""
        tid = self.w.card(END - 60 * DAY, END - 59 * DAY, reviews=0)
        for d in range(1, 41):
            day_start = END - d * DAY
            k = crash_per_day_new if d <= new_days else crash_per_day_old
            for i in range(per_day):
                t = day_start + 3600 + i * 600
                fast = i < k
                self.w.run(tid, "reviewer", t, t + (30 if fast else 400 + shift_value), "crashed" if fast else "completed")

    def sig(self, *extra):
        return cli("retro_signal.py", "--board-db", str(self.w.board), "--profiles-dir", str(self.w.profiles), "--end", "2026-11-12",
                   "--findings", str(self.fj), *extra)

    def test_worse_for_7_days_is_bucket_7_and_has_no_raw_values(self):
        self.build(0, 3, new_days=7)
        r = self.sig()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("runs.crash_fast.reviewer:7d", lines[0])
        self.assertIn("worse", lines[0])
        self.assertNotRegex(lines[0], r"\d+\.\d")  # 生の値を出さない
        self.assertNotRegex(lines[0], r"\d{4}-\d\d-\d\d")  # 日付(時刻)も出さない

    def test_streak_buckets_only_1_3_7(self):
        outs = {}
        for n in (1, 2, 3, 4, 6, 7):
            self.w = World(tempfile.mkdtemp())
            for p in M.PROFILES:
                self.w.session(p, END - 100 * DAY, 1, 1)
            self.build(0, 3, new_days=n)
            outs[n] = self.sig().stdout
        self.assertEqual(outs[1], outs[2])
        self.assertEqual(outs[3], outs[4])
        self.assertEqual(outs[3], outs[6])
        self.assertNotEqual(outs[2], outs[3])
        self.assertNotEqual(outs[6], outs[7])

    def test_value_drift_inside_band_leaves_line_unchanged(self):
        self.build(0, 3, new_days=7)
        a = self.sig().stdout
        self.w = World(tempfile.mkdtemp())
        for p in M.PROFILES:
            self.w.session(p, END - 100 * DAY, 1, 1)
        self.build(0, 3, new_days=7, shift_value=7)  # 所要だけ数秒動く
        self.assertEqual(a, self.sig().stdout)

    def test_steady_when_nothing_moves(self):
        self.build(1, 1, new_days=7)
        r = self.sig()
        self.assertEqual(r.stdout.strip(), "RETRO steady")

    def test_thin_data_is_not_judged(self):
        self.w.card(END - 3 * DAY, END - 2 * DAY)
        self.assertEqual(self.sig().stdout.strip(), "RETRO steady")

    def test_open_finding_adds_second_line_with_age_bucket_not_date(self):
        self.build(1, 1)
        F_ = Path(self.fj)
        F_.write_text(json.dumps({"schema": 1, "findings": [
            {"metric": "runs.crash_fast.reviewer", "row": ROW, "note": "x", "opened": "2026-11-09", "def_v": 1}]}))
        lines = self.sig().stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("FINDINGS "))
        self.assertIn("runs.crash_fast.reviewer", lines[1])
        self.assertNotIn("2026-11-09", lines[1])
        self.assertIn("3d", lines[1])
        F_.write_text(json.dumps({"schema": 1, "findings": [
            {"metric": "runs.crash_fast.reviewer", "row": ROW, "note": "x", "opened": "2026-11-11", "def_v": 1}]}))
        self.assertIn("1d", self.sig().stdout.strip().splitlines()[1])

    def test_missing_board_prints_unmeasured_line_not_steady(self):
        r = cli("retro_signal.py", "--board-db", str(Path(self.tmp.name) / "none.db"), "--profiles-dir", str(self.w.profiles),
                "--end", "2026-11-12", "--findings", str(self.fj))
        self.assertEqual(r.returncode, 0)
        self.assertIn("UNMEASURED", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
