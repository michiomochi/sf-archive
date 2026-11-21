#!/usr/bin/env python3
"""cron の monitor 用: 指標を「帯」と「連続日数の区切り」だけに圧縮して出す(読むだけ・決定論・保存しない)。

生の値・日付・時刻は出さない。monitor は出力が変わった朝にだけ AI を起こすので、値が帯の中で動くだけなら同じ行になる。
帯: 直近7日の窓(今日 00:00 JST まで)を、その前の7日の窓と同じ定義・同じ版で比べ、許容の幅(retro_metrics.DEFINITIONS の tol)を
超えて動いたら 良化(better)/悪化(worse)、収まっていれば横ばい。どちらかの n が 3 件未満なら判断しない(行に出さない)。
連続: 同じ判定の窓を1日ずつ遡って、同じ帯が続いた日数を 1(1〜2日)・3(3〜6日)・7(7日以上)の3つの区切りにする。
  RETRO worse=runs.crash_fast.reviewer:3d,runs.failed:1d better=review.changes_rate:1d UNMEASURED=state.db:dev
  RETRO steady                              # 動いた指標が無い(判断できない指標だけのときも)
  FINDINGS runs.crash_fast.reviewer:3d      # 未返済の指摘と、起票からの日数の区切り(あるときだけ。日付は出さない)

  python3 tools/retro_signal.py [--end YYYY-MM-DD] [--findings FILE] [--board-db ...]

元データが読めない指標は UNMEASURED=<取得元> に出す(測れていないことを steady と言わない)。常に exit 0(失敗は cron 側が拾う)。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retro_findings as F  # noqa: E402
import retro_metrics as M  # noqa: E402

STREAK_DAYS = 7  # 遡る日数の上限(これ以上は 7 と同じ区切り)
WINDOW_GROUPS = ("cards", "runs", "review", "stall", "ai")


def bucket(days):
    return 7 if days >= 7 else 3 if days >= 3 else 1


def signal(end, findings_path, **sources):
    """→ 出力の行のリスト。"""
    windows = []  # k 日前に終わる7日の窓と、その前の7日の窓
    for k in range(STREAK_DAYS):
        e = end - k * M.DAY
        cur = M.compute(e - 7 * M.DAY, e, groups=WINDOW_GROUPS, **sources)["metrics"]
        prev = M.compute(e - 14 * M.DAY, e - 7 * M.DAY, groups=WINDOW_GROUPS, **sources)["metrics"]
        windows.append((cur, prev))
    worse, better, unmeasured = [], [], set()
    for key, d in M.DEFINITIONS.items():
        if d["scope"] != "window" or d["better"] not in ("low", "high") or M.group_of(key) not in WINDOW_GROUPS:
            continue
        now_m = windows[0][0][key]
        if now_m["status"] in ("unmeasured", "error"):
            unmeasured.add(d["src"])
            continue
        verdicts = [M.compare(key, prev[key], cur[key])[0] for cur, prev in windows]
        if verdicts[0] not in ("improved", "worse"):
            continue
        streak = 0
        for v in verdicts:
            if v != verdicts[0]:
                break
            streak += 1
        (worse if verdicts[0] == "worse" else better).append(f"{key}:{bucket(streak)}d")
    parts = ["RETRO"]
    if worse:
        parts.append("worse=" + ",".join(sorted(worse)))
    if better:
        parts.append("better=" + ",".join(sorted(better)))
    if unmeasured:
        parts.append("UNMEASURED=" + ",".join(sorted(unmeasured)))
    if len(parts) == 1:
        parts.append("steady")
    lines = [" ".join(parts)]
    today = datetime.fromtimestamp(end, M.JST).date()
    try:
        open_ = F.load(findings_path)["findings"]
    except F.Reject as e:
        open_, lines = [], lines + [f"FINDINGS ERROR {e}"]
    if open_:
        ages = [f"{f['metric']}:{bucket((today - datetime.strptime(f['opened'], '%Y-%m-%d').date()).days)}d" for f in open_]
        lines.append("FINDINGS " + ",".join(sorted(ages)))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="cron の monitor 用: 指標を帯と連続日数だけにして1行出す")
    ap.add_argument("--end", help="窓の終わりの日付(JST の 0 時・含まない)。既定: 今日")
    ap.add_argument("--findings", default=str(F.DEFAULT_FILE))
    ap.add_argument("--board-db")
    ap.add_argument("--profiles-dir")
    a = ap.parse_args(argv)
    end = M.parse_when(a.end) if a.end else M.default_window(datetime.now(M.JST), 0)[1]
    print("\n".join(signal(end, a.findings, board_db=a.board_db, profiles_dir=a.profiles_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
