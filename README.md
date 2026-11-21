# sf

> **Archived experiment.** This is a snapshot of a personal side project, published as a reference
> for an article. It is not maintained, and issues and pull requests are not accepted.
>
> - **What it was:** a "software factory". A request written as one row in Notion went through a
>   duplicate check, became an epic with five fixed phases (requirements, design, implementation,
>   test, release decision), was executed by an AI executor and checked by an independent AI
>   reviewer. Hermes Agent cron jobs and its kanban board drove it, and plain scripts handled every
>   step that needs no judgment. A human approved only the final release.
> - **What changed for publication:** the git history was dropped, so this is a single commit.
>   Organization and repository names, Notion IDs and chat IDs were replaced with neutral
>   placeholders (`example-org`, `example-app`, generated IDs). One plugin that depended on one
>   organization's internal tools was removed and replaced by a minimal `plugins/example` that only
>   shows the plugin layout. Everything else is the code as it ran.
> - **Running it:** it needs Hermes Agent, a Notion workspace with the three databases described in
>   `notion/schema.json`, and your own values in `envs/<env>.toml` (start from
>   `factory.toml.example`). The offline test suite needs none of them:
>   `python3 tools/test_plan.py --py python3 --env-file "$PWD/envs/michiomochi-wsl.toml" --test-config "$PWD/tests/config.test.toml" --all`.
> - **License:** MIT (see [LICENSE](LICENSE)).
>
> The rest of this README is the original text, in Japanese.

依頼を Notion に1行書くだけで、AI が重複確認、カード化、工程ごとの実行、独立したレビューまで進め、人は**承認だけ**を判断する仕組み。Hermes の cron と kanban、Notion を使う。

- セットアップ: [INSTALL.md](INSTALL.md)（どの環境でもここから）
- 日々の運用: `skills/software-factory-ops/SKILL.md`
- 仕様（設計書）: [docs/design.md](docs/design.md)

## 何をする仕組みか

1. 人が Notion の「タスク」DB に依頼を1行登録する。
2. cron が取り込み、重複を確認し、エピック（案件）にして kanban のカードにする。
3. 実行役（executor）が工程ごとに作業し、別の AI（reviewer）が独立に検証する。
4. 判断が要らない部分（工程の順番、カード作成、承認の依頼）は AI を使わないスクリプトが動かす。
5. 人は、完了時にレビュー DB に届く1つの承認（リリース承認）だけを決める。要件定義〜テストは AI レビューだけで進む。

| 部品 | 実体 |
|---|---|
| SSOT（唯一の記録場所） | Notion の3つの DB（エピック / タスク / レビュー）。ID は `envs/<環境>.toml` の `[notion]` |
| 実行キュー | Hermes の kanban ボード（`[hermes].board`） |
| 実行役 | Hermes プロファイル `executor` |
| レビュー役 | Hermes プロファイル `reviewer` |
| 定期ジョブ（cron） | 一覧は `hermes/cron.toml`（本体）と `plugins/<名前>/cron.toml`（プラグイン）。AI を使うもの（取り込み、プランナー、監視、レビュー反映、判断待ちダイジェスト、朝会）と、AI を使わないもの（ゲート制御、タスク更新通知、リポジトリと本番の差の確認）がある |

通知は専用スレッド（`[chat].deliver`。Discord / Slack のどちらでも、カンマ区切りで両方でもよい）、失敗は `failure_deliver`。

## 5工程と承認1つ

工程は固定の順番: **要件定義 → 設計 → 実装 → テスト → リリース判定**。工程の行は常に5つです。エピックのステータスは、要件定義 / 設計 / 実装 / テスト / 承認待ち / 完了 / 中止の7つで、ゲート制御が書きます（全工程が終わってリリース承認待ちなら承認待ち、承認され、出荷が終われば完了。中止は人だけ）。

人の承認は完了時の1つ。**リリース承認**（リリース判定が完了したとき）だけで、要件定義・設計・実装・テストは AI レビュー（reviewer）だけで進む。承認はレビュー DB の行で人だけが書く。AI は承認を書かず、PR の作成・公開・マージとデプロイもしない。承認のあと、スクリプト `ship_approved.py`（cron『SF 出荷（3分）』）が、下書き PR の公開・squash マージ・登録された repo の本番反映までを行う（承認がその指示を兼ねる）。

## リポジトリの構成

| パス | 内容 |
|---|---|
| `envs/` | 環境ごとの設定 `<環境名>.toml`（本番の値の正本） |
| `factory.toml.example` | 新しい環境用の設定の雛形（コメント付き） |
| `scripts/` | 本番に配るスクリプト（ゲート制御、監視、取り込み）、カードのテンプレート、`sfconfig.py`（設定の読み込み）、自己テスト |
| `prompts/` | cron の指示文の雛形（`${...}` を含む） |
| `skills/` | 本体のスキル11個。工程の運用（autonomous-task-execution、product-development-process、software-factory-ops）と、個人でも使う汎用のもの（pr-review-response、project-status-synthesis、profile-standup、dotfiles-tool-management、notion-edit-safety、notion-mcp-schema-editing、notion-local-mcp-setup、chatgpt-imagegen-via-codex） |
| `plugins/` | Add-on parts for the core. `plugins/example` is a minimal example plugin that only shows the layout. Turn a plugin on with `plugins` in the env settings and off by removing its name. Details: [plugins/README.md](plugins/README.md) |
| `hermes/` | `cron.toml`（cron ジョブの定義）、`wrapper.sh.tmpl`、`profiles/`（SOUL と承認ポリシー）。詳細は [hermes/README.md](hermes/README.md) |
| `notion/` | `schema.json`（3つの DB の定義）、`templates/`（DB の本文テンプレートの正本） |
| `tools/` | `factory.py`（インストーラ、標準ライブラリのみ）、`safe_deploy.sh`、`sync_live.py`、`rename_to_sf.py`（置き場所を旧名から sf に移す1回だけの切り替え。既定は dry-run）、テスト |
| `tests/` | オフラインテスト用の設定 `config.test.toml` |
| `docs/` | `design.md`（設計書） |
| `Makefile` | 操作の入口（`make test` / `make init` / `make doctor` / `make ship` / `make deploy` など） |

プロファイル名（executor / reviewer）、5工程の順番、ゲート、cron の間隔はリポジトリに固定で、環境差ではない。

## 環境ごとに違う値

違う値は `envs/<環境名>.toml` の**1枚**だけ。Notion の ID、通知の配信先（Discord / Slack）、モデル名、パスなどが入る。新しい環境は `factory.toml.example` をコピーして `envs/<環境名>.toml` にし、値を埋める。make は `ENV=<環境名>` で環境を選ぶ（既定は `michiomochi-wsl`）。

`${...}` を含む雛形（指示文、スキル、SOUL、カードのテンプレート）は `make render` で設定の値に置換される。置換漏れや知らない名前は失敗する。

## 前提と制限

- Hermes **v0.22.6 以上**（`make doctor` の基準。cron の `--monitor-script` / `--failure-deliver` と kanban boards が要る）
- Python 3.11 以上
- `ntn`（Notion CLI）、`gh`（GitHub CLI）、`git`、`make`
- **GitHub Actions は、この org の非公開リポジトリでは分数が止まっていてワークフローが走らない**（実測: ワークフローは active だが runs は 0 件）。当面は **`make test` を手元のゲート**にする。`.github/workflows/test.yml` は残してあり、分数が戻れば動く（`workflow_dispatch` で手動実行もできる）。
- 担当プロファイルの SOUL 以外のトークンや Notion の中身は含まない。トークンは `~/.hermes/.env` から読む。

## 変更の流れ

ブランチ（`sf/<slug>`）→ PR → `make test` が通ってセルフマージ → main で `make ship`（`make test` → `make deploy`。deploy 単体はテストを回さない）→ `make check` → `make smoke`。main への直接 push と本番の直接編集はしない。詳細は [CONTRIBUTING.md](CONTRIBUTING.md)、手順のコマンドは INSTALL.md の「4 日々の変更と配布」。エピックの変更は承認後に自動で出荷される。エピックでない変更だけ、この手順で手で出荷する。

Note: sample repo names such as `dotfiles` in the docs and skills are generic examples, not references to a specific repository.
