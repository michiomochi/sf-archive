#!/usr/bin/env python3
"""List the タスク rows in ステータス レビュー中 that have no live レビュー row (the key `task-<32 hex of the task id>`, written by request_review.py). Read only.

usage: check_review_pairs.py
`MISSING <task id> <title> <url>`: no row with that key (nobody is being asked). `STALE …`: the key has a 承認 / 差し戻し / 取り下げ row (反映済み or not) and none
waiting, but the task is still レビュー中 (the decision was not carried into the task: 承認→完了, 差し戻し→進行中, 取り下げ→キャンセル). A row `…-r2` (a re-ask) counts for the same task,
and a レビュー待ち row anywhere in the key means asked. Phase rows are left out (the AI reviewer checks them).
Then `review pairs: N missing` (and `review pairs: M stale` when M > 0). Exit 0 = none, 1 = at least one, 2 = Notion could not be read.
For the daily standup (profile_inventory.py) and for hand use; not part of `make test` (it reads Notion). Fix: `request_review.py <task>` (never PATCH the status by hand).
"""
import re
import sys

import epic_common as C

_ID = r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}"
_KEY_RE = re.compile(rf"task-({_ID})(?:-r\d+)?", re.I)


def task_key(task_id):
    """The review row key of a task: `task-` + its id as 32 hex without hyphens."""
    return "task-" + task_id.replace("-", "").lower()


def key_task_id(key):
    """32-hex task id of a task key (hyphens, case and a `-rN` re-ask suffix do not matter); None for any other key."""
    m = _KEY_RE.fullmatch((key or "").strip())
    return m.group(1).replace("-", "").lower() if m else None


def find_pairs(tasks, reviews):
    """([(id, title, url)] MISSING = no row for the key, [...] STALE = only decided rows for it) of the レビュー中 tasks (checked here too: the query filter is not trusted alone)."""
    by_task = {}
    for r in reviews:
        pr = r.get("properties", {})
        tid = key_task_id("".join(x.get("plain_text", "") for x in (pr.get("キー") or {}).get("rich_text", [])))
        if tid:
            by_task.setdefault(tid, set()).add(C.status_of(r))
    missing, stale = [], []
    for p in tasks:
        if C.status_of(p) != "レビュー中" or C.is_phase_row(p):
            continue
        row = (p["id"], C.title_of(p, "タスク名"), p.get("url") or C.page_url(p["id"]))
        seen = by_task.get(p["id"].replace("-", "").lower(), set())
        if "レビュー待ち" in seen:
            continue
        (stale if seen & set(C.REVIEW_DECIDED) else missing).append(row)
    return missing, stale


def main(argv=None):
    try:
        tasks = C.query(C.TASK_DS, {"property": "ステータス", "status": {"equals": "レビュー中"}})
        reviews = C.query(C.REVIEW_DS, {"property": "キー", "rich_text": {"starts_with": "task-"}})
    except C.PipelineError as e:
        print(f"ERROR {e}")
        return 2
    missing, stale = find_pairs(tasks, reviews)
    for tag, rows in (("MISSING", missing), ("STALE", stale)):
        for pid, title, url in rows:
            print(f"{tag} {pid} {title} {url}")
    print(f"review pairs: {len(missing)} missing")
    if stale:
        print(f"review pairs: {len(stale)} stale")
    return 1 if missing or stale else 0


if __name__ == "__main__":
    sys.exit(main())
