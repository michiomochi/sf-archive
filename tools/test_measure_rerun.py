#!/usr/bin/env python3
"""Offline tests of tools/measure_rerun.py (fake kanban.db and state.db in a temp dir; never reads the real ones)."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_rerun as M  # noqa: E402

WT = "/home/u/ghq/github.com/o/sf/.worktrees/one/"
WT2 = "/home/u/ghq/github.com/o/sf/.worktrees/two/"


def call(name, **args):
    return {"function": {"name": name, "arguments": json.dumps(args)}}


class MeasureRerun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.board, self.state = d / "kanban.db", d / "state.db"
        con = sqlite3.connect(self.board)
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, started_at INTEGER, ended_at INTEGER)")
        con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT)")
        con.executemany("INSERT INTO task_runs VALUES (?,?,?,?,?)", [
            (1, "t_x", "executor", 1000, 2000), (2, "t_x", "reviewer", 2100, 2500), (3, "t_x", "executor", 2600, 3600)])
        for run, sid, db in ((1, "s1", self.state), (2, "s2", d / "reviewer.db"), (3, "s3", self.state)):
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?,?,?,?)",
                        ("t_x", run, "owner_admitted", json.dumps({"session_id": sid, "db": str(db)})))
        con.commit()
        con.close()
        con = sqlite3.connect(self.state)
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_calls TEXT, timestamp REAL)")
        msgs = [
            ("s1", "user", None, 1001.0),
            ("s1", "assistant", [call("read_file", path=WT + "tools/a.py"), call("terminal", command=f"cd {WT} && cat Makefile && sed -n 1,9p docs/b.md")], 1010.0),
            ("s1", "assistant", [call("terminal", command="ntn api v1/pages/0711d27eef43b6cb5b84462e38dcfe63/markdown > p.json")], 1020.0),
            ("s1", "assistant", [call("patch", path=WT + "tools/a.py")], 1030.0),
            ("s3", "user", None, 2601.0),
            ("s3", "assistant", [call("kanban_show")], 2605.0),
            ("s3", "assistant", [call("read_file", path=WT2 + "tools/a.py"), call("terminal", command="git status -sb && git rev-list --count origin/main...HEAD")], 2620.0),
            ("s3", "assistant", [call("terminal", command="ntn api v1/pages/0711d27e-ef43-b6cb-5b84-462e38dcfe63/markdown > p.json; grep -n x docs/new.md")], 2640.0),
            ("s3", "assistant", [call("terminal", command="git add -A && git commit -m fix")], 2700.0),
            ("s3", "assistant", [call("read_file", path=WT2 + "Makefile")], 2710.0),
            ("s3", "assistant", None, 2720.0),
        ]
        con.executemany("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)",
                        [(s, r, json.dumps(t) if t else None, ts) for s, r, t, ts in msgs])
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_measures_the_rerun_against_the_previous_run_of_the_same_profile(self):
        r = M.measure(self.board, "t_x", 3)
        self.assertEqual(r["prev_run"], 1, "the reviewer run in between is skipped")
        self.assertEqual(r["seconds_to_first_edit"], 99, "from the session's first message (2601) to git commit (2700)")
        self.assertEqual(r["tool_calls_before_first_edit"], 4, "kanban_show, read_file, two terminal calls")
        self.assertEqual((r["run_seconds"], r["tool_calls"], r["model_calls"]), (1000, 6, 6))
        self.assertEqual(r["reads_before_first_edit"], 3, "tools/a.py, the Notion page, docs/new.md (git status is no read)")
        self.assertEqual(r["rereads_before_first_edit"], 2, "tools/a.py across worktrees and the page with or without dashes")
        self.assertEqual(r["rereads"], 3, "Makefile read after the edit counts for the whole run")
        self.assertIn("notion:0711d27eef43b6cb5b84462e38dcfe63", r["reread_paths"])

    def test_first_run_has_no_previous_run_and_the_cli_prints_the_numbers(self):
        r = M.measure(self.board, "t_x", 1)
        self.assertIsNone(r["prev_run"])
        self.assertEqual((r["seconds_to_first_edit"], r["tool_calls_before_first_edit"]), (29, 3))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            M.main(["--task", "t_x", "--run", "3", "--board-db", str(self.board), "--json"])
        self.assertEqual(json.loads(out.getvalue())["rereads"], 3)

    def test_missing_run_or_session_is_an_error_line(self):
        with self.assertRaises(SystemExit) as e:
            M.measure(self.board, "t_x", 9)
        self.assertIn("ERROR", str(e.exception))

    def test_first_edit_is_named_and_a_write_to_the_handoff_note_is_not_an_edit(self):
        self.assertEqual(M.measure(self.board, "t_x", 3)["first_edit"], "terminal git add -A && git commit -m fix")
        self.assertEqual(M.measure(self.board, "t_x", 1)["first_edit"], "patch tools/a.py")
        con = sqlite3.connect(self.state)
        con.executemany("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)", [
            ("s4", "user", None, 3701.0),
            ("s4", "assistant", json.dumps([call("patch", path="/w/ws/t_x/CONTEXT.md")]), 3710.0),
            ("s4", "assistant", json.dumps([call("write_file", path="CONTEXT.md")]), 3720.0),
            ("s4", "assistant", json.dumps([call("patch", path=WT + "tools/b.py")]), 3800.0),
            ("s4", "assistant", None, 3850.0)])
        con.commit()
        con.close()
        con = sqlite3.connect(self.board)
        con.execute("INSERT INTO task_runs VALUES (4, 't_x', 'executor', 3700, NULL)")
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES ('t_x', 4, 'owner_admitted', ?)",
                    (json.dumps({"session_id": "s4", "db": str(self.state)}),))
        con.commit()
        con.close()
        r = M.measure(self.board, "t_x", 4)
        self.assertEqual((r["seconds_to_first_edit"], r["tool_calls_before_first_edit"]), (99, 2),
                         "the two CONTEXT.md writes are skipped; the first edit is tools/b.py at 3800")
        self.assertEqual(r["first_edit"], "patch tools/b.py")
        self.assertEqual(r["prev_run"], 3)
        self.assertIsNone(r["run_seconds"], "the run has not ended")
        self.assertEqual(r["run_seconds_so_far"], 150, "started_at 3700 .. last message 3850")
        self.assertNotIn("run_seconds_so_far", M.measure(self.board, "t_x", 3), "an ended run has run_seconds only")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            M.main(["--task", "t_x", "--run", "4", "--board-db", str(self.board)])
        self.assertIn("first_edit: patch tools/b.py", out.getvalue())
        self.assertIn("run_seconds_so_far: 150", out.getvalue())

    def test_reads_of_ignores_revision_ranges_and_options(self):
        self.assertEqual(M.reads_of("terminal", {"command": "git diff origin/main...HEAD -- tools/x.py; grep -rn foo --include=*.py ."}),
                         {"tools/x.py"})
        self.assertEqual(M.reads_of("terminal", {"command": "git show origin/main:envs/a.toml"}), {"envs/a.toml"})
        self.assertEqual(M.reads_of("terminal", {"command": "ntn api v1/pages/0711d27eef43b6cb5b84462e38dcfe63 -X PATCH -d @/a.json"}), set(),
                         "a PATCH is a write, not a read")


if __name__ == "__main__":
    unittest.main()
