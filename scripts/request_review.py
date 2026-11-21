#!/usr/bin/env python3
"""Ask a person to review a task, then set the task レビュー中: the only way to put a task in レビュー中 (never PATCH the status by hand).

usage: request_review.py <task page id or URL> --recommend R --check C [--check C2 ...] --why W [--title T] [--dry-run]
Order is fixed: the レビュー row (key `task-<32 hex of the task id>`, made by ask_human.py with the four headings, which also mentions the person in
Discord) first, the status after it. When the row cannot be made the status is not touched. A row with that key already there is not made again
("既にある"); only the status is set. --dry-run writes nothing (Notion is only read): it prints the key and what ask_human.py --dry-run prints.
A 完了 / キャンセル task is refused. Exit 0 = done, 1 = nothing changed (ERROR printed).
"""
import argparse
import contextlib
import io
import re
import sys

import ask_human as ah
import check_review_pairs as cp
import epic_common as C


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help="task page id or URL")
    ap.add_argument("--recommend", required=True, help="the one recommended decision")
    ap.add_argument("--check", required=True, action="append", help="what the person checks (repeat for more)")
    ap.add_argument("--why", required=True, help="why: the evidence")
    ap.add_argument("--title")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        m = re.findall(cp._ID, a.task, re.I)
        if not m:
            raise ValueError(f"no page id in {a.task!r}")
        tid = m[-1].replace("-", "").lower()
        page = C.ntn(f"v1/pages/{tid}")
        status, title = C.status_of(page), C.title_of(page, "タスク名").strip()
        if status in ("完了", "キャンセル"):
            raise ValueError(f"the task is {status}; not asking for a review")
        url = page.get("url") or C.page_url(tid)
        epic = ((page.get("properties", {}).get(C.TASK_EPIC_PROP) or {}).get("relation") or [{}])[0].get("id")
        key = cp.task_key(tid)
        text = "\n".join(["# 判断すること", f"タスク「{title}」の成果を承認するか、差し戻すか。", "", "# 推奨", a.recommend, "", "# 人が確かめること",
                          *[f"{i}. {c}" for i, c in enumerate(a.check, 1)], "", "# 根拠", f"[{title}]({url})", a.why])
        args = ["--key", key, "--title", a.title or title, "--text", text] + (["--epic", epic] if epic else [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ah.main(args + (["--dry-run"] if a.dry_run else []))
        if rc != 0:
            raise RuntimeError(out.getvalue().strip())
        if a.dry_run:
            print(f"KEY: {key}\n{out.getvalue()}", end="")
            return 0
        state = out.getvalue().split()[0]  # CREATED | DUP
        if status != "レビュー中":
            C.ntn(f"v1/pages/{tid}", "-X", "PATCH", data={"properties": {"ステータス": {"status": {"name": "レビュー中"}}}})
        print(f"{'既にある' if state == 'DUP' else '作成'} {key}; タスクは レビュー中")
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}"[:600])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
