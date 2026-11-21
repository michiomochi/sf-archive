# Code work, L1 scope, PR 本文の下書き, private vs public

Use before code or branch work, when writing the PR 本文の下書き, and before anything leaves the private DBs.

## Code work
ghq (`mise exec -- ghq get <url>`), worktree and branch `michiomochi/<short-task-slug>`; read the repo's AGENTS.md/CLAUDE.md first, run its lint and tests, push the branch, draft the PR body (the PR itself is the controller's).

An L1 row permits investigation only: no branch, commit or PR (a draft included). A needed code change needs an L2 / L3 epic; record the finding and create a follow-up 候補 or row.

## PR body (what the draft PR is opened from)
- L2: the chapter 'PR 本文' of the 判定書の草稿 (its template release_judgement_l2.md gives the shape; no page of its own). L3 and an old row: a child page of the リリース判定 row titled exactly 'PR 本文の下書き'. Shape (ship_approved checks it): first line `タイトル：<PR title>`, then exactly `## 概要`, `## 背景`, `## 変更内容`, `## 検証`, each with real content and no Notion links or DB names. If the repo has `.github/PULL_REQUEST_TEMPLATE.md`, fit its content under these four sections and delete its guidance and samples.
- At the release ask the controller (`ask_human.py --release`) opens a draft PR (base main) from it. If it cannot, no review row is made and the reason (no-branch, branch-not-pushed, no-pr-body, pr-body-shape, conflict, no-diff, gh-permission, draft-unsupported) returns as a HELD line and a 候補: fix that, the gate asks again.
- A section with nothing to write says 'なし'; claim no check you did not run. 背景 explains, self-contained, why the change became necessary now, with its source. Japanese, です・ます調; re-read and compare the headings with the four sections.

## Private vs public
Private: the three Notion DBs and everything in them (task and deliverable pages, 作業メモ, 完了レポート, hub page). Public: GitHub PRs (body, comments, commits, code comments, test names) and shared chat. In public text never link a private page, name the DBs or task titles, or write 'see the Notion task'; state facts with source, period and what was not re-verified. Chat replies to ${owner} may link private pages. Before publishing and in the reviewer's check, scan for private markers (the data source ids, 'タスク DB', 'エピック DB', 'Notionタスク') and remove them.
