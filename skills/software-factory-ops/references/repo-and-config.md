# リポジトリ構成・設定・コマンド（sf）

正本リポジトリ: `${repo}`（非公開。`upstream` は元のリポジトリ）。

## ファイル構成

| パス | 中身 |
|---|---|
| `README.md` / `INSTALL.md` | 全体像と、人がやる作業＋自動コマンド列 |
| `docs/design.md` | 設計書（5工程、完了時の承認1つ、ゲート制御の規則）の正本 |
| `envs/<環境名>.toml` | その環境の値（Notion ds、ボード、通知先、パス、モデル、branch_prefix） |
| `factory.toml.example` | 設定の全キーとコメント |
| `scripts/` | 本番に配る Python（`sfconfig.py` が設定を読む） |
| `scripts/templates/*.md` | カード本文のテンプレート（`hub_name` などの実行時変数は置換対象外） |
| `prompts/*.txt` | cron の指示文の雛形 |
| `skills/*/` | 配布するスキル（`make sync` が `~/.hermes/shared/skills` へ置換して置く） |
| `hermes/cron.toml` | cron 7本の定義（間隔・monitor・prompt・reasoning） |
| `hermes/wrapper.sh.tmpl` | 薄い `sf-*.sh` の雛形 |
| `hermes/profiles/` | executor / reviewer の SOUL と smart_policy |
| `notion/schema.json` | 3つの DB のスキーマ（`make notion-export-schema` で書き出す） |
| `notion/templates/*.md` | DB の既定テンプレート本文の正本（テンプレートの新規作成と既定の切り替えは API に無いので UI。本文の反映は `ntn pages edit`。行の作成は `scripts/create_row.py` が既定テンプレートを適用する） |
| `tools/factory.py` | インストーラ本体（render / check / cron / profiles / board / wrappers / notion-bootstrap / doctor / smoke） |
| `tools/safe_deploy.sh` | staging で検査→原子的置換→失敗でロールバック |

## make ターゲット

`test` / `ship` / `precheck` / `render` / `stage` / `deploy-check` / `deploy` / `check`（旧 sync-check）/ `sync` / `init` / `doctor` / `notion-export-schema` / `notion-bootstrap` / `profiles` / `board` / `wrappers` / `cron-adopt` / `cron-sync` / `cron-pause` / `cron-resume` / `smoke`。

- `FLAGS=--dry-run` を付けると書き込みなしで計画だけ出す。
- `make deploy` は `sync_live` も走らせる（スキルと cron の指示文まで置換して配る）。**`make deploy` はテストを回さない。単独では使わず `make ship`（test → deploy）を使う。**
- `make check` は「雛形を置換した結果」と本番を比べる。差分があれば非0（make 経由だと 2 になる）。

## 設定のキー（envs/<環境>.toml）

`owner`、`[hermes] home / cron_profile / board / path_prepend`、`[paths] repo / live / skills / state`、`[notion] parent_page / task_ds / epic_ds / review_ds`、`[chat] deliver / failure_deliver`、`[models] provider / worker / design / reviewer`、`[git] branch_prefix / repo_url`。

- スクリプトは `SF_CONFIG`（あれば）→ 無ければスクリプトの隣の `config.toml` を読む。`make deploy` が `envs/<環境>.toml` を `config.toml` として配る。
- 指示文・スキル・SOUL のプレースホルダ（ドル記号＋波かっこの名前）は `factory.py render` が `string.Template.substitute` で置換し、知らない名前や置換残りは失敗させる。
- テストは `SF_CONFIG=tests/config.test.toml`（本番と同じ値）で回す。golden はこの値に依存しているので、変えるなら golden も同じ置換で直す。

## 行の起票（scripts/create_row.py）

テンプレート適用・本文の穴埋め・読み返し検査までスクリプトがやる。手で `POST v1/pages` を組み立てない。

```
cd ${repo}
SF_CONFIG=envs/michiomochi-wsl.toml python3 scripts/create_row.py --spec spec.json --dry-run   # まず plan を見る
SF_CONFIG=envs/michiomochi-wsl.toml python3 scripts/create_row.py --spec spec.json            # CREATED epic <id> <url>
```

- spec（JSON）のキー: epic = `kind / title / type / chat / purpose / candidates / measurements / log / docs`、task = `kind / title / status / epic / overview / checklist / memo`。知らないキーはエラー（typo を黙って捨てない）。
- **候補の1項目には 打ち手・判断の宣言・感度の見積もり・開始できるフェーズ の4語を全部入れる。** 1項目でも欠けると書き込まずに `ERROR spec` で止まる（エピック本文のひな形がそう定めている）。打ち手が複数あっても、項目ごとに4要素を揃える。
- スクリプトは行を待機状態（task=進行中 / epic=新規作成の停止=on）で作り、検査を通った後で本来の状態へ戻す。作った後 `ntn api v1/pages/<id>` で ステータスと 新規作成の停止=false を確かめる。
- `create_row.py` が main に無い時期は、実装ブランチの worktree（`.worktrees/<branch>/scripts/create_row.py`）の**コミット済みコピー**を `SF_CONFIG=envs/michiomochi-wsl.toml` 付きで実行する。編集中の作業ツリーには触らない。

## パス・リポジトリ名を変えるとき

旧名は1か所ではなく、`envs/<環境>.toml` の `paths.repo` / `paths.live` / `git.repo_url`、配布済みスキル、`sf-*.sh`、`hermes/cron.toml`、docs に**文字列で**入っている。

- 範囲は先に数える: `grep -rIl <旧名> ~/.hermes/{shared,profiles/dev/scripts,scripts} <repo> --exclude-dir=.git`。ログ・バックアップ・過去セッションは数えない。
- GitHub 側の改名は `gh repo rename <新名> -R <owner>/<旧名> --yes`。出力が空でも失敗とは限らないので、`gh api repos/<owner>/<新名> --jq .full_name` と、旧 URL を叩いて新名が返る（リダイレクト）ことで確かめる。改名後すぐ `git remote set-url origin <新URL>` と `git fetch` まで済ませる。旧 URL はリダイレクトが効くので、ローカル移設を後回しにしても cron は止めずに済む。
- ローカルのディレクトリを移すと worktree の紐付けが切れる。移設後に `git worktree list` で確認し、切れていれば `git worktree repair` を各 worktree に対して実行する。
- 移設は board が空いてから。`hermes kanban --board dev ls` に running/ready が無く、かつリポジトリ配下（`.worktrees/` を含む）を cwd にしたプロセスが無いことを `readlink /proc/<pid>/cwd` で確かめる。作業中の worker の下で親ディレクトリを動かすと、そのカードは原因不明の失敗になる。
- 切り替えは1回にまとめる（手順は references/change-and-deploy.md の「変更の手順」4。cron-sync を deploy の後にしない・wrappers を cron-sync の前に流す、の順序もそこ）。途中で `paths.live` を変えると cron の実行パスも変わるので、cron を止めたままにする時間を最小にする。

## 新しい環境を立てる順（INSTALL.md の要約）

1. 人がやる: Notion インテグレーションとトークン（3プロファイルの `.env`）、親ページの共有、MCP の OAuth、通知先（Discord のスレッドか Slack のチャンネル。Slack なら Bot Token も）、`gh auth login`。
2. 自動: `make notion-bootstrap`（3DB 作成→ds ID を env に書き戻す）→ `make init`（profiles / board / wrappers / cron を `--paused` で作成）→ `make ship` → `make check` → `make doctor` → `make smoke` → `make cron-resume`。
3. 確認: エピックを1行 → ゲート制御の1回で行（levels.toml の rows）とそのカード → `~/.hermes/state/epic_gate_status.json` の last_success が進む。

## まだ残っている整理（環境側）

- ラッパー名 `michiomochi-*.sh` → `sf-*.sh`、cron ジョブ名 → 「SF …」、`paths.live` → `~/.hermes/scripts/sf`。1つずつ `make sync` / `make check` を挟んで進める。
- 旧リポジトリ `example/sample-repo` は移転先を README に書いたうえで archive する（人の承認が要る操作）。
- `~/.hermes/state/` は動かさない（`ask_human.json` を守るため）。
