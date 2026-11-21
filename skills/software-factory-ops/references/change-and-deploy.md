# 変更の手順と本番反映（deploy）

SKILL.md の索引から: スクリプト・テンプレート・指示文・スキルを変えて本番へ出すとき、cron 定義を切り替えるとき、複数のブランチを main に入れるとき、make / deploy / check の挙動で迷ったとき。
（リポジトリが正・main へ直接 push しない・deploy は1本だけ、は SKILL.md の規約。ここは手順と挙動の詳細。）

## 変更の手順

- **変更は必ず リポジトリ → PR → マージ → 本番反映 の順**。main へ直接 commit / push しない（詳細は CONTRIBUTING.md）。
  1. ブランチ `sf/<slug>` で変更し、テストを通す。
  2. PR を作る（本文は 概要 / 背景 / 変更内容 / 検証）。自分のリポジトリなのでセルフマージでよい。
  3. マージ後、main で `make deploy-check` → `make ship`（test → deploy。deploy 単体はテストを回さない。cron 定義を変えたら deploy より先に 4 の cron の前半を流す。スキルと指示文だけなら `make sync`。`make sync-check` は差があれば 1、make 経由だと 2）→ `make check` が差分なし → `make smoke`。
  4. 稼働中に切り替えるときは `make cron-pause` → `make cron-sync FLAGS=--dry-run`（「同じ名前のジョブ … が既にあります」で止まったら、案内どおり `make cron-adopt IDS="<キー>=<ID>"`）→ `make wrappers` → `make cron-sync` → `make ship`（または `make sync`）→ check → smoke → `make cron-resume`。cron-sync を deploy の後にすると、指示文を配るジョブが本番に無くて `make deploy` が失敗する。wrappers を cron-sync の後にすると、新しいジョブの `.sh` が無くて作れない（2026-11-10 の切り替えで両方を踏んだ）。
- エピックの変更は承認後に自動で出荷される。エピックでない変更だけ、手で `make ship`。
- golden は古いコードから作ったものを保つ。テンプレートの文言を変えたら、同じ置換を golden にも当てる。新しいコードで作り直さない。

## 複数ブランチを順に main へ入れる

承認済み（または人が「マージしてよい」と言った）ブランチを1本ずつ入れる。

```bash
cd ~/ghq/github.com/example-org/sf
git fetch -q origin && git status -sb          # main の作業ツリーが汚れていないこと
git log --oneline origin/main..origin/michiomochi/<branch>
gh pr list -R example-org/sf --state all --limit 6
make pr TITLE="<型>: <題>" BODY_FILE=<本文>   # そのブランチの worktree で
make merge PR=<PR>
```

- **`git merge-base --is-ancestor` が no でも、squash 済みなら既に main にある。** 内容で判定する: `git diff --name-only main origin/<branch> | wc -l` が 0 なら取り込み済み。
- ブランチは消さない（履歴の手掛かりが消える）。squash なのでコミットの題は `<題> (#N)`。
- squash が「the merge commit cannot be cleanly created」で断られたら、worktree でコンフリクトを解いて push してから再マージ（**force push はしない**）:

```bash
make worktree NAME=merge-<slug>
cd .worktrees/merge-<slug> && git merge origin/<branch>   # 衝突を直す
${verify} && git add -A && git commit -m "merge: 衝突を解く"
git push origin HEAD:<branch>
```

- 解いた直後は `mergeable: UNKNOWN` が数十秒続く。`gh pr view <PR> -R example-org/sf --json state,mergeCommit` で確認する。`make merge` が「OPEN ではありません(MERGED)」を返すのも正常（二度マージしない）。
- 順に通すときは、**委譲の指示に「両方の意図を残す」「force push・rebase・ブランチ削除・deploy をしない」「各マージの後テストを緑にする」「意味の衝突は自分で決めずに止めて報告」**を明記する。squash 済みの兄弟ブランチは同じ場所（`Makefile`・`CONTRIBUTING.md`・`skills/*/SKILL.md`）で必ず衝突する。

## 反映を live で確かめる

```bash
grep -E '^(worker|design|reviewer)' ~/.hermes/scripts/sf/config.toml
grep -oE '^# [^{]+' ~/.hermes/scripts/sf/templates/<雛形>.md | head
hermes cron list | grep -A5 '<新しいジョブ名>'
hermes cron runs <ID>          # 新しいジョブが1回動いて completed になること
```

`make check` が「差分なし」でも、設定値・雛形の中身が意図どおりとは限らない。**少なくとも1つの値と1つのファイルを live で読み返す。**

## 落とし穴（実際に踏んだもの）

- `approvals.cron_mode: deny` のため、cron の AI は `python -c` や heredoc を実行できない。DB を書く修復はスクリプトのファイルにする。
- `make deploy` は `sync_live` も走らせる。cron ジョブが無い名前だと「見つからない」で終了コード 2 になる（異常ではない）。
- シェルに `SAFE_DEPLOY_*` / `SYNC_LIVE_*` が残っていると、dry run が飛ばされ scratch に書く。make の前に `unset` する。
- 配布後に読み直して確かめる: `${live}` の中身と `~/.hermes/cache/michiomochi_task_snapshot.json`。
- **repo から消したファイルは、`make sync` が `~/.hermes/state/pruned_skills/` へ退避する**（削除しない）。逆に、live にだけ書き足されたファイルは sync で退避されるので、**先に `make adopt` で取り込む**（curator の書き足しはこの形）。順序は adopt → sync。
- 使い終わった worktree は、**ブランチが main と内容一致（`git diff --name-only main <branch>` が 0）かつ未コミットが無い**ものだけ消す。squash 済みでも内容が古いブランチは残す（消すかは人が決める）。走行中のカードの worktree には触らない。
