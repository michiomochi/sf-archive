# Creating an epic from a request ("エピックをたてて対応して")

Use when a request arrives in chat #開発, or ${owner} asks for an epic to be raised and handled end to end (do not stop at a draft).

## Routing: every #開発 request goes through the factory
Work requests are never executed as chat work: the SSOT row comes first. One deliverable = a タスク row under an epic (未着手; 概要 with 目的, 完了条件, 対象外, 対象 = repository and branch); intake makes a card within a minute. Several phases, or "I want X" / "build X" = an エピック row (種別 プロダクト開発, ステータス 要件定義, a 目的 table with 目標値 and 完了条件, a 候補 checklist with at least one item naming its 打ち手, `チャット` = the requesting Discord thread/message URL, empty if unknown, never guessed). Research-only = 種別 調査. In doubt, create the epic. The agent that took the request does not do the work: it creates the row and replies once in #開発 with the row URL, the decision and what happens next. Reading-only questions get no row.

## Order
1. Read the linked message (Discord message link = channel and message id; gateway Discord tools; never print tokens); check what its number measures.
2. Duplicate check (SKILL.md); an epic with only template text is a candidate 打ち手, not a duplicate.
3. Investigate (baseline, causes, sensitivity table) before fixing target and deadline; set both as 仮置き (deadline = release date + one month of measurement).
4. Create the hub row with ONE command: write the spec JSON (kind epic; keys in the script's docstring) and run `python3 ${live}/create_row.py --spec <spec.json>` (data source ${epic_ds}). It applies the DB's default template, fills and reads back the page, checks it (the hub chapters and tables, no empty to_do, no template text left) and prints `CREATED epic <page id> <url>`. Exit 2 = spec wrong, nothing written. Exit 1 with `page=<id>` = half-filled row held by 新規作成の停止: fix that page in place, never create twice. Never build the page by hand (notion_create_pages / template_id).
5. The spec carries the whole page: 目的 (指標, 定義, 基準値, 目標値, 期限, 完了条件), 計測ログ, 候補 (each: 打ち手, 判断の宣言, 感度の見積もり, 開始できるフェーズ; only those passing the value gate), the 管理ログ line recording the request, 関連ドキュメント. Later edits: notion_update_page / `ntn`.
6. When the hub row has 目的 + a 候補 naming its 打ち手, create no タスク row for the request: the planner makes the phase rows (要件定義 as 完了). Record the request in 管理ログ and say so in the report.
