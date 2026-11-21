#!/usr/bin/env python3
"""人が・AI レビューが「何を捕まえたか」を数える(読むだけ・決定論・保存しない・AI を使わない。標準ライブラリのみ)。

第1段(計測)の土台。主データはレビュー DB の行(人の判断。差し戻し・取り下げの理由がコメントか本文に書かれる)、
副データは kanban `task_events.changes_requested` の reason(AI レビューが捕まえたもの)。
項目の頭に付いた分類タグ `[軸1/軸2/軸4]` を正規表現で読むだけで、後から AI に分類させない。タグが無い項目は「未分類」と数える。

  python3 tools/review_catches.py report --since 2026-11-08
  python3 tools/review_catches.py report --since 2026-11-08 --review-json rows.json --board-db kanban.db   # 元データを差し替える(テスト・再計算)

分類タグの書式: `[STALE/①/S1]`(軸1 / 軸2 / 軸4)。軸1 は複数なら `・` か `,` で並べる(`[DESIGN・DUP/②/S1]`)。
  軸1 STALE INTENT DESIGN WIRING CLAIM LEAK TESTGAP DUP PERF UI PROC / 軸2 ①型 ②静的解析 ③検証 ④人 / 軸4 S0〜S3。
  軸3(誰が)はデータの出所から機械で決める: レビュー DB の行 = HREL(人のリリース承認)、kanban の差し戻し = AIR(AI レビュー)。
  GARD・POST・HSAMP は、それを書く道具(第2段・第3段)ができるまで出ない。

数え方: 項目 = 理由の番号付き 1 点(`1)` `(1)` `①` や行頭の `-` `・` で分ける。印が無ければ理由の全文が 1 項目)。同じ行(キーに回 `-rN` を含む)・同じ軸1 は 1 件。
`代筆`(「AI が記入」「委任により」を含む行)は「仮」として別に数える(人の抜き取りで確定するまで)。
元データが読めなければ、その元は 0 でなく status=unmeasured と理由を出す。出力に時刻(いま)を入れない。同じ入力なら同じバイト列。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import retro_metrics as M  # noqa: E402  (kanban の読み方・smoke の除外・窓の読み方を使い回す)

AXIS1 = ("STALE", "INTENT", "DESIGN", "WIRING", "CLAIM", "LEAK", "TESTGAP", "DUP", "PERF", "UI", "PROC")
AXIS2 = ("①", "②", "③", "④")
AXIS4 = ("S0", "S1", "S2", "S3")
DECIDED = ("差し戻し", "取り下げ")  # 理由を持つ行の状態
TAG_RE = re.compile(r"\[(?P<a1>[A-Z]+(?:[・,][A-Z]+)*)/(?P<a2>[①②③④])/(?P<a4>S[0-3])\]")
MARKER_RE = re.compile(r"(?:^|(?<=[\s。：:]))(?:\(\d{1,2}\)|\d{1,2}[)）]|[①-⑩])", re.M)
BULLET_RE = re.compile(r"^\s*[-・*]\s+", re.M)
PROXY_RE = re.compile(r"AI\s*が記入|AI\s*が代筆|委任により")
DEFAULT_SINCE = "2026-11-08"


def split_items(text):
    """理由の文章 → 項目の文字列のリスト(印が無ければ全文が 1 項目。空なら 0 件)。"""
    text = (text or "").strip()
    if not text:
        return []
    cuts = [m.start() for m in MARKER_RE.finditer(text)]
    if not cuts:
        cuts = [m.start() for m in BULLET_RE.finditer(text)]
    if not cuts:
        return [text]
    return [text[a:b].strip() for a, b in zip(cuts, cuts[1:] + [len(text)]) if text[a:b].strip()]


def parse_tag(item):
    """項目の頭のタグ → (軸1のリスト, 軸2, 軸4) か None。タグは項目の先頭(印のあと)にあるものだけを読む。"""
    m = TAG_RE.search(item[:80])
    if not m:
        return None
    a1 = [x for x in re.split(r"[・,]", m.group("a1")) if x in AXIS1]
    return (a1, m.group("a2"), m.group("a4")) if a1 else None


# ---------------------------------------------------------------- 元データ
def read_review_rows(since_epoch, review_json=None):
    """-> (rows, None) か (None, 理由)。row = dict(key, status, created(epoch), texts[理由の文章])。"""
    if review_json:
        try:
            raw = json.loads(Path(review_json).read_text(encoding="utf-8"))
            rows = [dict(key=r["key"], status=r["status"], created=M.parse_when(r["created"]), texts=list(r.get("texts") or [])) for r in raw]
        except (OSError, ValueError, KeyError) as e:
            return None, f"{review_json} を読めない: {type(e).__name__}: {e}"[:200]
        return [r for r in rows if r["created"] >= since_epoch], None
    try:
        sys.path.insert(0, str(TOOLS.parent / "scripts"))
        import epic_common as C  # 設定が無ければ SystemExit になる
        out = []
        for p in C.query(C.REVIEW_DS):
            created = M.parse_when(p["created_time"])
            if created < since_epoch:
                continue
            status = C.status_of(p, default="レビュー待ち")
            texts = []
            if status in DECIDED:  # 理由は行のコメントと、本文の章「判断の結果」にある
                for c in (C.ntn(f"v1/comments?block_id={p['id']}") or {}).get("results", []):
                    texts.append("".join(x.get("plain_text", "") for x in c.get("rich_text", [])))
                md = (C.ntn(f"v1/pages/{p['id']}/markdown") or {}).get("markdown", "")
                m = re.search(r"^#{1,6}\s+判断の結果[^\n]*\n(.*?)(?=^#{1,6}\s|\Z)", md, re.M | re.S)
                if m and m.group(1).strip():
                    texts.append(m.group(1).strip())
            out.append(dict(key="".join(x.get("plain_text", "") for x in (p["properties"].get("キー") or {}).get("rich_text", [])) or p["id"],
                            status=status, created=created, texts=texts))
        return out, None
    except (Exception, SystemExit) as e:
        return None, f"レビュー DB を読めない: {type(e).__name__}: {e}"[:200]


def read_kanban_reasons(since_epoch, board_db):
    """-> (events, None) か (None, 理由)。event = dict(key, texts)。tenant=smoke と題名【smoke】のカードは除く(retro_metrics と同じ規則)。"""
    try:
        con = M.ro(board_db)
    except (OSError, Exception) as e:
        return None, f"kanban.db を読めない: {type(e).__name__}: {e}"[:200]
    try:
        rows = con.execute("SELECT e.id, e.task_id, e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
                           f"WHERE e.kind = 'changes_requested' AND e.created_at >= ? AND {M.SMOKE_SQL} ORDER BY e.id", (since_epoch,)).fetchall()
    except Exception as e:
        return None, f"kanban.db を読めない: {type(e).__name__}: {e}"[:200]
    finally:
        con.close()
    out = []
    for eid, task, payload in rows:
        try:
            reason = (json.loads(payload) or {}).get("reason") or ""
        except (ValueError, TypeError, AttributeError):
            reason = ""
        out.append(dict(key=f"kanban:{task}:{eid}", texts=[str(reason)]))
    return out, None


# ---------------------------------------------------------------- 数える
class Tally:
    """項目を重複なく数える。key = 同じ行・同じ回、axis1 = 同じ軸1 は 1 件。"""

    def __init__(self):
        self.seen = set()
        self.total = self.tagged = self.untagged = self.provisional = 0
        self.by_axis = {}        # "STALE|HREL" -> 件数(確定分)
        self.prov_by_axis = {}   # 同(仮の分)
        self.by_axis2 = {}
        self.by_weight = {}

    def add(self, key, text, a3, proxy):
        tag = parse_tag(text)
        if tag is None:
            ident = (key, " ".join(text.split()))
            if ident in self.seen:
                return
            self.seen.add(ident)
            self.total += 1
            self.untagged += 1
            self.provisional += 1 if proxy else 0
            return
        a1s, a2, a4 = tag
        fresh = [a for a in a1s if (key, a) not in self.seen]
        if not fresh:
            return
        self.seen.update((key, a) for a in fresh)
        self.total += 1
        self.tagged += 1
        self.provisional += 1 if proxy else 0
        for a in fresh:
            bucket = self.prov_by_axis if proxy else self.by_axis
            bucket[f"{a}|{a3}"] = bucket.get(f"{a}|{a3}", 0) + 1
        if not proxy:
            self.by_axis2[a2] = self.by_axis2.get(a2, 0) + 1
            self.by_weight[a4] = self.by_weight.get(a4, 0) + 1

    def out(self):
        rate = round(100.0 * self.tagged / self.total, 1) if self.total else None
        return dict(items=self.total, tagged=self.tagged, untagged=self.untagged, provisional=self.provisional, classification_rate=rate,
                    by_axis1_axis3=dict(sorted(self.by_axis.items())), provisional_by_axis1_axis3=dict(sorted(self.prov_by_axis.items())),
                    by_axis2=dict(sorted(self.by_axis2.items())), by_axis4=dict(sorted(self.by_weight.items())))


def report(since, review_json=None, board_db=None):
    """-> dict(窓・元ごとの状態・項目の数え方)。保存しない。"""
    since_epoch = M.parse_when(since)
    board_db = M.BOARD_DB if board_db is None else board_db
    tally = Tally()
    sources, unmeasured = {}, []
    rows, why = read_review_rows(since_epoch, review_json)
    if rows is None:
        sources["review_db"] = dict(status="unmeasured", reason=why)
        unmeasured.append(dict(source="review_db", reason=why))
    else:
        by_status = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r["status"] not in DECIDED:
                continue
            proxy = any(PROXY_RE.search(t) for t in r["texts"])
            for t in r["texts"]:
                for item in split_items(t):
                    tally.add(r["key"], item, "HREL", proxy)
        sources["review_db"] = dict(status="ok", rows=len(rows), by_status=dict(sorted(by_status.items())))
    events, why = read_kanban_reasons(since_epoch, board_db)
    if events is None:
        sources["kanban"] = dict(status="unmeasured", reason=why)
        unmeasured.append(dict(source="kanban", reason=why))
    else:
        for e in events:
            for t in e["texts"]:
                for item in split_items(t):
                    tally.add(e["key"], item, "AIR", False)
        sources["kanban"] = dict(status="ok", changes_requested=len(events))
    out = dict(since=M.iso(since_epoch), sources=sources, unmeasured=unmeasured)
    out.update(tally.out())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="差し戻し・取り下げの理由を、タグで数える(読むだけ)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="窓の中の状態ごとの件数・項目数・タグ付き/未分類・軸1×軸3・仮の数を JSON で出す")
    r.add_argument("--since", default=DEFAULT_SINCE, help=f"窓の始め(含む)。日付 / ISO 日時 / epoch。既定 {DEFAULT_SINCE}")
    r.add_argument("--review-json", default=None, help="レビュー DB の代わりに読む JSON(行の配列: key, status, created, texts)")
    r.add_argument("--board-db", default=None, help=f"kanban.db(既定 {M.BOARD_DB})")
    a = ap.parse_args(argv)
    print(json.dumps(report(a.since, a.review_json, a.board_db), ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
