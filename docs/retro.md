# 日次の振り返りと改善の循環（設計書）

> 適用したスキル: `writing-plans`（Goal／全体の制約／Review Focus／1段=1PR の段に Files・Interfaces・合格条件を固定する型）と `verification-before-completion`（表の「実測」の数値は 2026-11-12 にコマンドを打って得た値だけを書き、測れていないものは「未計測」と書く）。

> **保存の設計は最小版に変更済み（みちゃんの指示。このブロックが以下の2章・3章・4章の保存まわりより優先）。** 日次ファイル（`days/`）・rollup・Notion の日次／週次ページは作らない（記録を残すこと自体が目的化していたため）。残すのは3つだけ: ①未返済の指摘（metric／行の URL／起票日）②指標の定義の版 ③修正の基準点（マージ日時と、判定できたときの前後の値）。**数値は保存せず、元データ（`kanban.db`・各 `state.db`・git・`skill_metrics`）から毎回計算し直す。** 実装は `tools/retro_metrics.py`（指標を JSON で出す。保存しない。n<3 は null）・`tools/retro_findings.py`（`~/.hermes/state/retro/findings.json` に未返済の指摘だけ。`add`／`check`／`list`／`close`。行の URL 必須・同じ metric の未返済は1件だけ・前後7日が各 n≥3 のときだけ判定）・`tools/retro_signal.py`（cron の monitor 用に帯と1/3/7日の連続だけを出す。生の値は出さない）。cron は次の段。出力は #開発 に5行、指摘は行にする。

**Goal:** 日々の動きを決定論で記録する。数値の判定が変わった日だけ AI が振り返り、指摘は必ず行に起票する。直したかどうかは、翌日以降に同じ指標・同じ定義で before/after を出して確かめる。

**Architecture:** 収集は sf repo の `scripts/retro_daily.py` 1本でやる（決定論・時刻なし）。これを monitor にした cron 1本が、判定の行が変わった日だけ AI を起こす（`skill-review-signal.sh` → `06746a3e37b6` と同じ型）。指摘は `findings.json` に置き、書き換えは CLI 経由に限る（AI は JSON を直接編集しない）。実カードの通し試験は `scripts/smoke.py`（引数なしで1手進める状態機械。`ship_approved.py` と同じ型）を no_agent の cron で回す。

**Tech Stack:** Python 標準ライブラリ、sqlite3（読み取り専用 `mode=ro`）、`gh`、`ntn`／Notion MCP、Hermes cron（`hermes/cron.toml` → `factory.py cron sync`）。

## 全体の制約（全段に効く）
- 収集は 100% 決定論。AI がやるのは解釈と起票だけ。
- 出力（日ファイル・集計・signal）に、収集した時刻を入れない。日付は「集計の対象窓」から決まる入力値なので入れてよい。
- 新しい抽象化を足さない。monitor 型 cron、`[wrappers]`、`create_row.py`、`remaining_work_check.py`、`card_timing.py`、`skill_effect_report.py`、`ask_human.py`、`make test` を使い回す。
- 1つの指標は1つの定義の版を持つ。版が違う値どうしは比べない。
- サンプルが3件未満のときは値を記録するだけで、判定しない（green にしない）。
- 出力は日本語。#開発 には5行以内。詳細は Notion の1ページに書く。

## Review Focus（どの段のテストでも突かれにくく、実際に困りやすい入力）
1. **元データが無い**（`state.db` が無い、`gh` の認証切れ、Notion が 5xx）→ 0 と書かず `unmeasured` にし、signal に `UNMEASURED` 行を出す。→ 段1 `test_missing_source_is_unmeasured`
2. **日付の境目をまたぐ run**（23:50 に始まり 00:20 に終わる）→ `ended_at` の属する JST の日に1回だけ数える。→ 段1 `test_run_across_midnight_counted_once`
3. **smoke のカードが本番の数値に混ざる**→ tenant=`smoke`、または題名が `【smoke】` で始まるものは、すべての本番指標から外す。→ 段1 `test_smoke_cards_excluded`
4. **値だけ毎日少し動く**（中央値 23.1→23.4）→ 判定の帯が変わらなければ signal は同じにし、AI を起こさない。→ 段4 `test_signal_stable_within_band`
5. **同じ指摘が日をまたいで繰り返される**→ 同じ指標に開いた指摘があるあいだは、新しく足せない（CLI が exit 2 で拒否）。→ 段4 `test_finding_add_rejects_duplicate_metric`

---

## 1. 毎日記録する指標

凡例: **窓**＝JST の閉じた1日（`[D 00:00, D+1 00:00)`）に起きた出来事。あとから再計算しても同じ値になる。**時点**＝集計したときの状態。入力（repo の sha など）を一緒に記録し、あとから再計算しない。「最小 n」を下回るときは値だけ記録し、判定しない。

本番カード＝`~/.hermes/kanban/boards/dev/kanban.db` の `tasks` のうち、tenant≠`smoke` かつ題名が `【smoke】` で始まらないもの。run の担当は `tasks.assignee` ではなく `task_runs.profile` で見る（assignee は最後の担当に書き換わる。実測では reviewer 80 / executor 8）。

| キー | 定義 | 取得元 | 決定論 | 単位 | 良い方向 | 種別 | 最小 n |
|---|---|---|---|---|---|---|---|
| `cards.done` | 窓の中に `completed_at` がある本番カードの枚数 | kanban.db `tasks` | ○ | 枚 | 判定しない（量の文脈） | 窓 | – |
| `cards.lead_min_p50` / `_p90` | カードの `created_at`→`completed_at`。中央値と p90（最近順位法。小数1桁に丸める） | kanban.db `tasks` | ○ | 分 | 小 | 窓 | 3 |
| `cards.work_min_p50` | カードの run の所要（`ended_at-started_at`）の合計の中央値 | kanban.db `task_runs` | ○ | 分 | 小 | 窓 | 3 |
| `cards.hang_min_p50` / `cards.delegate_min_p50` | `card_timing.py` の分解の「固まり」（返答待ちが 300 秒超）と delegate_task の分 | `card_timing.card_runs`＋`state.db` | ○ | 分 | 小 | 窓 | 3 |
| `cards.model_wait_share` | モデル待ちの分 ÷ run の分（`card_timing.py` と同じ分解） | 同上 | ○ | % | 小 | 窓 | 3 |
| `runs.failed{profile}` | outcome が `crashed`・`timed_out`・`gave_up` の run の数（`blocked` は人が止めたものなので別に数える） | kanban.db `task_runs` | ○ | 回 | 小 | 窓 | – |
| `runs.crash_fast{profile}` | `crashed`・`timed_out` のうち所要 120 秒未満の run の数（起動直後に落ちたもの） | 同上 | ○ | 回 | 0 | 窓 | – |
| `runs.rate_limited{profile}` | outcome が `rate_limited` の run の数 | 同上 | ○ | 回 | 小 | 窓 | – |
| `cards.retry_exhausted` | 窓の中に `gave_up` イベントがあるカードの枚数（再試行を使い切った） | kanban.db `task_events` | ○ | 枚 | 0 | 窓 | – |
| `stall.count` | ready のカードが claim されないまま `STALL_SECONDS`（900 秒。`epic_pipeline_health.py` から import する）を超えた回数。イベントの列から計算し直す | kanban.db `task_events` | ○ | 回 | 0 | 窓 | – |
| `review.rounds_p50` | 窓の中で完了したカード1枚あたりの reviewer run の数（`epic_pipeline_timing.py` の rounds と同じ定義） | kanban.db `task_runs` | ○ | 回 | 小（1 が理想） | 窓 | 3 |
| `review.changes_rate` | reviewer run のうち outcome が `changes_requested` のものの割合 | 同上 | ○ | % | 小 | 窓 | 3 |
| `pr.merged{via}` | 窓の中にマージされた PR の数。`via=ship`（`ship_approved.json` に記録がある）と `via=direct`（工場を通らずに入ったもの）に分ける | `gh pr list --state merged --json number,createdAt,mergedAt`（`ship.repos` に登録した repo と skills repo） | ○（GitHub の状態が入力） | 本 | `direct` は小 | 窓 | – |
| `pr.lead_h_p50{via=ship}` | 下書き PR の作成→マージ（承認待ちを含む） | 同上 | ○ | 時間 | 小 | 窓 | 3 |
| `approval.waiting` / `approval.oldest_h` | 窓の終わりの時点でレビュー待ちの行の数と、いちばん古い行の待ち時間（窓の終わり−`作成日時`） | Notion レビュー DB＋段3の判断ログ | ○（段3から） | 件・時間 | 小 | 窓 | – |
| `approval.decide_h_p50` | 窓の中に判断された行の `作成日時`→判断（判断の時刻は `review_inbox_status.py` が最初に DECIDED を見た時刻。誤差は3分以内） | 段3で追加する `review_decisions.jsonl` | ○ | 時間 | 小 | 窓 | 3（7日窓で判定） |
| `make_test.sec_p50` | `ship_approved.py` が main に重ねて流した `make test` の所要 | 段3で追加する `ship_events.jsonl` の `test_seconds` | ○ | 秒 | 小 | 窓 | 3（7日窓で判定） |
| `deploy.count` / `deploy.failed` | 窓の中に `done`／`deploy_failed` に移った出荷の記録の数 | 段3で追加する `ship_events.jsonl` | ○ | 回 | failed は 0 | 窓 | – |
| `lint.{skill,procedures,envs,bash32,dollar}` | 各 lint の違反の数。sf の `origin/main` の sha を固定して流す。procedures はベースラインを差し引かない生の数 | `tools/skill_lint.py`、`lint_procedures.py`、`lint_envs.py`、`lint_bash32.py`、`lint_dollar.py` | ○（sha が入力） | 件 | 小 | 時点 | – |
| `skills.{count,refs,total_chars,violations,nested,dup}` | `skill_metrics.py --json` の値をそのまま使う | skills repo `tools/skill_metrics.py --other <sf>/skills` | ○ | 本・字・件 | total・違反は小 | 時点 | – |
| `skills.views_per_card` | executor のカードのセッション1つあたりの `skill_view` 呼び出しの数（参照されている量） | `state.db` のメッセージ | ○ | 回 | 判定しない（総量と並べて読む） | 窓 | 3 |
| `ai.calls{profile,source}` / `ai.cache_read{…}` / `ai.output{…}` | 窓の中に始まったセッションの `api_call_count`・`cache_read_tokens`・`output_tokens` の合計 | `~/.hermes/profiles/{dev,executor,reviewer}/state.db` `sessions` | ○ | 回・トークン | 小 | 窓 | – |
| `ai.calls_per_card` | executor と reviewer の kanban 由来の calls ÷ `cards.done` | 上の2つ | ○ | 回/枚 | 小 | 窓 | 3 |
| `cron.ai_wakes{job}` | AI を使う cron ジョブの、窓の中の実行の数（output のファイル名の時刻で数える） | `~/.hermes/profiles/dev/cron/output/<job_id>/*.md` | ○ | 回 | 小 | 窓 | – |
| `rows.unfiled` | 窓の中に「完了」になったタスク行のうち、完了レポートが `remaining_work_check.check()` で落ちるもの | Notion タスク DB（読むだけ）＋`remaining_work_check.py` | ○ | 件 | 0 | 窓 | – |
| `review_row.chapters_ok` | 窓の中に作られたレビュー行のうち、9章がそろっている行の割合 | Notion レビュー DB（本文の見出し） | ○ | % | 100 | 窓 | 3 |
| `smoke.probe` / `smoke.canary` | 通し試験の最後の結果（`pass`・`fail:<段>`・`skip:<理由>`） | `~/.hermes/state/smoke/state.json` | ○ | – | pass | 時点 | – |
| `retro.findings{open,improved,not_improved,escalated}` | 指摘の台帳の状態ごとの件数 | `~/.hermes/state/retro/findings.json` | ○ | 件 | open は小、improved は大 | 時点 | – |
| `retro.coverage` | 全指標のうち `measured` の割合（`unmeasured`・`thin`・`error` を除いた残り） | 日ファイル自身 | ○ | % | 大 | 時点 | – |

**「今日直したものが効いたか」を測るのは次の指標。** どれも窓の指標なので、修正の前の日を過去30日ぶん遡って計算し直せる（段1で backfill する）。
- reviewer が 60 秒で落ちて再試行を使い切った件 → `runs.crash_fast{reviewer}` と `cards.retry_exhausted`
- 83 分かかったカードの内訳（33.3 分返らない呼び出し＋14.6 分 delegate_task）→ `cards.hang_min_p50` と `cards.delegate_min_p50`
- レビュー行の9章 → `review_row.chapters_ok`
- スキル整理（864,078→557,620字。今日の `skill_metrics` の値は 667,676字・59本）→ `skills.total_chars` と `cards.work_min_p50`。今ある `skill-effect.sh` が1回だけやっている前後比較を、どの指標でも同じ手順でできるようにする。

**今日の実測（基準値の目安。2026-11-12 にコマンドを打って得た値）:** 直近7日の kanban セッションは、executor 136 本（1本あたり平均 63.8 calls・cache_read 12.7M・出力 66k・23.2 分）、reviewer 115 本（23.5 calls・cache_read 2.78M・出力 12.9k・11.0 分）。run の outcome の累計は、reviewer の `rate_limited` が 152、`changes_requested` が 32、`crashed` が 2、`gave_up` が 1。dev の cron セッションは直近1日で 2,154 本・1,014 calls。sf の直近の PR #79〜#84 は作成からマージまでが数秒（`via=direct`）。

---

## 2. 保存の形

置き場所は `~/.hermes/state/retro/`（sf の `paths.state` の下）。

```
~/.hermes/state/retro/
  days/2026-11-11.json     # 1日1ファイル。書くのは1回だけ（上書きしない）
  rollup.json              # 直近7日・30日の集計。days/ から毎回作り直す派生物
  findings.json            # 指摘の台帳。retro_daily.py finding … からしか書かない
  review_decisions.jsonl   # 段3: review_inbox_status.py が DECIDED を最初に見た記録（追記だけ）
  ship_events.jsonl        # 段3: ship_approved.py の段の移り変わりと test_seconds（追記だけ）
~/.hermes/state/smoke/state.json, history.jsonl   # 段6・7
```

**衝突しない理由:**
- `make check`（`factory.py check`）が比べるのは、置換した prompts・skills・SOUL・templates と本番だけ。state は見ない。
- `safe_deploy.sh` が置き換えてバックアップするのは LIVE（`~/.hermes/scripts/sf`）だけ。state には `pipeline_deploy_ts` を書くだけ。
- `sync_live.py` が state で触るのは `sf_deployed_skills.json` と `pruned_skills/` だけ。
- repo の外にあるので、`require_main.sh` の「追跡外のファイルなし」に引っかからない。
- repo に足すのは `scripts/retro_daily.py`・`scripts/smoke.py`（`safe_deploy.sh` の `DEFAULT_FILES` に足す。足し忘れは `test_deploy_list.py` が落とす）、`scripts/tests/retro_definitions.golden.json`、`prompts/retro_review.txt`、`hermes/cron.toml` のジョブと wrapper、`tools/test_retro_daily.py`・`tools/test_smoke.py`（`make test` に足す）。

**日ファイルのスキーマ（`schema: 1`）:**
```json
{
  "schema": 1,
  "day": "2026-11-11", "tz": "Asia/Tokyo", "window": [1794322800, 1794409200],
  "defs": {"sha": "<DEFINITIONS を正準化した JSON の sha256 の先頭16桁>", "v": {"cards.lead_min_p50": 1, "...": 1}},
  "inputs": {"kanban_max_event_id": 9123, "sf_main": "c242976", "skills_main": "679e9ca",
             "state_db_max_session_started": {"executor": 1794408000.1, "...": 0}},
  "metrics": {
    "cards.lead_min_p50": {"value": 41.2, "n": 7, "unit": "分", "status": "measured"},
    "make_test.sec_p50":  {"value": null, "n": 0, "unit": "秒", "status": "unmeasured", "reason": "ship_events.jsonl が無い（段3）"},
    "review.rounds_p50":  {"value": 2.0,  "n": 2, "unit": "回", "status": "thin"}
  },
  "evidence": {"runs.crash_fast{reviewer}": ["t_xxxxxxxx#41"]}
}
```
- `status` は `measured`（判定してよい）・`thin`（n が最小 n 未満。値は残すが判定しない）・`no_sample`（n=0）・`unmeasured`（元データが無い）・`error`（読もうとして失敗した）のどれか。
- 小数は1桁に丸め、キーは並べ替え、`json.dumps(sort_keys=True, ensure_ascii=False)` で書く。同じ入力なら同じバイト列になる。
- `inputs` は「何を見て出した値か」を示す指紋。`retro_daily.py verify --day D` は窓の指標を計算し直して、保存した値と比べる。差があれば元データがあとから書き換わったということで、`DRIFT` 行を出す（上書きはしない）。
- `rollup.json` は `{"as_of": "2026-11-11", "metrics": {key: {"d7": {"median", "n_days", "n_samples"}, "d30": {...}, "v": 1}}}`。版が同じ日だけで作る。版がまたがるときは `"mixed_versions": true` にして判定に使わない。

**定義の版:** `retro_daily.py` の中の `DEFINITIONS = {key: {"v": 1, "text": "定義の文", "unit", "better": "low"|"high"|"zero"|"none", "min_n", "tol": {"rel": 0.2, "abs": 1}}}` が唯一の正本。`scripts/tests/retro_definitions.golden.json` に `key → (v, sha256(text))` を固定しておき、文を変えたのに `v` を上げていなければテストが落ちる。`v` を上げた指標は `rollup` で新旧の版を混ぜない。

---

## 3. 日次の振り返り（AI）

**cron は2本だけ**（どちらも `hermes/cron.toml` に書き、`factory.py cron sync` で `--paused` のまま作る）:

```toml
[jobs.retro]
name = "SF 振り返り（毎朝・変化の日だけ）"
schedule = "40 8 * * *"            # 8:00 朝会・8:10/8:20 スキル日次とずらす
monitor = "sf-retro-signal.sh"
prompt = "retro_review"
model = "worker"
reasoning = "medium"
deliver = "failure_deliver"        # #開発 本体（既存のキー。新しいキーは足さない）
skills = ["autonomous-task-execution", "software-factory-ops"]

[jobs.smoke]                        # 段6・7
name = "SF 通し試験（10分・段が変わったときだけ出力）"
schedule = "*/10 * * * *"
no_agent = true
script = "sf-smoke.sh"

[wrappers."sf-retro-signal.sh"]
run = "python3"
target = "live/retro_daily.py"
about = "振り返りの monitor（閉じた前日の日ファイルが無ければ作ってから、判定の行だけを出す。値と時刻は出さない）"
```

wrapper には引数を渡せないので、`retro_daily.py` は**引数なしで「前日の日ファイルが無ければ作る → rollup を作り直す → signal を出す」**動きにする。monitor が state に書く前例として、`review_inbox_status.py` の snapshot がある。日ファイルは1回しか書かないので、何度呼んでも結果は同じ。

**monitor の条件（signal に出す行。並べ替えて出す）。値は出さず、帯と連続日数の区切りだけを出す:**
```
FINDING F-20261112-1 runs.crash_fast{reviewer} verdict=improved|not_improved|thin|waiting_fix|incomparable streak=1|3|7
ESCALATE F-20261112-1 reason=not_improved_3d|waiting_fix_7d|reopened_2
MOVE cards.hang_min_p50 worse streak=1|3|7        # 帯の判定: d7 の中央値（前日まで）と比べ、tol を超えたら better/worse
UNMEASURED approval.decide_h_p50 reason=no_source
SMOKE canary fail stage=review_run
DEFS cards.lead_min_p50 v1->v2
DRIFT 2026-11-10 cards.done
WEEK 2026-W46 ready                               # 閉じた日が土曜日のときだけ出す（日曜の朝に週次を書く）
ERROR <読めなかった元データ>
```
- 出る順番は `FINDING`・`ESCALATE` が先、そのあと `SMOKE`・`MOVE`・`UNMEASURED`・その他。どの日もまず「昨日の指摘が直ったか」から読む形になる。
- streak は 1・3・7 のときだけ値が変わる。悪いままでも2日目・4〜6日目は行が同じなので、AI は起きない（トークンの節約）。3日目と7日目には必ず起きる。
- 判定が `same` の指標、`thin` の指標は行を出さない（判断しない）。`thin` が7日続いた指標は `UNMEASURED … reason=thin_7d` を出す（測れていないことを黙らせない）。

**プロンプトの骨子（`prompts/retro_review.txt`）:**
1. 上の MONITOR CHANGE の差分だけを根拠にする。数値は `python3 ${live}/retro_daily.py show --day <D>`（読むだけ）で取る。推測で数値を作らない。
2. **最初に `FINDING`・`ESCALATE` 行を処理する。**
   - `improved` なら `retro_daily.py finding close <F> --verdict improved` を打つ。台帳の判定と違う verdict は CLI が拒否する。
   - `not_improved` は、同じ打ち手を繰り返さず、行に計測結果を追記する（`notion-edit-safety` の手順で、読み直してから追記する）。
   - `ESCALATE` は `ask_human.py` で人に判断を求める行を作る。選択肢は「別の打ち手」「指標の定義を見直す」「受け入れて閉じる」の3つ。
3. `SMOKE fail`・`MOVE worse`・`UNMEASURED` の行それぞれについて、原因の当たりを付ける。使うのは `evidence` のカード／run と、`card_timing.py`・`epic_pipeline_timing.py`・`skill_effect_report.py`（読むだけ）。事実と仮説は分けて書く。
4. 直す必要があるものは、**必ず** `create_row.py --spec`（kind=task。完了条件に「指標キー・今の値・目標値」を書く）で起票する。そのあと `retro_daily.py finding add --metric <key> --row <URL>` を打つ。同じ指標に開いた指摘があるとき、開いた指摘が3件あるときは、CLI が exit 2 で拒否するので、新しくは足さず既存の行に追記する。
5. Notion の日次ページ（親は `[notion].retro_parent`、題名は「振り返り YYYY-MM-DD」）を書く。章は「昨日の指摘の結果／今日の変化／原因の当たり（事実と仮説）／起票した行／未確認・残件」。
6. ページの本文を `retro_daily.py lint-report <md>` に通す。中身は `remaining_work_check.check()` に加えて、本文の F-id がすべて台帳にあって行の URL を持つか、signal の `MOVE`・`SMOKE` 行がすべて本文に出てくるか、を見る。exit 0 になるまで直す。
7. #開発 に5行以内で投稿する。①昨日の指摘の結果（前→後の数値）②今日の変化 ③起票した行（URL）④エスカレーション（あれば）⑤Notion ページの URL。
8. 禁止: `make sync`・`make deploy`・マージ・承認の代行。cron と DEFINITIONS は書き換えない（直すときは行に起票する）。

---

## 4. 改善の循環（誰が・何を・いつ）

| いつ | 誰 | 何を |
|---|---|---|
| 毎朝 8:40 | `retro_daily.py`（script） | 前日の日ファイル、rollup、指摘ごとの verdict を計算し、signal を出す |
| signal が変わった朝 | AI（`retro`） | 3章の手順で処理する。指摘には必ず行を作り、台帳に登録する |
| 行ができたあと | 工場（intake→planner→gate→executor→reviewer） | 通常のタスクとして直す。AI が起票するときに、行の完了条件へ「PR 本文に `Retro: F-…` を書く」と書いておく（プロンプトや雛形は変えない） |
| マージ | `retro_daily.py`（翌朝） | `gh` でマージ済みの PR の本文から `Retro: F-…` を拾い、その指摘の `fixed_day` を決める（決定論）。拾えなかったときは、人か AI が `finding fixed F --pr N` で明示する |
| fixed_day の翌日から | `retro_daily.py` | before＝fixed_day の前の7日、after＝fixed_day の翌日から最大7日、で同じ指標・同じ版を比べ、verdict を出す |
| 3日続けて not_improved、修正待ちが7日、同じ指標で2回開き直し | script が `ESCALATE` を出す → AI が `ask_human.py` | 人が決める。決めるまでは同じ指標で新しい指摘を足せない |
| 日曜 8:40（`WEEK` 行） | AI | 週次の1ページを書く（下） |

**verdict の規則（決定論）:**
- `improved`: after の中央値が before の中央値より `better` の向きに `tol` を超えて動いた。カード単位の指標は before と after にそれぞれ n≥3 が要る。1日1値の指標は after に3日以上が要る。
- `not_improved`: サンプルは足りているのに、改善していない。
- `thin`: サンプルが足りない。判断しない。7日たっても `thin` なら `UNMEASURED` にする。
- `incomparable`: before と after で定義の版が違う。
- `waiting_fix`: 行はあるが、`Retro: F-…` の付いた PR がまだマージされていない。
- 開いた指摘は最大3件（WIP の上限）。上限を超えて新しく足せないので、「直らない指摘ばかり積み上がる」状態にならない。

**週次の1ページ（日曜。題名は「週次 2026-W46」）で見るもの:**
1. 指摘の流れ: 開いた・直した・improved・not_improved・エスカレーションの件数と、改善率（improved ÷ 閉じた件数）。
2. 主要8指標の、今週の7日と先週の7日の比較: `cards.lead_min_p50`・`_p90`、`runs.failed` の率、`runs.crash_fast`、`review.rounds_p50`、`approval.decide_h_p50`、`ai.calls_per_card`、`make_test.sec_p50`、`smoke.canary`。
3. 測れている度合い: `retro.coverage` と、`UNMEASURED` の一覧。
4. 判断の規則:
   - 改善率が2週続けて 50% 未満なら、翌週の開いた指摘の上限を2件に下げる（打ち手を絞る）。
   - `pr.merged{via=direct}` が `via=ship` を上回った週は、「工場を通らない変更が主流になっている」と1行書く。
   - smoke が2週続けて `skip` なら `UNMEASURED` として扱う。
5. 来週の重点にする指標を1つ選び、行に書く。

---

## 5. 実カードの smoke

**分け方（fake で済むもの／実物が要るもの）:**

| 層 | 何で回す | 何を確かめる | 頻度 | AI のコスト |
|---|---|---|---|---|
| S0 fake | `make test`（既にある `epic_selftest.py`・`test_ship_e2e.py`）＋段6で足す `tools/test_pipeline_e2e.py` | ゲートの順番、カード本文、承認依頼の9章の雛形、出荷（偽の gh と本物の git）。段6では、偽の Notion・偽の kanban の上で偽のワーカーが done・changes_requested・crashed を返したとき、承認依頼まで進むか、止まるかを見る | PR ごと | 0 |
| S1 起動の試験 | `smoke.py` が `[smoke] probe` カードを1枚作る（tenant=`smoke`）。executor は「すぐ review を求めて、要約に `probe` と書く」だけ、reviewer は「要約が `probe` なら承認」だけ | プロファイルの起動、provider の認証、dispatcher が reviewer を起こせるか、起動直後のクラッシュ（今日の 60 秒の件）、rate limit | 毎日 05:00 と、sf の deploy のあと（`pipeline_deploy_ts` が前回の試験より新しいとき） | 約 10〜15 calls・cache_read 約 2〜4M／回（見積もり。1 call あたり 200k 前後の文脈を読む前提） |
| S2 通し試験（canary） | `create_row.py` で `【smoke】canary <週>` のエピックを作る（tenant=`smoke`。中身は sf の `docs/smoke/canary.md` に週番号を1行足すだけ）。intake → planner → gate → 5工程 → リリース承認の行ができるまで、実物で回す | 本物の Notion の雛形と権限、planner と gate の実際の動き、モデル、skills の読み込み、レビュー行の9章、承認依頼のメンション | 週1（日曜 03:00）。ただし直近7日に、本番のエピックが人の手を介さずに承認依頼まで届いていれば `skip:passive_ok` にする（下の注を参照） | 約 700 calls・cache_read 約 100M・出力 約 0.5M・3〜4 時間（見積もり） |

**S2 のコスト見積もりの根拠（どれも上の実測値）:** 5工程 × executor 1本（63.8 calls・12.7M）＋reviewer 1本（23.5 calls・2.78M）。差し戻し率は 32/115 ≒ 28% なので、やり直しを約1.5往復ぶん足す（1往復あたり 87 calls・15.5M）。これで約 570 calls・約 100M。intake・planner・review_apply の起床が約 10 回 × 10〜20 calls で 100〜200 calls。合計で約 700 calls。週あたりのカードの実績は executor 136×63.8＋reviewer 115×23.5 ≒ 11.4k calls なので、canary は**週に1回なら約 6%、毎日だと約 40%** になる。だから週1にする。軽いレーン（L1。PR #57 は下書きのまま）が入れば、工程が減って約 1/2 になる見込み。

**`skip:passive_ok` の注:** 本番の流れが通ったことを、代わりの証拠に使う。日ファイルの `e2e.passive`（tenant≠smoke のエピックで、作成から承認依頼の行まで、`ESCALATE` も人の介入もなかったもの）が1件以上あれば skip する。sf の deploy があった週は skip しない。

**`smoke.py` の状態機械（引数なしで1手進める。段が変わったときだけ1行出力する）:**
1. `created`（`CREATED epic` が出てから 2 分以内）
2. `phase_rows`（15 分以内）
3. `card[要件定義]` ができる（10 分以内）
4. 工程ごとに `card_done`。1工程あたり 60 分以内。reviewer の run に `crashed`・`gave_up` があれば、その場で fail。
5. `approval_row`（9章がそろい、メンションがある）。開始から4時間以内。

pass か fail かが決まったら後片付けに進む。

**失敗したときの止め方:**
- 期限を過ぎた段で `fail:<段>` を記録する。`evidence` にはカード id、run id、Notion の page id を入れる。
- エピックを `新規作成の停止=true` にし、planner と gate が次のカードを作らないようにする。
- 残った canary のカードは `hermes kanban block` で止める。
- 1行出力する（no_agent なので #開発 に届く）。翌朝の signal に `SMOKE canary fail stage=…` が出て、AI が起票する（起票は AI の仕事という線を守る）。
- 本番の待ちが多いとき（ready の本番カードが3枚より多い）は始めずに `skip:busy` にする。2週続いたら `UNMEASURED` にする。

**後片付け（pass でも fail でも、証拠を記録したあとに必ずやる）:**
- レビュー行を `取り下げ` にする。これは smoke 専用の行を `smoke.py` が自分で取り下げるだけで、承認の代行ではない。`承認` には絶対にしない。取り下げは review_apply が反映し、PR を閉じてブランチを残す（既存の `E4` の動き）。
- `michiomochi/smoke-` で始まるブランチだけを、remote と worktree から消す（接頭辞が違えば消さない）。
- tenant=`smoke` のカードは `hermes kanban archive` する。
- エピック・タスク・工程の行は `キャンセル` にする（`phase_row_cancel_note.md` を使う）。削除はしない（`create_row.py` の「消さない」規則に揃える）。
- 後片付けの結果も `history.jsonl` に残す。残った物があれば、次の回は始めずに `fail:cleanup` にする。

---

## 6. 計測の信頼性

- **元データが無い指標を 0 として扱わない。** 元データが無ければ `unmeasured`、読んで失敗すれば `error`。どちらも signal に出す。
- **n が最小 n 未満のときは判定しない。** 1日の値は `thin` として残す。週次は7日窓で判定する（承認や make test は1日に数件しかないため）。
- **定義の版:** `DEFINITIONS` の `v` と golden ファイルで管理する（2章）。版をまたいで比べない。版を上げたら `DEFS` 行が出て、AI が日次ページに「この日から定義が変わった」と書く。
- **再計算の一致:** `verify` が窓の指標を計算し直して保存値と比べ、ずれたら `DRIFT` を出す。
- **自分自身の監視:** `retro.coverage` を指標に含める。monitor が失敗したら、cron の `failure_deliver`（既存の仕組み）で知らせる。
- **smoke を本番の数値に混ぜない:** tenant と題名の両方で外す。外せたかはテストで固定する。
- **backfill:** 段1で窓の指標を30日ぶん作る。時点の指標は過去に遡れないので `unmeasured` から始める。

---

## 7. 実装の段（1段=1PR）と合格条件

各段の共通の条件: `make test` が通る。新しいファイルは `make test` と `DEFAULT_FILES` に入っている。cron は `--paused` で作り、人が `make cron-resume` で動かす。

### 段1: 窓の指標の収集（kanban だけ）と日ファイル
- **Files:** Create `scripts/retro_daily.py`（`collect --day D [--backfill 30]`・`show --day D`・`verify --day D`）、`tools/test_retro_daily.py`、`scripts/tests/retro_definitions.golden.json`。Modify `tools/safe_deploy.sh` の `DEFAULT_FILES`、`Makefile` の `test`。
- **Interfaces:** `collect(day: str, sources: Sources) -> dict`（日ファイルの dict）、`write_day(path, dict) -> str`（sha256）。`Sources` には kanban.db と state.db のパスを持たせ、テストでは一時ファイルに差し替える。カードの分解には `card_timing.card_runs` と `skill_effect_report.window` を import して使う。
- **指標:** `cards.*`・`runs.*`・`stall.count`・`review.*`。
- **合格条件:**
  - `test_collect_twice_same_sha`: 同じ fixture から2回作ると、sha256 が一致する。
  - `test_missing_source_is_unmeasured`: kanban.db が無いとき、`status=unmeasured` になり、`value` は null になる。
  - `test_two_cards_is_thin`: n=2 のとき `thin` になる。
  - `test_run_across_midnight_counted_once`、`test_smoke_cards_excluded`。
  - 本番で `collect --backfill 30` を流すと、`runs.crash_fast{reviewer}` に今日の件が出る。
- **わざと壊して落ちる例:**
  - 出力に `time.time()` を入れる → `test_collect_twice_same_sha` が落ちる。
  - 無いファイルを 0 と数える → `test_missing_source_is_unmeasured` が落ちる。
  - `DEFINITIONS["cards.lead_min_p50"]["text"]` を書き換えたのに v を上げない → golden の照合が落ちる。

### 段2: 残りの元データ（state.db・cron の output・gh・lint・skill_metrics・Notion）
- **Files:** Modify `scripts/retro_daily.py`、`tools/test_retro_daily.py`。Modify `envs/*.toml` に `paths.skills_repo` を足す（無い環境では `skills.*` が `unmeasured` になる）。
- **指標:** `ai.*`・`cron.ai_wakes`・`pr.*`・`lint.*`・`skills.*`・`rows.unfiled`・`review_row.chapters_ok`。
- **合格条件:**
  - `test_gh_failure_is_error`: 偽の `gh` が exit 1 を返すと `error` になる。
  - `test_rows_unfiled_uses_remaining_work_check`: 「未確認・残件」に URL の無い項目がある完了レポートを、1件と数える。
  - `lint_envs.py` が新しいキーを全環境で通す。
- **わざと壊して落ちる例:**
  - `remaining_work_check` を import せず、別の正規表現で数える → `test_rows_unfiled_uses_remaining_work_check` が「なし」の書き方の違いで落ちる。
  - `envs/michiomochi-mac.toml` に `skills_repo` を足し忘れる → `lint_envs.py` が落ちる。

### 段3: 足りない記録を足す（判断の時刻・make test の所要・出荷の段）
- **Files:** Modify `scripts/review_inbox_status.py`（DECIDED を最初に見たら `review_decisions.jsonl` に追記する）、`scripts/ship_approved.py`（段が移るごとに `ship_events.jsonl` に追記し、test の段では `test_seconds` も書く）、`tools/test_ship_e2e.py`、`scripts/epic_selftest.py`。
- **合格条件:**
  - `E1` に、`ship_events.jsonl` に `test_seconds` が1件あり、`stage=done` が1件あることを足す。
  - `E5` に `deploy_failed` が1件あることを足す。
  - review_inbox の selftest で、同じ DECIDED を2回見ても jsonl は1行のままになる。
  - これらが入ると `approval.decide_h_p50`・`make_test.sec_p50`・`deploy.*` が `measured` になる（まだサンプルが無い日は `no_sample`）。
- **わざと壊して落ちる例:**
  - 追記を毎回にする → 「2回見ても1行」のテストが落ちる。
  - `test_seconds` を書かない → `E1` の追加の検査が落ちる。

### 段4: signal・指摘の台帳・日次の cron
- **Files:** Modify `scripts/retro_daily.py`（引数なしの動き、`signal`、`finding add|close|note|fixed`、`lint-report`）。Create `prompts/retro_review.txt`。Modify `hermes/cron.toml`（`jobs.retro` と `wrappers."sf-retro-signal.sh"`）、`envs/*.toml` に `notion.retro_parent`、`docs/design.md` の8章。
- **合格条件:**
  - `test_signal_stable_within_band`: 値だけが帯の中で動いても、signal のバイト列は同じ。
  - `test_streak_buckets_only_1_3_7`: 2日目と4日目の signal は前日と同じ。
  - `test_finding_add_rejects_duplicate_metric`、`test_finding_add_requires_row_url`、`test_finding_wip_limit_3`: いずれも exit 2 になる。
  - `test_finding_close_rejects_wrong_verdict`。
  - `test_verdict_thin_when_after_lt_3`。
  - `test_findings_first_in_signal`。
  - `test_lint_report_requires_move_lines`。
  - `factory.py cron sync --dry-run` が `retro` を `--paused` で作ると表示する。
- **わざと壊して落ちる例:**
  - signal に値（`41.2`）を入れる → `test_signal_stable_within_band` が落ちる。
  - streak を毎日の数で出す → `test_streak_buckets_only_1_3_7` が落ちる。
  - 重複の検査を外す → `test_finding_add_rejects_duplicate_metric` が落ちる。

### 段5: 週次（`WEEK` 行と `week` の出力）
- **Files:** Modify `scripts/retro_daily.py`（`week --week 2026-W46`）、`prompts/retro_review.txt`（WEEK の分岐）、テスト。
- **合格条件:**
  - `test_week_line_only_when_closed_day_is_saturday`。
  - `test_week_compares_same_version_only`: 版が混ざった指標は `mixed_versions` になる。
  - 改善率は、閉じた件数が0件のとき `null` になり、0% にはならない。
- **わざと壊して落ちる例:** 閉じた件数が0件のとき 0% を返す → 改善率のテストが落ちる。

### 段6: S0 の fake の通し試験と S1 の起動の試験
- **Files:** Create `tools/test_pipeline_e2e.py`（make test に入れる）、`scripts/smoke.py`（probe の部分）、`tools/test_smoke.py`。Modify `hermes/cron.toml`（`jobs.smoke` と `wrappers."sf-smoke.sh"`）。
- **合格条件:**
  - `test_pipeline_e2e`: 偽のワーカーが全部 done を返すと承認依頼まで進み、承認依頼に9章がそろう。reviewer が crashed を2回返すとそこで止まり、承認依頼は作られない。
  - `test_probe_due_after_deploy`: `pipeline_deploy_ts` が前回の試験より新しいと、その日に2回目の probe が走る。
  - `test_probe_tags_tenant_smoke`。
  - 本番で probe を1回流すと pass になり、`runs.*` の本番の指標が変わらない。
- **わざと壊して落ちる例:**
  - tenant を付けずにカードを作る → `test_probe_tags_tenant_smoke` と段1の除外のテストが落ちる。
  - 偽の reviewer の crashed を無視して進める → `test_pipeline_e2e` の停止の検査が落ちる。

### 段7: S2 の週次 canary（状態機械と後片付け）
- **Files:** Modify `scripts/smoke.py`（canary）、`tools/test_smoke.py`。Create `docs/smoke/canary.md`。
- **合格条件:**
  - `test_canary_timeout_stops_epic`: 段の期限を過ぎると `新規作成の停止=true` にし、カードを block し、`fail:<段>` を出す。
  - `test_cleanup_only_smoke_prefix`: `michiomochi/feature-x` は消さず、`michiomochi/smoke-…` だけを消す。
  - `test_cleanup_never_approves`: どの経路を通っても、レビュー行を `承認` にしない。
  - `test_skip_when_busy` と `test_skip_passive_ok`（deploy のあった週は skip しない）。
  - 本番で1回流し、承認依頼の行ができたら pass、後片付けのあとに残る物が0になる（`fail:cleanup` が出ない）。
- **わざと壊して落ちる例:**
  - 後片付けで接頭辞を見ない → `test_cleanup_only_smoke_prefix` が落ちる。
  - 取り下げの代わりに承認にする → `test_cleanup_never_approves` が落ちる。

**順番の理由:** 段1・2で記録を貯め始めれば、段4を入れた初日から before の材料（backfill した30日ぶん）がある。段3は、`unmeasured` のまま放っておく期間を短くするため、早めに入れる。smoke（段6・7）は結果を日ファイルに載せる先が要るので、最後に入れる。

---

## 未確認・残件
- Notion の振り返りページの親（`notion.retro_parent`）をどこにするかは、みちゃんが決める。決まらなければ段4は sf の `parent_page` の下に「振り返り」を1ページ作る案で進める。
- `review_row.chapters_ok` の「9章」の見出しの正本は、`templates/release_judgement.md` と `gate_ask.md` のどちらか。段2で読んで固定する（未確認）。
- S1 と S2 のコストは、上の実測の平均から出した**見積もり**で、まだ1回も流していない。段6・7で1回流したあとに、実測で置き換える。
- 起票は、この設計が承認されたあとに段ごとに行う（設計書の段階では起票していない）。
