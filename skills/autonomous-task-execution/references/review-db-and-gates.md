# Human decisions: asking, review items and the レビュー DB

Use when something needs a person or a レビュー DB row is decided.

## Review item resolution and gates
One human gate: リリース承認 (要件定義, 設計, 実装, テスト are checked by the AI reviewer only). Other external sends are NOT gates: forbidden unless ${owner} says so in chat for that one send. Only the gate, the completion confirmation and external-send items reach ${owner}.
- Review requests go in the comment of `hermes kanban request-review`; the AI reviewer decides (adopt the recommended option unless evidence contradicts it) and the executor applies follow-ups. Design, measurement, estimate and wording choices never go to a person.
- Ticked items get ' → レビュー結果（YYYY-MM-DD、reviewer）：<decision, why>'; never tick without a result or delete an item.
- The controller asks the gate (key `<epic8>-承認`) when the リリース判定 row is 完了; you only keep the deliverable (a page under the phase row, real content, linked in 関連ドキュメント) ready.
- Completion confirmation (key `<epic8>-done`) and external sends only: draft under the task row (what to approve, why, evidence links, recommended option, ready-to-send text), then `python3 ${live}/ask_human.py --key <epic8>-<slug> --epic <hub row id> --text "<Japanese ask: 何を判断するか, 根拠 (Markdown links), 推奨>"` (`<epic8>` = last 8 hex of the hub row id; once per key). Never post or mention anything yourself and never ask another way; gate items never hold a phase row.

## Human decisions come through the レビュー DB
One row per request in the レビュー DB (${review_ds}); no chat ask. ステータス: レビュー待ち, then 承認 / 差し戻し / 取り下げ; 反映済み is ticked once applied (guards against applying twice); the decision and 根拠 links are in the body.
- Asking only through ask_human.py (it mentions ${owner} in the epic's thread; the release approval is built by `--release` from the 判定書). Keys: `<epic8>-承認` (old `-リリース承認`), `<epic8>-done`, `<epic8>-send-<slug>`, rework `<key>-r2`, `-r3`.
- Only the person sets ステータス; agents never set 承認 / 差し戻し / 取り下げ or delete rows.
- Applying: cron 'SF レビュー反映' (review_inbox_status.py, no LLM, every 3 min: a DECIDED line per decided, un-反映済み row; ALERT after 30 min) wakes an agent with prompts/review_apply.txt only then. It applies the row as below, appends '反映結果(日付): ...' to its body, ticks 反映済み.
  - 承認 of the gate = publish the draft PR, squash-merge, deploy: with a ship state ship_approved.py (cron 'SF 出荷（3分）', no AI) does it and ticks 反映済み, leave the row alone. An older row without one: write one 管理ログ line (date, what was approved, decision, row URL), nothing else about the gate; if it names a number agents use afterwards (e.g. 目標値), update the 目的 table.
  - 差し戻し (PR stays a draft; never touch it): add one 候補 on the hub (打ち手 = the fix, 判断の宣言, 感度の見積もり, 開始できるフェーズ = the gate's phase) and a 管理ログ line with the reason; the planner asks again with `<key>-r<n>` once the fix is reviewed.
  - External send (`<epic8>-send-<slug>`): 承認 = ${owner}'s instruction for exactly that send as written (never PR creation, merge, release, deploy): execute under the repo rules, put the result URL in 反映結果 and the hub 管理ログ. 差し戻し: change the draft and ask again (`-r<n>`); 取り下げ: do not send.
  - Other rows (`<epic8>-done` etc.): 承認 = proceed as the row says. 取り下げ always: record ' → 取り下げ(日付)' on the hub item, nothing else.
