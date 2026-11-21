PY ?= python3
# 環境ごとの値は envs/<ENV>.toml(本番の正本)。オフラインのテストは tests/config.test.toml(golden を作ったときの固定値)。
# 既定は OS で決める(macOS は michiomochi-mac、それ以外は michiomochi-wsl)。ENV=... で明示すればそれが優先。
ENV ?= $(if $(filter Darwin,$(shell uname -s)),michiomochi-mac,michiomochi-wsl)
ENV_FILE ?= $(CURDIR)/envs/$(ENV).toml
TEST_CONFIG ?= $(CURDIR)/tests/config.test.toml
# 設定の hermes.path_prepend(mise の shims、~/.local/bin)を PATH の先頭に足す。safe_deploy.sh などの python3 / hermes / ntn はこれで見つかる。
export PATH := $(shell SF_CONFIG=$(ENV_FILE) $(PY) scripts/sfconfig.py get hermes.path_prepend):$(PATH)
# 本番のスクリプトの置き場所(safe_deploy の LIVE)。~/.hermes/scripts/ にある既存のファイル(tests/ など)と混ざらない専用の場所。
HERMES_SCRIPTS ?= $(shell SF_CONFIG=$(ENV_FILE) $(PY) scripts/sfconfig.py get paths.live)
# cron の --script は cron プロファイルの scripts/ から解決される。
DEV_SCRIPTS ?= $(shell SF_CONFIG=$(ENV_FILE) $(PY) scripts/sfconfig.py get hermes.home)/profiles/$(shell SF_CONFIG=$(ENV_FILE) $(PY) scripts/sfconfig.py get hermes.cron_profile)/scripts
# safe_deploy.sh の LIVE / staging / backup を HERMES_SCRIPTS に合わせる(テストで一時ディレクトリへ向けても取り違えない)。
export SAFE_DEPLOY_LIVE_DIR = $(HERMES_SCRIPTS)
export SAFE_DEPLOY_STATE_DIR ?= $(shell SF_CONFIG=$(ENV_FILE) $(PY) scripts/sfconfig.py get paths.state)
# make test が最後に書く記録(テストした木のパスと HEAD の sha)。make ship-tested と ship_approved.py が deploy の直前に、出荷する木の HEAD と照らす。
TESTED_TREE ?= $(SAFE_DEPLOY_STATE_DIR)/tested_tree.json

.PHONY: pr merge worktree verify test-fast test-paths lint-skills lint-procedures lint-procedures-baseline lint-cron lint-cron-baseline render-all skill-lint test precheck ship ship-tested render deploy-check deploy-check-run deploy deploy-run require-main check sync-check sync sync-run stage stage-run init doctor notion-export-schema notion-bootstrap profiles board wrappers cron-adopt cron-sync cron-pause cron-resume smoke

# スキルと指示文の規約（docs/skill-and-request-rules.md）を機械で検査する。
# 予算・参照切れ・孤立・重複・カード雛形のサイズをまとめて見る唯一の道具。
# スキルの違反は落とす（--strict-skills）。カード雛形(3,000 字上限)の超過は --warn-only でも落とす。
# 検査7: 設定値(行の題・章の数・成果物の数・テストのコマンド・レビューの回数)を文に書き写したら落とす。同じ行に ${名前} か「levels.toml を見る」を書けば通る。
# ベースライン tools/skill_lint_baseline.json は空(段10で返済済み)。1件でも書き写せば落ちる。
skill-lint:
	$(PY) tools/skill_lint.py

# テストの一覧と重さは tools/tests.toml(正本)。回すのは tools/test_plan.py(Makefile にシェルの表を書かない)。
# test_plan.py は最初に tests.toml を検査する: 登録漏れ(tools/*.py・scripts/tests/* などが tests.toml に無い)・存在しない paths は何も回さずに落とす。
# 環境ごと(envs/*.toml)の壊れは、その環境でだけ起きる。lint_envs・lint_dollar・lint_bash32 で原因を行まで先に出し、そのあと render --all-envs で全環境の置換を通す(tests.toml の並びがその順)。
TEST_PLAN = $(PY) tools/test_plan.py --py $(PY) --env-file $(ENV_FILE) --test-config $(TEST_CONFIG)

# Offline selftest (no Notion, no kanban, no Discord). Warnings are errors. Settings: tests/config.test.toml (fixed values the golden files were made with).
# 毎回必ず全部回す(飛ばす仕組みは無い)。fast も heavy も。
test:
	$(TEST_PLAN) --all
	$(PY) scripts/tested_tree.py write --file $(TESTED_TREE) --tree $(CURDIR)

# fast だけ(CPU 時間の合計が 60 秒以内。超えたら落ちる)。最後に、回していない重い検査の名前を出す。出荷の前の make test の代わりにはならない。
test-fast:
	$(TEST_PLAN) --fast

# 変えたパスに合う heavy だけ: make test-paths PATHS="tools/safe_deploy.sh scripts/ops.py"。合うものが無ければ「該当なし」で rc=0(fast は test-fast)。
test-paths:
	$(TEST_PLAN) --paths $(PATHS)

# レビュー依頼・PR の前の機械検査: 空白の誤り、commit メッセージと PR 本文の下書きにある非公開の印。test は回さない(先に make test)。
# PR 本文の下書きも見るときは PR_BODY=<ファイル>。比べる相手は PRECHECK_BASE(既定 origin/main)。
PRECHECK_BASE ?= origin/main
precheck:
	$(PY) tools/precheck.py --config $(ENV_FILE) --base $(PRECHECK_BASE) $(if $(PR_BODY),--pr-body $(PR_BODY))

# ${...} の雛形(prompts, skills, SOUL, カードのテンプレート)を環境の設定で置換して build/<環境>/ に出す。
render:
	$(PY) tools/factory.py render --config $(ENV_FILE)

# 置換した結果と本番(cron の指示文、スキル、SOUL、カードのテンプレート)を比べる。差があれば終了コード 1。
check:
	$(PY) tools/factory.py check --config $(ENV_FILE)
	@# cron.toml の値(schedule・名前)の差分も見る(指示文は factory check が見ている)
	$(FACTORY) cron sync --config $(ENV_FILE) --dry-run

# 本番の置き場所への書き込み(stage の cp、safe_deploy.sh、sync_live)は、リポジトリ全体で1つの flock で直列化する(木に依らない1か所のロックファイル)。
# 入口(stage / deploy-check / deploy / sync)が $(call DEPLOY_LOCK,名) でロックを取り、本体(*-run)を子の make で走らせる。入れ子の呼び出し(deploy の中の stage)は取り直さない。
# 本体(*-run)を直接打つ抜け道は、各本体の先頭の HELD_GUARD(環境変数 SF_DEPLOY_LOCK_HELD がこのロックのパスと同じか)で塞ぐ。
# 待ちの上限は SF_DEPLOY_LOCK_WAIT 秒(既定 600。deploy-check 1回が約 95〜145 秒かかる(実測 93.7〜144.2 秒)ので、deploy 1回ぶんは待てる値)。超えたら理由を1行出して中止する。
DEPLOY_LOCK = $(PY) tools/with_lock.py --label $(1) $(HERMES_SCRIPTS)/.deploy.flock --
# 本体(*-run)の先頭で、ロックを持つ親の下で呼ばれたかを確かめる。直接打つと、ロックの外で本番の置き場所に書いてしまうため。
HELD_GUARD = $(PY) tools/with_lock.py --check-held --label $(1) $(HERMES_SCRIPTS)/.deploy.flock

# スクリプト、設定(config.toml)、置換済みのテンプレートを本番の staging/ に置く。
stage: render
	$(call DEPLOY_LOCK,stage) $(MAKE) --no-print-directory stage-run

stage-run:
	@$(call HELD_GUARD,stage-run)
	mkdir -p $(HERMES_SCRIPTS)/staging/templates $(HERMES_SCRIPTS)/staging/tests
	cp scripts/*.py $(HERMES_SCRIPTS)/staging/
	cp $(ENV_FILE) $(HERMES_SCRIPTS)/staging/config.toml
	cp scripts/levels.toml $(HERMES_SCRIPTS)/staging/levels.toml
	cp build/$(ENV)/scripts/templates/*.md $(HERMES_SCRIPTS)/staging/templates/
	cp scripts/tests/* $(HERMES_SCRIPTS)/staging/tests/
	cp tests/config.test.toml $(HERMES_SCRIPTS)/staging/tests/config.test.toml
	cp tools/safe_deploy.sh $(HERMES_SCRIPTS)/staging/safe_deploy.sh
	printf '%s\n' "$(CURDIR)" > $(HERMES_SCRIPTS)/staging/repo_root

# Copy this repo's scripts into the live staging dir, then run every check without replacing live files.
# マージ前のブランチからも打てる(ブランチの検査はせず、ロックだけ掛ける)。
deploy-check: render
	$(call DEPLOY_LOCK,deploy-check) $(MAKE) --no-print-directory deploy-check-run

deploy-check-run:
	@$(call HELD_GUARD,deploy-check-run)
	$(MAKE) --no-print-directory stage-run
	bash $(HERMES_SCRIPTS)/staging/safe_deploy.sh --check-only

# main の作業ツリーでだけ打てる(tools/require_main.sh: ブランチ main・worktree でない・fetch のあと origin/main と同じ・未コミットの変更なし・本番へ写すパスに追跡外のファイルなし)。
# ロックを待つ前に1回、ロックを取ったあとにもう1回検査する(待っている間に origin/main が進んだ場合を拾う)。
# deploy はテストを回さない。単独では使わず make ship を使う(test_install.py・test_rename_to_sf.py・render の dry-run は safe_deploy.sh が回さない)。
deploy:
	bash tools/require_main.sh
	$(call DEPLOY_LOCK,deploy) $(MAKE) --no-print-directory deploy-run

# stage → safe_deploy.sh(バックアップ、原子的な置き換え、失敗でロールバック) → sync_live。順序は固定(並列で走らせない)。テストは回さない。
deploy-run:
	@$(call HELD_GUARD,deploy-run)
	bash tools/require_main.sh
	$(MAKE) --no-print-directory render stage-run
	bash $(HERMES_SCRIPTS)/staging/safe_deploy.sh
	$(PY) tools/sync_live.py --apply --config $(ENV_FILE)
	@# cron.toml の値(schedule と名前)は sync_live.py が持っていない。ここで本番の jobs.json に反映する
	$(FACTORY) cron sync --config $(ENV_FILE)
	@# SOUL(profiles/*/SOUL.md)も sync_live.py の担当外。置換して本番のプロファイルへ配る
	$(FACTORY) profiles --config $(ENV_FILE) --update

require-main:
	bash tools/require_main.sh

# テスト → デプロイの入口。依存(ship: test ship-tested)で書かない: -j では並行に走り、-k では test が落ちても deploy を作りに行く。
# recipe の行は上から順に実行され、1行目が落ちれば2行目に進まないので、順序はここで保つ。check と smoke は含めない(別に流す)。
# 順序は test(緑なら最後に tested_tree.json を書く) → ship-tested(deploy の直前に、出荷する木の HEAD が記録と同じか照らす) → deploy。
ship:
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory ship-tested

# テストした木と出荷する木が同じかを照らしてから deploy する。TESTED_TREE の head が、この木の HEAD と違えば deploy せず rc 1
# (「tested <sha> / now <sha>。make test を回してから出荷する」)。ship_approved.py も同じ照合を、make test のあと ship-tested の前に自分で行う。
# 単独で打てるのは、同じ木で make test を済ませた直後だけ(記録が無い・別の HEAD なら拒否される)。
ship-tested:
	$(PY) scripts/tested_tree.py check --file $(TESTED_TREE) --tree $(CURDIR)
	$(MAKE) --no-print-directory deploy

# deploy-check 後の確認用の別名(従来の名前)。check と同じ。
sync-check: check

# スキルと cron の指示文を、リポジトリを正として(置換して)本番へ反映し、差の確認の薄い .sh を dev の scripts/ に置く。
# 本番のスキルと cron の指示文に書くので、deploy と同じく main の作業ツリーからだけ、同じロックで1本ずつ通す。
sync:
	SF_REQUIRE_MAIN_LABEL=sync bash tools/require_main.sh
	$(call DEPLOY_LOCK,sync) $(MAKE) --no-print-directory sync-run

sync-run:
	@$(call HELD_GUARD,sync-run)
	SF_REQUIRE_MAIN_LABEL=sync bash tools/require_main.sh
	$(PY) tools/sync_live.py --apply --config $(ENV_FILE)
	mkdir -p $(DEV_SCRIPTS)

# ---- 手順を覚える必要のある操作(scripts/ops.py を呼ぶだけ。repo は設定から、PR 本文はファイルか stdin。規則は skills/software-factory-ops/SKILL.md)。
# --dry-run や --method などは FLAGS で渡す(例: make pr TITLE=... BODY_FILE=... FLAGS=--draft)。
OPS = SF_CONFIG=$(ENV_FILE) $(PY) scripts/ops.py

# 現在のブランチから PR を作る。未コミット / base と同じブランチ / 未 push / 既存 PR は拒否。
pr:
	$(OPS) pr-create --title "$(TITLE)" --body-file "$(BODY_FILE)" $(FLAGS)

# OPEN の PR をマージする(人が打つ): make merge PR=79
merge:
	$(OPS) pr-merge "$(PR)" $(FLAGS)

# origin/main から .worktrees/<NAME> を切る: make worktree NAME=foo
worktree:
	$(OPS) worktree "$(NAME)" $(FLAGS)

# skills repo の skill_lint を --strict-skills で流す(skills repo の場所は FLAGS="--skills-repo DIR" で変えられる)。
lint-skills:
	$(OPS) lint $(FLAGS)

# 移し先のある手順のコマンド(gh pr create / gh pr merge / git worktree add / kanban create / skill_lint --strict-skills)が、
# 移し先を書かないままスキルにあれば落とす(規則は skills repo の auditing-skills の規則9)。既存の違反はベースラインで許し、新規・悪化だけ落とす。
lint-procedures:
	$(PY) tools/lint_procedures.py --baseline tools/lint_procedures_baseline.json

# 今の違反でベースラインを書き直す(スキルを直したあとにだけ打つ。違反を足すために使わない)。
lint-procedures-baseline:
	$(PY) tools/lint_procedures.py --write-baseline tools/lint_procedures_baseline.json

# cron のジョブ名と schedule の一致、毎分のジョブの RunLock、文(指示文・スキル・SOUL)への周期の書き写しを検査する(規則は tools/lint_cron.py の冒頭)。
# 周期は hermes/cron.toml が正本。文は ${gate_every} / ${planner_every} などの名前で書く。既存の違反はベースラインで許し、新規・悪化だけ落とす。
lint-cron:
	$(PY) tools/lint_cron.py --baseline tools/lint_cron_baseline.json

# 今の違反でベースラインを書き直す(文を直したあとにだけ打つ。違反を足すために使わない)。
lint-cron-baseline:
	$(PY) tools/lint_cron.py --write-baseline tools/lint_cron_baseline.json

# 登録の test_fast(と、変えたパスに合う重い検査 make test-paths)を回し、作業メモに貼る1行(コマンド・rc・壁時計と CPU 時間)を出す。登録の無い repo は既定の make test。
verify:
	$(OPS) verify $(FLAGS)

# 全環境の render を dry-run で通す(1環境でも落ちたら非 0)。
render-all:
	$(OPS) render-all $(FLAGS)

# ---- インストーラ(tools/factory.py。標準ライブラリのみ)。どれも環境は ENV(envs/<ENV>.toml)。--dry-run は FLAGS=--dry-run で渡す(例: make cron-sync FLAGS=--dry-run)。
FACTORY = $(PY) tools/factory.py
FLAGS ?=

# 新しい環境の初期化: プロファイル(executor / reviewer)、kanban ボード、cron の薄い .sh、cron ジョブ(無いものは --paused で作る)。
# Notion の DB は別(make notion-bootstrap)。稼働中の環境で使うときは、先に make cron-pause と cron-adopt をする(cron-sync は差のあるジョブを編集する)。
init: profiles board wrappers cron-sync

# 環境の検査。足りないものを並べ、1つでもあれば終了コード 1。
doctor:
	$(FACTORY) doctor --config $(ENV_FILE)

# 本番の3つの DB を読んで notion/schema.json を作り直す(読むだけ)。
notion-export-schema:
	$(FACTORY) notion-export-schema --config $(ENV_FILE) $(FLAGS)

# notion/schema.json から3つの DB を作り、data source ID を envs/<ENV>.toml に書き戻して git commit する。
notion-bootstrap:
	$(FACTORY) notion-bootstrap --config $(ENV_FILE) $(FLAGS)

# executor / reviewer(あれば作らない)。既存の差も直すなら FLAGS=--update。approvals.smart_policy の設定はエージェント経由では拒否される設定があるので、利用者のシェルで実行する。
profiles:
	$(FACTORY) profiles --config $(ENV_FILE) $(FLAGS)

# kanban ボードと cron プロファイルの kanban.review_dispatch(あれば何もしない)。
board:
	$(FACTORY) board --config $(ENV_FILE) $(FLAGS)

# cron の薄い .sh を hermes/wrapper.sh.tmpl から cron プロファイルの scripts/ に置く。
wrappers:
	$(FACTORY) wrappers --config $(ENV_FILE) $(FLAGS)

# 稼働中の cron ジョブのIDを取り込む(<live>/cron_ids.json): make cron-adopt IDS="intake=370ce6b41322 gate=126625ee9870 ..."
cron-adopt:
	$(FACTORY) cron adopt --config $(ENV_FILE) $(IDS)

# hermes/cron.toml と本番の cron ジョブを比べて直す(無いジョブは --paused で作る)。
cron-sync:
	$(FACTORY) cron sync --config $(ENV_FILE) $(FLAGS)

# cron_ids.json のジョブだけを一時停止 / 再開する。
cron-pause:
	$(FACTORY) cron pause --config $(ENV_FILE) $(FLAGS)

cron-resume:
	$(FACTORY) cron resume --config $(ENV_FILE) $(FLAGS)

# 読み取りだけの動作確認(ゲート制御の試し実行 + 監視スクリプト)。
smoke:
	$(FACTORY) smoke --config $(ENV_FILE) $(FLAGS)
