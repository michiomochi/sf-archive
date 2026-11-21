#!/usr/bin/env python3
"""未返済の指摘の保管と、直ったかどうかの判定(標準ライブラリのみ)。

~/.hermes/state/retro/findings.json に「いま開いている指摘」だけを数十行持つ。残すのは3つだけ:
  1. 未返済の指摘: metric / 行の URL(Notion) / 起票日 / メモ
  2. 指標の定義の版(def_v): 起票した時点の版。版が違えば前後を比べない
  3. 修正の基準点(fix): PR のマージ日時と、判定できたときの前後の値(check が書く)
数値の履歴は持たない。前後の値は元データ(kanban.db など)から tools/retro_metrics.py で毎回計算し直す。close した指摘は消える。
行の作成(Notion)はここではやらない。人か別の段が作った行の URL を受け取るだけ。

  python3 tools/retro_findings.py add --metric runs.crash_fast.reviewer --row <Notion の行 URL> --note "reviewer が60秒で落ちる"
      行の URL が無い・Notion の URL でない・知らない metric・同じ metric の未返済が既にある → 拒否(exit 2。ファイルは変えない)
  python3 tools/retro_findings.py check --metric runs.crash_fast.reviewer --since 2026-11-12T10:30
      since(PR のマージ日時。日付だけなら JST の 0 時)の前7日と後7日を、同じ定義・同じ版で比べる。
      前後それぞれ n が 3 件以上のときだけ improved / worse / flat を出す。足りなければ「判定しない」(verdict=none)。
      後の窓は「いま」で切る(まだ7日たっていなければ、そこまで)。時点の指標(lint・skills・filing)は過去に遡れないので判定しない。
      未返済の指摘があれば、基準点を findings.json の fix に書く。--since を省くと書いてある基準点を使う。
  python3 tools/retro_findings.py list
  python3 tools/retro_findings.py close --metric runs.crash_fast.reviewer      # 返済した指摘を消す

置き場所は --file か環境変数 SF_RETRO_FINDINGS で変えられる(テスト用)。
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retro_metrics as M  # noqa: E402

DEFAULT_FILE = Path(os.environ.get("SF_RETRO_FINDINGS") or Path("~/.hermes/state/retro/findings.json").expanduser())
ROW_URL = re.compile(r"https://(?:app\.notion\.com|www\.notion\.so)/\S+")
WINDOW_DAYS = 7


class Reject(Exception):
    pass


def load(path):
    path = Path(path)
    if not path.exists():
        return {"schema": 1, "findings": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(d["findings"], list)
        return d
    except (ValueError, KeyError, AssertionError) as e:
        raise Reject(f"{path} を読めない(手で直さず、壊れた理由を確かめる): {e}")


def save(path, d):
    """1指摘=1行で書く(数十行に収める)。一時ファイル→rename で、書きかけを残さない。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ",\n".join("  " + json.dumps(f, ensure_ascii=False, sort_keys=True) for f in d["findings"])
    text = '{"schema": %d, "findings": [\n%s\n]}\n' % (d.get("schema", 1), rows) if rows else '{"schema": %d, "findings": []}\n' % d.get("schema", 1)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".findings.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def find(d, metric):
    return next((f for f in d["findings"] if f["metric"] == metric), None)


def add(path, metric, row, note, today):
    if metric not in M.DEFINITIONS:
        raise Reject(f"知らない指標: {metric}(tools/retro_metrics.py --list-definitions にある key を使う)")
    if not row or not ROW_URL.fullmatch(row.strip()):
        raise Reject("行の URL が要る(https://www.notion.so/… か https://app.notion.com/…)。行が無い指摘は足せない: 先に行を作る")
    d = load(path)
    cur = find(d, metric)
    if cur:
        raise Reject(f"{metric} には未返済の指摘がある(起票 {cur['opened']} {cur['row']})。新しく足さず、その行に追記するか close する")
    d["findings"].append({"metric": metric, "row": row.strip(), "note": note or "", "opened": today, "def_v": M.DEFINITIONS[metric]["v"]})
    save(path, d)
    return d["findings"][-1]


def close(path, metric):
    d = load(path)
    f = find(d, metric)
    if not f:
        raise Reject(f"{metric} の未返済の指摘は無い")
    d["findings"].remove(f)
    save(path, d)
    return f


def judge(metric, since, now, finding=None, **sources):
    """マージ前7日 vs 後7日。→ (verdict, 理由, before, after, 前後の窓)。verdict は improved / worse / flat / none。"""
    d = M.DEFINITIONS[metric]
    if d["scope"] == "point":
        return "none", "時点の指標は過去を再計算できない", None, None, None
    if finding and finding.get("def_v") != d["v"]:
        return "none", f"指摘を起こした定義の版(v{finding.get('def_v')})と今の版(v{d['v']})が違う", None, None, None
    b_win = (since - WINDOW_DAYS * M.DAY, since)
    a_win = (since, min(since + WINDOW_DAYS * M.DAY, now))
    if a_win[1] <= a_win[0]:
        return "none", "マージがまだ「いま」より後で、後の窓が空", None, None, (b_win, a_win)
    g = M.group_of(metric)
    before = M.compute(*b_win, groups=(g,), **sources)["metrics"][metric]
    after = M.compute(*a_win, groups=(g,), **sources)["metrics"][metric]
    verdict, why = M.compare(metric, before, after)
    return verdict, why, before, after, (b_win, a_win)


def describe(m):
    return f"n={m['n']} 値={m['value']}{m['unit']}" if m and m.get("value") is not None else f"n={m.get('n') if m else '-'} 値=null({m.get('status') if m else '-'})"


def cmd_check(a, path):
    metric = a.metric
    if metric not in M.DEFINITIONS:
        raise Reject(f"知らない指標: {metric}")
    d = load(path)
    f = find(d, metric)
    since_arg = a.since or (f or {}).get("fix", {}).get("merged_at")
    if not since_arg:
        raise Reject("--since(PR のマージ日時)が要る")
    since = M.parse_when(since_arg)
    now = M.parse_when(a.now) if a.now else int(datetime.now(M.JST).timestamp())
    verdict, why, before, after, wins = judge(metric, since, now, f, **M.source_kwargs(a))
    merged = M.iso(since)
    if verdict == "none":
        extra = f" 前 {describe(before)} / 後 {describe(after)}" if before else ""
        print(f"CHECK {metric} verdict=none 判定しない: {why}.{extra} (前後それぞれ n が {M.MIN_N} 件以上要る) merged={merged}")
    else:
        assert before and after and wins
        (b0, b1), (a0, a1) = wins
        print(f"CHECK {metric} verdict={verdict} 前 {describe(before)} → 後 {describe(after)} (定義の版 v{before['v']}) merged={merged} "
              f"前={M.iso(b0)[:10]}..{M.iso(b1)[:10]} 後={M.iso(a0)[:10]}..{M.iso(a1)[:10]}")
    if f is not None:
        fix = {"merged_at": merged, "verdict": None if verdict == "none" else verdict}
        if verdict != "none":
            assert before and after
            fix["before"] = {"value": before["value"], "n": before["n"]}
            fix["after"] = {"value": after["value"], "n": after["n"]}
        f["fix"] = fix
        save(path, d)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="未返済の指摘の保管と before/after の判定")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help=f"findings.json(既定 {DEFAULT_FILE})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="指摘を足す(行の URL 必須・同じ metric の未返済があれば拒否)")
    p.add_argument("--metric", required=True)
    p.add_argument("--row", required=True, help="Notion の行の URL")
    p.add_argument("--note", default="")
    p.add_argument("--today", help="起票日(YYYY-MM-DD。既定: 今日 JST。テスト用)")
    p = sub.add_parser("check", help="マージ前7日 vs 後7日を同じ定義・同じ版で比べる")
    p.add_argument("--metric", required=True)
    p.add_argument("--since", help="PR のマージ日時")
    p.add_argument("--now", help="いま(後の窓の上限。テスト用)")
    M.add_source_args(p)
    sub.add_parser("list", help="未返済の指摘を出す")
    p = sub.add_parser("close", help="返済した指摘を消す")
    p.add_argument("--metric", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "add":
            today = a.today or datetime.now(M.JST).strftime("%Y-%m-%d")
            f = add(a.file, a.metric, a.row, a.note, today)
            print(f"ADDED {f['metric']} def_v={f['def_v']} opened={f['opened']} {f['row']}")
        elif a.cmd == "check":
            return cmd_check(a, a.file)
        elif a.cmd == "list":
            fs = load(a.file)["findings"]
            for f in fs:
                fix = f.get("fix") or {}
                print(f"{f['metric']}\t起票 {f['opened']}\tv{f['def_v']}\t{f['row']}\t{f['note']}" + (f"\tマージ {fix['merged_at']} 判定 {fix.get('verdict')}" if fix else ""))
            print(f"open findings: {len(fs)}")
        elif a.cmd == "close":
            f = close(a.file, a.metric)
            print(f"CLOSED {f['metric']} {f['row']}")
    except Reject as e:
        print(f"REJECT {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
