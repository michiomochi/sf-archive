# 部品と ID・cron

SKILL.md の索引から: ファクトリーの部品（Notion の DB・kanban ボード・プロファイル・リポジトリ・本番・出荷・通知）と cron の一覧を引くとき。

## 部品と ID

- SSOT（Notion）: `タスク` ds `${task_ds}` / `エピック` ds `${epic_ds}` / `レビュー` ds `${review_ds}`。関連プロパティ名は `エピック`。**DB 名から michiomochi を外してある**（スクリプトの ID は不変）。
- `タスク` DB のプロパティは `エピック`(relation) / `ステータス` / `タスク名`(title) だけ。
  **`種別` はエピック側の属性で、タスクには無い**（`POST v1/pages` に送ると `is not a property that exists`）。
- 実行キュー: kanban ボード **`${board}`**（`~/.hermes/kanban/boards/${board}/kanban.db`）。ボードは default と分けている（監視の自動修復が他セッションのカードに触れないため）。
- プロファイル: `executor`（${worker_model}、high、設計の工程も同じ。工程カードには model を付けないので、レビューは reviewer 自身のモデルで動く）と `reviewer`（${reviewer_model}、high）。スキルは `${skills_dir}` だけを見る。
- リポジトリ: `${repo}`（非公開、upstream は元のリポジトリ）。
- 本番: `${live}`。薄い .sh は `~/.hermes/profiles/${cron_profile}/scripts/sf-*.sh`。
- 出荷: `ship_approved.py`（AI なし）と cron『SF 出荷（3分）』（薄い .sh は `sf-ship.sh`）。リリース承認の行が 承認 になったら、下書き PR の公開 → squash マージ → 登録された repo は本番反映（`[ship.repos."owner/repo"]`）まで進める。下書き PR は、依頼を作るとき `ask_human.py --release` が内部で `ship_approved.py prepare` を呼んで作る。AI は PR の作成・公開・マージ・デプロイをしない。
- 通知: 通常の出力は専用の宛先 `${deliver}`、失敗は #開発 の宛先 `${failure_deliver}`（Discord / Slack、カンマ区切りで複数）。人の判断は Notion のレビュー DB だけ（行の本文の章は levels.toml の review_chapters が正本。リリース承認は `ask_human.py --release` が判定書から機械的に写す。確かめるには `--dry-run`）。

## cron 8本（すべて dev）

取り込み 1分 / タスク更新通知 1分 / ゲート制御 2分 / プランナー 5分 / 監視 1分 / レビュー反映 3分 / 出荷 3分（AI なし）/ 差の確認 毎時:37。

- 監視スクリプト付きの4本は、出力のハッシュが変わったときだけ AI が起きる。出力に時刻を入れない。
- `--catch-up off`。止めるときは `hermes cron pause <id>`、再開は `resume`。ID は `~/.hermes/profiles/${cron_profile}/cron/jobs.json` で引く。
