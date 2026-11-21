#!/usr/bin/env python3
"""Create one エピック or タスク row with the DB's default template, fill it, and check the result (the only way rows are made by hand).

usage: create_row.py --spec <spec.json> | --spec-json '<json>'  [--level L1|L2|L3]  [--dry-run]
Prints `CREATED <kind> <page id> <url>` (exit 0) when the row exists AND passed the read-back check; `--level L1` prints
`CREATED epic <id> <url> task <id> <url>`. Exit codes:
  2  the spec is wrong (printed as `ERROR spec: ...`); nothing was written to Notion. A task without an epic is wrong (see level)
  1  anything else went wrong (`ERROR <reason>`, plus ` page=<id> url=<url>` when a page was already created); the page is
     left in its waiting state and is never deleted: fix that page, do not run the command again for the same row
  0  CREATED

spec (JSON, `kind` picks the shape; an unknown key is an error, a typo is never ignored):
  task : {"kind": "task", "title": "...", "status": "未着手"(default), "level": "L2"(default; `--level` is the same key),
          "epic": "<エピック row id or URL>"(required unless level is L1),
          "overview": {"目的": "...", "完了条件": "...", "対象外": "...", "対象": "..."}, "checklist": ["...", ...], "memo": ["...", ...]}
  epic : {"kind": "epic", "title": "...", "type": "プロダクト開発"|"調査", ("priority" is accepted and ignored: the epic has no 優先度 any more), "level": "L1"|"L2"|"L3"(optional; the epic's レベル),
          "chat": "<Discord URL>"(optional),
          "stop_new": false(default), "purpose": {"指標", "定義", "基準値", "目標値", "期限", "完了条件" (and optionally "種別" = type)},
          "candidates": ["打ち手...／判断の宣言...／感度の見積もり...／開始できるフェーズ...", ...],
          "measurements": [["日付", "指標", "値", "出典"], ...], "log": ["...", ...], "docs": ["[name](https://...)", ...]}
level (task only): a task always sits under an epic. L2 / L3: `epic` is required and must be an existing row (the level lives on that epic; nothing is
written to it). L1: no `epic`; ONE call makes the epic (種別 プロダクト開発, レベル L1 (levels.toml gives L1 no phase row: the gate controller and the planner skip it by the level, not by a flag), 目的 =
the task's 目的 and 完了条件) and then the task under it. If the second step fails the epic is kept and named in the error (`epic=<id>`, plus `page=<id>`).
Values are single-line Markdown (backticks and [text](url) links work). File names such as create_row.py are wrapped in backticks
automatically: Notion turns a bare name into a link to a site that does not exist.

How: the default template of the DB (looked up at run time, never configured) is applied by `POST v1/pages` with a waiting state
(task 進行中: the intake cron only takes 未着手; epic 新規作成の停止 checked: the gate controller and the planner skip it), the holes of the
template body are filled by ONE update_content call, every replacement is confirmed (Notion drops a replacement that matches nothing
without an error when another one matched), then the blocks are read back and checked (chapters, colors, tables, checklist items,
no leftover template text). Only after that the row gets its final state. Standard library only; Notion through epic_common.ntn.
"""
import argparse
import json
import re
import sys

import epic_common as C

TASK_STATUSES = ("未着手", "進行中", "レビュー中", "完了", "キャンセル")
TASK_WAITING = "進行中"
TASK_OVERVIEW = ("目的", "完了条件", "対象外", "対象")
TASK_CHAPTERS = ("概要", "やることチェックリスト", "作業メモ", "完了レポート")
EPIC_PURPOSE = ("指標", "定義", "基準値", "目標値", "期限", "完了条件", "種別")
EPIC_CHAPTERS = ("目的", "候補", "計測ログ", "管理ログ", "関連ドキュメント", "完了レポート")
EPIC_TYPES = ("プロダクト開発", "調査")
REPORT_SECTIONS = ("実施内容", "結果", "確認した方法", "未確認・残件", "次のタスク")
CANDIDATE_WORDS = ("打ち手", "判断の宣言", "感度の見積もり", "開始できるフェーズ")
MEASURE_HEAD = ("日付", "指標", "値", "出典")
H1_COLOR, H2_COLOR = "yellow_background", "blue_background"
BODY_WAIT = 10  # seconds to wait for the template body (measured: it is there after 0.8 s)
LEVELS = ("L1", "L2", "L3")
TASK_KEYS = {"kind", "title", "status", "level", "epic", "overview", "checklist", "memo"}
EPIC_KEYS = {"kind", "title", "type", "priority", "level",  # "priority": dropped property, still accepted and never written (old specs keep working)
              "chat", "stop_new", "purpose", "candidates", "measurements", "log", "docs"}
NO_EPIC = "タスクはエピック配下に作る（L1 でも器として1行作る。`--level L1` を使う）"
NEW_EPIC = "(the epic made just before)"  # the relation of the task in an L1 dry run


class SpecError(Exception):
    """The spec is wrong: exit 2, nothing is written."""


class RowError(Exception):
    """A step failed: exit 1. `page` is the (id, url) of the already created page, if any."""

    def __init__(self, msg, page=None):
        super().__init__(msg)
        self.page = page


class Mismatch(Exception):
    """The template body does not have the structure the replacements are built for."""


# ---------------------------------------------------------------- text

_KEEP = re.compile(r"`[^`]*`|\[[^\]]*\]\([^)]*\)|https?://\S+")
_NAME = re.compile(r"(?<![A-Za-z0-9_./`-])([A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:py|md|json|sh|txt|com))(?![A-Za-z0-9_-])")  # ASCII only: Japanese text next to a name is not part of it


def protect_names(text):
    """Wrap bare file names (.py .md .json .sh .txt .com) in backticks outside code spans, links and URLs (Notion auto-links them)."""
    out, pos = [], 0
    for m in _KEEP.finditer(text):
        out.append(_NAME.sub(r"`\1`", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_NAME.sub(r"`\1`", text[pos:]))
    return "".join(out)


def plain(s):
    """Comparable form of a Markdown value or a Notion text: links -> anchor text, no backticks / backslashes / emphasis marks, no whitespace.

    Notion rewrites `_x_` to `*x*` and escapes `< > |` with a backslash (measured), so the marks are dropped on both sides."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s or "")
    return re.sub(r"[\s`\\*_]", "", s)


# ---------------------------------------------------------------- spec

def _line(v, what):
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f"{what} must be a non-empty string")
    if "\n" in v or "\r" in v:
        raise SpecError(f"{what} must be a single line (one value per cell / item)")
    return v.strip()


def _lines(v, what, minimum=0):
    if not isinstance(v, list) or len(v) < minimum:
        raise SpecError(f"{what} must be a list with at least {minimum} item(s)")
    return [protect_names(_line(x, f"{what}[{i}]")) for i, x in enumerate(v)]


def _only(spec, allowed, kind):
    extra = sorted(set(spec) - allowed)
    if extra:
        raise SpecError(f"unknown key(s) for kind {kind}: {', '.join(extra)} (allowed: {', '.join(sorted(allowed))})")


def _table(v, labels, what):
    if not isinstance(v, dict):
        raise SpecError(f"{what} must be an object with the keys {', '.join(labels)}")
    missing = [k for k in labels if k not in v]
    extra = [k for k in v if k not in labels]
    if missing or extra:
        raise SpecError(f"{what} must have exactly the keys {', '.join(labels)} (missing: {', '.join(missing) or '-'}; unknown: {', '.join(extra) or '-'})")
    return {k: protect_names(_line(v[k], f"{what}.{k}")) for k in labels}


def validate(spec):
    """Spec dict -> normalized spec (names protected, defaults filled). Raises SpecError."""
    if not isinstance(spec, dict):
        raise SpecError("the spec must be a JSON object")
    kind = spec.get("kind")
    if kind == "task":
        _only(spec, TASK_KEYS, kind)
        st = spec.get("status", "未着手")
        if st not in TASK_STATUSES:
            raise SpecError(f"status must be one of {', '.join(TASK_STATUSES)}")
        level = spec.get("level", "L2")
        if level not in LEVELS:
            raise SpecError(f"level must be one of {', '.join(LEVELS)}")
        epic = spec.get("epic")
        if epic is None and level != "L1":
            raise SpecError(NO_EPIC)
        if epic is not None and level == "L1":
            raise SpecError("level L1 makes its own epic: leave `epic` out (or use L2 / L3 for an existing epic)")
        if epic is not None:
            try:
                h = C.epic_id_from_relation(_line(epic, "epic"))
            except C.PipelineError as e:
                raise SpecError(f"{e}. {NO_EPIC}")
            epic = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        title = _line(spec.get("title"), "title")
        if C.broken_row_title(title):  # a typo of a range (設計-リリース判定) would make an ordinary task the intake starts as a second card
            raise SpecError(f"title {title!r} is shaped like a range of phases but is not one (a phase, or <first>〜<last> with the first before the last): "
                            f"use 〜 between two phases, the first before the last")
        return {"kind": "task", "title": title, "status": st, "level": level, "epic": epic,
                "overview": _table(spec.get("overview"), TASK_OVERVIEW, "overview"),
                "checklist": _lines(spec.get("checklist"), "checklist", 1), "memo": _lines(spec.get("memo", []), "memo")}
    if kind == "epic":
        _only(spec, EPIC_KEYS, kind)
        typ = spec.get("type")
        if typ not in EPIC_TYPES:
            raise SpecError(f"type must be one of {', '.join(EPIC_TYPES)}")
        lvl = spec.get("level")
        if lvl is not None and lvl not in LEVELS:
            raise SpecError(f"level must be one of {', '.join(LEVELS)}")
        chat = spec.get("chat")
        if chat is not None and not re.match(r"^https://\S+$", _line(chat, "chat")):
            raise SpecError("chat must be a URL (leave it out when unknown; never guess)")
        stop = spec.get("stop_new", False)
        if not isinstance(stop, bool):
            raise SpecError("stop_new must be true or false")
        purpose = dict(spec.get("purpose") or {})
        if "種別" in purpose and purpose["種別"] != typ:
            raise SpecError(f"purpose.種別 ({purpose['種別']!r}) differs from type ({typ!r})")
        purpose.setdefault("種別", typ)
        cands = _lines(spec.get("candidates"), "candidates", 1)
        for i, c in enumerate(cands):
            gap = [w for w in CANDIDATE_WORDS if w not in c]
            if gap:
                raise SpecError(f"candidates[{i}] must name {', '.join(CANDIDATE_WORDS)} (missing: {', '.join(gap)})")
        meas = spec.get("measurements", [])
        if not isinstance(meas, list) or any(not isinstance(r, list) or len(r) != 4 for r in meas):
            raise SpecError("measurements must be a list of [日付, 指標, 値, 出典]")
        meas = [[protect_names(_line(c, f"measurements[{i}][{j}]")) for j, c in enumerate(r)] for i, r in enumerate(meas)]
        return {"kind": "epic", "title": _line(spec.get("title"), "title"), "type": typ, "level": lvl, "chat": chat, "stop_new": stop,
                "purpose": _table(purpose, EPIC_PURPOSE, "purpose"), "candidates": cands, "measurements": meas,
                "log": _lines(spec.get("log", []), "log"), "docs": _lines(spec.get("docs", []), "docs")}
    raise SpecError("kind must be task or epic")


def l1_epic(task):
    """The smallest epic that holds an L1 task (a normalized task spec): the task's 目的 and 完了条件 only. Not held by 新規作成の停止: levels.toml gives L1 no phase rows, and the gate controller and the planner read the レベル."""
    o = task["overview"]
    return validate({"kind": "epic", "title": task["title"], "type": "プロダクト開発", "level": "L1", "stop_new": False,
                     "purpose": {"指標": f"目的：{o['目的']}", "定義": "-", "基準値": "-", "目標値": "-", "期限": "-", "完了条件": o["完了条件"]},
                     "candidates": [f"打ち手：{task['title']} ／ 判断の宣言：L1（1回で終える作業。器としてのエピックで、工程の行は作らない） ／ 感度の見積もり：なし ／ 開始できるフェーズ：実装"]})


# ---------------------------------------------------------------- markdown (the format of GET v1/pages/<id>/markdown)

def _section(md, name):
    """(heading start, body start, body end) of the H1 chapter `name`. The body ends at the next H1."""
    m = re.search(r'^# %s(?: \{color="\w+"\})?[ \t]*$' % re.escape(name), md, re.M)
    if not m:
        raise Mismatch(f"chapter {name} not found in the template body")
    nxt = re.search(r"^# ", md[m.end():], re.M)
    return m.start(), m.end(), m.end() + nxt.start() if nxt else len(md)


def _guidance(md, a, b):
    """(start, end) of the first plain paragraph line of md[a:b] (the template's explanation line), or None."""
    pos = a
    for line in md[a:b].split("\n"):
        s = line.strip()
        if s and not s.startswith(("<", "-", "#")):
            return pos, pos + len(line)
        if s.startswith(("<", "-", "#")):
            return None
        pos += len(line) + 1
    return None


def _once(md, old):
    if md.count(old) != 1:
        raise Mismatch(f"the text to replace is not unique in the body ({md.count(old)}x): {old[:50]!r}")
    return old


def _append(md, chapter, lines):
    """Add `lines` after the explanation paragraph of a chapter."""
    if not lines:
        return []
    h, a, b = _section(md, chapter)
    g = _guidance(md, a, b)
    old = md[g[0]:g[1]] if g else md[h:a]
    if md.count(old) != 1:  # not unique alone: take the heading line with it
        old = md[h:g[1]] if g else md[h:a]
    return [(_once(md, old), old + "\n" + "\n".join(lines))]


def _todos(md, chapter, items):
    """Replace every to_do line of a chapter (default items, empty items) with `items`, keeping the explanation paragraph."""
    h, a, b = _section(md, chapter)
    lines = md[a:b].split("\n")
    pos, first, last = a, None, None
    for ln in lines:
        if re.match(r"^- \[[ xX]\]", ln):
            first = pos if first is None else first
            last = pos + len(ln)
        pos += len(ln) + 1
    if first is None:
        raise Mismatch(f"chapter {chapter} has no checklist lines in the template body")
    g = _guidance(md, a, b)
    start = g[0] if g and g[1] < first else first
    old = md[start:last]
    return [(_once(md, old), (md[start:first] if start < first else "") + "\n".join(f"- [ ] {t}" for t in items))]


def _cells(md, chapter, values):
    """Second cell of the table row `label` (inside the chapter) -> value."""
    h, a, b = _section(md, chapter)
    out = []
    for label, value in values.items():
        m = re.search(r"(<td>%s</td>)(\s*)(<td>)(.*?)(</td>)" % re.escape(label), md[a:b], re.S)
        if not m:
            raise Mismatch(f"table row {label} not found in chapter {chapter}")
        old = m.group(0)
        out.append((_once(md, old), m.group(1) + m.group(2) + m.group(3) + value + m.group(5)))
    return out


def _tr(cells):
    return "<tr>\n" + "".join(f"<td>{c}</td>\n" for c in cells) + "</tr>"


def build_updates(spec, md):
    """[(old_str, new_str)] that turn the template body `md` into the row described by `spec`. Raises Mismatch."""
    if spec["kind"] == "task":
        ups = _cells(md, "概要", spec["overview"]) + _todos(md, "やることチェックリスト", spec["checklist"])
        ups += _append(md, "作業メモ", [f"- {t}" for t in spec["memo"]])
        return ups
    ups = _cells(md, "目的", spec["purpose"]) + _todos(md, "候補", spec["candidates"])
    if spec["measurements"]:
        h, a, b = _section(md, "計測ログ")
        m = re.search(r"<tr>(?:\s*<td></td>)+\s*</tr>", md[a:b])
        if not m:
            raise Mismatch("計測ログ has no empty row in the template body")
        ups.append((_once(md, m.group(0)), "\n".join(_tr(r) for r in spec["measurements"])))
    ups += _append(md, "管理ログ", spec["log"]) + _append(md, "関連ドキュメント", [f"- {t}" for t in spec["docs"]])
    return ups


def template_cells(md, chapter, labels):
    """{label: plain text of its second cell} in a template / page markdown (the leftover-template-text check compares with this)."""
    h, a, b = _section(md, chapter)
    out = {}
    for label in labels:
        m = re.search(r"<td>%s</td>\s*<td>(.*?)</td>" % re.escape(label), md[a:b], re.S)
        out[label] = plain(m.group(1)) if m else None
    return out


# ---------------------------------------------------------------- Notion

def default_template(ds):
    """Id of the data source's default template. Raises SpecError-free RowError when there is none."""
    d = C.ntn(f"v1/data_sources/{ds}/templates")
    hit = [t for t in d.get("templates", []) if t.get("is_default")]
    if not hit:
        raise RowError(f"the data source {ds} has no default template (nothing was created)")
    return hit[0]["id"]


def get_markdown(pid):
    return C.ntn(f"v1/pages/{pid}/markdown").get("markdown", "")


def wait_for_body(pid, chapters, page):
    """Markdown of the new page once all template chapters are there (at most BODY_WAIT seconds)."""
    md = ""
    for i in range(BODY_WAIT + 1):
        md = get_markdown(pid)
        if all(re.search(r"^# %s\b" % re.escape(c), md, re.M) for c in chapters):
            return md
        C._sleep(1)
    raise RowError(f"the template body did not appear within {BODY_WAIT}s", page)


def _rt(rich):
    return "".join(x.get("plain_text", "") for x in rich or [])


def _rows_of(table):
    """[[cell text]] of a table block (header row included)."""
    return [[_rt(c) for c in (r.get("table_row") or {}).get("cells", [])] for r in C.blocks(table["id"])]


def read_back(pid):
    """Page blocks as [(kind, level, text, extra)]: h (heading_n, color), t (table rows), d (to_do text, checked), p (any other text)."""
    out = []
    for b in C.blocks(pid):
        t = b.get("type")
        if t in ("heading_1", "heading_2", "heading_3"):
            out.append(("h", int(t[-1]), C.block_text(b), b[t].get("color")))
        elif t == "table":
            out.append(("t", 0, "", _rows_of(b)))
        elif t == "to_do":
            out.append(("d", 0, C.block_text(b), b["to_do"].get("checked")))
        else:
            out.append(("p", 0, C.block_text(b), None))
    return out


def inspect(spec, blocks, tpl_md):
    """Problems found in the read-back blocks of the new row (empty list = it passed). The definition of a good row."""
    task = spec["kind"] == "task"
    chapters = TASK_CHAPTERS if task else EPIC_CHAPTERS
    bad = []
    h1 = [(x[2], x[3]) for x in blocks if x[0] == "h" and x[1] == 1]
    if [n for n, _ in h1] != list(chapters):
        bad.append(f"H1 chapters are {[n for n, _ in h1]}, expected {list(chapters)}")
    bad += [f"H1 {n} has color {c}, expected {H1_COLOR}" for n, c in h1 if c != H1_COLOR]
    h2 = [(x[2], x[3]) for x in blocks if x[0] == "h" and x[1] == 2]
    if [n for n, _ in h2] != list(REPORT_SECTIONS):
        bad.append(f"完了レポート H2 sections are {[n for n, _ in h2]}, expected {list(REPORT_SECTIONS)}")
    bad += [f"H2 {n} has color {c}, expected {H2_COLOR}" for n, c in h2 if c != H2_COLOR]
    tables = [x[3] for x in blocks if x[0] == "t"]
    want_tables = 1 if task else 2
    if len(tables) != want_tables:
        bad.append(f"{len(tables)} table(s), expected {want_tables}")
    if tables:
        labels, chap = (TASK_OVERVIEW, "概要") if task else (EPIC_PURPOSE, "目的")
        rows = tables[0]
        if not rows or rows[0][:2] != ["項目", "内容"]:
            bad.append(f"the {chap} table has no 項目/内容 header")
        body = rows[1:] if rows else []
        if [r[0] for r in body] != list(labels):
            bad.append(f"the {chap} table rows are {[r[0] for r in body]}, expected {list(labels)}")
        else:
            want = spec["overview"] if task else spec["purpose"]
            tpl = template_cells(tpl_md, chap, labels)
            for r in body:
                if plain(r[1]) != plain(want[r[0]]):
                    bad.append(f"{chap} / {r[0]} is not the spec value")
                elif plain(r[1]) == tpl[r[0]]:
                    bad.append(f"{chap} / {r[0]} still holds the template text")
    if not task and len(tables) == 2:
        rows = tables[1]
        if not rows or rows[0] != list(MEASURE_HEAD):
            bad.append(f"the 計測ログ table header is {rows[0] if rows else None}, expected {list(MEASURE_HEAD)}")
        else:
            filled = [r for r in rows[1:] if any(c.strip() for c in r)]
            if [[plain(c) for c in r] for r in filled] != [[plain(c) for c in r] for r in spec["measurements"]]:
                bad.append("the 計測ログ rows are not the spec measurements")
    chap = "やることチェックリスト" if task else "候補"
    want = spec["checklist"] if task else spec["candidates"]
    todo, cur = {}, None
    for x in blocks:
        if x[0] == "h" and x[1] == 1:
            cur = x[2]
        elif x[0] == "d":
            todo.setdefault(cur, []).append(x[2])
    mine = todo.get(chap, [])
    if any(not t.strip() for sec in todo.values() for t in sec):
        bad.append("an empty to_do item is left in the page")
    if [plain(t) for t in mine] != [plain(t) for t in want]:
        bad.append(f"{chap} items are not the spec items ({len(mine)} found, {len(want)} expected)")
    text_of = {}
    cur = None
    for x in blocks:
        if x[0] == "h" and x[1] == 1:
            cur = x[2]
        elif x[0] in ("p", "d") and x[2].strip():
            text_of.setdefault(cur, []).append(plain(x[2]))
    for chap, lines in (("作業メモ", spec["memo"]),) if task else (("管理ログ", spec["log"]), ("関連ドキュメント", spec["docs"])):
        bad += [f"{chap}: the line {t[:40]!r} is missing" for t in lines if plain(t) not in text_of.get(chap, [])]
    return bad


def _unlink(md):
    """`[text](url)` -> `text`: Notion auto-links things that look like hostnames (e.g. `session.dev` inside
    a metric name), which would otherwise make a landed replacement look missing in the read-back."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)


def confirm_updates(md_after, updates):
    """Problems of the update_content answer: a replacement whose new text is missing or whose old text is still there."""
    flat = re.sub(r"[\s\\*_`]", "", _unlink(md_after))  # layout, escapes, emphasis and auto-links are Notion's to rewrite
    bad = []
    for old, new in updates:
        if re.sub(r"[\s\\*_`]", "", _unlink(new)) not in flat:
            bad.append(f"the replacement for {old[:40]!r} did not land")
        elif old not in new and re.sub(r"[\s\\*_`]", "", _unlink(old)) in flat:
            bad.append(f"the old text {old[:40]!r} is still in the page")
    return bad


def properties(spec, waiting):
    t = spec["title"]
    if spec["kind"] == "task":
        p = {"タスク名": {"title": [{"text": {"content": t}}]},
             "ステータス": {"status": {"name": TASK_WAITING if waiting else spec["status"]}}}
        if spec["epic"]:
            p[C.TASK_EPIC_PROP] = {"relation": [{"id": spec["epic"]}]}
        return p
    p = {"名前": {"title": [{"text": {"content": t}}]}, "種別": {"select": {"name": spec["type"]}},
         "新規作成の停止": {"checkbox": True if waiting else spec["stop_new"]}}
    if spec["chat"]:
        p["チャット"] = {"url": spec["chat"]}
    if spec["level"]:
        p["レベル"] = {"select": {"name": spec["level"]}}
    return p


def final_check(spec, pid, page):
    """Set the final state, read it back."""
    final = properties(spec, False)
    if spec["kind"] == "task":
        patch = {"ステータス": final["ステータス"]}
        got = lambda pr: C.status_of(pr)
        want = spec["status"]
    else:
        patch = {"新規作成の停止": final["新規作成の停止"]}
        got = lambda pr: bool((pr.get("新規作成の停止") or {}).get("checkbox"))
        want = spec["stop_new"]
    C.ntn(f"v1/pages/{pid}", "-X", "PATCH", data={"properties": patch})
    now = C.ntn(f"v1/pages/{pid}").get("properties", {})
    if got(now) != want:
        raise RowError(f"the final state is {got(now)!r}, expected {want!r}", page)


def check_epic(epic):
    """The epic of a task must be an existing row of the エピック DB (read only). Raises SpecError (not there) / RowError (cannot tell)."""
    try:
        page = C.ntn(f"v1/pages/{epic}")
    except C.PipelineError as e:
        if re.search(r"object_not_found|404|Could not find", str(e)):
            raise SpecError(f"the epic {epic} does not exist in Notion. {NO_EPIC}")
        raise RowError(f"cannot check the epic {epic} ({e}); nothing was created")
    ds = (page.get("parent") or {}).get("data_source_id")
    if page.get("in_trash") or page.get("archived") or (ds and ds.replace("-", "") != C.REGISTRY_DS.replace("-", "")):
        raise SpecError(f"{epic} is not a live row of the エピック DB. {NO_EPIC}")


def create(spec, dry_run=False):
    """-> (page id, url) or, for a dry run, (None, plan). Raises SpecError (the epic of a task is not there) / RowError / Mismatch."""
    task = spec["kind"] == "task"
    if task and spec["epic"] != NEW_EPIC:
        check_epic(spec["epic"])
    ds = C.TASK_DS if task else C.REGISTRY_DS
    tid = default_template(ds)
    tpl_md = get_markdown(tid)
    updates = build_updates(spec, tpl_md)  # the same replacements against the template itself: a structure mismatch stops here, before anything is created
    body = {"parent": {"data_source_id": ds}, "properties": properties(spec, True), "template": {"type": "template_id", "template_id": tid}}
    if dry_run:
        return None, {"post": body, "updates": [{"old": o, "new": n} for o, n in updates]}
    try:
        page = C.ntn("v1/pages", "-X", "POST", data=body, retries=1)  # a retried create would make a second row
    except C.PipelineError as e:
        raise RowError(f"create failed ({e}); the row may exist: search the DB by the title before trying again")
    pid, url = page["id"], page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}"
    ref = (pid, url)
    md = wait_for_body(pid, TASK_CHAPTERS if task else EPIC_CHAPTERS, ref)
    try:
        updates = build_updates(spec, md)
    except Mismatch as e:
        raise RowError(str(e), ref)
    patch_err = None
    try:
        answer = C.ntn(f"v1/pages/{pid}/markdown", "-X", "PATCH", timeout=90, retries=1,
                       data={"type": "update_content", "update_content": {"content_updates": [{"old_str": o, "new_str": n} for o, n in updates]}})
        after = answer.get("markdown") or get_markdown(pid)
    except C.PipelineError as e:  # it may still have been applied (timeout): the read-back below decides
        patch_err, after = e, get_markdown(pid)
    bad = confirm_updates(after, updates)
    if bad:
        raise RowError("; ".join(bad[:5]) + (f" (update call: {patch_err})" if patch_err else ""), ref)
    bad = inspect(spec, read_back(pid), tpl_md)
    if bad:
        raise RowError("inspection failed: " + "; ".join(bad[:6]), ref)
    final_check(spec, pid, ref)
    return pid, url


def create_l1(spec, dry_run=False):
    """level L1: the epic, then the task under it. -> ((epic id, url), (task id, url)) or, for a dry run, (None, plan)."""
    epic = l1_epic(spec)
    if dry_run:
        return None, {"plan": "L1: エピック1行＋作業1行", "epic": create(epic, True)[1], "task": create(dict(spec, epic=NEW_EPIC), True)[1]}
    try:
        eid, eurl = create(epic)
    except RowError as e:
        raise RowError(f"{e} (level L1: the epic is not complete, the task row was not made)", e.page)
    try:
        return (eid, eurl), create(dict(spec, epic=eid))
    except RowError as e:
        made = "left half-filled (page=)" if e.page else "not made"
        raise RowError(f"{e} (level L1: the epic row epic={eid} is complete and was kept, the task row was {made}; never run this command again for the same work)", e.page or (eid, eurl))


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec", help="path of the spec JSON file")
    g.add_argument("--spec-json", help="the spec JSON itself (when a file cannot be written)")
    ap.add_argument("--level", choices=LEVELS, help="task only: L1 = make the epic and the task in one call; L2 / L3 = under an existing epic (default L2)")
    ap.add_argument("--dry-run", action="store_true", help="validate, read the template, print the planned writes; write nothing")
    a = ap.parse_args(argv)
    try:
        try:
            raw = a.spec_json if a.spec_json is not None else open(a.spec, encoding="utf-8").read()
            raw = json.loads(raw)
            if a.level and isinstance(raw, dict) and raw.get("kind") == "task":
                if raw.get("level", a.level) != a.level:
                    raise SpecError(f"level in the spec ({raw['level']!r}) differs from --level {a.level}")
                raw["level"] = a.level
            spec = validate(raw)
        except (OSError, ValueError) as e:
            raise SpecError(f"cannot read the spec: {e}")
    except SpecError as e:
        print(f"ERROR spec: {e}")
        return 2
    try:
        if spec["kind"] == "task" and spec["level"] == "L1":
            pid, res = create_l1(spec, a.dry_run)
        else:
            pid, res = create(spec, a.dry_run)
    except SpecError as e:  # a spec that only Notion can judge (the epic is not there): still nothing was written
        print(f"ERROR spec: {e}")
        return 2
    except RowError as e:
        print(f"ERROR {e}" + (f" page={e.page[0]} url={e.page[1]}" if e.page else ""))
        return 1
    except (Mismatch, C.PipelineError) as e:
        print(f"ERROR {e}")
        return 1
    if a.dry_run:
        print("DRY-RUN " + json.dumps(res, ensure_ascii=False))
        return 0
    if spec["kind"] == "task" and spec["level"] == "L1":
        print(f"CREATED epic {pid[0]} {pid[1]} task {res[0]} {res[1]}")
    else:
        print(f"CREATED {spec['kind']} {pid} {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
