# Row lifecycle: create the row, work the card, close the row

Use when you create a タスク row, work a card, or close a row.

## Creating the row
The row is the SSOT (scope, checklist, notes, 完了レポート); chat carries pointers. One row per separable deliverable; a follow-up extending it goes into the existing row's 結果. A row made after the work is a violation: create it now and say so.

**A task always sits under an epic** (no epic = exit 2). L1 too: `--level L1` makes the epic and the task in ONE call (`CREATED epic <id> <url> task <id> <url>`); L2 / L3 need an existing epic. **An L1 epic is closed in the same turn that closes its work row** (`finish_rows` does it with the row).

Run the Duplicate check; if the request belongs to a managed epic, add a 候補 to its hub page instead (the planner makes the task). Otherwise ONE command: write the spec JSON (kind task; title; level; epic = hub row id for L2 / L3, none for L1; overview = exactly 目的 / 完了条件 / 対象外 / 対象 = repository and branch; checklist; memo = lines for 作業メモ, card ID first if any) and run `python3 ${live}/create_row.py --spec <spec.json>` (spec in its docstring; `--dry-run`). It applies the default template (概要, やることチェックリスト, 作業メモ, 完了レポート), reads the page back to check it and prints `CREATED task <page id> <url>`. Exit 2 = spec wrong, nothing written. Exit 1 with `page=<id>` = half-filled row: fix that page in place, never create twice. Default status 未着手 hands the row to intake (a card within a minute): pass `"status": "進行中"` when you do the work yourself.

## Lifecycle of a card
1. kanban_show; the body holds the task page URL. notion_fetch it, write the card ID on the first line of 作業メモ, read 概要, checklist, 作業メモ, 完了レポート; set 進行中 if the row is 未着手 (phase rows already are).
2. Work; tick `- [x]` as items finish; every deliverable (names: levels.toml) is a child page of the task row, created first, edited in place; what levels.toml does not list (an L2 card's estimate, note, test result, PR body) is a line of 作業メモ or a chapter of the 判定書の草稿, never a page.
3. Verify (re-run commands, tests, push the branch), write the 完了レポート, then: with a card → kanban_request_review reviewer='reviewer' (summary lists what to re-verify); the reviewer passes, or kanban_request_changes (unticked box, unsupported tick, missing/empty/contradicting report); the gate controller then sets 完了. Without a card, close the row yourself:

## Closing the row (same turn that finishes the work)
A row with a live card is never closed by the executor; 'all ticked and filled but 進行中' on a card-less row is a defect.
1. Re-fetch; compare the checklist with what is proven. Tick only what is proven; an item asking a human to decide is ticked only after that decision exists, else rewrite it. An item that cannot be done: fix the cause, or remove it with a reason in 作業メモ, or set レビュー中.
2. Fill the 完了レポート (実施内容 / 結果 with numbers or branch / 確認した方法 / 未確認・残件) with deliverable links in the matching section; empty or 'なし' alone counts as unticked. **Leftover work is a row:** file it with `create_row.py` and put its URL in 未確認・残件.
3. Set the status in its own call (`update_properties`, see SKILL.md). If not done, leave it open and say in 未確認・残件 what keeps it open.
4. Re-fetch: ステータス 完了, no box unticked; only then send the final report. If the final reply is itself the deliverable, it ends the turn: put the result in 結果 and close the row first.
5. レビュー中 only for a person's decision, and only through `python3 ${live}/request_review.py <task id or URL> --recommend <one decision> --check <what to verify> --why <evidence>`: it makes the レビュー row (key `task-<id32>`, four headings, Discord mention) and then sets レビュー中; never PATCH the status by hand (`check_review_pairs.py` lists a レビュー中 row without a live review row in the daily standup). A finished AI review is 完了 or 進行中.
