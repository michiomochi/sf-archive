# カードの所要時間を実測で分解する

「なんでこんなに時間がかかるの？」に推測で答えないための手順。カード1枚の所要時間は、モデルの推論より**固定費（テスト・deploy・記録のターン数）**が支配することが多い。数字を出してから改善策を言う。

## 1. まずカードの run を読む（時刻の意味に注意）

`hermes kanban --board ${board} show <id>` の `Runs` は

```
#50  review_requested @executor  675s  2026-11-10 01:12
```

**時刻は「開始」で、675s が所要**。終了時刻はイベント側に出る（`[01:23] [run 50] review_requested` / `worker_result` / `[01:24] [run 51] claimed`）。開始時刻を終了時刻と読み違えると「レビューが12分遅れている」と誤答する。レビューはディスパッチャの tick（1分）で拾われるので、`review_requested` の1分後に `claimed` が来るのが正常。

## 2. 中身をログから集計する

executor / reviewer のエージェントログにツール単位の所要時間が残っている。

```
~/.hermes/profiles/executor/logs/agent.log
~/.hermes/profiles/reviewer/logs/agent.log
```

`tool_executor: tool <name> completed (<秒>s, <文字数> chars)` を拾い、合計と「呼び出し間の空白 − 実行時間」を出すと、ツール実行とモデルの推論・待ちに分けられる。

```python
import re, datetime, collections
pat = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO .*tool_executor: tool (\w+) completed \(([\d.]+)s, (\d+) chars\)")
rows = []
for line in open("~/.hermes/profiles/executor/logs/agent.log".replace("~", __import__("os").path.expanduser("~")), errors="ignore"):
    m = pat.match(line)
    if m:
        rows.append((datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2), float(m.group(3))))
tot = collections.Counter()
for t, n, d in rows: tot[n] += d
print(tot.most_common()); print("tool total", round(sum(tot.values()), 1))
gaps = [(rows[i][0] - rows[i-1][0]).total_seconds() - rows[i][2] for i in range(1, len(rows))]
print("gaps>15s", sum(g for g in gaps if g > 15))
```

対象は時刻の範囲で絞る（run の開始〜終了）。`process_manage` の待ち、起動直後の `tool_search` / `skill_view` も別枠で出る。

## 3. 実測の目安（軽微な1ファイル修正のカード、675秒）

- terminal 25回 **219.6秒**（うち1回が **150.8秒**＝`make deploy`）
- モデルの推論・待ち **157秒以上**
- `process_manage`（バックグラウンド待ち）**64.5秒**
- 起動＋プロンプト読み込み 約26秒、Notion 記録（`write_file` 9回）は数秒

## 4. 固定費の正体と、削る順

- **`make deploy` は `test` に依存している**（`deploy: test stage`）。だからテストは「手前の1回」と「deploy の中」で**2回**走る。deploy 1回が2分半ブロックする主因。
- **記録の義務**（タスク行・作業メモ・完了レポート・エピックの管理ログ）が毎回5〜10ターン分。スクリプト1回で書く形にすると減る。
- カード本文は約1万文字の規約で、起動と読み込みだけで30秒前後かかる。
- モデルの変更（Opus→Sonnet）は推論時間しか縮めない。上の固定費が支配的なので、体感は1〜2割。**速度の相談で最初に言うべきは固定費の話**。
- カードの切り方でも変わる: 単発の修正カード（実装＋deploy まで）は 675秒＝11分で終わっている。1枚にまとめすぎると設計の往復が乗る。

## 5. モデルや工程を変えたときの前後比較

「Sonnet にしたら速くなった？」に体感で答えない。`~/.hermes/profiles/${cron_profile}/scripts/sf-model-report.py` が、`model_trial.jsonl`（カード作成時のモデル）と kanban の `tasks` / `task_runs` を突き合わせて、基準時刻の前後で次を出す。

- 速度: 1枚あたりの run 合計時間、完了カードの経過時間
- 質の代理指標: 差し戻しが入ったカードの割合、1枚あたりの run 回数、完了率

基準時刻はスクリプト冒頭の `SWITCH`。切り替えを反映した時刻に合わせて更新する。cron の `--no-agent --script` で回せば、同じ数字を毎日決まった時刻に配れる（`--repeat 1` で単発）。

質は数字だけでは決まらない。差し戻し率・run 回数・完了率を出したうえで、何が原因で増減したか（レビューの指摘の種類、リハーサルの回数）を run の summary から読んで1段落で解釈を添える。

## 6. 実測の数字（カード1枚の所要・テストの所要）

- **所要の内訳は `scripts/card_timing.py` で測る。** `python3 ~/.hermes/scripts/sf/card_timing.py --card <id>` が モデル待ち / ツール実行 / 固まり を分けて出す。実測 2026-11-11: 83分のうち 固まった呼び出し1本で33.3分・ワーカーが自分で起てた `delegate_task` 1回で14.6分・ツール実行1.9分。**所要の9割は「ターン数 × 1ターン中央値6秒」と待ち**で、ツール実行は安い（平均11秒はまれな長い呼び出しに引かれる。中央値で見る）。
- **test の「14.4秒」は重い4つ抜き**（`test_safe_deploy.sh`・rename・deploy_guard・ゲート制御の selftest）。全体は約90秒で `test_safe_deploy.sh` だけで58.8秒（66%）。所要を語るときはどちらの数字かを書く。
