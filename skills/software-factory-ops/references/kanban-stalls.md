# カードが動かない・止まる（kanban の落とし穴）

SKILL.md の索引から: カードが spawn されない、`respawn_guarded` が並ぶ、クォータ切れの後に回復しない、カードがどこまでやるか知りたいとき。

- **カードがどこまでやるかは本文の「対象」で決まる**。調査だけを書いた行はコードを変えずに終わる（異常ではなく、実装は同じエピックの工程カードで進む）。進捗を答えるときは行のステータスだけで判断せず、`hermes kanban --board ${board} ls` で `epic-*` の工程カードも追う。
- **`respawn_guarded {'reason': 'blocker_auth'}` が並ぶのは、カードではなくプロバイダ側の停止**（クォータ切れ・認証失敗）。
  ①`hermes kanban --board ${board} show <id>` の Events を見る（毎分同じ行が並ぶ） ②`grep -c blocker_auth` を**複数のカード**で見る —
  どのカードでも同じなら工場全体、1枚だけならそのカード ③`grep "dispatcher stuck" ~/.hermes/logs/gateway.log | head -1` で停止した
  時刻を出し、警告は5分ごと・tick は分なので開始時刻が分かる ④回復はクォータの窓のリセット待ち。
  **クォータが戻っているのに毎分続くなら、ガードが古い失敗文面で固まっている**（次の項）。
  **カードを respawn し直したり、レビュー経路を別モデルに差し替えて通そうとしない**（レビューは Opus の規約）。
  止まっている間も成果物（push 済みブランチ・子ページ）は残るので、人には「どこまで終わって、何が待ちか」を数字で伝える。
- **rate-limit で requeue したカードは、その文面のせいで二度と spawn されないことがある。** requeue は `tasks.last_failure_error` に
  `pid … exited rate-limited (quota wall) — requeued without counting a failure [owner admission settled failed]` を刻み、
  spawn ガードの `_RESPAWN_BLOCKER_RE` は `quota|rate.?limit|429|403|auth…` に当たるので **`blocker_auth` と誤判定して永久に止める**
  （実測: クォータが戻ったのにレビュー待ち8枚が同時に沈黙し、`task_events` に毎分 `respawn_guarded` だけが増えた）。
  手順: `sqlite3 -readonly ~/.hermes/kanban/boards/${board}/kanban.db "select id,status,substr(last_failure_error,1,60) from tasks where status in ('review','ready');"`
  で全部を並べ、同じ文面が付いていれば `sqlite3 $$DB "update tasks set last_failure_error=NULL where status in ('review','ready') and last_failure_error like '%quota wall%';"`
  → 次の tick（1分）で spawn が始まる。**クォータが戻ったことを先に確かめてから消す**（戻っていなければ空回りが戻るだけ）。
  恒久対策は上流（requeue が hard blocker の文面を残さない／ガードが requeue 由来を除外する）をカードにする。
  下ごしらえ: ボード DB の表は `tasks` / `task_runs` / `task_events` / `task_comments` / `task_links`。イベント本文は `task_events.payload`（`detail` は無い）、
  `tasks` に `updated_at` は無い（代わりに `last_failure_error` / `consecutive_failures`）、run の状態は `task_runs.status` と `outcome`。読みは必ず `sqlite3 -readonly`。
