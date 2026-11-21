# Pipeline timing and cost

Use when the pipeline feels slow or time per phase, tokens or cost is asked. Measure first (read-only); ask before changing structure.

Data: `~/.hermes/kanban/boards/${board}/kanban.db` (`hermes kanban runs` shows 'no runs' for finished cards): `tasks` (title '[<phase>] ...', created_at, completed_at) and `task_runs` (profile, started_at, ended_at, outcome incl. rate_limited), epoch seconds. Per phase card, in minutes: first pass = first non-reviewer run; review = reviewer runs (rounds = count); rework = later non-reviewer runs; handoff gap = start of run n+1 − end of run n; start lag = first run start − created_at. State the sample size; `rate_limited` wall time is quota waiting, not work. `python3 ${live}/epic_pipeline_timing.py` prints phase × worker × model.
- Near-zero gaps and lag: the controller and dispatcher are not the bottleneck (do not shorten cron periods); the cost is the first pass, piecemeal reviewer rounds and serial phases.
- Proposals keep the phase order: a consistency self-check before review; one complete finding list per review round; a light 要件定義; parallel items inside one phase; never parallel phases (MERGE_PHASES is approved).
- Judge quality by review rounds, not time alone; label estimates (a card's `--model` drives its review runs too).
