# INSTALL

新しい環境（または作り直し）で sf（ソフトウェアファクトリー）を動かす手順。**「人がやる作業」は人が、「自動の作業」はコマンドで**行う。

用語: `<ENV>` は環境名（例 `michiomochi-wsl`）。make は `ENV=<ENV>` で環境を選ぶ（既定 `michiomochi-wsl`）。確認だけしたいときは多くのターゲットで `FLAGS=--dry-run` が使える。

## 0 前提

次が入っていること。

| もの | 確認 |
|---|---|
| Hermes v0.22.6 以上（gateway が動く） | `hermes --version` |
| Python 3.11 以上 | `python3 --version` |
| `ntn`（Notion CLI） | `ntn --help` |
| `gh`（GitHub CLI） | `gh --version` |
| `git`、`make` | `git --version && make --version` |

リポジトリを取得する（置き場所は `envs/<ENV>.toml` の `[paths].repo` と合わせる）。

```sh
gh repo clone example-org/sf ~/ghq/github.com/example-org/sf
cd ~/ghq/github.com/example-org/sf
make test        # オフラインの自己テスト。ここが通らなければ先へ進まない
```

## 1 人がやる作業

ブラウザや OAuth が要るので自動化できない。番号順に行う。

1. **`gh auth login`** を済ませ、`gh auth status` で確認する。
2. **Notion のインテグレーションを作る**: Notion の設定 → インテグレーション → 新規（内部）。権限は「コンテンツを読む / 更新 / 挿入」。発行されたトークンを `~/.hermes/.env` に `NOTION_API_TOKEN=...` として自分で書く（チャットやリポジトリに貼らない）。`ntn whoami` が通ることを確認する。
3. **親ページを作って共有する**: DB を置く Notion のページを1つ作り、そのページを 2. のインテグレーションに共有（コネクト）する。ページの ID（URL の末尾32桁）を控える。
4. **Notion MCP の OAuth**: executor / reviewer が使う。`hermes -p executor mcp login notion`、`hermes -p reviewer mcp login notion` のように各プロファイルで OAuth を完了する（プロファイルは「2 自動の作業」の `make profiles` で作られる。作成後に実行する）。確認は `hermes -p executor mcp list`、`hermes -p reviewer mcp list` に notion の enabled が出ること。
5. **通知先を決める（Discord / Slack のどちらでもよい。両方に同時に送ることもできる）**: 通常の出力用の宛先（専用のスレッドやチャンネル）と、失敗通知用の宛先を決め、設定に書く形にそろえる。
   - 形: Discord は `discord:<ID>`（スレッドなら `discord:<チャンネルID>:<スレッドID>`）、Slack は `slack:<チャンネルID>`（`C` で始まる ID。チャンネル名の `#` ではなく ID）。
   - 両方に送るときはカンマでつなぐ: `deliver = "discord:123,slack:C0123456789"`。cron は増えない（1ジョブが両方へ配る）。
   - Markdown の変換（リンク、太字、表）は Hermes の各アダプタがやる。本文は標準 Markdown のままでよい。
   - **Discord の人**: gateway の Discord bot が宛先に書ける権限を持つこと。ID はスレッド/チャンネルを右クリックして控える。
   - **Slack の人**（Slack を含めるときだけ）:
     1. <https://api.slack.com/apps> でアプリを作る（From scratch）。
     2. 「OAuth & Permissions」の Bot Token Scopes に `chat:write` を足し、ワークスペースにインストールして **Bot User OAuth Token**（`xoxb-...`）を得る。
     3. 通知先の各チャンネルで `/invite @<アプリ名>` し、bot をチャンネルに入れる（入れないと `not_in_channel` で送れない）。チャンネルの ID はチャンネルの詳細の下部で控える。
     4. cron プロファイルの `.env`（`~/.hermes/profiles/<cron_profile>/.env`。`default` なら `~/.hermes/.env`）に `SLACK_BOT_TOKEN=xoxb-...` を自分で書く（チャットやリポジトリに貼らない）。
     5. 試し送信: `hermes -p <cron_profile> send -t slack:<チャンネルID> "test"` が通ること。Discord も同様に `-t discord:<ID>` で試せる。
     6. `make doctor ENV=<ENV>` が「`.env` に SLACK_BOT_TOKEN」を OK にすること（値は表示されない。`[chat]` に `slack:` があるときだけ検査する）。
6. **環境の設定を書く**: `cp factory.toml.example envs/<ENV>.toml` を開き、`owner`、`[hermes]`、`[paths]`、`[notion].parent_page`（3. の ID）、`[chat]`（5. の宛先。古い `[discord]` の節名は読まない）、`[models]`、`[git]` を自分の値にする。`[notion]` の3つの ds は `UNSET` のままにする。キーの意味は `factory.toml.example` のコメントに書いてある。

## 2 自動の作業

以降は、ここに書いたコマンドを順に実行する。以後 `ENV=<ENV>` を付ける（既定の環境なら不要）。

```sh
make test                         # 0. 念のため
make doctor ENV=<ENV>             # 現状の不足を一覧する（この時点では足りないものが出てよい）

make notion-bootstrap ENV=<ENV>   # Notion に エピック→タスク→レビュー の3DBを作り、ds ID を envs/<ENV>.toml に書き戻して git commit
make init ENV=<ENV>               # = profiles → board → wrappers → cron-sync
                                  #   executor / reviewer、kanban ボード、cron の薄い .sh、cron ジョブ（無いものは --paused で作る）
```

ここで 1 の 4（Notion MCP の OAuth）を行う。済んだら続ける。

```sh
make deploy-check ENV=<ENV>       # staging に置き、全チェックを実行（本番は置き換えない）
make ship ENV=<ENV>               # make test → make deploy（バックアップを取って本番に置換 → スキル・指示文を反映。失敗はロールバック）。deploy 単体はテストを回さない
make check ENV=<ENV>              # 置換した雛形と本番に差が無いこと（差があれば終了コード 1）
make doctor ENV=<ENV>             # 「0 足りない」になること
make smoke ENV=<ENV>              # 読み取りだけの動作確認
make cron-resume ENV=<ENV>        # 作成時は --paused なので、ここで動かし始める
```

`make notion-bootstrap` は3つの ds がすでに存在する設定（`UNSET` 以外）だと止まる（作り直しはしない）。

### すでに動いている環境へ入れるとき

稼働中のジョブを二重に作らないよう、先に既存のジョブ ID を取り込む。

```sh
make cron-pause ENV=<ENV>
make cron-adopt IDS="intake=<ID> planner=<ID> health=<ID> review_apply=<ID> gate=<ID> task_events=<ID> drift=<ID>" ENV=<ENV>
make init ENV=<ENV>               # cron-sync は差のあるジョブを編集する
make ship ENV=<ENV>
make check ENV=<ENV> && make smoke ENV=<ENV>
make cron-resume ENV=<ENV>
```

`make cron-pause` / `cron-resume` は `<live>/cron_ids.json` に登録したジョブだけを対象にする。

## 3 動作確認

エピックを1行作り、次の3つを確認する。

1. Notion のエピック DB に1行（タイトルだけ）作る。**5分以内**に、そのエピックに工程の行（要件定義 → 設計 → 実装 → テスト → リリース判定）が付く。
2. タスク DB に依頼を1行作る。**2分以内**に kanban にカードができる（`hermes kanban --board <board> list` で確認。ボード名は `[hermes].board`）。
3. `~/.hermes/state/epic_gate_status.json` の `last_success` が進む（ゲート制御は2分ごと。`[paths].state` を変えた環境ではそのパス）。

```sh
cat ~/.hermes/state/epic_gate_status.json   # 数分あけて2回見て、last_success が新しくなること
```

3つとも満たせば完了。満たさないときは「5 困ったとき」へ。

## 4 日々の変更と配布

変更は必ず **ブランチ → PR → マージ → 本番反映** の順。main へ直接 push しない。詳細は [CONTRIBUTING.md](CONTRIBUTING.md)。

```sh
git switch -c sf/<slug>
# 変更する（本番のファイルは直接編集しない。不具合を直したら scripts/epic_selftest.py に再現テストを足す）
make test                    # 手元のゲート。GitHub Actions は今走らない（README 参照）
git push -u origin HEAD && gh pr create   # 本文は 概要 / 背景 / 変更内容 / 検証
gh pr merge                  # 自分のリポジトリなのでセルフマージでよい
git switch main && git pull
make deploy-check            # 本番に置き換えずに全チェック
make ship                    # make test → 反映（バックアップ、ロールバック付き）。make deploy はテストを回さないので単独では使わない
make check                   # 差分なし
make smoke                   # 読み取りだけの動作確認
```

- スキルと cron の指示文だけ反映し直す: `make sync`（deploy と同じく main の作業ツリーからだけ、同じロックで1本ずつ）。差の確認だけ: `make sync-check`。
- 本番側だけに入った編集は、先にリポジトリへ取り込む（`sync` は本番を上書きする）。取り込みも PR 経由。
- cron の定義（`hermes/cron.toml`）を変えたら、`make ship` より先に `make cron-sync`（先に `FLAGS=--dry-run` で確認）。`make deploy` は指示文を配るジョブが本番に無いと失敗にするため。
- 稼働中に切り替えるときは `make cron-pause` → `make cron-sync FLAGS=--dry-run`（「同じ名前のジョブ … が既にあります」で止まったら、案内どおり `make cron-adopt IDS="<キー>=<ID>"`）→ `make wrappers`（新しいジョブの `.sh` が無いと cron-sync が作れない）→ `make cron-sync` → `make ship`（または `make sync`）→ `make check` 差分なし → `make smoke` → `make cron-resume`。
- 日々の運用の見方は `skills/software-factory-ops/SKILL.md`。

## 5 困ったとき

| 症状 | 対処 |
|---|---|
| 何が足りないか分からない | `make doctor ENV=<ENV>`。足りないものを並べ、1つでもあれば終了コード 1 |
| `ntn whoami` が通らない | `~/.hermes/.env` の `NOTION_API_TOKEN`、親ページがインテグレーションに共有されているか |
| `doctor` の Notion MCP が NG | 1 の 4 をやり直す（プロファイルごと） |
| `doctor` の SLACK_BOT_TOKEN が NG | 1 の 5（Slack の人）の 4. をやり直す。cron プロファイルの `.env` にあること |
| 設定が「[chat] に改名してください」で止まる | `envs/<ENV>.toml` の `[discord]` を `[chat]` に書き換える（値はそのまま） |
| `cron_ids.json` が無い / ID が台帳に無い | `make cron-adopt IDS="..."` か `make init` |
| `make notion-bootstrap` が「既に存在」で止まる | ds を `UNSET` に戻すと再作成できるが、既存の DB は残る。意図したときだけ |
| `make check` に差が出る | 本番側の編集か、リポジトリ未反映。`make render` の出力（`build/<ENV>/`）と本番を見比べる |
| ジョブが走らない | `hermes -p <cron_profile> gateway status`（稼働中か）、`make cron-resume` |
| カードができない / 工程が進まない | 1 分の監視（SF 監視）の通知を見る。`make smoke` でゲート制御を試し実行する |
| 一時的に全部止めたい | `make cron-pause ENV=<ENV>` |
| テストが落ちる | `make test` の出力の最初の失敗から直す。警告はエラー扱い |
