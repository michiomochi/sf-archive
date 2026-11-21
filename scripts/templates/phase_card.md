Epic: ${epic_url}
Hub: ${hub_name}
Phase: [${ph}]. Read the earlier phases' deliverables first and build on them. Task row for this card (進行中): ${prow_url}. It is the task page (作業メモ, 完了レポート); put this card's ID on the first line of its 作業メモ; create no other private row. Do the row's やることチェックリスト items in order.

${parts}
CONTEXT FILE: CONTEXT.md in the card workspace (env HERMES_KANBAN_WORKSPACE, kept until completion). Read it first on every run (re-check only git status and rows). Create it in the first run; append at every milestone and before kanban_request_review or kanban_block, under 決めたこと, 捨てた案, 触ったファイル, 次にやること, 検証コマンドと結果 (facts and paths, not contents). Never commit it.

Follow skills autonomous-task-execution and product-development-process. Never put links or names of Notion rows (${owner} pages) into PRs, commits or code.

${parallel_note}HARD RULE: do NOT create, publish or merge a GitHub pull request and do NOT deploy yourself (the gate controller opens the draft PR; after approval ship_approved.py publishes, merges and deploys it), and do NOT ask another person to approve or review. Do NOT use delegate_task. Read each whole file in ONE call (no `sed -n a,bp` paging, no one-grep-per-turn); fetch each Notion page once, note it in CONTEXT.md. Do NOT `git reset --hard`: fix the diff. Each turn costs ~6 s: 100 turns = 10 min of waiting.

Code: 要件定義 and 設計 use no worktree and commit no code; otherwise work in a worktree on branch ${branch_prefix}<slug>; push it to origin (never master/main). Never run `make deploy` from a card: deploy runs once, from the main working tree, run by ship_approved.py after the approved squash merge (`make deploy-check` from your branch is fine).

Deliverables (${owner}'s rule): create every deliverable the checklist names as a child page of this card's task row and link it in the 完了レポート.

UI CHANGE (the branch changes a path under pages/ or src/livechat/ui/, or what the screen shows changes): in the テスト phase put before and after screenshots in the 判定書の草稿 (captions 変更前 / 変更後, at least one each) with one line 'UI 変更：あり' ('UI 変更：なし' otherwise); without them no release ask is created.

Keep anything for others (PR body, requests) as a draft. Ask ${owner} only for the gate approval, via python3 ${live}/ask_human.py (リリース承認 only); decide design, measurement, estimate and wording yourself, reason in 作業メモ.

FINISH: when the checklist and 完了レポート are done, self-check before kanban_request_review: (1) numbers and names repeated across deliverables are identical; (2) no earlier document contradicts a later one; (3) each checklist item is linked in the 完了レポート; (4) 未確認 is stated as such. Write the result as one line in 作業メモ, then kanban_request_review reviewer=reviewer (points to re-check in the comment of `hermes kanban request-review`). The approving reviewer sets the row to 完了 (AI-to-AI review passed = done); use レビュー中 only when a person must decide something that blocks the work.
