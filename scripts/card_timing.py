#!/usr/bin/env python3
"""カードの所要を、モデル待ち・ツール実行・固まりに分けて測る（読むだけ）。

なぜ要るか: カードが遅いとき、原因はほぼ「返らないモデル呼び出し」か「ワーカーが自分で
起てた delegate_task」で、ツール実行そのものは数分しかない（実測 2026-11-11: 83 分のうち
33.3 分外れの呼び出し 1 本 + 14.6 分 delegate_task 1 回、ツール実行は 1.9 分）。
`hermes kanban runs` は実行時間しか出さないので、内訳はセッションの生データから作る。
カードとセッションは直接は結ばれていないので、run の開始〜終了に重なる kanban セッションを拾う。

使い方:
  python3 card_timing.py --card t_61656b8a
  python3 card_timing.py --session local-061d8cbcaa --profile executor
  python3 card_timing.py --card t_61656b8a --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERMES = os.path.expanduser("~/.hermes")
HANG_SECONDS = 300.0  # これを超える「ツール結果→次の応答」は固まりと数える


def _ro(path):
    if not os.path.exists(path):
        sys.exit(f"not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def card_runs(card, board):
    db = os.path.join(HERMES, "kanban", "boards", board, "kanban.db")
    con = _ro(db)
    con.row_factory = sqlite3.Row
    task = con.execute("select id, status, assignee from tasks where id=?", (card,)).fetchone()
    if not task:
        sys.exit(f"card not found: {card} (board {board})")
    runs = [dict(r) for r in con.execute(
        "select id, status, profile, started_at, ended_at, outcome from task_runs where task_id=? order by started_at",
        (card,),
    )]
    return dict(task), runs


def sessions_in_window(profile, start, end):
    db = os.path.join(HERMES, "profiles", profile, "state.db")
    con = _ro(db)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
    tcol = "started_at" if "started_at" in cols else "created_at"
    lo = float(start) - 120
    hi = float(end or start) + 120
    rows = con.execute(
        f"select id, {tcol} as t0 from sessions where source='kanban' and {tcol} between ? and ?",
        (lo, hi),
    ).fetchall()
    return [(r["id"], r["t0"]) for r in rows]


def breakdown(profile, session_prefix):
    db = os.path.join(HERMES, "profiles", profile, "state.db")
    con = _ro(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select role, tool_name, timestamp from messages where session_id like ? order by timestamp",
        (session_prefix + "%",),
    ).fetchall()
    if not rows:
        return None
    model_s = tool_s = hang_s = 0.0
    tools = {}
    slow = []
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        dt = float(cur["timestamp"]) - float(prev["timestamp"])
        if dt < 0:
            continue
        if prev["role"] == "assistant" and cur["role"] == "tool":
            tool_s += dt
            tools[cur["tool_name"]] = tools.get(cur["tool_name"], 0) + 1
            slow.append((dt, cur["tool_name"]))
        elif cur["role"] == "assistant":
            model_s += dt
            if dt >= HANG_SECONDS:
                hang_s += dt
    span = float(rows[-1]["timestamp"]) - float(rows[0]["timestamp"])
    return {
        "session": session_prefix,
        "profile": profile,
        "span_s": round(span, 1),
        "model_s": round(model_s, 1),
        "hang_s": round(hang_s, 1),
        "tool_s": round(tool_s, 1),
        "messages": len(rows),
        "tool_calls": sum(tools.values()),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
        "slowest_tools": [{"seconds": round(d, 1), "tool": t} for d, t in sorted(slow, reverse=True)[:6]],
    }


def main():
    ap = argparse.ArgumentParser(description="カードの所要をモデル待ち・ツール実行・固まりに分ける")
    ap.add_argument("--card")
    ap.add_argument("--session")
    ap.add_argument("--profile", default="executor")
    ap.add_argument("--board", default="dev")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.card and not args.session:
        ap.error("--card か --session を指定する")

    out = []
    if args.card:
        task, runs = card_runs(args.card, args.board)
        cand = []
        for r in runs:
            prof = r.get("profile") or args.profile
            for sid, t0 in sessions_in_window(prof, r["started_at"], r["ended_at"]):
                cand.append((abs(float(t0) - float(r["started_at"])), sid, prof,
                             r["id"], r.get("outcome") or r.get("status")))
        cand.sort()
        seen = set()
        for dist, sid, prof, rid, outcome in cand:
            if sid in seen:
                continue
            seen.add(sid)
            b = breakdown(prof, sid)
            if not b:
                continue
            b.update({"card": args.card, "card_status": task["status"], "run": rid,
                      "run_outcome": outcome})
            out.append(b)
        out.sort(key=lambda b: b["run"])
        if not out:
            sys.exit("no kanban session found for this card's runs (yet)")
    else:
        b = breakdown(args.profile, args.session)
        if not b:
            sys.exit(f"no messages for session {args.session}")
        out.append(b)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    for b in out:
        head = (f"card {b['card']} ({b['card_status']}) run {b.get('run')} [{b.get('run_outcome')}]"
                if b.get("card") else f"session {b['session'][:20]}…")
        print(f"{head}  profile {b['profile']}")
        print(f"  合計 {b['span_s']/60:.1f}分 / モデル待ち {b['model_s']/60:.1f}分"
              f"（うち {HANG_SECONDS:.0f}秒超の固まり {b['hang_s']/60:.1f}分） / ツール実行 {b['tool_s']/60:.1f}分")
        print(f"  メッセージ {b['messages']} / ツール呼び出し {b['tool_calls']}  {b['tools']}")
        print("  遅いツール: " + ", ".join(f"{x['tool']}:{x['seconds']}s" for x in b["slowest_tools"]))
        if b["hang_s"] > 0:
            print("  → 固まった呼び出しがある。providers の request_timeout_seconds と STALL の監視を見る")
        print()


if __name__ == "__main__":
    main()
