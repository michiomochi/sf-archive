#!/usr/bin/env python3
"""Carry a person's 承認 on a task review row (key `task-<32 hex>`, see request_review.py) into the task: レビュー中 -> 完了, then tick the row's 反映済み.

usage: apply_review_decisions.py [--apply]     (default: dry run, writes nothing, prints `WOULD APPLY …`)
`APPLIED <task id> <title>` per task completed. A 承認 row whose task is already 完了 only gets 反映済み ticked (silent). A task in any other status is left alone;
差し戻し / 取り下げ rows are never touched (a person decides what follows). A row with 反映済み ticked is skipped, so a decision is applied once.
Order: task first, row second: when the task write fails the row stays unticked and the next run retries. Exit 0 = ok, 1 = a write failed, 2 = Notion could not be read.
"""
import argparse
import sys

import check_review_pairs as cp
import epic_common as C


def main(argv=None):
    ap = argparse.ArgumentParser(description="Complete the tasks of approved review rows (default: dry run)", allow_abbrev=False)
    ap.add_argument("--apply", action="store_true", help="write the task status and the row's 反映済み")
    a = ap.parse_args(argv)
    try:
        rows = C.query(C.REVIEW_DS, {"and": [{"property": "ステータス", "status": {"equals": "承認"}},
                                             {"property": "キー", "rich_text": {"starts_with": "task-"}}]})
    except C.PipelineError as e:
        print(f"ERROR {e}")
        return 2
    rc = 0
    for r in rows:
        pr = r.get("properties", {})
        key = "".join(x.get("plain_text", "") for x in (pr.get("キー") or {}).get("rich_text", []))
        tid = cp.key_task_id(key)
        if C.status_of(r) != "承認" or not tid or (pr.get("反映済み") or {}).get("checkbox"):
            continue  # the query filter is not trusted alone
        try:
            task = C.ntn(f"v1/pages/{tid}")
            status, title = C.status_of(task), C.title_of(task, "タスク名").strip()
            if status not in ("レビュー中", "完了"):
                continue
            if status == "レビュー中":
                if not a.apply:
                    print(f"WOULD APPLY {task['id']} {title}")
                    continue
                C.ntn(f"v1/pages/{tid}", "-X", "PATCH", data={"properties": {"ステータス": {"status": {"name": "完了"}}}})
                print(f"APPLIED {task['id']} {title}")
            if a.apply:
                C.ntn(f"v1/pages/{r['id']}", "-X", "PATCH", data={"properties": {"反映済み": {"checkbox": True}}})
        except C.PipelineError as e:
            print(f"ERROR {tid}: {e}"[:300])
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
