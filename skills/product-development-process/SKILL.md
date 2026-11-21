---
name: product-development-process
description: "Use when advancing product work. Follow the 5 phases."
---

# Product development process (5 phases, 1 human approval)

Phases, fixed order: 要件定義, 設計, 実装, テスト, リリース判定 (always exactly these five rows; no 効果測定, no リリース後作業). The hub ステータス (要件定義, 設計, 実装, テスト, 承認待ち, 完了, 中止) is written by the gate controller only. The only human approval is リリース承認 (when リリース判定 is 完了); the other phases are checked by the AI reviewer only. The source of truth for order and roles is scripts/epic_common.py (ORDER, GATE_ROWS, PHASE_ROLE); this skill says how agents behave around it.

## Core rule
Do not start from a pull request. A change begins with a problem proven by real data, then an epic (`python3 ${live}/create_row.py --spec <spec.json>`), then the phases. Work is not speculative. Standing policy (${owner}): agents go through every phase up to and including リリース判定 without waiting for an approval record; never write or tick an approval yourself. Request texts stay ready-to-send drafts (child pages of the task row, linked in the 完了レポート). The hard fence is in autonomous-task-execution.

## Deliverables: task row first
Every deliverable (names per level: levels.toml) is created FIRST as a child page of the task row and edited there. Never put its body into a public place (a PR, shared chat) on your own initiative; the draft PR is the one exception. When ${owner} instructs a send, copy the content self-contained (no private link or DB name) and record the URL and date in 作業メモ. Until then the private page is the single source.

## Judgement criteria (the only rules left in this skill: they need reading, no machine decides them)
1. **L2 or L3.** Read how much of the request is decided. Scope and acceptance conditions settled and one reviewable change: L2 (one row: `${l2_rows}`). Design still open or several changes: L3 (design reviewed before code). Investigation only: L1.
2. **Requirements are clear.** 範囲 and 受け入れ条件 can be tested as written; if not, 要件定義 is not done.
3. **The diff fits the purpose.** The change does what 目的 says and meets 完了条件, nothing else.
4. **The design memo and the diff agree.** Every file and behaviour the 設計書 names is in the diff, and the diff has nothing the 設計書 does not name.

## Held by machines (not written as rules)
Rows of a new epic: `epic_gate_controller.create_epic_rows`. Row completion (reviewer pass, ticks, 完了レポート, 判定書 PROBLEMS, L1 epic): `finish_rows`. Review rounds: `enforce_review_limit` (`max_reviews`). UI before/after images: `ui_evidence_verdict`. Order test → deploy and the main tree: `make ship`, `tools/require_main.sh`. 判定書 and review-row chapters: `ask_human.py --release` (levels.toml).

- An approval is given only by a person in the レビュー DB (ステータス 承認); no record = not approved. Production deploy, the draft PR and the merge are ship_approved.py's, never a card's. The only PRs are its draft PRs (body ends `<!-- sf-ship -->`): never close those. Any other PR: references/phases-and-gates.md.

## Read when (references/)
| Step | File |
|---|---|
| start a new initiative; deliverable depth; gates in full; PR made ahead of its gate; reporting | references/phases-and-gates.md |
| how cards start; board, tenant, registry; 'why is it stopped?'; notifications | references/gate-controller.md |
| executor and reviewer roles; models per profile; one card per initiative | references/profile-models.md |
| Notion API and MCP pitfalls (markdown PATCH, templates, headings, bulk copy) | references/notion-api-notes.md |

Changing a pipeline rule (every holder), the repo workflow, and request routing (#開発): skill autonomous-task-execution, autonomous-task-execution/references/pipeline-changes.md and autonomous-task-execution/references/epic-creation-from-request.md.
