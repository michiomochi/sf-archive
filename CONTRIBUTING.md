# CONTRIBUTING

sf（ソフトウェアファクトリー）の仕組み（scripts / templates / prompts / skills / tools / hermes / notion / envs）を変えるときの決まり。**変更は必ずリポジトリに入れ、PR をマージしてから本番に反映する。** main への直接 commit / push はしない。本番のファイルも直接編集しない。

## 順番

1. ブランチを切る: `git switch -c sf/<slug>`（worktree でもよい）
2. 変更して commit、push。不具合の修正なら `scripts/epic_selftest.py` に再現テストを足す。
3. PR を作る（`gh pr create`）。
4. マージ前に `make test` が通ること（GitHub Actions は今走らないので、手元の実行が正）。
5. 自分のリポジトリなので**セルフマージでよい**（人の承認待ちは不要）。`gh pr merge`。
6. main に戻って本番に反映する。

```sh
git switch main && git pull
make deploy-check            # 本番に置き換えずに全チェック
make ship                    # make test → make deploy の順に流す（deploy は main の作業ツリーからだけ・ロック付き）。cron 定義を変えたときは、この前に下の cron の前半を流す / スキルと指示文だけなら make sync（main の作業ツリーからだけ。deploy と同じ検査とロック）
make check                   # 「差分なし」になること
make smoke                   # 読み取りだけの動作確認
```

`make deploy`（`make ship` の中の deploy も同じ）と `make sync` は **main の作業ツリー**（ブランチ main、worktree でない）からだけ通る。`tools/require_main.sh` が `git fetch` のあと、HEAD が `origin/main` と同じで、追跡ファイルに未コミットの変更が無く、本番へ写すパス（scripts skills prompts tools plugins hermes envs）に追跡外で無視もされていないファイルが無いことも確かめ、満たさなければ `deploy 拒否: branch=<名前> rev=<短縮ハッシュ> (<理由>)`（sync では先頭の語が `sync`）を1行出して止まる（古い木は `git pull`、worktree からは main の作業ツリーへ移って打つ）。`make stage` / `make deploy-check` / `make deploy` / `make sync` の本番への書き込みは、`<paths.live>/.deploy.flock` の flock で1本ずつ直列化される（待ちは `SF_DEPLOY_LOCK_WAIT` 秒、既定 600）。超えると `tools/with_lock.py` が理由つきで終了コード 75 で終わり、make はそれを `Error 75` と表示して終了コード 2 で終わる。本体の `stage-run` / `deploy-check-run` / `deploy-run` / `sync-run` はロックを持つ親の下でしか動かず、直接打つと `<名> 拒否: … (ロックを持たずに直接は打てない…)` で止まる。`make deploy-check` はマージ前のブランチからも打てる（ロックだけ掛かる）。カードは `make deploy` も `make ship` も打たない。

エピックの変更（エピックの工程で作ったブランチ）は、人が承認したあと、`ship_approved.py`（cron『SF 出荷（3分）』）が下書き PR の公開、squash マージ、登録された repo の本番反映まで自動で行う。上の順番と手順は、エピックでないこのリポジトリ自身の変更に使う。

## test と deploy と ship、レビュー依頼前の検査

- `make test` は印に関係なく毎回全部回す（飛ばす仕組みは無い）。
- **テストの一覧は `tools/tests.toml`、回すのは `tools/test_plan.py`**（Makefile にシェルの表は書かない）。テストごとに名前・コマンド・`tier`（`fast`/`heavy`）・`paths`（対象のファイルの glob）・`cpu_limit`（秒）を持つ。`make test` は全部、`make test-fast` は fast だけ（**CPU 時間の合計が 60 秒以内**。子プロセスの user＋sys で見る。壁時計は見ない。超えたら落ちる。最後に回していない重い検査の名前を出す）、`make test-paths PATHS="tools/safe_deploy.sh ..."` は変えたパスに合う heavy だけ（合うものが無ければ「該当なし」で rc=0。fast は回さない）。`ops.py verify`（`make verify`）は登録の `test_fast` と `test-paths` を回して、作業メモに貼る1行（コマンド・rc・壁時計と CPU 時間）を出す。
- **新しいテスト・道具を足したら tests.toml に書く。** `tools/*.py`・`tools/test_*.sh`・`scripts/tests/*`・`scripts/*_selftest.py` のうち tests.toml の cmd にも paths にも無いものがあると、`make test`・`make test-fast` は何も回さずに落ちる（`tests.toml に無いテスト: tools/test_new.py`）。`tools/test_*.py|sh` は paths に書くだけでは登録にならず、cmd に書く。どのファイルにも当たらない paths の名前も読み込みで落ちる。遅いテストを速く見せるために tier を動かさない（heavy の一覧は `tools/test_test_plan.py` の `HEAVY` にもあり、動かすと差分に出る）。
- `make test` は環境（`envs/*.toml`）を横断して検査する。`tools/lint_envs.py`（必須キー・plugins の実在・`${名前}` の重複・置き場の衝突・環境名と OS）、`tools/lint_dollar.py`（雛形の `$` のエスケープ漏れを file:line で）、`tools/lint_bash32.py`（`set -u` の下の `"${arr[@]}"`。bash 3.2 は空配列で落ちるので `${arr[@]+"${arr[@]}"}` か `${#arr[@]}` の分岐にする。常に非空の配列は `# nonempty: 名前`、例外は行末に `# bash32-ok: 理由`）を先に流し、そのあと `render --all-envs --dry-run` で全環境を置換する。検査自体の再現テストは `tools/test_lint_checks.py`。スキルに手順のコマンド（`gh pr create` など）を移し先なしで書くと `tools/lint_procedures.py` が落とす（既存はベースライン、書き直しは `make lint-procedures-baseline`。テストは `tools/test_lint_procedures.py`）。新しい環境を足すときは `lint_envs.py` の `ENV_SUFFIX_OS` と Makefile の `ENV` を一緒に直す。
- **`make deploy` はテストを回さない。単独では使わず `make ship` を使う。** `make ship` は `make test` が通ってから `make ship-tested`（deploy の直前に、テストした木と出荷する木が同じかを照らしてから `make deploy`）を流す（test が落ちたら deploy は走らない）。`make test` は緑のとき最後に `<paths.state>/tested_tree.json`（テストした作業木のパスと HEAD の sha。時刻なし）を書き、出荷する木の HEAD と違えば `出荷を拒否します: tested <sha> / now <sha>。make test を回してから出荷する` で deploy せず止まる（承認後の自動出荷 ship_approved.py も同じ照合を deploy の前に行い、違えば FAIL tested-tree）。未コミットの変更がある木では記録を書かない。`check` と `smoke` は含めないので、あとに別に流す。
- `ship` を `ship: test deploy` と依存で書かない。`-j` では test と deploy が並行に走り、`-k` では test が落ちても deploy を作りに行く。`tools/test_makefile.py` が実物の Makefile でこれを確かめる。
- レビュー依頼・PR の前に、先に `make test`、続けて `make precheck` を回す（`make precheck` は test を回さない。`git diff --check`、commit メッセージに Notion の URL・data source の ID・DB 名が無いこと）。PR 本文の下書きも見るときは `make precheck PR_BODY=<ファイル>`。落ちたら依頼しない。

## リハーサルの差分実行（`tools/stamp.py run`）

サンドボックスで段を順に回すリハーサルは、各段を `tools/stamp.py run` で包む。入力（`--inputs`）と前の段が変わっていない段は飛ばし、変わった段とその後だけを回す。共有するディレクトリは、前の段の `--snapshot` の写しから戻してから回す（写しが無ければ終了コード 3 で止まる）。手直しの間は差分で回し、**レビュー依頼の前に `STAMP_FORCE=1` で通しを1回**回す。

```sh
ST="tools/stamp.py --dir $R/stamps"   # 印の置き場所。--snapshot のディレクトリはこの親($R)の中に置く
python3 $ST run build   --inputs build.py                    --snapshot "$S" -- python3 build.py "$S"
python3 $ST run prep    --after build --inputs prep.sh       --snapshot "$S" -- bash prep.sh
python3 $ST run dry     --after prep  --inputs tools/x.py    --stateless     -- python3 tools/x.py
python3 $ST run apply   --after dry   --inputs tools/x.py                    -- python3 tools/x.py --apply
python3 $ST run inv     --after apply                        --stateless     -- python3 inventory.py "$S"
python3 $ST run rollback --after inv  --inputs tools/x.py                    -- python3 tools/x.py --rollback
```

- `--stateless` は状態を変えない段（dry-run、棚卸し）。`--snapshot` は、後の段を回し直すときに戻したい状態を作る段に付ける。
- 段が落ちると、その段の印が消え、後の段は回らずに終了コード 3 になる。直したらもう一度回す。

## PR 本文

次の4節を書く。

- **概要**: 何を変えたか（1〜2行）
- **背景**: なぜ必要か
- **変更内容**: ファイルごとの要点
- **検証**: 実行したコマンドと出力（`make test` の結果など）

## golden を触るとき

golden は古いコードから作ったものを保つ。テンプレートや文言を変えたら、**同じ置換を golden にも当てる**。新しいコードで作り直さない。

## 稼働中に切り替えるとき

cron が動いている間に本番を入れ替えるときは、この順で行う。

```sh
make cron-pause
# cron の前半(cron 定義を変えたとき。make deploy はジョブが本番に無いと失敗にするので、deploy より先に流す)
make cron-sync FLAGS=--dry-run   # 「同じ名前のジョブ … が既にあります」で止まったら、案内どおり make cron-adopt IDS="<キー>=<ID>"
make wrappers                # 新しいジョブの .sh が無いと cron-sync が作れない
make cron-sync
make ship                    # make test → make deploy。main の作業ツリーからだけ、<paths.live>/.deploy.flock で1本ずつ（待ちは SF_DEPLOY_LOCK_WAIT 秒）。スキルと指示文だけなら make sync（同じ条件とロック）
make check                   # 差分なし
make smoke
make cron-resume
```

## 規則を変えるとき

持ち主を全部同時に直す: スクリプト、テンプレート、プロファイルの SOUL、cron の指示文、スキル、Notion のルール文書。

## PR を作るときの注意
`gh pr create` が `Head sha can't be blank / No commits between ...` で失敗することがあります（org の非公開リポジトリで発生を確認）。そのときは REST で作れます。

```
gh api repos/example-org/sf/pulls -X POST \
  -f title="..." -f head="sf/<slug>" -f base="main" -f body="$(cat /tmp/pr_body.md)"
```
