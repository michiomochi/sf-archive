#!/usr/bin/env python3
"""tools/review_catches.py report のオフライン検査(偽のレビュー行の JSON と偽の kanban.db を一時ディレクトリに作る。本物の Notion・kanban は読まない)。

守ること: 同じ入力で同じバイト列 / 元データが読めなければ 0 でなく unmeasured / タグ無しは「未分類」と数える / 同じ行・同じ軸1 は 1 件 /
代筆の行は「仮」に別に数える / smoke のカードと窓の外は数えない / 読むだけ(元のファイルも置き場も変わらない)。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import review_catches as R  # noqa: E402

JST = timezone(timedelta(hours=9))
SINCE = "2026-11-08"
SINCE_EPOCH = int(datetime(2026, 11, 8, tzinfo=JST).timestamp())

ROWS = [
    dict(key="aaaa0001-承認", status="差し戻し", created="2026-11-10T10:00:00+09:00", texts=[
        "差し戻し（2026-11-13 棚卸し。決定: みちゃん ／ AI が記入）\n理由：1) [STALE/①/S1] main と 11 ファイルが衝突する。 2) [STALE/②/S1] 判定書の前提が古い。 3) 文言の直し"]),
    dict(key="aaaa0001-承認-r2", status="差し戻し", created="2026-11-11T10:00:00+09:00", texts=["[DESIGN・DUP/②/S2] 規則が文にしか無い。値の写し"]),
    dict(key="bbbb0002-承認", status="取り下げ", created="2026-11-09T09:00:00+09:00", texts=["[INTENT/④/S1] 重複", "[INTENT/④/S1] 重複"]),
    dict(key="cccc0003-承認", status="承認", created="2026-11-09T09:00:00+09:00", texts=["これは数えない（承認の行）"]),
    dict(key="dddd0004-承認", status="レビュー待ち", created="2026-11-12T09:00:00+09:00", texts=[]),
    dict(key="old00005-承認", status="差し戻し", created="2026-11-01T09:00:00+09:00", texts=["[STALE/①/S1] 窓の外"]),
]


def make_board(path):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE tasks(id TEXT, tenant TEXT, title TEXT, created_at INT, completed_at INT);"
                      "CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at INT, payload TEXT);")
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", [("t_1", "epic-x", "実装", 0, None), ("t_smoke", "smoke", "x", 0, None), ("t_2", "epic-y", "【smoke】y", 0, None)])
    ev = [("t_1", "changes_requested", SINCE_EPOCH + 100, json.dumps({"reason": "(1) [WIRING/②/S1] deploy の経路に入っていない (2) 理由の文だけ"})),
          ("t_1", "changes_requested", SINCE_EPOCH + 200, json.dumps({"reason": "[CLAIM/③/S1] 主張と実測が違う"})),
          ("t_1", "changes_requested", SINCE_EPOCH - 5, json.dumps({"reason": "窓の外"})),
          ("t_smoke", "changes_requested", SINCE_EPOCH + 300, json.dumps({"reason": "smoke は除く"})),
          ("t_2", "changes_requested", SINCE_EPOCH + 300, json.dumps({"reason": "題名が smoke"})),
          ("t_1", "claimed", SINCE_EPOCH + 300, "{}")]
    con.executemany("INSERT INTO task_events(task_id, kind, created_at, payload) VALUES (?,?,?,?)", ev)
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.rows, self.board = self.dir / "rows.json", self.dir / "kanban.db"
        self.rows.write_text(json.dumps(ROWS, ensure_ascii=False), encoding="utf-8")
        make_board(self.board)

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, **kw):
        return R.report(kw.pop("since", SINCE), kw.pop("review_json", str(self.rows)), kw.pop("board_db", str(self.board)))


class Split(unittest.TestCase):
    def test_numbered_items(self):
        self.assertEqual(R.split_items("前置き 1) あ 2) い ③ う"), ["1) あ", "2) い", "③ う"])
        self.assertEqual(R.split_items("理由：(1) あ。(2) い"), ["(1) あ。", "(2) い"])

    def test_no_marker_is_one_item_and_empty_is_none(self):
        self.assertEqual(R.split_items("全文で 1 項目"), ["全文で 1 項目"])
        self.assertEqual(R.split_items("  "), [])

    def test_tag(self):
        self.assertEqual(R.parse_tag("[STALE/①/S1] x"), (["STALE"], "①", "S1"))
        self.assertEqual(R.parse_tag("1) [DESIGN・DUP/②/S2] x"), (["DESIGN", "DUP"], "②", "S2"))
        for bad in ("[NOPE/①/S1] x", "[STALE/⑤/S1] x", "[STALE/①/S9] x", "タグ無し"):
            self.assertIsNone(R.parse_tag(bad), bad)


class Report(Base):
    def test_counts(self):
        r = self.report()
        self.assertEqual(r["sources"]["review_db"], {"status": "ok", "rows": 5, "by_status": {"レビュー待ち": 1, "取り下げ": 1, "差し戻し": 2, "承認": 1}})
        self.assertEqual(r["sources"]["kanban"], {"status": "ok", "changes_requested": 2})  # 窓の外・smoke(tenant と題名)・他の種類は数えない
        # 人: aaaa0001 は 3 項目のうち STALE が 2 回(同じ行・同じ軸1 = 1 件)で 2 件(タグ付き 1・未分類 1・仮)、aaaa0001-r2 は 1 項目(DESIGN と DUP の 2 軸で 1 項目)、
        # bbbb0002 は同じ項目 2 つで 1 件。AI: 項目 3(タグ付き 2・未分類 1)
        self.assertEqual((r["items"], r["tagged"], r["untagged"]), (7, 5, 2))
        self.assertEqual(r["provisional"], 2)
        self.assertEqual(r["by_axis1_axis3"], {"DESIGN|HREL": 1, "DUP|HREL": 1, "INTENT|HREL": 1, "WIRING|AIR": 1, "CLAIM|AIR": 1})
        self.assertEqual(r["provisional_by_axis1_axis3"], {"STALE|HREL": 1})  # 同じ行・同じ軸1(STALE が 2 回)は 1 件
        self.assertEqual(r["classification_rate"], round(100.0 * 5 / 7, 1))
        self.assertEqual(r["unmeasured"], [])

    def test_window_excludes_old_rows(self):
        self.assertEqual(self.report(since="2026-11-12")["sources"]["review_db"]["rows"], 1)

    def test_same_input_same_bytes(self):
        a = json.dumps(self.report(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(a, json.dumps(self.report(), ensure_ascii=False, sort_keys=True))
        self.assertNotIn("generated", a)

    def test_unreadable_source_is_unmeasured_not_zero(self):
        r = self.report(review_json=str(self.dir / "missing.json"))
        self.assertEqual(r["sources"]["review_db"]["status"], "unmeasured")
        self.assertEqual([u["source"] for u in r["unmeasured"]], ["review_db"])
        self.assertEqual(r["sources"]["kanban"]["status"], "ok")
        r = self.report(board_db=str(self.dir / "missing.db"))
        self.assertEqual(r["sources"]["kanban"]["status"], "unmeasured")
        self.assertEqual(r["sources"]["review_db"]["status"], "ok")
        self.assertGreater(r["items"], 0)

    def test_empty_window_has_no_rate(self):
        r = self.report(since="2027-01-01")
        self.assertEqual((r["items"], r["classification_rate"]), (0, None))  # 0 件の率は 0 と書かない

    def test_reads_only(self):
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        self.report()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.dir.iterdir()})


class Cli(Base):
    def run_cli(self, *args, env=None):
        e = dict(os.environ, **(env or {}))
        return subprocess.run([sys.executable, str(TOOLS / "review_catches.py"), *args], capture_output=True, text=True, env=e, timeout=60)

    def test_report_prints_the_same_json_as_the_function(self):
        p = self.run_cli("report", "--since", SINCE, "--review-json", str(self.rows), "--board-db", str(self.board))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), json.loads(json.dumps(self.report(), ensure_ascii=False)))

    def test_notion_unreadable_is_unmeasured(self):
        p = self.run_cli("report", "--since", SINCE, "--board-db", str(self.board), env={"SF_CONFIG": str(self.dir / "no-such-config.toml")})
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["sources"]["review_db"]["status"], "unmeasured")
        self.assertEqual(out["sources"]["kanban"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
