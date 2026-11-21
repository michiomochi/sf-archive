# hermes/

Hermes 側（cron、プロファイル）の定義。環境ごとの値は含まず、`envs/<環境>.toml` の名前で参照する。反映は `tools/factory.py`（`make` 経由）。

| ファイル | 内容 |
|---|---|
| `cron.toml` | cron ジョブ（`[jobs.*]`）と薄いラッパー（`[wrappers.*]`）の定義。間隔、監視スクリプト、スキル、推論の強さはここに固定。model / deliver / provider は `envs` の `[models]` / `[chat]` の名前で参照 |
| `wrapper.sh.tmpl` | cron の `--script` / `--monitor-script` 用の薄い `.sh` の雛形。cron はプロファイルの `scripts/` からしか解決しないので、実体（`paths.live` かリポジトリ）を呼ぶだけのラッパーを置く。実行時に設定は読まない |
| `profiles/executor/SOUL.md`、`profiles/reviewer/SOUL.md` | 実行役・レビュー役のプロファイルの SOUL（`${...}` 入りの雛形） |
| `profiles/smart_policy.txt` | `approvals.smart_policy` の文面 |

## ラッパー

`make wrappers`（`make init` に含まれる）が `wrapper.sh.tmpl` と `cron.toml` の `[wrappers.*]` から `.sh` を作り、cron プロファイルの `scripts/` に置く。自動生成なので直接編集しない。

## cron ジョブ

- 本番のジョブとの対応は名前ではなく `<paths.live>/cron_ids.json`（キー → ジョブ ID）で行う。
- `make cron-sync`: `cron.toml` と本番の差を `hermes cron edit` で直す。無いジョブは `--paused` で作る。先に `make cron-sync FLAGS=--dry-run`。`cron_ids.json` に無いキーと同じ名前のジョブが本番にあると「同じ名前のジョブ … が既にあります」で何も変えずに止まるので、案内どおり `make cron-adopt IDS="<キー>=<ID>"` で取り込んでから流し直す（下の adopt）。新しいジョブの `--script` の `.sh` は `make wrappers` が置くので、wrappers を先に流す。`make deploy` は指示文を配るジョブが本番に無いと失敗にするので、cron の定義を変えたときは cron-sync を deploy より先に流す。
- `make cron-pause` / `make cron-resume`: `cron_ids.json` のジョブだけを止める / 動かす。

## adopt（稼働中のジョブの取り込み）

すでに動いているジョブがある環境で、二重に作らないために ID を明示して取り込む。

```sh
make cron-adopt IDS="intake=<ID> planner=<ID> health=<ID> review_apply=<ID> gate=<ID> task_events=<ID> drift=<ID>"
```

キーは `cron.toml` の `[jobs.<キー>]`。台帳（jobs.json）に無い ID があると、何も書かずに中止する。その後に `make cron-sync`。
