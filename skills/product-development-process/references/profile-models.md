# Profiles: who does what, and which model runs it (adopted)

Provider ${provider}. Models available in this setup: claude-opus-5-5, claude-sonnet-5-5[1m], claude-haiku-5-5.

Only two pipeline profiles exist:
- executor: sonnet-5-5[1m], reasoning high. Works every phase (要件定義, 設計, 実装, テスト, リリース判定); its SOUL holds three role texts and the card's phase picks one. The 設計 phase runs on the same sonnet ([models].design = worker), so the controller pins no phase card: a card gets --model/--provider only when a phase's model (PHASE_MODEL in epic_common.py) differs from the executor's default. A card's model drives every run of the card, the review run included, so an unpinned card is reviewed on the reviewer's own opus and a pinned one on the pinned model. Pin opus by hand for a card that concludes a root cause (its review then also runs on opus).
- reviewer: opus-5-5, high (independent verification; strongest model). Weakness: both profiles are Claude family, so errors correlate with the executor's; the reviewer must re-run things instead of trusting the report.
- cron intake and monitoring jobs: sonnet-5-5, medium. Mostly-silent jobs may use haiku-5-5, low.

Set a profile with `hermes -p <profile> config set model.default <m>` and `agent.reasoning_effort <e>`; set a cron job with `hermes -p ${cron_profile} cron edit <id> --model M --provider P --reasoning-effort E`. Running workers keep the old model until restarted; verify by reading config.yaml and ~/.hermes/profiles/${cron_profile}/cron/jobs.json.

## Why one executor (measured)
Measured over 100 cards: 442 runs, pipeline cost about 3x the chat cost, one new session per card re-reading the same premises. Separate profiles per role repeat that cost, so one `executor` works an initiative end to end and only verification (`reviewer`) stays separate. Compare before and after with `epic_pipeline_timing.py` (profile column `executor`).

## One card per initiative (MERGE_PHASES)
Approved by ${owner}: the controller starts the first startable phase and attaches every later contiguous phase whose row is 未着手 and has agent processes to the same card. Key `phase-<epic>-<ph1>+<ph2>+...`; one session works the phases in order and the reviewer closes all rows in one pass. Human gates still apply: only phases the gates already allow join. Roll back by setting MERGE_PHASES = False in epic_common.py.

## Who does what (profiles)
Pipeline cards are assigned to one `executor` profile and the independent `reviewer`; there are no other profiles. The executor's SOUL carries three role texts and the card's phase picks one; splitting by profile re-reads the same premises in every session, so keep one agent working an initiative end to end and keep only verification separate.
- Role text A (phases 要件定義, テスト, リリース判定): the deliverables of levels.toml, risk items and test cases.
- Role text B (phases 設計, 実装): 設計書, task breakdown and estimates, implementation in a worktree, development tests, PR body drafts (kept as drafts: the AI creates no PR, the script opens it). Never merges, releases or deploys.
- Role text C (research and 効果検証): data investigation, definitions with sourced findings, effect check after the release.
- reviewer: independent verification of every deliverable against the template and the checklist, and gate-evidence checks. It sets 完了 only when the completion condition for that phase is met and evidenced. It re-runs things itself and does not trust the producer's claims.
