# 動きの見方（カード・ゲート・承認待ち・出荷）

SKILL.md の索引から: 今なにが動いているか、止まっていないか、行を作った直後に拾われたか、出荷がどこまで進んだかを実測で答えるとき。

1. カード: `hermes kanban --board ${board} ls` / `show <id>` / `tail <id>`（`tail` は追尾するので timeout 必須）。kanban のツール（`kanban_show` / `kanban_comment` / `kanban_list`）は **`board="${board}"` を毎回明示する**（省略すると `unknown task` で見つからない）。状態で絞るときは、行頭の記号を飛ばして**3列目（status）**で判定する（`... | awk '$$3 ~ /^(running|ready|review)$$/'`）。`grep review` は担当者名 `reviewer` にも当たるので使わない。
2. ゲートの鼓動: `${state_dir}/epic_gate_status.json` の last_success。10分進まなければ異常。
3. cron の実行履歴: `hermes cron runs <id>`。
4. 承認待ち（通知）: 行ができると ask_human.py が、エピックの `チャット` で決まるスレッドでメンションする。スレッド（チャンネル）の URL ならそこ、メッセージの URL ならそのメッセージに新規スレッド「承認待ち: エピック名」を作り（既にあればそのスレッド）、空なら 設定 `chat.fallback_parent` の下に同じ名前の新規スレッドを作る。後の2つはスレッドの URL を `チャット` に書き戻す。投稿に失敗しても行は残る（stderr に WARN）。承認待ち: レビュー DB を ステータス=レビュー待ち で見る。`bash ~/.hermes/profiles/${cron_profile}/scripts/sf-review-inbox.sh` は反映待ちの決定だけを出す。人の承認は完了時の「リリース承認」1つだけ（リリース判定の行が完了したとき、ゲート制御が1回だけ作る）。リリース判定より前の工程は AI レビューだけで進む。
5. 工程の行は `タスク` DB の行。題は工程名、または工程の範囲 `<最初>〜<最後>`（1行が1枚のカード）。エピックができて行が無いとき、ゲート制御が levels.toml の rows から1回だけ作る（プランナーは作らない）。
6. エピック行を作った直後の確認: `cd ${live} && python3 epic_planner_status.py | grep <エピックID末尾8桁>`。1行に `status kind tasks allow watch_hash` が出る（`allow=none` は止まっている）。**作った直後に工程の行が0件でも異常ではない** — 行とそのカードは、ゲート制御の次の1回で作られる（プランナーは行を作らない）。処理中かは `hermes cron runs <ゲート制御のID>` の `running` で見る。**工程行の有無を relation の ID 先頭8桁で判定しない。** Notion の ID は時刻順で、同時刻に作られた別エピックと先頭8桁が一致する（`413e663d-…` が並ぶ）。全 UUID で比べる。
   per-epic の `stop=`（allow が none か）と、工程の行・カードの有無を一目で見るなら **`${state_dir}/epic_health_snapshot.txt`** を読む（毎分更新。`EPIC <ID末尾8桁> <名前> stop=no` の下に工程ごとの `row=MISSING|未着手|進行中|完了 cards=-|running:<担当>|done:<担当>`）。`python3 epic_pipeline_health.py` は前回と変化が無いと `ok` しか返さないので、状態の確認には使えない。`stop=yes` のときだけ「意図的に止まっている」と判断し、行が0件なだけなら待つ。
7. 出荷の状態: `python3 ~/.hermes/scripts/sf/ship_approved.py status`（`--key <キー>` で1件）。進む順は drafted → tested → readied → merged → deployed → done。行き止まりは rejected（差し戻し）/ withdrawn（取り下げ）/ void（マージ前に崩れた。head が動いた・コンフリクト・テスト赤・チェック赤。直ったら `-r(N+1)` で出し直す）/ halted（マージ後に承認が変わった）/ deploy_failed（マージ後の失敗。main は戻さない。原因を直したら人が `ship_approved.py retry --key <キー>`）。
8. 出荷の履歴: cron『SF 出荷（3分）』の実行履歴は `hermes cron runs <出荷のID>`（ID は `~/.hermes/profiles/${cron_profile}/cron/jobs.json` で引く）。出力は状態が変わった回だけなので、出力が空の回は何も起きていない。
