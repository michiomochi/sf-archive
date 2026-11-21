# 進め方の規律（カードを走らせる・止める・割る）

SKILL.md の索引から: 走行中のカードに方針変更を伝えるとき、固まったカードを疑うとき、タイムアウト・STALL・カード本文の禁止事項・エピックの大きさ・L1/L2/L3 の入れ方で迷ったとき。
（所要時間の測り方と実測の数字は references/card-timing.md。）

- **スキル・指示文の正はリポジトリ。** live（`~/.hermes/shared/skills`）を直接編集すると、次の `make deploy` / `make sync` で消える（この節を live にだけ書いて、自分で流した deploy で消した）。追記は repo → PR → マージ → sync。
- **heartbeat が進む＝生きている、ではない。** モデル呼び出しが返らないと heartbeat だけ毎分進む。判定は workspace の最終書き込み（`ls -lt ~/.hermes/kanban/boards/dev/workspaces/<card>/`）。固まっていたら `hermes kanban --board dev reclaim <card>` で再キューする（`CONTEXT.md` と作業ファイルは残る）。
- **カード本文で禁じていること**（`templates/phase_card.md` の HARD RULE。golden 27件も同じ置換で揃える）: `delegate_task` を使わない（並列化はファクトリーがカードを分ける）／要件定義・設計で worktree を作らずコードもコミットしない／読み取りは1回にまとめる（1ターン中央値6秒・CONTEXT.md の読み直し禁止）。
- **モデル呼び出しのタイムアウトは 420秒 / stale 180秒**（`providers.<id>.request_timeout_seconds` / `stale_timeout_seconds`）。既定は 1800秒（30分）で、返らない呼び出しを30分待つ。
- **STALL の検知は 3分（警告）/ 6分（打ち切り）。** 2回続いたら人に返す。
- **エピックは「半日以内に終わる大きさ」に割る。** 25ファイル規模は要件定義だけで83分かかった。設計が2枚になった時点で、最初に出す半分に切る（後回しにすると走行中のカードを止めて切り直すことになる）。
- **L1/L2/L3 と「行＝カード」は1本のブランチで入れる。** 片方だけだと取り込み（`notion_pending_tasks.py`）が範囲の行を工程の行と誤認して2枚目のカードができる。設計はエピック `80749a13-72cc-da6c-2cd3-f62c982c613c`（L1）と `019ca5cc-b1e6-3f54-1bf9-02afa7024f1d`（行＝カード以降）。
