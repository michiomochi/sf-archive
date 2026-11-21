---
name: autonomous-task-execution
description: "Use when running a ${owner} task card. Notion is SSOT."
---

# Autonomous task execution (${owner} tasks)

Notion is the SSOT: タスク (${task_ds}), エピック (${epic_ds}), レビュー (${review_ds}); a task points to its epic by the relation エピック. The kanban board `${board}` is only the queue. Profiles: `executor` (every phase), `reviewer` (independent verifier). Phases: 要件定義, 設計, 実装, テスト, リリース判定; the only human approval is リリース承認. Non-negotiables and index below; read the reference the table names (at most two files per question).

## Non-negotiables
1. **Row first.** Before any work (chat requests and follow-ups too), as your first action, create or find the タスク row and work from it. If you cannot name its URL you started without one: create it now and say so. Create rows only with `python3 ${live}/create_row.py --spec <spec.json>` (references/row-lifecycle.md).
2. **Close the row in the turn that finishes the work.** With a card: finish with kanban_request_review; the gate controller sets 完了 (`finish_rows` checks the reviewer pass, the ticks and the 完了レポート via `tools/remaining_work_check.py`). No card: you set 完了 yourself. ステータス is a property: set it with its own `update_properties` call. Never tick what you did not do. レビュー中 only together with a レビュー DB row (ask_human.py).
3. **Hard fence.** An agent never creates, publishes or merges a PR, never deploys, never writes or ticks an approval, and never asks another person to approve or review (nor sends anything external, nor deletes data) unless ${owner} says so in chat. The scripts do it: the gate controller opens the draft PR from the PR body at the release ask; after the person's 承認 ship_approved.py (cron 'SF 出荷（3分）', no AI) publishes, squash-merges and deploys. A person is reached only through ask_human.py rows in the レビュー DB; never ask in chat or by kanban_block what agents can decide.
4. **Private vs public.** The three DBs and their pages are private; PRs, commits, code comments and shared chat are public: no private link, DB name or task title there.
5. **L1 work** is investigation only: no branch, commit or PR; a code change needs an L2 / L3 epic.
6. **Code and pipeline files.** Fetch with ghq, work in a worktree on `${branch_prefix}<slug>`, push the branch, never main/master. Never edit ${live} in place. The order test → deploy and the main tree are held by the Makefile (`make ship`, `tools/require_main.sh`).
7. **Safety.** Two failures with the same cause: stop and block with the cause. Never print secrets or delete Notion pages on your own initiative (cancel = ステータス キャンセル and stop its card). Volume-billed queries (BigQuery): dry_run first, stop above 10 GB. Notion and kanban tools are required: if missing or CONNECTION_REQUIRED, block with kind=capability naming the tool, never mark done.
8. **Page bodies.** Write with `ntn pages edit <page id> < file`; never pass `{color=...}` through the Notion MCP tools (skills/product-development-process/references/notion-api-notes.md).

## Duplicate check before creating anything (mandatory)
Before creating ANY task, epic, hub row, follow-up or kanban card: (1) notion_search タスク and エピック with 2-3 short keyword queries, plus `hermes kanban --board ${board} list` and open PRs/issues for code work; (2) read the top hits (notion_fetch), not titles: same goal and scope = duplicate, link to it (relation or the parent's 次のタスク) and note it in 作業メモ; overlapping scope = create and state the difference in 概要 目的; related epic = link, no new epic; (3) no record row: write a finding only in 完了レポート. Never skip it because the work looks new. Create a card only with `python3 ${live}/create_card.py --title ... --assignee <profile>` (it sets the board; test_create_card keeps every other code path from building `kanban create`).

## Value gate before creating any task (mandatory)
A task needs, in its 概要 目的: the decision its result changes, how far it could move the priority or effect estimate (else do not create it), its relation to the epic's 指標. Derived work goes to the hub's 候補 list, not scattered rows (references/epic-planner.md).

## Index: question → read (references/)
| Question | Read |
|---|---|
| Create a row (always under an epic), work or close it; completion gate (every card) | references/row-lifecycle.md |
| Code or branch work; PR 本文の下書き; private vs public | references/code-work-and-pr.md |
| A person must decide; ask_human.py; a レビュー DB row is decided | references/review-db-and-gates.md |
| Planner, 候補 items, 要件定義 light | references/epic-planner.md, references/epic-hub-db.md |
| Hub schema and statuses; converting an epic to 調査 | references/epic-hub-db.md |
| A #開発 request: task or epic? Raise an epic | references/epic-creation-from-request.md |
| Edit scripts, templates, prompts, skills, SOUL, schema; deploy; drift | references/pipeline-changes.md |
| Pipeline setup, nothing moving, cancelling; health ALERT, card stuck `ready`/`review`, monitor-mode cron | references/kanban-operations.md, references/pipeline-monitoring.md |
| Time per phase, tokens, cost | references/pipeline-timing.md |

Phases, gates, the gate controller and the four judgement criteria (L2 or L3, requirements, diff vs purpose, design vs diff): skill product-development-process.
