---
name: software-factory-ops
description: Use when running or fixing the michiomochi factory.
version: 1.0.0
author: sf maintainers
metadata:
  hermes:
    tags: [pipeline, notion, kanban, cron, dev]
---

# ソフトウェアファクトリーの運用（このマシン）

Notion を SSOT、Hermes の cron と kanban で動かす、AI による開発の仕組み。設計書は Notion `dc99b611-a7d7-5bd6-23c4-d95a58cf4afa`、構築の記録は「ソフトウェアファクトリー構築（進行中）」ページ。この SKILL.md は索引と外せない規約だけ。手順・実測・落とし穴は references/ に置く。

## いつ使うか

ファクトリーの状態を見る・止まりを直す・仕組み（スクリプト／テンプレート／指示文／スキル／cron）を変えて出す・#開発 の依頼を受けるとき。

## 方針: 制約はスキルでなくスクリプトで掛ける

スキルは AI に気を付けさせるだけで、守られないことがある。**間違えようがない仕組みを先に作り、スキルはその1コマンドを使えと案内するだけにする。**

- 同じ間違いを2回したら、次は手順を書かずにスクリプト（入力検証・テンプレート適用・作った後の読み返し検査・失敗時の非0終了）を作り、スキルはその実行方法だけを残す。
- 例: エピック／タスクの行は `scripts/create_row.py`（software-factory）で作る。手で `POST v1/pages` を組み立てない。
- Notion を書く手順も同じ扱い（表のセル・チェックリスト・章の検査はスクリプト側に持たせる）。

Notion を SSOT、Hermes の cron と kanban で動かす、AI による開発の仕組み。設計書は Notion `dc99b611-a7d7-5bd6-23c4-d95a58cf4afa`、構築の記録は「ソフトウェアファクトリー構築（進行中）」ページ。

## 手順を覚える操作は scripts/ops.py か make ターゲットで行う

人の努力で正しさを担保しない。新しい繰り返し操作を見つけたら、まずスクリプトにする。ops.py は repo（`-R`）・base・パス・拒否の条件を持ち、PR 本文はファイルか stdin だけ（バッククォートが展開されない）。失敗は日本語1行、終了コード 2 = 拒否 / 1 = 実行失敗。`FLAGS=--dry-run` で実行するコマンドを表示して終わる。

| やること | 打つもの |
|---|---|
| 作業の枝を切る | `make worktree NAME=<slug>`（fetch → `.worktrees/<slug>` → ブランチ `<branch_prefix><slug>`） |
| PR を作る | `make pr TITLE="..." BODY_FILE=<path>`（未コミット・base と同じ枝・未 push・既存 PR は拒否） |
| PR をマージする（人だけ） | `make merge PR=<番号>`（OPEN 以外は拒否） |
| スキルの規約を見る | `make lint-skills`（skills repo の `skill_lint.py --strict-skills --skills-dir skills`。場所は `FLAGS="--skills-repo DIR"`） |
| 全環境の置換を見る | `make render-all` |

新しい script を `scripts/` に足したら、`tools/safe_deploy.sh` の `DEFAULT_FILES`（本番に置くもの）か `NOT_DEPLOYED`（理由付き）に必ず載せる。

## 外せない規約

- スクリプト・テンプレート・指示文・スキルはリポジトリが正。本番を直接編集しない。
- **変更は リポジトリ → PR → マージ → 本番反映 の順。** main へ直接 commit / push しない。ブランチ `sf/<slug>` で変更し、テストを通してから PR（手順は references/change-and-deploy.md）。live（`~/.hermes/shared/skills` など）を直接編集すると次の deploy / sync で消える。
- **本番反映（deploy）は同時に走らせない**。複数カードが並行して deploy すると最後に終わったものが勝ち、他カードの変更が live から消える。deploy の前後に live の値を読み返す（上の「変更の手順」3）。
  deploy と ship は main の作業ツリーからだけ。カードは `make deploy` / `make ship` を打たない。
- 規則を変えるときは、持ち主を全部同時に直す: スクリプト、テンプレート、プロファイルの SOUL、cron の指示文、スキル、Notion のルール文書。
- **gh は毎回 `-R owner/repo` を付ける**（付けないと upstream の元リポジトリを指す。実測）。
- 反映した値は live を読んで確かめる（`make check` が差分なしでも設定値が意図どおりとは限らない）。
- **行が先で、作業は後。** #開発 の依頼は SSOT に行（タスクかエピック）を作り、実行はファクトリーに任せる。行は `scripts/create_row.py` で作る。行を作ったターンで本文の「概要」表と「やること」まで書く。進捗はチャットの記憶でなく、SSOT の行と kanban のカード（`board="${board}"` を毎回明示）で、答える前に実測する。
- **設計が返る前にカードを走らせない。** 行を作ったら `新規作成の停止` にチェックを入れる（`create_row.py` の spec は `"stop_new": true`。チェックの間はプランナーもゲート制御も行・カードを作らない）。設計が確定して行の本文に入れてから外す。走行中のカードに長文の設計を足すと、ワーカーが読み直して書きかけの成果物を作り直す（実測: 設計コメント3回で要件定義が83分）。
- **走行中に方針が変わったら、コメントを積まずに ブロック → 本文を直す → 再開。** コメントはレビュー往復専用で、正本は本文1か所。
- 承認・差し戻し・リリース承認は人だけ。AI は PR の作成・公開・マージ・デプロイをしない（出荷は `ship_approved.py` と cron『SF 出荷（3分）』）。
- `respawn_guarded` が並んだときに、カードを respawn し直したり、レビュー経路を別モデルに差し替えて通そうとしない（原因の切り分けは references/kanban-stalls.md）。
- カード本文の HARD RULE（`templates/phase_card.md`）と、読み取りは1回にまとめる運用は references/working-discipline.md。

## 索引（どの参照を、いつ読むか）

| 参照（references/） | いつ読むか |
|---|---|
| `components-and-cron.md` | Notion の DB・ボード・プロファイル・本番・通知先の ID、cron 8本の間隔と止め方を引くとき |
| `monitoring.md` | カード・ゲートの鼓動・承認待ち・行を作った直後の確認・出荷の状態（drafted〜done）を実測で見るとき |
| `change-and-deploy.md` | 変更を本番へ出す・cron 定義を切り替える・`make deploy` / `check` の挙動や環境変数で迷うとき |
| `notion-rows-and-writing.md` | 行の作成（テンプレート適用）・`ntn api` の query・見出しの色・タスクとエピックの章立て・承認の重複防止 |
| `kanban-stalls.md` | `respawn_guarded` / `blocker_auth`・クォータ切れ後に回復しない・カードの範囲 |
| `request-intake.md` | #開発 で依頼を受けたとき（タスクかエピックか・設計を伴う依頼・段階のある案件・対象外の切り出し） |
| `working-discipline.md` | 走行中の方針変更・heartbeat だけ進む固まり・タイムアウト／STALL・エピックの大きさ・L1/L2/L3 |
| `card-timing.md` | 「なんでこんなに遅い？」に答えるとき。所要時間の内訳の測り方とモデル前後比較 |
| `repo-and-config.md` | リポジトリのファイル構成・make ターゲット・設定キー・`create_row.py` の spec・改名／移設・新環境の立て方 |

## 検証（終わりの条件）

- テスト（Makefile の test。selftest ほか全部）が緑。スキルの規約は `python3 tools/skill_lint.py`（SKILL.md 6,000 字・references 12,000 字・孤立と参照切れ・80 字以上の重複行）。
- 反映後は main で `make deploy-check` → `make ship` → `make check` が差分なし → `make smoke`（スキルと指示文だけなら `make sync`）。
- 配布後に `${live}` の中身と `~/.hermes/cache/michiomochi_task_snapshot.json` を読み直す。
