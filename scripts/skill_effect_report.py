#!/usr/bin/env python3
"""スキルを削る前後で、カードの実行が速くなったかを比べる（読むだけ・決定論）。

カードの所要は `task_runs` とセッションの生データから作る（`card_timing.py` と同じ分解）。
比べる指標: カードの件数 / 所要（中央値・平均）/ モデル待ち / 固まり / ツール実行 /
ツール呼び出し回数 / メッセージ数。**時刻を出力に入れない**ので、同じデータなら同じ文になる。

使い方:
  python3 skill_effect_report.py                      # 既定の前後（11/09–11/11 12:00 / それ以降）
  python3 skill_effect_report.py --before 2026-11-09T00:00 --split 2026-11-11T12:00 --after-end 2026-11-12T12:00
  python3 skill_effect_report.py --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import card_timing as CT  # noqa: E402

HERMES = os.path.expanduser("~/.hermes")


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() if s else None


def cards_between(board, lo, hi):
    db = os.path.join(HERMES, "kanban", "boards", board, "kanban.db")
    con = CT._ro(db)
    con.row_factory = None
    rows = con.execute(
        "select id, title, assignee, started_at, completed_at from tasks "
        "where completed_at is not null and completed_at >= ? and completed_at < ? "
        "order by completed_at",
        (lo, hi),
    ).fetchall()
    return [dict(zip(("id", "title", "assignee", "started_at", "completed_at"), r)) for r in rows]


def window(board, lo, hi, profile="executor"):
    out = []
    for c in cards_between(board, lo, hi):
        _task, runs = CT.card_runs(c["id"], board)
        if not runs:
            continue
        r = runs[0]
        sess = CT.sessions_in_window(profile, r["started_at"], r["ended_at"])
        parts = [CT.breakdown(profile, s[0]) for s in sess]
        parts = [p for p in parts if p]
        if not parts:
            continue
        out.append({
            "card": c["id"],
            "minutes": round(((c["completed_at"] or 0) - (r["started_at"] or 0)) / 60, 1),
            "model_s": sum(p["model_s"] for p in parts),
            "hang_s": sum(p["hang_s"] for p in parts),
            "tool_s": sum(p["tool_s"] for p in parts),
            "tool_calls": sum(p["tool_calls"] for p in parts),
            "messages": sum(p["messages"] for p in parts),
        })
    return out


def summary(rows):
    if not rows:
        return {"cards": 0}
    med = lambda k: round(statistics.median(r[k] for r in rows), 1)  # noqa: E731
    avg = lambda k: round(sum(r[k] for r in rows) / len(rows), 1)  # noqa: E731
    return {
        "cards": len(rows),
        "minutes_median": med("minutes"),
        "minutes_mean": avg("minutes"),
        "model_min_median": round(med("model_s") / 60, 1),
        "hang_min_median": round(med("hang_s") / 60, 1),
        "tool_min_median": round(med("tool_s") / 60, 1),
        "tool_calls_median": med("tool_calls"),
        "messages_median": med("messages"),
    }


def skill_corpus():
    total = 0
    n = 0
    for p in glob.glob(os.path.join(HERMES, "shared", "skills", "*", "SKILL.md")):
        total += len(open(p, encoding="utf-8").read())
        n += 1
    return {"skills": n, "chars": total}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="2026-11-09T00:00")
    ap.add_argument("--split", default="2026-11-11T12:00")
    ap.add_argument("--after-end", default=None, help="整理後の窓の終わり（既定は今）")
    ap.add_argument("--board", default="dev")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    lo, split = ts(args.before), ts(args.split)
    hi = ts(args.after_end) or datetime.now(timezone.utc).timestamp()
    before = summary(window(args.board, lo, split))
    after = summary(window(args.board, split, hi))
    corpus = skill_corpus()
    data = {"before": before, "after": after, "skills_now": corpus}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    print("スキル整理の前後（dev ボード・executor のカード）")
    print(f"  スキル本文はいま {corpus['skills']}本 / {corpus['chars']:,}字")
    for label, s in (("整理前", before), ("整理後", after)):
        if not s.get("cards"):
            print(f"  {label}: カードが無い（この窓では測れない）")
            continue
        print(f"  {label}: {s['cards']}枚 / 所要 中央{s['minutes_median']}分・平均{s['minutes_mean']}分"
              f" / モデル待ち 中央{s['model_min_median']}分 / 固まり 中央{s['hang_min_median']}分"
              f" / ツール実行 中央{s['tool_min_median']}分"
              f" / ツール呼び出し 中央{s['tool_calls_median']}回 / メッセージ 中央{s['messages_median']}件")
    if before.get("cards") and after.get("cards"):
        d = round(after["minutes_median"] - before["minutes_median"], 1)
        print(f"  差: 中央所要 {d:+.1f}分（負なら速くなった）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
