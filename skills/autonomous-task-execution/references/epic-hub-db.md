# Epic hub DB (エピック)

Use when you touch a hub row or its schema, read its statuses, or convert an epic to 調査.

Private DB, one row per managed initiative (data source ${epic_ds}, title 名前). Its page body has these chapters: 目的 (table 指標, 定義, 基準値, 目標値, 期限, 完了条件), 候補, 計測ログ, 管理ログ (state changes; one line per human decision: date, what was approved, decision, review row URL), 関連ドキュメント, 完了レポート. Tasks link to it by the エピック relation of the タスク DB (${task_ds}). One layer of epics only.

## Hub properties (only what is filtered or linked)
種別 (プロダクト開発 / 調査; only プロダクト開発 enters the phase flow; unset = out of scope), ステータス (要件定義 → 設計 → 実装 → テスト → 承認待ち → 完了 / 中止), タスク (relation), 新規作成の停止 (manual stop). The gate controller alone writes the work statuses from the earliest unfinished phase row (リリース判定 → 承認待ち) and never overwrites 完了/中止; it writes 完了 only once the review row `<epic8>-承認` is 承認, its ship is done and no phase card is live.

## Task properties (タスク)
タスク名, ステータス (未着手 / 進行中 / レビュー中 / 完了 / キャンセル), エピック (relation, required). No 種別 on tasks; no priority on tasks or epics: the card's priority is left to the kanban default. Phase rows are task rows titled with the phase name alone.

## Converting a managed product-development epic into a 調査
On the user's instruction: (1) create the task row, then read the hub, every task row of the epic and `hermes kanban --board ${board} list` (the pipeline may be phases ahead of what the user remembers); (2) set the hub 種別 to 調査; (3) set not-yet-finished phase rows キャンセル and archive the live phase card (`hermes kanban --board ${board} archive <id>`), else its reviewer keeps closing phases; leave investigation cards and finished investigation rows alone; (4) add one 管理ログ line; (5) close the task row once the 完了レポート lists leftovers in 未確認・残件.
