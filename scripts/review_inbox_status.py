#!/usr/bin/env python3
"""Monitor script of the review-apply cron: michiomochiレビュー rows a person has decided but the flow has not applied yet.

No LLM. One stable line per row (sorted), empty output = nothing to do (the cron agent is not woken):
    DECIDED <page id> <ステータス> key=<キー> epic=<対象エピックの page id(複数はカンマ区切り、無ければ "-")> <url>
A row is DECIDED when its ステータス is 承認 / 差し戻し / 取り下げ and 反映済み is unchecked. The agent applies the decision to the flow
(skill autonomous-task-execution, 'Human decisions come through the review DB'), then checks 反映済み.
    UNREFLECTED task=<task page id> review=<row page id> 決定=<ステータス> 反映済み=<済|未> -> <what to do to the task row> <task url> <review url>
A row is UNREFLECTED when its ステータス is decided, its key is a task key (`task-<id32>`, request_review.py), the task is still レビュー中 and no row of that task is
レビュー待ち (a re-ask `…-r2`): whatever 反映済み says. The task row must follow the decision: 承認→完了, 差し戻し→進行中, 取り下げ→キャンセル. (A 反映済み tick alone silenced
the monitor while the task stayed レビュー中.) Of several decided rows of one task the newest counts. The line is the instruction: it needs no skill text.
ALERT lines: a decided row older than 30 minutes that is still unapplied or unreflected (the agent run failed or never started).
A release approval 承認 whose key has a ship record (ship_approved.json) is neither: ship_approved.py run applies it without an agent.
The full picture (waiting rows with their age) goes to ~/.hermes/state/review_inbox_snapshot.txt, never to stdout, so that adding a
new レビュー待ち row does not wake the agent. Exit code is non-zero (with an ERROR line) when Notion fails.
"""
import sys
import time
import traceback
from datetime import datetime

import check_review_pairs as cp
import epic_common as C

STALE_SECONDS = 1800
SNAPSHOT_FILE = "review_inbox_snapshot.txt"
TASK_TO = {"承認": "完了", "差し戻し": "進行中", "取り下げ": "キャンセル"}  # what a decision means for the task row


def _plain(prop):
    return "".join(x.get("plain_text", "") for x in (prop or {}).get("title", []) or (prop or {}).get("rich_text", []))


def _status(p):
    return ((p["properties"].get("ステータス") or {}).get("status") or {}).get("name", "レビュー待ち")


def _epics(p):
    """対象エピック page ids ('-' when none: a row from before the property, or one made without --epic)."""
    ids = [x.get("id", "") for x in ((p["properties"].get("対象エピック") or {}).get("relation") or [])]
    return ",".join(i for i in ids if i) or "-"


def _applied(p):
    return bool((p["properties"].get("反映済み") or {}).get("checkbox"))


def _epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _shipped_approval(p):
    """A release approval 承認 whose key has a ship record: the cron 'SF 出荷' (ship_approved.py, no AI) carries it out and ticks 反映済み itself."""
    key = _plain(p["properties"].get("キー"))
    return _status(p) == "承認" and key and C.ship_record(key) is not None


def decided_rows(pages):
    return sorted((p for p in pages if _status(p) in C.REVIEW_DECIDED and not _applied(p) and not _shipped_approval(p)), key=lambda x: x["id"])


def unreflected(pages, review_tasks):
    """[(row, task)] sorted by row id: the newest decided row of each レビュー中 task that has no レビュー待ち row (反映済み does not matter)."""
    tasks = {t["id"].replace("-", "").lower(): t for t in review_tasks if not C.is_phase_row(t)}
    rows, waiting = {}, set()
    for p in sorted(pages, key=lambda x: (x["created_time"], x["id"])):
        tid = cp.key_task_id(_plain(p["properties"].get("キー")))
        if tid in tasks and _status(p) == "レビュー待ち":
            waiting.add(tid)
        elif tid in tasks and _status(p) in C.REVIEW_DECIDED:
            rows[tid] = p
    return sorted(((p, tasks[tid]) for tid, p in rows.items() if tid not in waiting), key=lambda x: x[0]["id"])


def lines_for(pages, now, review_tasks=()):
    """`review_tasks`: the task pages in ステータス レビュー中 (no UNREFLECTED line without them)."""
    out, lost = [], unreflected(pages, review_tasks)
    for p in decided_rows(pages):
        key = _plain(p["properties"].get("キー")) or "-"
        out.append(f"DECIDED {p['id']} {_status(p)} key={key} epic={_epics(p)} {p['url']}")
    for p, t in lost:
        to = TASK_TO[_status(p)]
        out.append(f"UNREFLECTED task={t['id']} review={p['id']} 決定={_status(p)} 反映済み={'済' if _applied(p) else '未'} -> "
                   f"タスク行のステータスを「{to}」にし、レビュー行の反映結果に「タスク行を{to}にしました」と書く(タスク行は レビュー中 のまま) {t['url']} {p['url']}")
    for p in sorted({p["id"]: p for p in decided_rows(pages) + [p for p, _ in lost]}.values(), key=lambda x: x["id"]):
        if now - _epoch(p["last_edited_time"]) > STALE_SECONDS:
            out.append(f"ALERT unapplied review decision older than 30 minutes: {p['id']} {_status(p)} {_plain(p['properties'].get('名前'))[:60]}")
    return out


def snapshot_text(pages, now):
    rows = []
    for p in sorted(pages, key=lambda x: x["created_time"]):
        if _status(p) == "レビュー待ち":
            age_h = (now - _epoch(p["created_time"])) / 3600
            rows.append(f"WAITING {age_h:.1f}h epic={_epics(p)} {_plain(p['properties'].get('名前'))[:60]} {p['url']}")
    return "\n".join(rows) + ("\n" if rows else "")


def main(now=None):
    try:
        now = now if now is not None else time.time()
        pages = C.query(C.REVIEW_DS)
        out = lines_for(pages, now, C.query(C.TASK_DS, {"property": "ステータス", "status": {"equals": "レビュー中"}}))
        snap = snapshot_text(pages, now)
        path = C.state_path(SNAPSHOT_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snap, encoding="utf-8")
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        return 1
    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
