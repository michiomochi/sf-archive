#!/bin/bash
# Sandbox tests of safe_deploy.sh and of the selftest's ability to catch regressions (mutation checks).
# Never touches the real scripts dir: live + staging are copied into a temp sandbox (SAFE_DEPLOY_* overrides, no Notion).
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$TOOLS/.." && pwd)"
HERE="$REPO/scripts"                              # the repo's scripts/ = what `make deploy` stages
SAFE_DEPLOY="$TOOLS/safe_deploy.sh"
SB="$(mktemp -d "${TMPDIR:-/tmp}/sd_test.XXXXXX")"
trap 'rm -rf "$SB"' EXIT
pass=0; fail=0
ok()  { echo "ok   $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }

fresh() {  # new sandbox: live = the repo's current scripts (stand-in for the live dir; never the real one), staging = copy of them
  rm -rf "$SB/live" "$SB/staging" "$SB/backup" "$SB/state"; mkdir -p "$SB/live"
  for f in epic_gate_controller.py epic_gate_apply.py epic_planner_status.py notion_pending_tasks.py epic_pipeline_health.py; do cp -p "$HERE/$f" "$SB/live/"; done
  cp -R "$HERE" "$SB/staging"; cp "$TOOLS/../tests/config.test.toml" "$SB/staging/config.toml"; rm -rf "$SB/staging/__pycache__" "$SB/staging/.deploy.lock"
  # staging は repo の外にあるので、selftest のテンプレート・notion/ の検査は paths.repo の checkout を見る。
  # tests/config.test.toml の paths.repo は旧リポジトリのままなので、この repo に向ける(本番の staging は paths.repo = この repo)。
  sed -i.bak "s#^repo = .*#repo = \"$REPO\"#" "$SB/staging/config.toml" && rm -f "$SB/staging/config.toml.bak"
}
deploy() { SAFE_DEPLOY_LIVE_DIR="$SB/live" SAFE_DEPLOY_STAGING_DIR="$SB/staging" SAFE_DEPLOY_BACKUP_DIR="$SB/backup" SAFE_DEPLOY_STATE_DIR="$SB/state" SAFE_DEPLOY_SKIP_DRYRUN=1 "$SAFE_DEPLOY" "$@" > "$SB/out.txt" 2>&1; }
sums() { (cd "$SB/live" && find . -type f ! -name '*.pyc' | sort | xargs shasum); }

# 1. good deploy
fresh; before="$(sums)"
if deploy && [ -f "$SB/live/epic_common.py" ] && [ -f "$SB/live/templates/phase_card.md" ] && ls "$SB/backup"/epic_gate_controller.py.* > /dev/null 2>&1 \
   && cmp -s "$SB/staging/epic_gate_controller.py" "$SB/live/epic_gate_controller.py" && [ -s "$SB/state/pipeline_deploy_ts" ]; then ok "good deploy replaces files, writes backups and the deploy marker"; else bad "good deploy"; cat "$SB/out.txt"; fi

# 2. the classic bug: missing comma between strings (SyntaxWarning 'str' object is not callable) must block deploy
fresh; python3 - "$SB/staging/epic_gate_controller.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = s.replace("RUN_BUDGET_SECONDS = 180", 'RUN_BUDGET_SECONDS = 180\n_BAD = ("free string "\n        "second "(RUN_BUDGET_SECONDS))')
open(p, "w", encoding="utf-8").write(s)
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "compile (-W error) failed" "$SB/out.txt"; then ok "SyntaxWarning in staged file blocks deploy; live unchanged"; else bad "syntax-warning block"; cat "$SB/out.txt"; fi

# 3. template with a placeholder the code does not know ("missing ${var}") must block deploy
fresh; printf '\nExtra line with ${undefined_var}\n' >> "$SB/staging/templates/phase_card.md"
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "template with unknown \${var} blocks deploy; live unchanged"; else bad "template var block"; cat "$SB/out.txt"; fi

# 4. template with free text changed (body no longer equals the original) must block deploy
fresh; sed -i.bak 's/HARD RULE: do NOT/HARD RULE (edited): do NOT/' "$SB/staging/templates/phase_card.md" && rm -f "$SB/staging/templates/phase_card.md.bak"
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "edited card text blocks deploy (golden mismatch); live unchanged"; else bad "golden block"; cat "$SB/out.txt"; fi

# 5. mutation: wrapper ignores a failing controller
fresh; python3 - "$SB/staging/epic_gate_apply.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
assert "return 1 if failed else 0" in s
open(p, "w", encoding="utf-8").write(s.replace("return 1 if failed else 0", "return 0"))
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: wrapper swallowing failures is caught"; else bad "wrapper mutation"; cat "$SB/out.txt"; fi

# 6. mutation: private row patched before all group cards exist
fresh; python3 - "$SB/staging/epic_gate_controller.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
a = '    for g in groups:\n        if g in plan["cards"]:\n            continue\n'
assert a in s
s = s.replace(a, '    set_status(prow["id"], "進行中")\n' + a)
open(p, "w", encoding="utf-8").write(s)
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: row patched before cards exist is caught"; else bad "row-order mutation"; cat "$SB/out.txt"; fi

# 7. mutation: health stops alerting on a stale heartbeat
fresh; python3 - "$SB/staging/epic_common.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
assert "HEARTBEAT_STALE_SECONDS = 600" in s
open(p, "w", encoding="utf-8").write(s.replace("HEARTBEAT_STALE_SECONDS = 600", "HEARTBEAT_STALE_SECONDS = 600000"))
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: heartbeat threshold change is caught"; else bad "heartbeat mutation"; cat "$SB/out.txt"; fi

# 7b. mutation: レビュー中 counted as a finished phase row again (the order bug)
fresh; python3 - "$SB/staging/epic_common.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
a = 'DONE_ROW = ("完了", "キャンセル")'
assert a in s
open(p, "w", encoding="utf-8").write(s.replace(a, 'DONE_ROW = ("レビュー中", "完了", "キャンセル")'))
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: レビュー中 counted as finished is caught"; else bad "review-row mutation"; cat "$SB/out.txt"; fi

# 7c. mutation: the idempotency hit on an old done card is no longer detected
fresh; python3 - "$SB/staging/epic_gate_controller.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
a = 'if cstatus == "done":'
assert a in s
open(p, "w", encoding="utf-8").write(s.replace(a, 'if False:'))
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: undetected done-card idempotency hit is caught"; else bad "done-card mutation"; cat "$SB/out.txt"; fi

# 7d. mutation: gates asked again on every run (key check removed)
fresh; python3 - "$SB/staging/epic_gate_controller.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
a = 'if key in ctx["asked"] or C.legacy_gate_key(epic_id, gate) in ctx["asked"]:'
assert a in s
open(p, "w", encoding="utf-8").write(s.replace(a, 'if False:'))
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: gate asks without the once-per-key check are caught"; else bad "gate mutation"; cat "$SB/out.txt"; fi

# 7e. mutation: hub status mapping writes an approval-type value
fresh; python3 - "$SB/staging/epic_common.py" <<'E'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
a = '"実装": "実装", "テスト": "テスト"'
assert a in s, "mapping text"
open(p, "w", encoding="utf-8").write(s.replace(a, '"実装": "案件承認待ち", "テスト": "テスト"'))
E
before="$(sums)"
if ! deploy && [ "$(sums)" = "$before" ] && grep -q "selftest failed" "$SB/out.txt"; then ok "mutation: approval value in the hub status mapping is caught"; else bad "hub mapping mutation"; cat "$SB/out.txt"; fi

# 8. concurrent deploy lock
fresh; mkdir "$SB/staging/.deploy.lock"
if ! deploy && grep -q "another deploy is running" "$SB/out.txt"; then ok "deploy lock refuses a concurrent deploy"; else bad "lock"; fi
rmdir "$SB/staging/.deploy.lock" 2>/dev/null

# 9. EPIC 行の比較(デプロイ前・デプロイ後)。本物のコントローラの代わりに偽のコントローラを置き、dry run を有効にして流す。
#    偽のコントローラは Notion の代わりの状態ファイル($SB/notion_state)から EPIC 行を出し、STUB_FLIP_AT の番号の呼び出しの直前に
#    状態ファイルを書き換える(cron の Notion 書き込みの再現)。呼び出しの番号: 1=デプロイ前の staged、2=デプロイ前の live、
#    3=デプロイ後の live、取り直しでは続けて旧コードの写し・置き換え後の live。Notion・cron・本番の置き場には触らない。
STUB_SRC='#!/usr/bin/env python3
import os, sys
DROP = 0       # 1: 状態が同じでも EPIC 行を1行落とす(staged の退行)
LIVE_DROP = 0  # 1: live という名前のディレクトリで動くときだけ落とす(置き換え後の live の退行)
if "--selftest" in sys.argv:
    print("selftest: 0 checks, 0 failed")
    sys.exit(0)
sb = os.environ["STUB_SB"]
cf = os.path.join(sb, "calls")
n = (int(open(cf).read()) if os.path.exists(cf) else 0) + 1
open(cf, "w").write(str(n))
sf = os.path.join(sb, "notion_state")
if n in [int(x) for x in os.environ.get("STUB_FLIP_AT", "").split(",") if x]:
    cur = open(sf).read().strip()
    open(sf, "w").write("テスト" if cur == "実装" else "実装")
st = open(sf).read().strip()
lines = ["EPIC aaaaaaaa status=%s open_phases=[1]" % st, "EPIC bbbbbbbb status=設計 open_phases=[2]"]
if DROP or (LIVE_DROP and os.path.basename(os.getcwd()) == "live"):
    lines = lines[:1]
print("\n".join(lines))
print("NOROW cccccccc not an EPIC line")
'
stub_fresh() {  # $1: staged の DROP、$2: staged の LIVE_DROP(いずれも 0 か 1)。live は旧版(落とさない)
  rm -rf "$SB/live" "$SB/staging" "$SB/backup" "$SB/state" "$SB/calls"; mkdir -p "$SB/live" "$SB/staging"
  printf '%s' "$STUB_SRC" > "$SB/live/epic_gate_controller.py"
  printf '%s' "$STUB_SRC" | sed "s/^DROP = 0/DROP = $1/; s/^LIVE_DROP = 0/LIVE_DROP = $2/" > "$SB/staging/epic_gate_controller.py"
  printf '実装' > "$SB/notion_state"
}
# 引数: STUB_FLIP_AT(カンマ区切り、空なら変えない)、SAFE_DEPLOY_EPIC_TRIES(空なら既定)。dry run は有効(SAFE_DEPLOY_SKIP_DRYRUN を渡さない)。
stub_deploy() {
  local env_tries=(); [ -n "${2:-}" ] && env_tries=(SAFE_DEPLOY_EPIC_TRIES="$2")
  # bash 3.2(macOS 標準)は set -u の下で空配列の "${arr[@]}" を unbound として落とす。+展開で空なら何も渡さない。
  env STUB_SB="$SB" STUB_FLIP_AT="${1:-}" ${env_tries[@]+"${env_tries[@]}"} SAFE_DEPLOY_LIVE_DIR="$SB/live" SAFE_DEPLOY_STAGING_DIR="$SB/staging" \
      SAFE_DEPLOY_BACKUP_DIR="$SB/backup" SAFE_DEPLOY_STATE_DIR="$SB/state" "$SAFE_DEPLOY" epic_gate_controller.py > "$SB/out.txt" 2>&1
}
calls() { cat "$SB/calls" 2>/dev/null || echo 0; }

# S1 状態が変わらない: 成功し、dry run は今までと同じ3回(取り直しの分を増やさない)
stub_fresh 0 0
if stub_deploy "" && [ "$(calls)" = "3" ] && cmp -s "$SB/staging/epic_gate_controller.py" "$SB/live/epic_gate_controller.py" && grep -q "post-deploy dry run OK" "$SB/out.txt"; then
  ok "EPIC compare: no state change deploys with exactly 3 controller dry runs"; else bad "S1 no state change (calls=$(calls))"; cat "$SB/out.txt"; fi

# S2 デプロイ前の比較の途中(呼び出し2の直前)で状態が変わる: 取り直して一致し、成功する
stub_fresh 0 0
if stub_deploy "2" && [ "$(calls)" = "5" ] && cmp -s "$SB/staging/epic_gate_controller.py" "$SB/live/epic_gate_controller.py" \
   && grep -q "EPIC lines differ (attempt 1/3)" "$SB/out.txt" && grep -q "identical after 2 attempts" "$SB/out.txt"; then
  ok "EPIC compare: state change during the pre-deploy comparison is re-compared, not a failure"; else bad "S2 pre-deploy drift (calls=$(calls))"; cat "$SB/out.txt"; fi

# S3 デプロイ後の比較(呼び出し3の直前)で状態が変わる: 旧コードの写しと取り直して一致し、ロールバックしない(10:08 の再現)
stub_fresh 0 0
if stub_deploy "3" && [ "$(calls)" = "5" ] && cmp -s "$SB/staging/epic_gate_controller.py" "$SB/live/epic_gate_controller.py" \
   && grep -q "post-deploy EPIC lines differ (attempt 1/3)" "$SB/out.txt" && grep -q "post-deploy dry run OK" "$SB/out.txt" && ! grep -q "ROLLING BACK" "$SB/out.txt"; then
  ok "EPIC compare: state change after the replacement is re-compared with the pre-deploy code, no rollback"; else bad "S3 post-deploy drift (calls=$(calls))"; cat "$SB/out.txt"; fi

# S4 本物の退行(staged が同じ状態でも EPIC 行を落とす): 取り直しても同じ差が残り、置き換えの前に止まる。live は変わらない
stub_fresh 1 0; before="$(sums)"
if ! stub_deploy "" && [ "$(sums)" = "$before" ] && [ "$(calls)" = "6" ] && grep -q "EPIC lines differ between live and staged dry run (identical in none of 3 attempts)" "$SB/out.txt" \
   && ! grep -q "backup written" "$SB/out.txt"; then
  ok "EPIC compare: a staged regression (dropped EPIC line) still blocks the deploy after the re-comparisons; live unchanged"; else bad "S4 staged regression (calls=$(calls))"; cat "$SB/out.txt"; fi

# S5 置き換え後の live だけが EPIC 行を落とす: 取り直しても同じ差が残り、ロールバックして live が元に戻る
stub_fresh 0 1; before="$(sums)"
if ! stub_deploy "" && [ "$(sums)" = "$before" ] && [ "$(calls)" = "7" ] && grep -q "ROLLING BACK" "$SB/out.txt" && grep -q "post-deploy EPIC lines differ" "$SB/out.txt"; then
  ok "EPIC compare: a regression that appears only after the replacement still rolls back to the old files"; else bad "S5 post-deploy regression (calls=$(calls))"; cat "$SB/out.txt"; fi

# S6 どの呼び出しの前にも状態が変わる(比べ直しても一致を確認できない): 通さない
stub_fresh 0 0; before="$(sums)"
if ! stub_deploy "1,2,3,4,5,6,7,8" && [ "$(sums)" = "$before" ] && grep -q "none of 3 attempts" "$SB/out.txt"; then
  ok "EPIC compare: when no re-comparison ever matches, the deploy is stopped (unverifiable is not accepted)"; else bad "S6 never matches (calls=$(calls))"; cat "$SB/out.txt"; fi

# S7 対照: S3 と同じ状態変化を、取り直し無し(回数 1)で流すとロールバックする = 取り直しが救っている
stub_fresh 0 0; before="$(sums)"
if ! stub_deploy "3" "1" && [ "$(sums)" = "$before" ] && [ "$(calls)" = "3" ] && grep -q "ROLLING BACK" "$SB/out.txt"; then
  ok "EPIC compare: with SAFE_DEPLOY_EPIC_TRIES=1 the same state change rolls back (the retry is what prevents it)"; else bad "S7 control (calls=$(calls))"; cat "$SB/out.txt"; fi

echo "safe_deploy tests: $pass passed, $fail failed"
[ $fail -eq 0 ]
