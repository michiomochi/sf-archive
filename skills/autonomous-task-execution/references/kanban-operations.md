# Operating the autonomous task pipeline (Hermes kanban)

Use when (re)building or debugging the pipeline (Notion タスク → kanban → profiles), not for running a card.

Board `${board}` (DB `~/.hermes/kanban/boards/${board}/kanban.db`) is only the queue; `${cron_profile}` hosts the gateway (its dispatcher spawns a worker per ready card every 60 s; gateway down = ready cards sit forever) and the crons. No-LLM cron: `hermes -p ${cron_profile} cron create "every 1m" --name N --no-agent --script <file> --deliver ${deliver} --failure-deliver ${failure_deliver}` (stdout is posted, empty = silent; `make sync` installs the wrappers). `ntn` is not on PATH inside cron: `shutil.which('ntn') or ~/.local/bin/ntn`.

## Diagnosing 'nothing is moving' (read-only, in order)
1. `hermes -p ${cron_profile} cron list`: last status ok, next run on time.
2. `python3 ${live}/epic_gate_controller.py` (dry run): each managed epic and the phases it opens; `--assume-status=<status>` shows what the next gate would create.
3. List the epic's task rows that are not 未着手. A row set 進行中 by hand without a card is never picked up; human-only work never gets cards. An idle controller with only human work left is correct: report the real blocker (the person's gate or input).
4. `hermes kanban --board ${board} list` for running cards and heartbeats; a card stuck `ready`/`review` or an ALERT line is handled in the monitoring reference (SKILL.md index).

## Pitfalls
- Cloned profiles do NOT inherit MCP OAuth tokens (`<HERMES_HOME>/mcp-tokens/`): workers get CONNECTION_REQUIRED. Symlink each profile's `mcp-tokens` to the ${cron_profile} profile's.
- Cancelling: set the row キャンセル AND stop its card, else the worker keeps rewriting the page: `hermes kanban --board ${board} archive <id> [<id> ...]` (`reclaim` takes ONE id); re-run `list` and re-query the DB afterwards. 進行中 with a done card is finished work not yet closed: do not cancel it.
- Another session may hold `staging/.deploy.lock`: wait, never delete it.
