#!/usr/bin/env python3
"""Create a kanban card on THIS environment's board (never on "whatever board is current").

Why: the `kanban_create` tool takes `board` as an optional argument; omitted, it uses the
current board. A card created on the wrong board is invisible to the factory's dispatcher and
to `kanban list` on the board the operator watches. This wrapper reads the board from the
environment config (`[hermes].board` in envs/<env>.toml) and always passes `--board <slug>`.

Usage:
  python3 create_card.py --title "..." [--assignee executor] [--priority 10] [--tenant epic-xxxxxxxx]
      [--body "text" | --body-file <path>|-] [--skill <name> ...] [--idempotency-key <key>]
      [--parent <task id> ...] [--board <slug>] [--json] [--dry-run]

  --board overrides the config (only when a card must go to a non-default board); the value is
  checked against ~/.hermes/kanban/boards/ and refused when it does not exist.

Exit: 0 created (prints `CREATED <id> board=<slug>`), 2 bad arguments / unknown board (nothing
created), 1 `hermes kanban create` failed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import epic_common as C  # noqa: E402

KANBAN_ROOT = Path(os.path.expanduser("~/.hermes/kanban/boards"))


def boards(root=None):
    """Slugs that exist on this machine (empty when kanban has never run here)."""
    try:
        return sorted(p.name for p in Path(root or KANBAN_ROOT).iterdir() if p.is_dir())
    except OSError:
        return []


def build_args(board, title, assignee=None, priority=None, tenant=None, body_file=None,
               skills=(), idempotency_key=None, parents=(), as_json=True, model_args=()):
    """`hermes ...` argument list: THE ONE place that builds a `kanban create` command (the gate controller uses it too; a test fails when
    another file builds one). `--board` is a global flag: it must come before the subcommand. `model_args`: `--model/--provider` flags
    (epic_common.card_model_args: none when the card runs on the executor's default model)."""
    args = ["kanban", "--board", board, "create", title]
    if assignee:
        args += ["--assignee", assignee]
    if priority is not None:
        args += ["--priority", str(priority)]
    if tenant:
        args += ["--tenant", tenant]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    for s in skills:
        args += ["--skill", s]
    for p in parents:
        args += ["--parent", p]
    if body_file:
        args += ["--body-file", body_file]
    args += list(model_args)
    if as_json:
        args.append("--json")
    return args


def parse_created(stdout):
    """Card id from `kanban create --json` output (falls back to the 'Created t_…' text)."""
    out = (stdout or "").strip()
    try:
        d, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
        if d.get("id"):
            return d["id"]
    except (ValueError, KeyError, TypeError):
        pass
    m = re.search(r"Created (t_[0-9a-f]+)", out)
    return m.group(1) if m else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create a kanban card on the configured board.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--assignee")
    ap.add_argument("--priority", type=int)
    ap.add_argument("--tenant")
    ap.add_argument("--body", help="card body text ('-' means read stdin)")
    ap.add_argument("--body-file")
    ap.add_argument("--skill", action="append", default=[])
    ap.add_argument("--idempotency-key")
    ap.add_argument("--parent", action="append", default=[])
    ap.add_argument("--board", default=None, help=f"override the configured board ({C.BOARD})")
    ap.add_argument("--json", action="store_true", help="ask hermes for JSON output")
    ap.add_argument("--dry-run", action="store_true", help="print the command, create nothing")
    a = ap.parse_args(argv)

    board = a.board or C.BOARD
    known = boards()
    if known and board not in known:
        print(f"refused: board {board!r} does not exist (have: {', '.join(known)})", file=sys.stderr)
        return 2
    if a.body and a.body_file:
        print("refused: give --body or --body-file, not both", file=sys.stderr)
        return 2

    body, body_file = None, a.body_file
    if a.body == "-":
        body, body_file = sys.stdin.read(), "-"
    elif a.body:
        body, body_file = a.body, "-"
    args = build_args(board, a.title, a.assignee, a.priority, a.tenant, body_file,
                      a.skill, a.idempotency_key, a.parent, as_json=a.json)
    if a.dry_run:
        print("hermes " + " ".join(args))
        if body:
            print("--- body ---\n" + body, file=sys.stderr)
        return 0

    r = subprocess.run([C.which_tool("hermes"), *args], input=body, text=True, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout or "")
        sys.stderr.write(r.stderr or "")
        return 1
    print(f"CREATED {parse_created(r.stdout)} board={board}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
