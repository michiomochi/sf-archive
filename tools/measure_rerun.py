#!/usr/bin/env python3
"""Measure how much a re-run of the same kanban card spends rebuilding its context (read-only, no LLM, stdlib only).

For one run of a card (typically the executor run after a reviewer sent the card back) and the previous run of the same
profile on the same card, it prints:
  * seconds from the run's first session message to the first edit, and the tool calls before it
    (first edit = the first write_file / patch call, or a terminal command with `git commit` or `ntn pages edit`;
    a write_file / patch of the hand-off note CONTEXT.md is not an edit, because the card text asks the run to append
    to it at every milestone and counting it would make the re-run look faster than it is). The tool and the path or
    command of the first edit are printed as `first_edit`. Writes made through a helper script (for example
    `python3 notion_tool.py ...` writing to Notion) are not detected as edits: read `first_edit` to check.
  * re-reads: distinct files/documents this run read that the previous run had read as well (whole run and before the
    first edit). A read is a read_file path, or a path argument of cat / sed -n / head / tail / grep in a terminal command.
    Paths are normalised by dropping the worktree / repository prefix, so the same repo file in two worktrees is one path.
    This is a regular-expression approximation: expect an error of a few files either way.
  * the run's wall time (task_runs.started_at .. ended_at), its tool calls and model calls (assistant messages).
    A run that has not ended yet (it can measure itself) prints run_seconds: None and run_seconds_so_far
    (started_at .. its last recorded message) instead.

The session of a run is taken from the task_events row `owner_admitted` of that run (payload: session_id, db), so no new
column is needed. Usage:
  python3 tools/measure_rerun.py --task t_29d64bb7 --run 41 [--prev-run 34] [--board-db PATH] [--json]
"""
import argparse
import json
import re
import shlex
import sqlite3
import sys
from pathlib import Path

DEFAULT_BOARD_DB = Path.home() / ".hermes" / "kanban" / "boards" / "dev" / "kanban.db"
EDIT_TOOLS = ("write_file", "patch")
EDIT_COMMAND = re.compile(r"\bgit\s+commit\b|\bntn\s+pages\s+edit\b")
HANDOFF_NOTE = "CONTEXT.md"  # the hand-off note itself; writing it is not an edit of the work
READ_TOOLS = ("read_file",)
READ_COMMANDS = ("cat", "sed", "head", "tail", "grep")
# .../<repo>/.worktrees/<name>/rest  and  ~/ghq/github.com/<owner>/<repo>/rest  ->  rest
_PREFIXES = (re.compile(r"^.*?/\.worktrees/[^/]+/"), re.compile(r"^.*?/ghq/github\.com/[^/]+/[^/]+/"),
             re.compile(r"^.*?/worktrees/[^/]+/"))
_PATHLIKE = re.compile(r"^[\w.~/-]*[\w-]\.[A-Za-z0-9]{1,6}$|^[\w.~-]*/[\w./-]+$")
_BARE_NAMES = {"Makefile", "Dockerfile", "README", "LICENSE"}
_UUID = re.compile(r"[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}", re.I)
# ntn pages get <id> / ntn api v1/pages/<id>[/markdown] / ntn api v1/blocks/<id>/children (GET, i.e. without -X)
_NTN_READ = re.compile(r"\bntn\s+(?:pages\s+get\s+|api\s+v1/(?:pages|blocks)/)([0-9a-f-]{32,36})(?![^\n;&|]*-X\s)", re.I)
NOTION_READ_TOOLS = ("notion_fetch",)
USAGE = "Measure how much a re-run of the same kanban card spends rebuilding its context (read-only)."


def ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only=1")
    return con


def normalise(path):
    p = path.strip().strip("'\"")
    for rx in _PREFIXES:
        p = rx.sub("", p, count=1)
    return p.lstrip("./") if not p.startswith("/") else p


def run_info(board_db, task, run_id):
    con = ro(board_db)
    try:
        r = con.execute("SELECT id, profile, started_at, ended_at FROM task_runs WHERE task_id=? AND id=?", (task, run_id)).fetchone()
        if not r:
            raise SystemExit(f"ERROR run {run_id} of {task} not found in {board_db}")
        ev = con.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted' "
                         "ORDER BY id LIMIT 1", (task, run_id)).fetchone()
    finally:
        con.close()
    if not ev:
        raise SystemExit(f"ERROR run {run_id} of {task} has no owner_admitted event (no session recorded)")
    p = json.loads(ev[0])
    return dict(run=r[0], profile=r[1], started=r[2], ended=r[3], session=p["session_id"], db=p["db"])


def previous_run(board_db, task, run_id, profile):
    con = ro(board_db)
    try:
        r = con.execute("SELECT MAX(id) FROM task_runs WHERE task_id=? AND id<? AND profile=?", (task, run_id, profile)).fetchone()
    finally:
        con.close()
    return r[0] if r and r[0] else None


def calls_of(state_db, session):
    """[(timestamp, tool name, arguments dict)] of the session's assistant tool calls, in order; and the model call count."""
    con = ro(state_db)
    try:
        rows = con.execute("SELECT role, tool_calls, timestamp FROM messages WHERE session_id=? ORDER BY id", (session,)).fetchall()
    finally:
        con.close()
    out, model_calls, first_ts = [], 0, rows[0][2] if rows else None
    last_ts = rows[-1][2] if rows else None
    for role, tc, ts in rows:
        if role != "assistant":
            continue
        model_calls += 1
        for c in json.loads(tc) if tc else []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            out.append((ts, (fn.get("name") or "").split("__")[-1], args if isinstance(args, dict) else {}))
    return out, model_calls, first_ts, last_ts


def is_edit(name, args):
    if name in EDIT_TOOLS:
        return Path(str(args.get("path", ""))).name != HANDOFF_NOTE
    return name == "terminal" and bool(EDIT_COMMAND.search(str(args.get("command", ""))))


def describe(name, args):
    """One line naming a tool call: the tool and its path (file tools) or its command (terminal), shortened."""
    if name in EDIT_TOOLS:
        what = normalise(str(args.get("path", "")))
    else:
        what = " ".join(str(args.get("command", "")).split())
    return f"{name} {what[:120]}".rstrip()


def _pathlike(w):
    if ".." in w or w.startswith(("origin/", "refs/")):  # revision ranges and refs, not files
        return False
    return w in _BARE_NAMES or bool(_PATHLIKE.match(w))


def reads_of(name, args):
    """Normalised paths (and notion:<page id> for Notion pages) a tool call reads (approximation)."""
    if name in READ_TOOLS:
        return {normalise(args["path"])} if args.get("path") else set()
    if name in NOTION_READ_TOOLS:
        return {f"notion:{m.replace('-', '').lower()}" for m in _UUID.findall(json.dumps(args))}
    if name != "terminal":
        return set()
    out = set()
    cmd = str(args.get("command", ""))
    out.update(f"notion:{m.replace('-', '').lower()}" for m in _NTN_READ.findall(cmd))
    for part in re.split(r"&&|\|\||;|\||\n", cmd):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if not words:
            continue
        if words[:2] == ["git", "show"] or words[:2] == ["git", "diff"]:
            for w in words[2:]:
                w = w.split(":", 1)[1] if ":" in w else w  # git show <rev>:<path>
                if w and not w.startswith("-") and _pathlike(w):
                    out.add(normalise(w))
            continue
        if words[0] not in READ_COMMANDS:
            continue
        for w in words[1:]:
            if w.startswith("-") or "=" in w or not _pathlike(w):
                continue
            out.add(normalise(w))
    return out


def measure(board_db, task, run_id, prev_id=None):
    cur = run_info(board_db, task, run_id)
    prev_id = prev_id or previous_run(board_db, task, run_id, cur["profile"])
    calls, model_calls, first_ts, last_ts = calls_of(cur["db"], cur["session"])
    edit_i = next((i for i, (_, n, a) in enumerate(calls) if is_edit(n, a)), None)
    before = calls[:edit_i] if edit_i is not None else calls
    read_all = set().union(*(reads_of(n, a) for _, n, a in calls)) if calls else set()
    read_before = set().union(*(reads_of(n, a) for _, n, a in before)) if before else set()
    res = dict(task=task, run=run_id, profile=cur["profile"], prev_run=prev_id,
               seconds_to_first_edit=round(calls[edit_i][0] - first_ts) if edit_i is not None else None,
               tool_calls_before_first_edit=len(before),
               first_edit=describe(calls[edit_i][1], calls[edit_i][2]) if edit_i is not None else None,
               run_seconds=(cur["ended"] - cur["started"]) if cur["ended"] else None,
               tool_calls=len(calls), model_calls=model_calls,
               reads=len(read_all), reads_before_first_edit=len(read_before))
    if prev_id:
        p = run_info(board_db, task, prev_id)
        pcalls = calls_of(p["db"], p["session"])[0]
        prev_read = set().union(*(reads_of(n, a) for _, n, a in pcalls)) if pcalls else set()
        res.update(rereads=len(read_all & prev_read), rereads_before_first_edit=len(read_before & prev_read),
                   reread_paths=sorted(read_all & prev_read))
    if not cur["ended"] and last_ts is not None:
        res["run_seconds_so_far"] = round(last_ts - cur["started"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=USAGE, allow_abbrev=False)
    ap.add_argument("--task", required=True)
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--prev-run", type=int)
    ap.add_argument("--board-db", default=str(DEFAULT_BOARD_DB))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = measure(a.board_db, a.task, a.run, a.prev_run)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for k, v in res.items():
            if k != "reread_paths":
                print(f"{k}: {v}")
        for pth in res.get("reread_paths", []):
            print(f"  reread: {pth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
