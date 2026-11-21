# プラグイン

本体(個人で使うフロー)に足す部品。環境の設定で有効にする。

- 有効化: `envs/<環境>.toml` に `plugins = ["<名前>"]`。値は `[plugin.<名前>]` に書く(`plugin.toml` の `[defaults]` が既定値)。
- 無効化: `plugins` から名前を外す。そのプラグインの cron・スキル・指示文・ラッパーは一切入らず、本体だけになる(個人の環境は `plugins = []`)。
- 置き場所: sf の `plugins/<名前>/`。別のリポジトリに置く場合は、環境の設定の `[plugin_sources]` に `<名前> = "<作業コピーのパス>"` と書く(直下に `plugin.toml`)。
- 構成: `plugin.toml`(説明、必須の設定、`${名前}` の対応、`[defaults]`)、`cron.toml`、`prompts/*.txt`、`skills/<スキル>/`、`scripts/`。
- cron のジョブのキーは `<プラグイン名>.<キー>`。スクリプトのラッパーの target は `repo/plugins/<名前>/scripts/...`(別リポジトリのときは `plugin/<名前>/scripts/...`)。
- 本体と同じ名前のスキルは作れない。足したいときは `skills/<スキル>/SKILL.addendum.md` を置く(本体の SKILL.md の後ろに足される)。
- 実装: `tools/sfplugins.py`(`render` / `check` / `cron sync` / `sync_live` が使う)。

## 同梱のプラグイン

- `plugins/example`: a minimal example that only shows the plugin layout. In this archive it replaces a plugin that depended on one organization's internal tools.

## 本体とプラグインの振り分け基準

- 本体: 個人でも使う。Notion とチャットだけで動き、会社固有の名前や別プロジェクトのシステム、個人の DB 名・ID を前提にしない。
- プラグイン: 会社の仕組みや用語に依存するもの、特定の人の DB・チャンネル・ID を前提にするもの。判断のしかたは「他の人の環境にそのまま置いて意味があるか」。
- 本体のスキルに会社や個人の事情を足したいときは、プラグイン側の `SKILL.addendum.md` に書く。
