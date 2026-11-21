#!/usr/bin/env python3
"""List the open タスク rows that have no エピック (a task always sits under an epic). Read only; nothing is written.

usage: check_orphan_tasks.py
Open = ステータス 未着手 / 進行中 / レビュー中. Closed rows (完了 / キャンセル) without an epic are history and are never reported.
Prints `ORPHAN <page id> <ステータス> <title> <url>` per row and `orphan tasks: N`. Exit 0 = none, 1 = at least one, 2 = Notion could not be read.
For the daily review and for hand use; it is not part of `make test` (it reads Notion). Fix a row by putting it under an epic (or create the epic:
`create_row.py --level L1` makes an epic and a task together; an existing row gets its エピック relation set by hand).
"""
import sys

import epic_common as C

OPEN = ("未着手", "進行中", "レビュー中")


def find_orphans(rows):
    """[(id, status, title, url)] of the rows that are open and have an empty エピック relation (checked here too: the query filter is not trusted alone)."""
    out = []
    for p in rows:
        pr = p.get("properties", {})
        st = C.status_of(p)
        if st in OPEN and not (pr.get(C.TASK_EPIC_PROP) or {}).get("relation"):
            out.append((p["id"], st, C.title_of(p, "タスク名"), p.get("url") or f"https://www.notion.so/{p['id'].replace('-', '')}"))
    return out


def main(argv=None):
    flt = {"and": [{"or": [{"property": "ステータス", "status": {"equals": s}} for s in OPEN]},
                   {"property": C.TASK_EPIC_PROP, "relation": {"is_empty": True}}]}
    try:
        rows = C.query(C.TASK_DS, flt)
    except C.PipelineError as e:
        print(f"ERROR {e}")
        return 2
    orphans = find_orphans(rows)
    for pid, st, title, url in orphans:
        print(f"ORPHAN {pid} {st} {title} {url}")
    print(f"orphan tasks: {len(orphans)}")
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
