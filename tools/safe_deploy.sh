#!/bin/bash
# safe_deploy.sh - promote pipeline scripts from staging/ to the live scripts dir only after every check passed.
#
# Usage: safe_deploy.sh [--check-only] [file ...]      (files relative to staging/, default: every deployable file)
#
# Checks (all must pass, otherwise NOTHING is copied and the live files stay byte-identical):
#   1. every staged .py compiles FROM SOURCE with warnings as errors (python -W error + py_compile)
#   2. every staged template is a non-empty file
#   3. `epic_gate_controller.py --selftest` and `epic_pipeline_health.py --selftest` (offline suite, warnings as errors)
#   4. read-only dry runs: the staged controller prints the same `EPIC` lines as the live one (exit 0); the staged
#      planner/intake/health scripts run with exit 0 (their output is diffed against live for information only)
#      The two controller runs are taken at different moments, and cron writes Notion in between (the EPIC lines carry the epic and phase
#      statuses). So a mismatch is not final: both runs are repeated back to back, up to SAFE_DEPLOY_EPIC_TRIES times, and the deploy is
#      stopped only when no repetition matches. A real regression gives the same difference on every repetition; a state change does not.
# Then: timestamped backup under backup/, atomic replace (copy to temp name + mv) in dependency order, and a post-deploy
# verification (live --selftest + live dry run, compared with the pre-deploy output the same way; the repetitions run the
# pre-deploy code from a copy of live taken before the replacement). Any failure after the first replacement rolls everything back.
#
# Environment overrides (used by the tests): SAFE_DEPLOY_LIVE_DIR, SAFE_DEPLOY_STAGING_DIR, SAFE_DEPLOY_BACKUP_DIR,
# SAFE_DEPLOY_PY (default "python3": the caller puts hermes.path_prepend of the config on PATH, `make` does that), SAFE_DEPLOY_SKIP_DRYRUN=1 (no Notion access; sandbox tests only),
# SAFE_DEPLOY_GATEWAY_PY (default: the gateway's python under ~/.hermes/tools), SAFE_DEPLOY_STATE_DIR (default ~/.hermes/state; receives pipeline_deploy_ts, the health monitor's post-deploy grace marker),
# SAFE_DEPLOY_EPIC_TRIES (default 3: how many times the EPIC-line comparison is taken before it counts as a real difference; 1 = no repetition).
set -u

LIVE="${SAFE_DEPLOY_LIVE_DIR:-$HOME/.hermes/scripts/sf}"
STAGING="${SAFE_DEPLOY_STAGING_DIR:-$LIVE/staging}"
BACKUP="${SAFE_DEPLOY_BACKUP_DIR:-$LIVE/backup}"
STATE="${SAFE_DEPLOY_STATE_DIR:-$HOME/.hermes/state}"
read -r -a PY <<< "${SAFE_DEPLOY_PY:-python3}"  # nonempty: PY(未設定・空なら python3 が入る。bash 3.2 の set -u で空配列を展開すると落ちる: tools/lint_bash32.py)
export PYTHONDONTWRITEBYTECODE=1

CHECK_ONLY=0
if [ "${1:-}" = "--check-only" ]; then CHECK_ONLY=1; shift; fi

# dependency order: settings (sfconfig.py + config.toml: every script imports them) and data first, libraries, then scripts; health last (it reads the controller heartbeat)

# 本番に置かないファイル。追加するときは理由を書く(tools/test_deploy_list.py が、配布の対象が DEFAULT_FILES か NOT_DEPLOYED のどちらかにあることを検査する)。
#   profile_inventory.py skill_effect_report.py : 手元の分析用
#   tests/config.test.toml : staging の selftest 専用(上の SELFTEST_CFG)。本番の LIVE には置かない
NOT_DEPLOYED="profile_inventory.py skill_effect_report.py tests/config.test.toml"
DEFAULT_FILES="sfconfig.py levels.toml config.toml templates/phase_card.md templates/phase_card_process.md templates/phase_card_parallel_note.md templates/phase_card_merged_note.md templates/phase_card_range_note.md templates/gate_ask.md templates/release_judgement.md templates/release_judgement_l2.md templates/release_meaning.md templates/phase_row_cancel_note.md tests/golden_bodies.json tests/notion_template_task.md tests/notion_template_epic.md epic_common.py remaining_work.py tested_tree.py epic_selftest.py epic_gate_controller.py epic_gate_apply.py epic_planner_status.py notion_pending_tasks.py epic_pipeline_health.py ask_human.py create_row.py check_orphan_tasks.py check_review_pairs.py apply_review_decisions.py request_review.py create_card.py review_inbox_status.py ship_approved.py ship_selftest.py epic_pipeline_timing.py card_timing.py michiomochi_task_events.py ops.py"
if [ $# -gt 0 ]; then FILES="$*"; else FILES="$DEFAULT_FILES"; fi

say() { echo "[safe_deploy] $*"; }
die() { echo "[safe_deploy] ABORT: $*" >&2; exit 1; }

EPIC_TRIES="${SAFE_DEPLOY_EPIC_TRIES:-3}"
case "$EPIC_TRIES" in ''|*[!0-9]*|0) die "SAFE_DEPLOY_EPIC_TRIES must be a positive integer (got '$EPIC_TRIES')";; esac
# ctl_dry <dir> <stdout file> <stderr file> [python flags...]: one read-only dry run of the controller found in <dir>.
ctl_dry() { local dir="$1" out="$2" err="$3"; shift 3; ( cd "$dir" && "${PY[@]}" "$@" epic_gate_controller.py > "$out" 2> "$err" ); }

[ -d "$STAGING" ] || die "staging dir not found: $STAGING"
LOCKDIR="$STAGING/.deploy.lock"
mkdir "$LOCKDIR" 2>/dev/null || die "another deploy is running (remove $LOCKDIR if stale)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/safe_deploy.XXXXXX")"
cleanup() { rm -rf "$LOCKDIR" "$SCRATCH"; }
trap cleanup EXIT

# ---- 0. files exist
for f in $FILES; do [ -f "$STAGING/$f" ] || die "staged file missing: $f"; done

# ---- 1. compile from source, warnings as errors
for f in $FILES; do
  case "$f" in
    *.py)
      "${PY[@]}" -W error -c '
import os, py_compile, sys, tempfile
with tempfile.TemporaryDirectory() as d:
    py_compile.compile(sys.argv[1], cfile=os.path.join(d, "x.pyc"), doraise=True)
' "$STAGING/$f" 2> "$SCRATCH/compile.err" || { tail -4 "$SCRATCH/compile.err" >&2; die "compile (-W error) failed: $f"; }
      ;;
  esac
done
say "compile OK ($(echo $FILES | wc -w | tr -d ' ') files)"

# ---- 2. templates
for f in $FILES; do
  case "$f" in templates/*.md) [ -s "$STAGING/$f" ] || die "empty template: $f";; esac
done

# ---- 3. selftest of the STAGED code (offline)
# The selftest asserts the golden (test) values, so it runs on tests/config.test.toml, never on this environment's config.toml
# (Notion ids / board / ship registration differ per environment: Mac vs WSL).
SELFTEST_CFG="$STAGING/tests/config.test.toml"
[ -f "$SELFTEST_CFG" ] || SELFTEST_CFG="$STAGING/config.toml"
for s in epic_gate_controller.py epic_pipeline_health.py; do
  [ -f "$STAGING/$s" ] || continue
  ( cd "$STAGING" && SF_CONFIG="$SELFTEST_CFG" "${PY[@]}" -W error "$s" --selftest > "$SCRATCH/selftest.out" 2>&1 )
  rc=$?
  if [ $rc -ne 0 ]; then grep -v '^ok ' "$SCRATCH/selftest.out" | head -40 >&2; die "selftest failed ($s, exit $rc)"; fi
  say "selftest OK via $s: $(tail -1 "$SCRATCH/selftest.out")"
done

# cron scripts run under the gateway's own interpreter (not the PATH python): run the suite there too when it is found
GWPY="${SAFE_DEPLOY_GATEWAY_PY:-$(ls -d "$HOME"/.hermes/tools/python-3.1[4-9]*/bin/python3 2>/dev/null | head -1)}"
if [ -n "$GWPY" ] && [ -x "$GWPY" ] && [ -f "$STAGING/epic_gate_controller.py" ]; then
  ( cd "$STAGING" && SF_CONFIG="$SELFTEST_CFG" "$GWPY" -W error epic_gate_controller.py --selftest > "$SCRATCH/selftest_gw.out" 2>&1 ) \
    || { grep -v '^ok ' "$SCRATCH/selftest_gw.out" | head -40 >&2; die "selftest failed under the gateway interpreter ($GWPY)"; }
  say "selftest OK under the gateway interpreter: $(tail -1 "$SCRATCH/selftest_gw.out")"
fi

# ---- 4. dry runs (read-only)
epic_lines() { grep '^EPIC ' "$1" || true; }
if [ "${SAFE_DEPLOY_SKIP_DRYRUN:-0}" != "1" ]; then
  for f in $FILES; do
    case "$f" in
      epic_gate_controller.py)
        # Each round runs the staged controller and then the live one back to back. A mismatch of the EPIC lines may only mean that Notion
        # changed between the two runs (cron), so the round is repeated; a run that fails is never repeated (that is a fault, not a state change).
        attempt=0
        while :; do
          attempt=$((attempt + 1))
          ctl_dry "$STAGING" "$SCRATCH/new_ctl.out" "$SCRATCH/new_ctl.err" -W error \
            || { cat "$SCRATCH/new_ctl.err" "$SCRATCH/new_ctl.out" >&2; die "staged controller dry run failed"; }
          [ -f "$LIVE/epic_gate_controller.py" ] || break
          ctl_dry "$LIVE" "$SCRATCH/old_ctl.out" /dev/null || die "live controller dry run failed (not caused by this deploy; investigate first)"
          epic_lines "$SCRATCH/old_ctl.out" > "$SCRATCH/old_epic.txt"; epic_lines "$SCRATCH/new_ctl.out" > "$SCRATCH/new_epic.txt"
          diff "$SCRATCH/old_epic.txt" "$SCRATCH/new_epic.txt" > "$SCRATCH/epic.diff" && break
          if [ "$attempt" -ge "$EPIC_TRIES" ]; then
            cat "$SCRATCH/epic.diff" >&2
            die "EPIC lines differ between live and staged dry run (identical in none of $attempt attempts)"
          fi
          say "EPIC lines differ (attempt $attempt/$EPIC_TRIES); Notion may have changed between the two runs - running both again"
        done
        if [ -f "$LIVE/epic_gate_controller.py" ]; then
          [ "$attempt" -gt 1 ] && say "note: EPIC lines were identical after $attempt attempts (Notion changed between the runs)"
          # no managed epic yet (fresh environment): live and staged both print no EPIC line, which is fine. Only a staged run that lost
          # the lines the live one prints is an error (the diff above already catches that; this keeps the message explicit).
          if [ ! -s "$SCRATCH/new_epic.txt" ] && [ -s "$SCRATCH/old_epic.txt" ]; then die "staged dry run printed no EPIC lines"; fi
          say "dry run OK: $(wc -l < "$SCRATCH/new_epic.txt" | tr -d ' ') EPIC lines identical to live"
          diff "$SCRATCH/old_ctl.out" "$SCRATCH/new_ctl.out" > "$SCRATCH/other.diff" || { say "note: other output lines differ (informational):"; head -10 "$SCRATCH/other.diff"; }
        fi
        ;;
      epic_planner_status.py|notion_pending_tasks.py|epic_pipeline_health.py|review_inbox_status.py)
        ( cd "$STAGING" && "${PY[@]}" -W error "$f" > "$SCRATCH/new_$f.out" 2> "$SCRATCH/new_$f.err" ) \
          || { cat "$SCRATCH/new_$f.err" "$SCRATCH/new_$f.out" >&2; die "staged $f dry run failed"; }
        if [ -f "$LIVE/$f" ] && ( cd "$LIVE" && "${PY[@]}" "$f" > "$SCRATCH/old_$f.out" 2>/dev/null ); then
          if diff "$SCRATCH/old_$f.out" "$SCRATCH/new_$f.out" > /dev/null; then say "$f: output identical to live"; else say "note: $f output differs from live (informational)"; fi
        fi
        ;;
    esac
  done
fi

if [ $CHECK_ONLY -eq 1 ]; then say "check-only: all checks passed, nothing deployed"; exit 0; fi

# ---- 5. backup + atomic deploy (rollback on any failure)
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
DEPLOYED=""
rollback() {
  say "ROLLING BACK"
  for f in $DEPLOYED; do
    flat="$(echo "$f" | tr / _)"
    if [ -f "$BACKUP/$flat.$TS" ]; then cp -p "$BACKUP/$flat.$TS" "$LIVE/.$flat.rb.$$" && mv -f "$LIVE/.$flat.rb.$$" "$LIVE/$f"; else rm -f "$LIVE/$f"; fi
  done
  die "deploy failed and was rolled back (backups: $BACKUP/*.$TS)"
}
for f in $FILES; do
  if [ -f "$LIVE/$f" ]; then cp -p "$LIVE/$f" "$BACKUP/$(echo "$f" | tr / _).$TS" || die "backup failed: $f"; fi
done
say "backup written: $BACKUP/*.$TS"
# The comparison after the replacement may have to be repeated with the code that was live before this deploy (see below); it is gone
# from LIVE once replaced, so keep a copy. Only needed when the pre-deploy comparison ran (old_epic.txt exists).
if [ -f "$SCRATCH/old_epic.txt" ]; then
  mkdir -p "$SCRATCH/live_before" \
    && ( cd "$LIVE" && tar cf - --exclude=./staging --exclude=./backup --exclude=__pycache__ . ) | ( cd "$SCRATCH/live_before" && tar xf - ) \
    && [ -f "$SCRATCH/live_before/epic_gate_controller.py" ] || die "could not copy the live scripts for the post-deploy comparison (nothing was replaced)"
fi
for f in $FILES; do
  mkdir -p "$(dirname "$LIVE/$f")"
  tmpn="$(dirname "$LIVE/$f")/.$(basename "$f").deploying.$$"
  cp -p "$STAGING/$f" "$tmpn" || { rm -f "$tmpn"; rollback; }
  DEPLOYED="$DEPLOYED $f"
  mv -f "$tmpn" "$LIVE/$f" || rollback
done
say "replaced: $FILES"

# ---- 6. post-deploy verification of the LIVE files
if ! ( cd "$LIVE" && "${PY[@]}" -W error epic_gate_controller.py --selftest > "$SCRATCH/live_selftest.out" 2>&1 ); then
  case " $FILES " in *" epic_gate_controller.py "*|*" epic_common.py "*|*" epic_selftest.py "*) grep -v '^ok ' "$SCRATCH/live_selftest.out" | head -20 >&2; rollback;; esac
fi
if [ "${SAFE_DEPLOY_SKIP_DRYRUN:-0}" != "1" ] && [ -f "$SCRATCH/old_epic.txt" ]; then
  # old_epic.txt was taken before the replacement, so Notion may have changed since (cron). A mismatch is first re-checked with a fresh pair:
  # the pre-deploy code (copy of live) and the deployed code, run back to back. A dry run that fails is not repeated: roll back.
  post_attempt=1
  ctl_dry "$LIVE" "$SCRATCH/post_ctl.out" "$SCRATCH/post_ctl.err" -W error || { cat "$SCRATCH/post_ctl.err" >&2; rollback; }
  epic_lines "$SCRATCH/post_ctl.out" > "$SCRATCH/post_epic.txt"
  while ! diff "$SCRATCH/old_epic.txt" "$SCRATCH/post_epic.txt" > "$SCRATCH/post_epic.diff"; do
    if [ "$post_attempt" -ge "$EPIC_TRIES" ]; then cat "$SCRATCH/post_epic.diff" >&2; say "post-deploy EPIC lines differ (identical in none of $post_attempt attempts)"; rollback; fi
    say "post-deploy EPIC lines differ (attempt $post_attempt/$EPIC_TRIES); Notion may have changed since the pre-deploy run - running the pre-deploy and the deployed code again"
    post_attempt=$((post_attempt + 1))
    ctl_dry "$SCRATCH/live_before" "$SCRATCH/before_ctl.out" "$SCRATCH/before_ctl.err" || { cat "$SCRATCH/before_ctl.err" >&2; rollback; }
    ctl_dry "$LIVE" "$SCRATCH/post_ctl.out" "$SCRATCH/post_ctl.err" -W error || { cat "$SCRATCH/post_ctl.err" >&2; rollback; }
    epic_lines "$SCRATCH/before_ctl.out" > "$SCRATCH/old_epic.txt"; epic_lines "$SCRATCH/post_ctl.out" > "$SCRATCH/post_epic.txt"
  done
  [ "$post_attempt" -gt 1 ] && say "note: post-deploy EPIC lines were identical after $post_attempt attempts (Notion changed between the runs)"
  say "post-deploy dry run OK (EPIC lines unchanged)"
fi
mkdir -p "$STATE" && date +%s > "$STATE/.pipeline_deploy_ts.$$" && mv -f "$STATE/.pipeline_deploy_ts.$$" "$STATE/pipeline_deploy_ts"
say "DONE: deployed $(echo $FILES | wc -w | tr -d ' ') files; backups $BACKUP/*.$TS"
