# 行＝カード（PR #59）を script・lint・config で担保する形に組み直す設計

適用したスキル: writing-plans の「ファイルと検査を先に決め、手順は依存順で、各段に落ちる検査を付ける」と、verification-before-completion の「数字はこの場で実測したものだけ書く（下の『実測』はすべて 2026-11-12 に自分で回した結果）」を使った。

## 0. 実測した事実（この設計の根拠）

| # | 事実 | 確かめ方 |
|---|---|---|
| F1 | **#59 の L2 は判定書の検査で落ちる。** `JUDGEMENT_CHAPTERS` は8章固定のまま。`release_judgement_l2.md`（5章）を `judgement_sections()` に通すと「無い章」が4つ返る: 残っている危険・未確認・承認のあとに起きること・戻し方 | ブランチの worktree で `C.judgement_sections(l2の雛形)` を実行 |
| F2 | 「L2 のレビュー行は6章、L3 は9章」は `prompts/review_apply.txt` の文にしか無い。`ask_human.py` にはレベルの分岐が無く、いつも9章で作る | `grep -i level scripts/ask_human.py` で0件 |
| F3 | levels-and-rows.md の「カード本文は置換後 2,800字まで」は、すでに守られていない。ブランチの golden の最大は 3,167字（main は 3,746字）。`skill_lint --strict-skills` は雛形のサイズを警告にしかしない | golden_bodies.json の長さを数えた／skill_lint.py の 148〜152行 |
| F4 | 「差し戻しは3回まで」を数えるコードはどこにも無い | scripts/tools を grep |
| F5 | 設定値が文に書き写されている（ブランチ）。cron の周期は cron.toml の外に **37か所**、行の題（設計〜リリース判定など）**45か所**、章の数 **26か所**、`make test`／`test-fast`／`test-paths` が **82か所** | `grep -rnoE` で数えた |
| F6 | `release_judgement_l2.md` は配布の一覧に手で足した。`test_deploy_list.py` は `scripts/*.py` しか見ないので、雛形の足し忘れは検査で止まらない | test_deploy_list.py を読んだ |
| F7 | ゲート制御は `kanban_create_args()` で `hermes kanban --board … create` を自分で組んでいる。今日の `create_card.build_args()` とやっていることが同じ | 両方のファイルを読んだ |
| F8 | **今日の main** の `make test` は 23工程で合計 **128.0秒**。重い4つが 96.1秒（そのうち test_safe_deploy.sh だけで 78.9秒）、残りが **31.9秒**。今日足した test_ops.py（8.0秒）が入るので、test-fast は #59 が測った約18秒ではなく、**約32秒** になる（上限の60秒には収まる） | main の worktree で各工程を時間を計って実行。23工程すべて rc=0 |
| F9 | #59 を今の main に重ねると、衝突は **17ファイル**（重なるファイルは22）。`token-cost-analysis.md` は main で消えている（変更と削除の衝突） | `git merge-tree --write-tree origin/main origin/michiomochi/row-eq-card` |
| F10 | #59 の差分の内訳。コード（scripts/*.py）は7ファイル +509/−120、テストは5ファイル +705/−4、**文（スキル・指示文・SOUL・docs）は30ファイル +177/−110**、雛形と golden は4ファイル +82/−29、Makefile・設定・配布は5ファイル +74/−14 | `git diff --numstat` を分類して足した |
| F11 | phase_card.md は2つの方向で別々に短くされている。main は 2,999字（#73）、ブランチは 2,423字で、残した文が違う | `wc -m` で数えた |

## 1. #59 がやろうとしていること（全部）

PR 本文・コミット30本・要件書・設計書・判定書から拾った。

| # | 項目 | #59 での置き場所 |
|---|---|---|
| 1 | 行はカード1枚につき1行。題は工程の範囲（区切りは 〜／～／~） | epic_common `row_phases`・`ROW_SEPS` |
| 2 | レベルごとの行の題: L2 `設計〜リリース判定`、L3 `要件定義〜設計`＋`実装〜リリース判定` | `CARD_ROWS`・`LEVEL_PHASES` |
| 3 | 旧形（工程ごとに5行）の判定。MERGE_PHASES は旧形にだけ効き、行の数・キー・journal・題は変えない | `is_legacy`・ゲート制御 |
| 4 | レベルの決め方。DB に `レベル` の列があればそれを読み、無ければ行から決める（行が無ければ L3） | `effective_level`・`epic_level_prop`・`level_order`・`work_order` |
| 5 | ゲート制御を行の単位にする（next_action・start_phase・reconcile・_work・sanity。カードの題・キー・journal は行の題で） | epic_gate_controller.py |
| 6 | レベルを上げたら、足りない行を1回だけ作り、使わなくなった未着手の行を取り消す。下げるほうは作っていない | `level_change_plan`・`apply_level_change`・`create_card_row` |
| 7 | 工程の進み具合は、行のチェックリストの頭 `<工程名>：` で持つ。executor はこの頭を消さない | `split_items`・`range_items`、SOUL・スキルの文 |
| 8 | プランナーの状態出力で、範囲の行をタスクとして数えない。`missing_phase_rows` は「作る行の題」にする | epic_planner_status.py |
| 9 | health を行ごとに数え、警告は行ごとに1回にする | epic_pipeline_health.py `row_cards` |
| 10 | 取り込みは範囲の行と作業の行を拾わない | `is_phase_row`（notion_pending_tasks 経由）、intake.txt の文 |
| 11 | create_row は形の壊れた範囲の題を exit 2 で断る | create_row.py `broken_row_title` |
| 12 | プランナーが行を作る（spec の JSON を書いて create_row を呼ぶ）。0b: 要件がそろっていれば L3 の1行目の項目を写すだけにする | planner.txt の 0・0b の文 |
| 13 | 成果物を7枚から3枚にする（設計メモ・テスト結果・判定書）。旧い名前の項目は、そのページの節として扱う | phase_card.md の文、SOUL・スキルの文 |
| 14 | 判定書に「PR 本文」の章を足し、ship_approved がその章を読む（旧い子ページ「PR 本文の下書き」も読む） | release_judgement.md、ship_approved `pr_chapter` |
| 15 | 判定書の章の数: L2 は5章、L3 は8章 | release_judgement_l2.md（新規）、SOUL の文 |
| 16 | 人が見るレビュー行の章の数: L2 は6章、L3 は9章 | review_apply.txt の文だけ（F2） |
| 17 | 同じ行を1回しか読まない（collect_release_facts・read_inputs） | epic_common・ship_approved |
| 18 | テストを2段に分ける: test-fast（60秒以内）、test（test-fast＋重い4つ）、test-paths（変えたパスに合う重い検査）、heavy-* | Makefile の TEST_PATHS_TABLE（シェル） |
| 19 | test-fast の最後の行に、回していない重い検査の名前を出す | Makefile の echo |
| 20 | 出荷では、マージ後の木がテスト済みの木と同じなら `make ship-tested`（TESTED_TREE で照合）を使い、テストを回し直さない | Makefile の TREE_GUARD、ship_approved |
| 21 | 設定の `ship.repos.*.test_fast` を足す | envs/michiomochi-wsl.toml・sfconfig |
| 22 | カードと reviewer は `make test-fast` を回す（L3 は `make test-paths` も）。`make test` は出荷で1回だけ | phase_card・SOUL・スキルの文 |
| 23 | cron の周期: ゲート制御は毎分（`* * * * *`）、プランナーは1分ごと。ジョブ名の「（N分）」も直す | hermes/cron.toml、test_install |
| 24 | AI のレビューはカードの最後に1回。差し戻しは3回まで（L2 は1回、L3 は各カードで1回） | SOUL・スキルの文だけ（F4） |
| 25 | reviewer は合格のときにカードの行を完了にする。旧形なら本文に並んだ行を全部完了にする | reviewer SOUL の文 |
| 26 | reviewer が確かめること（L2 の①〜⑤、L3 はそれに要件とテストの対応、戻し方） | reviewer SOUL の文 |
| 27 | phase_card.md を短くし、「成果物は3枚」の文を入れる。golden 27件を置き換える。「置換後 2,800字まで」と書く | 雛形・golden・levels-and-rows.md |
| 28 | 配布の一覧に release_judgement_l2.md を足す | safe_deploy.sh |
| 29 | selftest 15項目、ship_selftest 4項目、test_makefile 10項目、test_ship_e2e | 各 selftest |
| 30 | 文書: design.md の G9・G10、README・INSTALL・CONTRIBUTING、新しい参照 levels-and-rows.md | docs・スキル |
| 31 | 指示文の書き直し: planner・intake・health・review_apply | prompts/ |
| 32 | L1 のために名前だけ置いておく: WORK_ROW、CARD_ROWS['L1']、LEVEL_PHASES、effective_level、epic_level_prop | epic_common |
| 33 | 「行＝カードは1本のブランチで入れる」という運用の規則 | working-discipline.md の文 |

## 2. 分類（script／lint／config／skill）

分類の決め方:
- 値は1か所に置く。環境で変わる値は `envs/*.toml`。環境で変わらない定義は新しい `scripts/levels.toml` と、今ある `hermes/cron.toml` に置く。レベルの定義を envs に置くと wsl と mac に同じものが2つでき、ずれる。
- 文（スキル・SOUL・指示文・雛形）は値を書き写さず、render の `${…}` で受け取る。
- skill に残すのは、機械では決められない判断の基準だけ。

| # | 項目 | 分類 | 実装先（ファイル名） | 移せない理由／検査の内容 |
|---|---|---|---|---|
| 1 | 題から工程を引く | script（ブランチのコードを移す） | scripts/epic_common.py `row_phases` | 検査: selftest 1。lint_levels が「設定にある行の題はすべて row_phases で読める」ことを見る |
| 2 | レベルごとの行の題 | **config** | **scripts/levels.toml**（新規）`[L2] rows`・`[L3] rows`。`CARD_ROWS` は epic_common がここから読む | lint_levels: ① レベルの行の工程を ORDER の順につなぐと、抜けも重なりも無く、そのレベルの工程の並びと同じ ② どの題も row_phases で読める ③ create_row の `broken_row_title` に当たらない |
| 3 | 旧形の判定と MERGE_PHASES | script | epic_common `is_legacy`、ゲート制御 | 検査: selftest 5・15。**golden 27件を1文字も変えない**ことを合格条件にする（§4。phase_card.md は1文字も変えない） |
| 4 | レベルの決め方（列が無ければ行から） | script | epic_common `effective_level` | 検査: selftest。L2 と L3 のどちらにするかは skill に残す。理由: 依頼の大きさは、要件がどれだけ決まっているかを読んで決めるもので、入力から機械的に決まらない（判断の基準だけを書く） |
| 5 | ゲート制御を行の単位にする | script | epic_gate_controller.py | 検査: selftest 3・4・8・12 |
| 6 | レベルを上げたときの行 | script | ゲート制御 `apply_level_change`。行は create_row の関数で作る（#12 と同じ道） | 検査: selftest 13。**下げたときは黙らず、health が ALERT を出し、ゲート制御は何もしない**（今は何も起きない＝気づけない） |
| 7 | チェックリストの頭 `<工程名>：` | script＋lint | 頭の付いた項目は create_row が `levels.toml` の `items` から書く。消えたら health が ALERT を出す。完了にする前の検査は #25 の自動化に入れる | SOUL の「頭を消さない」の文は消す。検査: 頭の無い項目がある行は完了にならず、ALERT が1回出る |
| 8 | プランナーの状態出力 | script | epic_planner_status.py | 検査: selftest 10 |
| 9 | health を行ごとに | script | epic_pipeline_health.py | 検査: selftest 11 |
| 10 | 取り込みで行を拾わない | script | `is_phase_row`（今の道） | intake.txt の行の題の列挙は消す（拾わないのは監視のスクリプトが済ませている）。検査: selftest 6。skill_lint の新しい検査7が、指示文に題が書かれたら落とす |
| 11 | 壊れた題を exit 2 | script | create_row.py | 検査: selftest 14 |
| 12 | 行を作るのはプランナー（AI が spec を書く） | **script（AI から外す）** | ゲート制御が、行の無いプロダクト開発のエピックを見たら、`levels.toml` の題と項目から create_row の関数で行を作る（レベルを上げたときと同じ関数）。0b の (a) 目的の表が埋まっている、(b) 打ち手のある候補がある、はスクリプトで判定して `req_ready=yes/no` を出す | 0b の (c)「範囲と受け入れ条件がはっきりしている」だけは skill に残す。理由: 文の中身が十分かは意味を読まないと判定できない。検査: fake の世界で、行の無いエピックを1回回すと行が L2 なら1つ・L3 なら2つでき、2回目は0。planner.txt の手順 0 は消す |
| 13 | 成果物は3枚 | config＋script | `levels.toml` の `deliverables = ["設計メモ","テスト結果","判定書"]`。ゲート制御が行を始めるときに、3枚を空の雛形で子ページとして先に作り、リンクは、ゲート制御がカード本文の今の `${parts}` の末尾に足す（範囲の行のときだけ） | 「成果物は3枚」の文は消す。検査: 始めた行の子ページの題が config と完全に同じで、2回目の実行で増えない |
| 14 | 判定書の「PR 本文」の章 | script（移す） | ship_approved `pr_chapter`。ドラフト PR を開く前に precheck.py の印の検査（Notion の URL や DB の ID が無いこと）を、取り出した章に掛ける | 検査: ship_selftest。わざと章に Notion の URL を入れると、PR を開かずに HOLD になる |
| 15 | 判定書の章の数（L2 5章／L3 8章） | **config＋lint** | `levels.toml` の `[L2] judgement_chapters`・`[L3] …`。`JUDGEMENT_CHAPTERS` を `judgement_chapters(level)` にし、ask_human --release はレベルで検査する | lint_levels: 雛形の H1 の並びが config と同じ。**今の #59 はここで落ちる（F1）** |
| 16 | レビュー行の章の数（L2 6章／L3 9章） | config＋script | `levels.toml` の `review_chapters`。ask_human がレベルで作る。review_apply.txt は数を書かず、行の H1 をそのまま読む | 検査: fake で L2 の承認依頼を作ると H1 が config と同じ並びになる。作らないことに決めるなら、文からも消す（文だけにある規則は検査7で落ちる） |
| 17 | 同じ行を1回しか読まない | script（移す） | epic_common・ship_approved | 検査: selftest 9 |
| 18 | テストの2段化と test-paths | **script＋config** | **tools/tests.toml**（新規）: テストごとに名前・コマンド・`tier = "fast"/"heavy"`・`paths`（heavy のときに走らせるパスの glob）。**tools/test_plan.py**（新規）が読んで回す。Makefile の test・test-fast・test-paths はこれを呼ぶ1行だけにする（Makefile にシェルの表を書かない。lint_bash32 は Makefile を見ないため） | 検査: ① tools/test_*.py・*.sh、scripts/*_selftest、`--selftest` を持つスクリプトが、すべて tests.toml に載っている（test_deploy_list と同じ形） ② fast の CPU 時間（子プロセスの user＋sys）の合計が60秒を超えたら落とす。壁時計の時間は使わない（125〜144秒の外れ値は、ほかの作業と CPU を取り合った待ちだった） |
| 19 | 回していない重い検査を名指しする | script | test_plan.py が tests.toml から出す | Makefile の echo の手書きは消す |
| 20 | ship-tested（木の照合） | script（移す） | Makefile の TREE_GUARD・ship_approved | 検査: test_makefile の既存2件（同じ木なら1回、違う木なら断る） |
| 21 | `test_fast` の設定 | config | envs/*.toml `ship.repos.*.test_fast` | lint_envs: `test` があるのに `test_fast` が無い、または文字列で書いた → 落とす |
| 22 | カードと reviewer が回すテスト | **script** | **ops.py verify**（新しいサブコマンド。ops.py はすでに本番に配られている）。repo の登録の `test_fast` を回し、続けて `test_plan.py --paths`（変えたパスに当たらなければ0秒）を回して、作業メモに貼る1行を出す。登録の無い repo では何を回したかを書いて、既定のコマンドを回す | **L2 と L3 で分けない**（test-paths は当たらなければ0秒で、当たれば重い検査が要る）。SOUL・雛形・スキルに `make test-fast` を書かず、「`${verify}` を回す」の1か所にする。検査7が `make test(-fast\|-paths)` の書き写しを落とす |
| 23 | cron の周期 | config＋lint | hermes/cron.toml（値はここだけ）。**tools/lint_cron.py**（新規）: ① ジョブ名の「（N分）」が schedule と合う ② 毎分のジョブは、そのスクリプトが RunLock を取る ③ test_install は値を cron.toml から読む（手で書かない） | 文の中の周期は `${gate_every}`・`${planner_every}` で受け取る。37か所あった書き写しは検査7で0になる |
| 24 | レビューの回数（最後に1回、差し戻しは3回まで） | **config＋script** | `levels.toml` の `max_review_rounds = 3`。health（またはゲート制御）が task_runs の reviewer の回数を数える（数え方は epic_pipeline_timing.py にもうある）。超えたらカードを block し、ALERT を1回出す | 「工程の境目ではレビューしない」は行＝カードの形そのもので決まる。途中で request_review されても #25 の検査で行は完了にならない。SOUL の文は消す |
| 25 | 合格したら行を完了にする（旧形なら全部の行） | **script（AI から外す）** | ゲート制御の reconcile: カードが done になり、task_runs に reviewer の合格がある → journal にあるそのカードの行をすべて完了にする（同じことを2回しない）。完了にする前に次の4つを見る: ① 頭付きの項目がすべてチェック済み ② remaining_work_check.py が完了レポートを通す ③ リリース判定を持つ行なら `ask_human --release --dry-run` に PROBLEMS が無い ④ UI の根拠 | reviewer の SOUL から「行を完了にする」を消す。**要確認**: kanban の done が reviewer の承認でしか起きないか（executor が自分で kanban_complete した場合は task_runs で見分ける） |
| 26 | reviewer が確かめること ①〜⑤ | 大半は script、2つは skill | ①テスト → `ops.py verify` を reviewer も回す。③完了レポートにコマンドと出力 → #25 の②（remaining_work_check を「コマンドと出力のコードブロックがある」まで広げる）。④ PROBLEMS → #25 の③。⑤のうちテスト結果の形・判定書の章・UI の画像 → script | skill に残すのは2つ。②「差分が目的と完了条件に合っているか」と、⑤のうち「設計メモと差分が合っているか」。理由: どちらも意味が合っているかの判断で、文字列を照らし合わせても決まらない |
| 27 | カード本文の短縮と golden | **lint（今日の削減を固定）** | skill_lint: 雛形が3,000字を超えたら**警告ではなく失敗**にする。golden の最大長に上限を置き（今は main の最大 3,746 字）、上げる変更は落とす（下げたら上限も下げる） | 「置換後 2,800字」の文は消し、上限は skill_lint の定数の1か所にする。**phase_card.md は触らない**（2,999字なので、`${…}` を1つ足しても上限の3,000字を超える）。範囲の行で要る文（成果物3枚へのリンクと `${verify}`）は、ゲート制御が今の `${parts}` の末尾に足す。旧形では足さないので golden は変わらない |
| 28 | 配布の一覧に雛形を足す | lint | tools/test_deploy_list.py を `scripts/templates/*.md`・`scripts/tests/*.json`・`scripts/*.toml` まで広げる | 検査: 一覧に無い雛形を足すと落ちる（F6 は手で足していた） |
| 29 | selftest 15項目ほか | script（移す） | epic_selftest・ship_selftest・test_makefile（test-paths の分は test_plan のテストに移す） | — |
| 30 | design.md の G9・G10、README など | skill（人向けの文） | docs/design.md | 理由: 「なぜこの形にしたか」は人が読む説明で、実行はされない。値は書かずに levels.toml を指す（検査7は docs/ にも掛ける） |
| 31 | 指示文の書き直し | 削る | prompts/ | #12・#22・#23・#25 をスクリプトにすると、書き直すはずだった段落の多くが要らなくなる。残るのは「何を判断するか」だけ |
| 32 | L1 のために名前だけ置く | **入れない** | — | YAGNI。L1 は levels.toml に `[L1]` を1節足せば入る形にする。今は使わない名前を置かない |
| 33 | 「1本のブランチで入れる」 | lint（もうある） | selftest 6（取り込み）が同じ木で通ること | 文は消す。行のコード（#5・#8・#9・#10）は1つの PR に入れる（§5 の段6） |

**数（33項目。1項目が2つの分類に入ることがある）:** script を含む 23・config を含む 8・lint を含む 6・削るか入れない 2・**skill 4**（#4 のレベルを選ぶ基準、#12 の (c)、#26 の②と⑤の一部、#30 の設計の理由）。どれも「意味を読まないと決まらない」ものだけで、手順・禁止・チェックリストは skill に残さない。

### 新しい lint: skill_lint の検査7「設定値を文に書き写さない」

新しい道具は作らず、今の `tools/skill_lint.py`（Makefile に「スキルと指示文の規約を見る唯一の道具」とある）に1つ足す。対象は `skills/**`・`prompts/*.txt`・`hermes/profiles/*/SOUL.md`・`scripts/templates/*.md`・`docs/*.md`。
- 落とすもの: cron の周期（`every Nm`、`N分毎`、`（N分）`）、levels.toml にある行の題、章の数（`N章`、`six/nine chapters`）、`make test`／`test-fast`／`test-paths`、成果物の枚数（`THREE`、`3枚`）
- 書いてよい形: `${gate_every}`・`${l2_rows}`・`${verify}` など、render が設定から入れる名前
- 入れ方: 最初は今ある件数を上限として記録し、増えたら落とす。文を直すたびに上限を下げ、最後の段（段10）で0にする

## 3. 今日入れたものと重ねない（統合案）

| #59 側／新しく要るもの | 今日入れたもの | 統合のしかた |
|---|---|---|
| ゲート制御の `kanban_create_args`（F7） | `create_card.build_args`（board を設定から取る） | build_args を、カードを作る唯一の関数にする。ゲート制御はこれを import する。test_create_card に「scripts/ の中で `kanban … create` を組むのは create_card.py だけ」を足す（AST の list の中身を見る） |
| プランナーの「spec を書いて create_row」と `create_card_row`（レベルを上げたとき） | create_row.py | `create_row.create_epic_rows(epic, titles)` の1つにする。最初の行もレベルを上げたときの行も、ゲート制御がこの関数で作る |
| Makefile の TEST_PATHS_TABLE（シェル）と heavy-* | ops.py・Makefile の配線の検査 | 表は tools/tests.toml に、回すのは tools/test_plan.py にする。heavy-* のターゲットは作らない。test_makefile は「test・test-fast・test-paths が test_plan.py を呼ぶ」ことと ship-tested の照合だけを見る |
| カードと reviewer の `make test-fast` | `ops.py`（手順の要る操作をスクリプトにしたもの） | `ops.py verify` を足す。新しいスクリプトは作らない |
| `make test` の中の `factory.py render --all-envs --dry-run` | `ops.py render-all`（中身は同じコマンド） | make test は `ops.py render-all` を呼ぶ。中身を2か所に書かない |
| `test_fast` の設定の検査 | lint_envs.py の 1（必須キーと型） | lint_envs に足す。sfconfig._ship_problems の型の検査はそのまま使う |
| release_judgement_l2.md を手で一覧に足す | test_deploy_list.py | 雛形・JSON・toml まで広げる（新しい検査は作らない） |
| 「雛形 3,000字」の警告 | skill_lint の検査6 | 失敗にする。golden の上限もここに置く |
| 完了の前の検査（#25） | remaining_work_check.py | ゲート制御が完了にする前にこれを呼ぶ。「コマンドと出力のブロックがある」を同じファイルに足す |
| PR 本文の章に非公開の印が混ざる | precheck.py の `markers()` | ゲート制御がドラフト PR を開く前に同じ関数を通す |
| 雛形の新しい `${…}`（`${verify}`・`${gate_every}` など） | lint_dollar.py・sfconfig.TEMPLATE_NAMES・lint_envs の3（名前の重複） | 名前を TEMPLATE_NAMES に足すだけ。検査は今のものが効く |

## 4. #59 のブランチをどうするか

**推奨: (b) ブランチは捨て、今日の main から script・lint 中心の新しいブランチ（`michiomochi/row-eq-card-v2`）で作り直す。#59 のコードとテストは、関数の単位で写して使う。**

理由（数字で）:
1. **衝突17ファイルのうち12は、新しい設計で消すか、別の形にする文と雛形**（CONTRIBUTING 1、autonomous-task-execution の SKILL.md 1 と references 8、phase_card.md 1、golden 1）。(a) で解いても、段10でほぼ全部消す。手間をかけて解くほど、#71（79,686→29,731字）と #73（5,226→2,999字）を巻き戻す危険が増える。`token-cost-analysis.md` は main で消えていて、解くと生き返る。
2. **使い回せるのは差分の 1,214 行（コード +509、テスト +705）。** そのうち衝突するコードは epic_common.py と epic_gate_controller.py の2ファイルだけ。関数ごとに今の main へ写せば、ほかは衝突しない。
3. **phase_card.md は 2,423字（ブランチ）と 2,999字（main）で別の方向に削られている（F11）。** 新しい設計では phase_card.md を1文字も変えず、範囲の行で要る文はゲート制御が `${parts}` に足すので、golden の書き換えは **27件から0件** になる。判定書の完了条件②（旧形は今のまま）も「一部」から「達成」になる。
4. **#59 は承認されても L2 では動かない（F1）。** JUDGEMENT_CHAPTERS と ask_human のコードを直す必要があり、どちらにしても「文を直したらコードも直す」作り直しになる。新しい設計では、章を levels.toml と lint_levels に置いて、ずれたら落ちる形にする。
5. **#59 は51ファイルの1つの PR だった。** 新しいブランチでは §5 の段ごとに PR を分ける（1本で入れる必要があるのは段6の行のコードだけ）。

扱い: ブランチと PR #59（下書き）は消さずに残す。判定書は「承認」を推奨しているので、レビュー行（89cafadc-承認）を「差し戻し」か「取り下げ」にするのは**みちゃん**（AI はレビュー行を書き換えない）。理由の文には、この §0 の F1・F9 と §4 を使ってよい。

## 5. 実装の順番（依存の順）

各段は1つの PR。どの段も `make test` が緑で、下の「わざと壊す」で落ちることを確かめてから次へ進む。

| 段 | 作るもの | 何を検査するか | 前に要る段 |
|---|---|---|---|
| 0 | 今日の削減を固定する: skill_lint の雛形の上限を失敗にし、golden の最大長に上限を置く。test_deploy_list を雛形・JSON・toml まで広げる | 雛形の字数、golden の最大長、配布の一覧の漏れ | なし |
| 1 | `scripts/levels.toml` と、sfconfig の読み込み、render の名前（`${l2_rows}`・`${deliverables}` など）。**tools/lint_levels.py** | 行が工程を抜けも重なりも無くつなぐ。題が row_phases で読める。判定書の雛形の H1 が config と同じ | 0 |
| 2 | **tools/lint_cron.py**。render の `${gate_every}`・`${planner_every}`。test_install は値を cron.toml から読む | ジョブ名と schedule が合う。毎分のジョブが RunLock を取る | なし |
| 3 | skill_lint の検査7（書き写し禁止）。今の件数を上限として記録する | 周期・題・章の数・テストのコマンド・枚数の書き写し | 1・2 |
| 4 | **tools/tests.toml**・**tools/test_plan.py**。Makefile の test・test-fast・test-paths。`ops.py verify`。lint_envs に test_fast | 登録漏れ。fast の CPU 時間が60秒以内。パスで重い検査を選ぶ | 0 |
| 5 | ship-tested（#59 の TREE_GUARD と ship_approved の木の照合を移す） | 同じ木なら1回、違う木なら断る | 4 |
| 6 | 行のコード（#1〜#11・#17、selftest 1〜15）。CARD_ROWS は levels.toml から読む。create_card.build_args にまとめる。行はゲート制御が `create_epic_rows` で作る（プランナーの手順0は消す） | selftest。`kanban create` を組むのが1か所だけ。行が1回だけできる | 1・4 |
| 7 | レベルごとの判定書と承認依頼（JUDGEMENT_CHAPTERS をレベルごとに、release_judgement_l2.md、ask_human、review_chapters）。`pr_chapter` と precheck の印。範囲の行では `${parts}` に成果物のリンクと `${verify}` を足し、成果物3枚をゲート制御が先に作る | L2 の判定書が PROBLEMS 0。golden の差分が0件。成果物の題が config と同じ | 1・6 |
| 8 | 完了の自動化（#25）とレビューの回数（#24）。下げたときの ALERT（#6） | 合格→行が完了（1回だけ）。合格なし・頭なし・PROBLEMS ありでは完了しない。4回目のレビュー依頼で block | 6・7 |
| 9 | cron の値だけを変える（ゲート制御 `* * * * *`、プランナー every 1m） | lint_cron。値だけの PR なので、戻すのも1行 | 2 |
| 10 | 文を削る: スクリプトにした規則を skills・SOUL・prompts から消し、skill には判断の基準（§2 の skill 4件）だけを残す。検査7の上限を0にする | 検査7が0件。この PR で skills の総字数が増えない（減るだけ） | 3〜9 |

## 6. 合格条件（効いていると言える状態と、わざと壊して落ちる例）

| 段 | 効いていると言える状態 | わざと壊す例 → 期待する落ち方 |
|---|---|---|
| 0 雛形の上限 | `make test` が skill_lint を「雛形 2,999 字／上限 3,000」で通す | phase_card.md に2文字足す → `カード雛形が 3,001 字（上限 3,000）`、rc=1 |
| 0 golden の上限 | golden の最大長が記録した上限以下 | 雛形に1行足して golden を作り直す → 最大長が上限を超えたと出て rc=1 |
| 0 配布の一覧 | 雛形・JSON・toml がすべて「配る」か「配らない」に入っている | `scripts/templates/foo.md` を作って一覧に足さない → `本番に配られない雛形: templates/foo.md`。#59 のブランチで一覧から release_judgement_l2.md を外しても同じく落ちる |
| 1 lint_levels | 今の levels.toml で0件 | L3 を `要件定義〜設計`・`テスト〜リリース判定` にする → `L3: 実装 がどの行にも無い`。題を `設計-リリース判定` にする → `row_phases が読めない`。**#59 の release_judgement_l2.md を今のまま置く → `L2: 雛形の章が config と違う`**（F1 を検査で捕まえる） |
| 2 lint_cron | 名前と schedule が合い、毎分のジョブが RunLock を持つ | 名前を `（1分）` のまま schedule を `every 2m` にする → `jobs.gate: 名前は1分、schedule は2分`。ゲート制御の RunLock の呼び出しを消す → `毎分のジョブ gate がロックを取らない` |
| 3 検査7 | 記録した上限以下（段10で0件） | スキルに「プランナー（5分毎）」と書く → `file:行: 周期の書き写し。${planner_every} を使う`。SOUL に `make test-fast` と書く → `${verify} を使う` |
| 4 tests.toml | 登録漏れ0件。test-fast の最後に CPU 時間と回していない重い検査が出る。今日の main では壁時計で約32秒の見込み（F8） | `tools/test_new.py` を作って登録しない → `tests.toml に無いテスト: tools/test_new.py`。fast のテストに CPU を回し続ける処理を入れる（テスト用の環境変数で） → `fast の CPU 時間が 60 秒を超えた` |
| 4 test-paths | `PATHS=README.md` → 「該当なし」で rc=0。`PATHS=tools/safe_deploy.sh` → deploy_guard と safe_deploy だけが回る | tests.toml の paths に、どのテストにも無い名前を書く → 読み込みで落ちる |
| 4 ops verify | 登録のある repo で test_fast と test-paths を回し、作業メモに貼る1行を出す | `test_fast = "make test-fast"`（文字列）にする → lint_envs が落とす。登録の無い repo → 回したコマンドの名前を出し、既定のコマンドを回す |
| 5 ship-tested | test_makefile の2件が通る | TESTED_TREE に別の head を渡す → `出荷を拒否します: tested <sha12> / now <sha12>。make test を回してから出荷する` で deploy しない（実装: `make test` の最後が `scripts/tested_tree.py write`、`make ship-tested` と ship_approved.py が deploy の直前に照合。`tools/test_tested_tree.py`・test_makefile・ship_selftest・test_ship_e2e E6） |
| 6 行のコード | selftest 1〜15 が通る。fake の世界で、行の無い L2 のエピックを1回回すと行が1つ・カードが1枚でき、2回目は何も作らない | gate controller に `--board` の無い `kanban create` を書く → test_create_card が落ちる。levels.toml の L2 を2行にする → selftest 3「L2 は1行でカード1枚」が落ちる |
| 7 判定書 | L2 の判定書の雛形をそのまま埋めると `ask_human --release --dry-run` の PROBLEMS が0件。**golden_bodies.json の差分が0件** | L2 の判定書から「PR 本文」の章を消す → `未確認：PR 本文の章がありません`。PR 本文に Notion の URL を入れる → PR を開かずに HOLD |
| 8 完了の自動化 | fake で、reviewer が合格 → 行が完了（何回回しても1回だけ）。旧形のカードなら本文の行がすべて完了になる | reviewer の合格なしで done にする → 完了にせず ALERT。頭の無い項目を1つ残す → 完了にせず ALERT。PROBLEMS を残す → 完了にしない。L2 で4回目の request_review → block と ALERT が1回 |
| 8 下げたとき | L3→L2 で ALERT が1回出て、行は1つも変わらない | ALERT を消すと、selftest「下げても黙らない」が落ちる |
| 9 cron | 本番で `hermes cron list` の gate が `* * * * *`、planner が `every 1m`。`epic_gate_status.json` の last_success が1分ごとに進む | cron.toml を変えずにジョブ名だけ変える → lint_cron が落ちる |
| 10 文を削る | 検査7が0件。この PR の skills の差分は削除だけ。skill に残る規則は §2 の4件 | 消した規則を1つ文に戻す（例:「差し戻しは3回まで」）→ 検査7が `levels.toml の max_review_rounds を書き写している` で落ちる |

出荷したあとに見る数（効いたかどうか）: 最初の新しい L2 エピックで、① プランナーが行のために書いた字数が0（今は 5,274字）、② 行ができるまでがゲート制御の1回（1分以内）、③ カード本文が golden の上限以下、④ 行を完了にしたのがゲート制御だけ（reviewer の Notion の書き込みが0件）。

## 段10で決めたこと

- L2 の成果物は 要件書・設計書・判定書の草稿 の3枚（§2 #13 の「設計メモ・テスト結果・判定書」から、名前を今の工程の成果物に合わせた）。見積もり・実装メモ・テスト計画と結果は作業メモ、PR 本文は判定書の「PR 本文」の章（L2 の `release_judgement_l2.md`）から ship_approved が下書き PR を組む。L3 と旧形は従来どおり。
- phase_card.md は成果物の名前を書かない（検査7）。lint_levels の「名前が phase_card.md にある」を、「カードへリンクを渡す道（range_note の `${links}`）がある」に替えた。golden は §4 の「書き換え0件」から、phase_card.md の2文の同じ文字列置換（27件）に変わり、最大長は 3,746 から 3,710 字に下がった（上限の定数も下げた）。
- 機械が担保する規則は文から消し、skill に残すのは判断の基準4つ（product-development-process の「Judgement criteria」）。selftest の「skills に main だけで deploy の文がある」は、Makefile の `require_main.sh` を見る検査に替えた。

## 未確認

- ~~kanban の done が reviewer の承認でしか起きないか（段8の前提）~~ → **段8で確かめた（2026-11-12、この機械の kanban の dev ボード `kanban.db` を読み取りだけで数えた）。** done 88件のうち、reviewer の `outcome = completed` の run が無いものは 0 件。executor の run に `outcome = completed` は 0 件で、executor の `kanban_request_review` は `outcome = review_requested`、reviewer の差し戻しは `outcome = changes_requested`、合格は `outcome = completed`。したがって完了の判定は「カードが done かつ、最後の request_review より後に reviewer の `completed` の run がある」にした（`epic_common.kanban_reviews` の `passed`）。done だけで run が無いカード（人が done にした・executor が自分で完了にした）は合格と見ず、行を完了にせず ALERT する。
- test-fast の CPU 時間の実際の値は測っていない（測ったのは壁時計の時間だけ）。段4で測り、上限の60秒が妥当かを見直す。
- L2 のレビュー行を本当に6章にするかは、プロダクトとして決めること（§2 #16）。決めなければ文からも消す。
