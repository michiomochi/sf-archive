# Pipeline monitoring: health alerts and monitor-mode crons

Use when a health ALERT (epic_pipeline_health.py) wakes you, a card sits `ready`/`review` without running, or you change a monitor-mode cron or prompt.

## Stalled cards and row alerts
- A card `ready` or `review` for many minutes with nothing running is parked by the dispatcher's respawn guard: `SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY created_at DESC` on `~/.hermes/kanban/boards/${board}/kanban.db` shows `respawn_guarded` / `blocker_auth` every minute (a past quota wall left `tasks.last_failure_error`). Fix with one write: `UPDATE tasks SET last_failure_error=NULL WHERE id=<id> AND status IN ('ready','review')`, then confirm a `worker_bound` event within a minute; after a quota wall sweep every ready/review card.
- Row alerts: `orphan:` (進行中 idle 15 min, no live card), `closeable:` / `nearly-done:` (work over, still 進行中: set 完了 with update_properties), `review-unreflected:` (レビュー中 without a レビュー DB row: request_review.py with the task id, or 完了/進行中 if the AI review is done); each carries a growing `[stage N]`. The alert shows only the last 8 hex of the page id: get the full id by querying the task DB for 進行中 (`ntn api v1/data_sources/${task_ds}/query -X POST ...`). Resume an orphan by PATCHing its ステータス to 未着手 (`ntn api v1/pages/<full id> -X PATCH ...`), then re-check `hermes kanban --board ${board} list` and the health script.

## Monitor-mode crons
A monitor cron wakes its agent only when the output changes; the agent fixes what is its own and asks ${owner} only for policy, adding no preconditions of its own to an automation the user asked for. Count cards in `review` as live.
- An unchanged ALERT never wakes the agent twice: when leaving an age-based alert because work is still running, write one line to the row's 作業メモ (resets the age); never end with [SILENT] and 'recheck next time'.
- Editing a prompt or skill changes nothing the monitor sees (`cron run` ends as no_change). To act on existing data make the monitor output change (a new field, an unchecked 候補 line, a phase row status) and confirm in the cron output directory that a real agent run followed.
