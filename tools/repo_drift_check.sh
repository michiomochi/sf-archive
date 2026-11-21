#!/bin/bash
# cron(--no-agent)用。リポジトリと本番に差があるときだけ標準出力に書く(空なら通知しない)。
# 通知は cron の配信先(Discord の専用スレッド)が標準出力をそのまま送る。このスクリプトは Slack/Discord に直接は送らない。
#
# 直接 Discord に送りたいとき(cron の配信を使わない場合)だけ、通知先の Webhook URL を渡す:
#   環境変数 MICHIOMOCHI_DRIFT_WEBHOOK_URL か、第1引数。URL が無ければ標準出力だけ。
#   例: MICHIOMOCHI_DRIFT_WEBHOOK_URL=https://discord.com/api/webhooks/... repo_drift_check.sh
# 差が無いときは何も出力せず、送りもしない。常に終了コード 0(通知の有無は出力で決める)。
REPO="${MICHIOMOCHI_AI_REPO:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"  # 既定はこのスクリプトのあるリポジトリ。cron に置いたコピーからは MICHIOMOCHI_AI_REPO で指す
WEBHOOK="${1:-${MICHIOMOCHI_DRIFT_WEBHOOK_URL:-}}"
cd "$REPO" || { echo "リポジトリが見つかりません: $REPO"; exit 0; }
git fetch -q origin 2>/dev/null
msgs=""
if [ -n "$(git status --porcelain)" ]; then msgs+="ローカルに未コミットの変更があります。"$'\n'; fi
ahead=$(git rev-list --count origin/main..main 2>/dev/null); behind=$(git rev-list --count main..origin/main 2>/dev/null)
[ "${ahead:-0}" != 0 ] && msgs+="main に未pushのコミットが ${ahead} 件あります。"$'\n'
if [ "${behind:-0}" != 0 ]; then
  git merge -q --ff-only origin/main 2>/dev/null || msgs+="main が origin/main より ${behind} 件遅れています。"$'\n'
fi
out=$(python3 tools/factory.py check 2>&1); rc=$?
[ $rc != 0 ] && msgs+="$out"$'\n'
if [ -n "$msgs" ]; then
  fence='```'
  text="リポジトリと本番(~/.hermes)に反映漏れがあります。make ship か make sync で反映してください(本番側にだけある編集は先にリポジトリへ取り込む)。"$'\n'"$fence"$'\n'"$msgs"$'\n'"$fence"
  echo "$text"
  if [ -n "$WEBHOOK" ]; then
    # Discord の content は 2000 文字まで。超える分は切る。
    python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1][:1900]}, ensure_ascii=False))' "$text" \
      | curl -sS -m 20 -X POST -H 'Content-Type: application/json' --data-binary @- "$WEBHOOK" > /dev/null \
      || echo "Discord への送信に失敗しました" >&2
  fi
fi
exit 0
