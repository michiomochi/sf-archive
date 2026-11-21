# Changing the pipeline: scripts, templates, prompts, skills, schema, policy

Use before editing anything the pipeline reads or runs (scripts, templates, prompts, skills, SOUL, hub template, Notion schema), when changing what an auto-creating cron makes, or when asked 'is everything in sync'. Commands also in `${repo}/CONTRIBUTING.md`.

## Procedure for ANY workflow change (without asking)
`${live}` scripts run from cron every 1-5 minutes: never patch them in place.
1. Create the Notion task row first.
2. Work on a branch `${branch_prefix}<slug>` in a worktree of the private repo ${repo_url} (ghq path ${repo}), never on main. The repo owns scripts, templates, selftest, prompts and skills; card text is `templates/*.md` (strict `$${var}` substitution).
3. Every bug fix ships with a prevention (monitor/alert/guard) AND a reproducing check in scripts/epic_selftest.py. Changed card wording: extend `epic_selftest.py` and change `scripts/tests/golden_bodies.json` by the same exact string replace, never regenerate it.
4. `${verify}`, then `make deploy-check` (stages into `${live}/staging/`, compiles with `-W error`, runs the selftest, compares the staged dry run with live) must pass. `make deploy` alone skips the tests: never use it alone.
5. Always a PR, never a push to main: open it (概要 / 背景 / 変更内容 / 検証 with commands and output), self-merge once the tests or CI pass, then `git switch main && git pull` and `make ship` (tests, tested-tree check, then deploy: backup, atomic replace, rollback on failure) from the main working tree only, one at a time. Then `make check` (no diff), `make smoke`, and confirm `last_success` in `${state_dir}/epic_gate_status.json` advances; `make cron-pause` / `make cron-resume` around it when the factory is live.
- Skills go to ${skills_dir}, prompts to the ${cron_profile} cron jobs (`make deploy` / `make sync`); never copy a skill into a profile's own skills directory (a local copy wins and diverges). A prompt change replaces the whole prompt: `hermes -p ${cron_profile} cron edit <job_id> --prompt "$$(cat prompts/x.txt)"`.

## Changing a rule or policy: sweep every holder
In the same pass: scripts (incl. `scripts/templates/*.md`, `tests/golden_bodies.json`), every `prompts/*.txt` naming the term, the skills, both SOUL.md files, the hub template and existing hub pages. Grep each for the old term before declaring done. One owner per rule: skills own rule text; the phase card only HARD RULEs; SOUL the role; prompts the job.

## Changing what an auto-creating cron creates
Pause it first (`hermes -p ${cron_profile} cron pause <id>`), dry-run and count what it WOULD create before resuming, then archive cards and reset rows made in the wrong shape. After renaming a convention (e.g. phase titles) fix old-shape rows and match exactly, else the planner re-creates duplicates. Before removing or renaming a property or status scripts read (e.g. 種別), fix every reader first (grep scripts/, prompts/, skills/).

## Drift check ('is everything pushed / in sync')
`git fetch` + `git status -sb` (repo), `make deploy-check` (live scripts), `make sync-check` (skills and prompts; exit 1, or 2 through make, on a difference). Name the newer side only after checking; offer both fixes (take live-only text into the repo, or deploy the repo over live).
