#!/usr/bin/env python3
"""List Notion michiomochiタスク pages that are 未着手 (the intake cron's monitor script).

Stable output (sorted ids only) so the cron monitor skips the agent when nothing changed. One line per page:
    <page id> <url> tenant=<epic-<epic8> | adhoc>
`tenant` is the kanban tenant the intake must pass with --tenant (epic-linked rows: epic-<last 8 hex of the hub row id>; others: adhoc).
All pages are read (paginated, page_size 100). Phase rows ("<フェーズ>" rows started by the gate controller) are never listed.

Runaway guard: when more than 5 non-phase rows created in the last 30 minutes are STILL 未着手 (rows already taken do not count, so the guard releases itself), ONE line
`ALERT runaway: ...` is printed and nothing is listed (the intake must not mass-create cards).
Exit code is non-zero (with an ERROR line) when Notion fails.
"""
import sys
import time
import traceback
from datetime import datetime, timezone

import epic_common as C

DS = C.TASK_DS
FLT = {"property": "ステータス", "status": {"equals": "未着手"}}
PHASES = C.ORDER
RUNAWAY_LIMIT = 5
RUNAWAY_WINDOW_SECONDS = 1800
RUNAWAY_ALERT = "ALERT runaway: more than 5 michiomochiタスク rows were created in the last 30 minutes; the intake lists nothing until it calms down"


def is_phase_row(p):  # "<フェーズ>" rows are started by the gate controller, never by this intake
    return C.is_phase_row(p)


def recent_filter(now):
    since = datetime.fromtimestamp(now - RUNAWAY_WINDOW_SECONDS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"timestamp": "created_time", "created_time": {"on_or_after": since}}


def runaway(recent_pages):
    """True when more than 5 still-未着手 non-phase rows were created in the window (recent_pages = rows of the window)."""
    return sum(1 for p in recent_pages if not is_phase_row(p) and _status(p) == "未着手") > RUNAWAY_LIMIT


def _status(p):  # rows already taken (進行中/完了/...) are handled work, not a runaway
    return ((p["properties"].get("ステータス") or {}).get("status") or {}).get("name", "未着手")


def tenant_of_row(p, cache):
    """kanban tenant for a row: epic-<epic8> (last 8 hex of the hub row id) when it belongs to an epic, else adhoc."""
    rel = (p["properties"].get(C.TASK_EPIC_PROP) or {}).get("relation") or []
    if not rel:
        return "adhoc"
    hub_id = rel[0]["id"]
    if hub_id not in cache:
        cache[hub_id] = C.tenant_of(C.epic_id_from_relation(hub_id))
    return cache[hub_id]


def pending_lines(pages, tenant_for=None):
    tenant_for = tenant_for or (lambda p: "adhoc")
    return [f"{p['id']} {p['url']} tenant={tenant_for(p)}" for p in sorted(pages, key=lambda x: x["id"]) if not is_phase_row(p)]


def main(now=None):
    try:
        now = int(now if now is not None else time.time())
        if runaway(C.query(DS, recent_filter(now))):
            print(RUNAWAY_ALERT)
            return 0
        pages = C.query(DS, FLT)
        cache = {}
        lines = pending_lines(pages, lambda p: tenant_of_row(p, cache))
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
