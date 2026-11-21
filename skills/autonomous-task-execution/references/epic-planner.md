# Epic planner and value gate

Use when you create or judge tasks of an epic, run the planner, write a 候補, or build a 要件定義 card.

## Planner: the only writer of epic tasks
Each managed initiative is one hub row driven to its 完了条件 by the planner cron (SF プランナー; epic_planner_status.py every 5 min, wakes the agent only on change). Reviewers and direct requests never create epic tasks: they add a 候補 line to the hub (打ち手, 判断の宣言, 感度の見積もり, 開始できるフェーズ); the planner turns qualifying candidates into task rows, at most 2 per epic per run. Never override the computed guards (allow=none = manual stop 新規作成の停止).
- One task per 打ち手 and per investigation/measurement need. The five phases are one phase card and one task row per epic and phase, titled just '<フェーズ>'; the gate controller makes the rows from levels.toml (the planner makes none), starts each (→ 進行中) only when every earlier phase row is 完了 or キャンセル (レビュー中 is not finished) and no earlier phase card is live. Workers never set phase row statuses: only the reviewer sets 完了 after AI review.
- 打ち手の見直し: when settled=yes (every non-cancelled task 完了 or レビュー中), once per tasks_hash judge whether the delivered 打ち手 meet the 完了条件 (感度の見積もり vs 目標値): yes = add nothing, log '打ち手見直し: 停止' in 管理ログ; no = add 候補.
- When the 完了条件 is met with measured evidence: write the 完了レポート, check 新規作成の停止, ask for the completion confirmation (ask_human.py). A reviewer finishing a task writes next steps from 未確認・残件 as 候補 (not tasks) and sends back only for unmet checks.

## Value gate before creating any task (mandatory)
The duplicate check proves a task is new, not worth doing; avoid task creep (investigations spawning investigations while the metric-moving work stalls). A task needs, in its 概要 目的: the decision its result changes; how far it could move the priority or effect estimate (else do not create it); its relation to the epic's 指標. Write 判断の宣言 and 感度の見積もり in 概要.
- The only machine guard is 新規作成の停止, so this gate is the real filter, applied when writing each 候補. An uncertainty that changes no action goes in 未確認・残件; no documentation-only tasks.
- Fill the エピック relation on every task; no task while 新規作成の停止 is checked.

## 要件定義 phase is kept light
The card builds the 要件書 from the hub page without re-investigating; the reviewer checks required sections and consistency, not recomputation, and sends back once with the full list.
