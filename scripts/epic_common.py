#!/usr/bin/env python3
"""Shared constants and helpers of the michiomochi epic pipeline scripts (light version: 5 phases, 2 human approvals).

Used by epic_gate_controller.py, epic_gate_apply.py, epic_pipeline_health.py, epic_planner_status.py and
notion_pending_tasks.py. Import-only module: importing it has no side effects except reading the environment config
(sfconfig.py: $SF_CONFIG or the neighbouring config.toml; exits 1 when it is missing or incomplete). No network.

Contents: Notion/kanban constants, the Notion `ntn` helper (retry on transient errors, bounded timeout),
the paginated `query`, block text reader, atomic JSON state files (controller heartbeat, partial-creation journal),
a non-blocking run lock, and a read-only kanban sqlite reader.
"""
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from string import Template

import sfconfig

SCRIPTS_DIR = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))
CFG = sfconfig.get()  # environment settings (ids, paths, board, models): see sfconfig.py / envs/*.toml
_CFG_RAW = sfconfig.get(expand=False)  # same, `~` kept: the texts (templates) quote paths the way they are written in the config
HERMES_ROOT = Path(CFG["hermes"]["home"])  # state.db of the default profile and profiles/*/state.db (agent session messages)
STATE_DIR = Path(os.environ.get("EPIC_PIPELINE_STATE_DIR") or CFG["paths"]["state"])
BOARD = CFG["hermes"]["board"]
KANBAN_ROOT = HERMES_ROOT / "kanban"  # default board (kanban.db) and boards/<name>/kanban.db
KANBAN_DB = KANBAN_ROOT / "boards" / BOARD / "kanban.db"
BRANCH_PREFIX = CFG["git"]["branch_prefix"]
TEMPLATE_DIR = SCRIPTS_DIR / "templates"

STATUS_FILE = "epic_gate_status.json"      # controller heartbeat
JOURNAL_FILE = "epic_gate_journal.json"    # partial-creation journal
LOCK_FILE = "epic_gate.lock"
ALERT_FILE = "epic_gate_alerts.json"       # what the gate controller already said with an ALERT line ({key: state}): each thing is said once
MODEL_TRIAL_FILE = "model_trial.jsonl"
DEPLOY_FILE = "pipeline_deploy_ts"          # written by safe_deploy.sh after a successful deploy (epoch seconds)
SNAPSHOT_FILE = "epic_health_snapshot.txt"   # full health snapshot (the monitor prints only ALERT/ERROR lines)
CLOSE_CACHE_FILE = "epic_close_cache.json"   # per task row: checklist / 完了レポート state, cached per page edit time
HEARTBEAT_STALE_SECONDS = 600
JOURNAL_STALE_SECONDS = 600

REGISTRY_DS = CFG["notion"]["epic_ds"]  # エピック
TASK_DS = CFG["notion"]["task_ds"]  # タスク
TASK_EPIC_PROP = CFG["notion"].get("task_epic_prop", "エピック")  # タスク DB の「エピックへの relation」の名前(環境ごとに違う。envs/*.toml の [notion].task_epic_prop)
REVIEW_DS = CFG["notion"]["review_ds"]  # レビュー: every request for a human decision lives here

# Work order. A phase starts only after the rows of ALL earlier phases are finished (the rows decide) and no live kanban card of an
# earlier phase exists. Light version: one layer of epics (エピック); no company epic, no company task DB.
ORDER = ["要件定義", "設計", "実装", "テスト", "リリース判定"]
# The values of the levels (L1 / L2 / L3: rows, deliverables, chapters, reviews) are read from scripts/levels.toml (sfconfig.get_levels); tools/lint_levels.py
# fails when the file and this code drift apart. A level whose `rows` is empty (L1) makes no phase row: the gate controller and the planner ask makes_phase_rows().
LEVEL_DEFS = sfconfig.get_levels()
LEVEL_PROP = "レベル"  # the select property of the エピック DB (L1 / L2 / L3; empty on epics made before the levels)
ROW_SEPS = ("〜", "～", "~")  # a row title that names a range of phases: "<first>〜<last>" (U+301C; U+FF5E and ~ read the same)
# There are always exactly these five phase rows per epic (no optional post-release phase).
# Phase row statuses that count as "phase finished". レビュー中 is NOT finished: it means a person must decide
# something; workers never touch the row, only the reviewer sets 完了 (AI review passed).
DONE_ROW = ("完了", "キャンセル")
FINISHED = set(DONE_ROW)

# Hub (エピック) ステータス written by the controller from the earliest unfinished phase row.
# The hub's 7 values (= how far the AI has got): 要件定義, 設計, 実装, テスト, 承認待ち, 完了, 中止.
# 承認待ち = the final stage: the リリース判定 phase row is being finished, or is finished and the person's release approval is pending.
HUB_WAITING_STATUS = "承認待ち"
HUB_PHASE_STATUS = {"要件定義": "要件定義", "設計": "設計", "実装": "実装", "テスト": "テスト", "リリース判定": HUB_WAITING_STATUS}
HUB_STATUSES = ("要件定義", "設計", "実装", "テスト", HUB_WAITING_STATUS, "完了", "中止")  # every value the hub can have
HUB_KEEP_STATUSES = {"完了", "中止"}  # never overwritten by the controller (中止 is only ever set by a person)
HUB_DONE_STATUS = "完了"  # written by the controller only when every phase row is finished AND the person approved the release review row
HUB_APPROVAL_STATUSES = set()  # the light hub has no approval-type status values (approvals are rows of the review DB)

# Gates (a person approves): (phase row that turns 完了, gate name). The ask key is "<epic8>-承認" (epic8 = last 8 hex of the hub row id).
GATE_ROWS = (("リリース判定", "リリース承認"),)  # the only human gate: at completion (no per-phase approvals)
GATE_NAMES = tuple(g for _, g in GATE_ROWS)
ASK_STATE_FILE = "ask_human.json"  # cache of keys ask_human.py already turned into a review row (the DB 'キー' column is the truth)
ASK_HUMAN = SCRIPTS_DIR / "ask_human.py"

# Per-phase model table (design ch.5): every phase runs on [models].worker (Sonnet, reasoning high); 設計 follows [models].design,
# which is the same Sonnet by default. A card gets --model/--provider only when its model differs from the executor profile's default
# ([models].worker): a pinned model also drives the review run of that card (Hermes uses the card's model for every run), so an
# unpinned card is worked on the executor's default and reviewed on the reviewer profile's own model ([models].reviewer).
# `hermes kanban create` takes --model/--provider only: the reasoning effort comes from the profile (agent.reasoning_effort);
# the value here documents the agreement and is written to model_trial.jsonl.
MODEL_PROVIDER = CFG["models"]["provider"]
MODEL_SONNET = CFG["models"]["worker"]
MODEL_DESIGN = CFG["models"]["design"]
REASONING = "high"


def row_phases(title):
    """Phases a row title names, in ORDER: "設計" -> ("設計",), "設計〜リリース判定" -> (設計, 実装, テスト, リリース判定). None when the title is not a phase or a range of phases in order."""
    t = (title or "").strip()
    if t in ORDER:
        return (t,)
    for sep in ROW_SEPS:
        a, found, b = t.partition(sep)
        if found and a in ORDER and b in ORDER and ORDER.index(a) <= ORDER.index(b):
            return tuple(ORDER[ORDER.index(a):ORDER.index(b) + 1])
    return None


def level_of(row):
    """The レベル (L1 / L2 / L3) of an エピック row, or None when it is not set (epics made before the levels). Raises PipelineError for a name levels.toml does not know."""
    name = ((row.get("properties", {}).get(LEVEL_PROP) or {}).get("select") or {}).get("name")
    if name is not None and name not in LEVEL_DEFS:
        raise PipelineError(f"the epic has the レベル {name!r}, which scripts/levels.toml does not define ({', '.join(LEVEL_DEFS)})")
    return name


def makes_phase_rows(level):
    """True when the level makes phase rows (levels.toml `rows` is not empty). A level not set yet (None: old epics) keeps its rows. The ONE place that decides this (L1: no)."""
    if level is None:
        return True
    if level not in LEVEL_DEFS:
        raise PipelineError(f"unknown level {level!r} (scripts/levels.toml defines {', '.join(LEVEL_DEFS)})")
    return bool(LEVEL_DEFS[level]["rows"])


def max_reviews(level):
    """How many times a card of `level` may call kanban_request_review (levels.toml `max_reviews`: L1 1, L2 and L3 3); a level not set yet (None) is L3.
    The request after that many is blocked by the gate controller (epic_gate_controller.enforce_review_limit)."""
    if level is not None and level not in LEVEL_DEFS:
        raise PipelineError(f"unknown level {level!r} (scripts/levels.toml defines {', '.join(LEVEL_DEFS)})")
    return LEVEL_DEFS["L3" if level is None else level]["max_reviews"]


def rows_level(titles):
    """The level whose rows (levels.toml `rows`) are exactly these row titles, or None (an old-form epic, or rows that belong to no level)."""
    want = set(titles)
    return next((lv for lv in sfconfig.LEVEL_NAMES if LEVEL_DEFS[lv]["rows"] and set(LEVEL_DEFS[lv]["rows"]) == want), None)


def level_rank(level):
    """1 / 2 / 3 for L1 / L2 / L3 (a level not set yet is L3): which of two levels is the lower one."""
    return sfconfig.LEVEL_NAMES.index("L3" if level is None else level) + 1


PHASE_MODEL = {ph: dict(model=MODEL_SONNET, provider=MODEL_PROVIDER, reasoning=REASONING) for ph in ORDER}
PHASE_MODEL["設計"] = dict(model=MODEL_DESIGN, provider=MODEL_PROVIDER, reasoning=REASONING)
# Retired trial: independent items of one phase as a second parallel card. One card per epic works the phases in order (see MERGE_PHASES);
# the mechanism stays, the table is empty. {phase: {group: {checklist item texts}}}
PARALLEL = {}
MERGE_PHASES = True  # approved: all consecutive ready phases of an epic run as ONE card (one session, one review)
PHASE_ROLE = {ph: "executor" for ph in ORDER}
LIVE_CARD_STATUSES = ("running", "ready", "todo", "blocked", "review", "triage", "scheduled")  # everything but done/archived


class PipelineError(RuntimeError):
    """A failure the pipeline scripts report loudly (ERROR line + non-zero exit)."""


# ---------------------------------------------------------------- subprocess / Notion

def _sh(cmd, *, input=None, timeout=90):
    """Single choke point for running external commands (tests replace it)."""
    return subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)


def _sh_bytes(cmd, input, timeout=120):
    """Like _sh but with binary stdin / stdout (file uploads). The selftest forbids it like _sh."""
    return subprocess.run(cmd, input=input, capture_output=True, timeout=timeout)


def _sleep(seconds):
    time.sleep(seconds)


def which_tool(name):
    """Path of an external tool (ntn, hermes): on PATH first, then in hermes.path_prepend (cron starts with the gateway's PATH); else the bare name."""
    return shutil.which(name) or shutil.which(name, path=sfconfig.tool_search_path(CFG)) or name


# ntn prints "Failed to execute public API request" for network blips (DNS / connection reset): retried like 429/5xx
_TRANSIENT = re.compile(r"\b(429|5\d\d)\b|rate.?limit|timed? ?out|temporar|unavailable|connection|reset by|bad gateway|overloaded"
                        r"|failed to execute public api request|nodename|name or service|econnreset|network", re.I)
NTN_BACKOFF = (2, 6)  # seconds before retry 2 and 3


def ntn(*args, data=None, timeout=60, retries=3):
    """`ntn api ...` returning parsed JSON. Retries transient failures (429/5xx/timeouts); raises PipelineError."""
    cmd = [which_tool("ntn"), "api", *args]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    last = None
    for attempt in range(retries):
        try:
            r = _sh(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = PipelineError(f"ntn timeout after {timeout}s: {' '.join(str(a) for a in args)[:80]}")
        else:
            if r.returncode == 0:
                try:
                    return json.loads(r.stdout)
                except ValueError as e:
                    last = PipelineError(f"ntn returned invalid JSON: {e}")
            else:
                msg = ((r.stderr or "").strip() or (r.stdout or "").strip())[:200]
                last = PipelineError(msg or f"ntn exit {r.returncode}")
                if not _TRANSIENT.search(msg):
                    raise last
        if attempt < retries - 1:
            _sleep(NTN_BACKOFF[min(attempt, len(NTN_BACKOFF) - 1)])
    raise last


def query(ds, flt=None, page_size=100):
    """Paginated data source query (follows next_cursor until has_more is false)."""
    out, cur = [], None
    while True:
        body = {"page_size": page_size}
        if flt is not None:
            body["filter"] = flt
        if cur:
            body["start_cursor"] = cur
        d = ntn(f"v1/data_sources/{ds}/query", "-X", "POST", data=body)
        out += d["results"]
        if not d.get("has_more"):
            return out
        cur = d["next_cursor"]


def title_of(p, key):
    return "".join(x["plain_text"] for x in (p.get("properties", p).get(key) or {}).get("title", []))


def status_of(p, key="ステータス", default="-"):
    return (((p.get("properties", p).get(key) or {}).get("status")) or {}).get("name", default)


_NO_DESCEND = {"child_page", "child_database", "unsupported"}


def page_text(pid, max_depth=2, max_extra_calls=30):
    """Plain text of a page's blocks: follows has_more (>100 blocks) and descends into nested blocks (indented)."""
    budget = [max_extra_calls]

    def walk(block_id, depth):
        lines, cur = [], None
        while True:
            args = [f"v1/blocks/{block_id}/children", "page_size==100"]
            if cur:
                args.append(f"start_cursor=={cur}")
            d = ntn(*args)
            for b in d.get("results", []):
                t = b.get(b["type"], {}).get("rich_text", [])
                lines.append("  " * depth + "".join(x["plain_text"] for x in t))
                if b.get("has_children") and depth < max_depth and b["type"] not in _NO_DESCEND and budget[0] > 0:
                    budget[0] -= 1
                    lines += walk(b["id"], depth + 1)
            if not d.get("has_more"):
                return lines
            cur = d["next_cursor"]

    return "\n".join(l for l in walk(pid, 0) if l.strip())


def epic_id_from_relation(rel_id):
    """32-hex epic id (the hub row id without dashes) from a relation page id; raises PipelineError when malformed."""
    m = re.search(r"([0-9a-f]{32})", str(rel_id).replace("-", ""))
    if not m:
        raise PipelineError(f"cannot read epic id from {rel_id!r}")
    return m.group(1)


def phase_key(epic_id, ph, group="A"):
    """Kanban idempotency key of a phase card: phase-<epic_id>-<phase>[-B]."""
    return f"phase-{epic_id}-{ph}" + ("" if group == "A" else "-" + group)


def merged_phase_key(epic_id, phases):
    """Kanban idempotency key of ONE card that works several consecutive phases: phase-<epic_id>-<ph1>+<ph2>+..."""
    return phase_key(epic_id, "+".join(phases))


def is_phase_card_key(key, epic_id, ph):
    """True when the card key belongs to phase `ph`: the single-phase key (with an optional -B group) or a merged key listing it."""
    if not key:
        return False
    base = phase_key(epic_id, ph)
    if key == base or re.fullmatch(re.escape(base) + r"-[A-Z]", key) is not None:
        return True
    prefix = f"phase-{epic_id}-"
    return key.startswith(prefix) and "+" in key and ph in key[len(prefix):].split("+")


def tenant_of(epic_id):
    return "epic-" + epic_id[-8:]


def model_for(phases):
    """Model entry (model, provider, reasoning) of a card working `phases`: the 設計 entry ([models].design) when the card covers 設計,
    else the first phase's."""
    return PHASE_MODEL["設計"] if "設計" in phases else PHASE_MODEL[phases[0]]


def card_model_args(phases):
    """`hermes kanban create` model flags of a card working `phases`: none when its model is the executor's default ([models].worker),
    so the executor works it on its own model and the reviewer reviews it on its own; else --model/--provider."""
    m = model_for(phases)
    return [] if m["model"] == MODEL_SONNET else ["--model", m["model"], "--provider", m["provider"]]


def hub_target_status(row_status):
    """Hub ステータス the controller should show: the earliest unfinished phase row mapped to a hub value (None = leave alone).

    row_status: {phase: row status}. Only phases that have a row count; when every row is finished nothing is written."""
    for ph in ORDER:
        st = row_status.get(ph)
        if st is not None and st not in DONE_ROW:
            return HUB_PHASE_STATUS[ph]
    return None


def phases_all_finished(row_status):
    """True when all five ORDER phase rows exist and each is 完了 or キャンセル."""
    return all(row_status.get(ph) in DONE_ROW for ph in ORDER)


def gates_due(row_status):
    """Gate names that are due according to the phase rows (row 完了), in a stable order, without duplicates."""
    out = []
    for ph, gate in GATE_ROWS:
        if row_status.get(ph) == "完了" and gate not in out:
            out.append(gate)
    return out


REVIEW_DECIDED = ("承認", "差し戻し", "取り下げ")  # レビュー ステータス values a person sets; レビュー待ち is the open one


def review_title(key, text, title=None):
    """Row title: explicit, else the first plain line of the ask (headings / tags skipped), else the key."""
    first = next((l.strip() for l in (text or "").splitlines() if l.strip() and not l.lstrip().startswith(("#", "<"))), "")
    return ((title or "").strip() or first or key)[:120]


# ---------------------------------------------------------------- review row = the release judgement, copied mechanically
# The chapters of the release approval row (レビュー DB; review_chapters(level): L2 6, L3 9) and of the release judgement page (判定書; judgement_chapters(level),
# 8 today) are read from scripts/levels.toml, not written here. The row's content is copied by machine from the chapters of the
# release judgement page (判定書; shape: scripts/templates/release_judgement.md): no AI writes or summarizes (the gate
# controller uses none). What cannot be read is written as "未確認：…" and the row is still created. The wording of the row lives in
# templates/gate_ask.md, and the three fixed sentences of chapter 1 (what 承認 / 差し戻し / 取り下げ do) in templates/release_meaning.md (release_meaning());
# the functions below only decide which text goes into which placeholder.

REVIEW_HEADING_COLOR = "yellow_bg"  # markdown name of the block color yellow_background
OLD_REVIEW_CHAPTERS = ("判断の対象", "何を判断するか", "なぜ", "根拠", "推奨の選択肢", "判断の結果", "反映の記録")  # rows made before the nine chapters: still read
JUDGEMENT_COPIED = ("推奨", "人が確かめること", "何を出すか", "完了条件の達成状況", "残っている危険", "未確認")  # the judgement chapters build_release_ask reads by name (a level's judgement_chapters must hold them: tools/lint_levels.py)
FREEFORM_CHAPTERS = ("判断すること", "推奨", "人が確かめること", "根拠")  # what the AI writes for a free-form ask (-done, -send-*, レビュー中)
STATUS_WORDS = ("達成", "一部", "未達", "未実施", "対象外")  # the words of the 状況 column of the judgement's table
REVIEW_BODY_MAX = 5000  # characters of the whole body (table notation not counted)
CHAPTER_CAPS = dict(judge=400, recommend=400, checks=700, conditions=1800, ships=600, risks=900, links=12)  # characters; links = number of links
CHECK_ITEMS_MAX, CHECK_ITEM_CHARS, CELL_CHARS, TABLE_ROWS_MAX, LINKS_MAX, EPIC_COND_CHARS = 5, 120, 200, 10, 12, 600
FREEFORM_CAPS = dict(judge=400, recommend=400, checks=700, links=1500)
HUB_NAME_CHARS, TITLE_HEAD_CHARS, TITLE_MAX = 80, 70, 120
RELEASE_MEANING_DEFAULTS = dict(pr="下書きのプルリクエスト", deploy="本番へ反映します")  # values of ${pr} / ${deploy} in templates/release_meaning.md when the caller passes none (ship_meaning() passes the real ones)
SQUEEZE = (("conditions", 300, 600), ("risks", 150, 300), ("ships", 100, 300), ("checks", 150, 300), ("recommend", 100, 200), ("links", 2, 6))  # (chapter, step, floor)

_TABLE_TAG_RE = re.compile(r"</?(?:table|colgroup|col|tr|td)\b[^>]*>")
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S)
_TR_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td>(.*?)</td>", re.S)
_ITEM_RE = re.compile(r"^\s*(?:[-*・]|\d+[.．)）])\s*(.+?)\s*$")
_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_BARE_URL_RE = re.compile(r"(?<![\w(\[<\"'`/])https?://[^\s)\]>\"'`]+")
_LINK_RE = re.compile(r"\[[^\]\n]+\]\(https?://[^)\s]+\)")
_URL_TAIL = "。、，．.,;:!?）」』"
PLACEHOLDER_RE = re.compile(r"〈[^〉\n]*〉")  # a writing hint of scripts/templates/release_judgement.md that was left in the judgement


def _level_chapters(level, key):
    """The chapters (levels.toml `key`) of `level`. A level that has none (L1 makes no judgement) or none given (None: an epic made before the levels,
    a free-form ask without an epic) reads the L3 shape, which is what every ask and judgement had before the levels."""
    if level is not None and level not in LEVEL_DEFS:
        raise PipelineError(f"unknown level {level!r} (scripts/levels.toml defines {', '.join(LEVEL_DEFS)})")
    names = LEVEL_DEFS[level or "L3"][key] or LEVEL_DEFS["L3"][key]
    return tuple(names)


def judgement_chapters(level=None):
    """H1 chapters of the release judgement page of `level` (levels.toml `judgement_chapters`)."""
    return _level_chapters(level, "judgement_chapters")


def review_chapters(level=None):
    """H1 chapters of the release approval row of `level`, in the order of templates/gate_ask.md (levels.toml `review_chapters`)."""
    return _level_chapters(level, "review_chapters")


def review_template_chapters():
    """Every H1 chapter of templates/gate_ask.md in its order: the most a level can ask for (a level's review_chapters is a subsequence of this)."""
    return tuple(re.findall(r'^# (\S+) \{color="%s"\}\s*$' % REVIEW_HEADING_COLOR, load_template("gate_ask"), re.M))


def render_review(level, **values):
    """templates/gate_ask.md rendered, keeping only the chapters levels.toml gives `level` (review_chapters). Raises PipelineError when the
    config names a chapter the template does not have (tools/lint_levels.py catches it before)."""
    want = review_chapters(level)
    text = render("gate_ask", **values)
    parts = re.split(r'(?m)^(?=# \S+ \{color="%s"\}\s*$)' % REVIEW_HEADING_COLOR, text)
    by_name = {}
    for part in parts:
        m = re.match(r'# (\S+) \{color="%s"\}' % REVIEW_HEADING_COLOR, part)
        if m:
            by_name[m.group(1)] = part
    missing = [n for n in want if n not in by_name]
    if missing:
        raise PipelineError(f"levels.toml gives {level or 'L3'} the review chapters {missing}, which templates/gate_ask.md does not have")
    return "".join(by_name[n].rstrip("\n") + "\n\n" for n in want).rstrip("\n")


class ReviewAskError(PipelineError):
    """The text of a review ask cannot become a review row (the row is not created; the message says what shape is expected)."""


def review_heading(name):
    return f'# {name} {{color="{REVIEW_HEADING_COLOR}"}}'


def has_review_chapters(text, level=None):
    """True when `text` already is a review body: the chapter headings of a level's review_chapters in template order (an old seven-chapter body is
    not). Without `level`, the chapters of any level count (the callers read a body, they do not know whose epic it was)."""
    heads = tuple(re.findall(r'^# (\S+) \{color="%s"\}\s*$' % REVIEW_HEADING_COLOR, text or "", re.M))
    if level is not None:
        return heads == review_chapters(level)
    return any(heads == tuple(d["review_chapters"]) for d in LEVEL_DEFS.values() if d["review_chapters"])


def chapter_text(body, name):
    """Text of the H1 chapter `name` of a review body (without its heading); '' when the chapter is absent."""
    m = re.search(r'^# %s(?: \{[^}]*\})?[ \t]*$' % re.escape(name), body or "", re.M)
    if not m:
        return ""
    rest = body[m.end():]
    n = re.search(r"^# \S", rest, re.M)
    return (rest[:n.start()] if n else rest).strip()


def visible_len(s):
    """Characters of a text without the table notation (the unit of every limit of the review body)."""
    return len(_TABLE_TAG_RE.sub("", s or ""))


def cut_text(s, n):
    """`s` cut to n characters; a Markdown link that the cut would split is dropped whole."""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    s = s[:n]
    s = re.sub(r"\[[^\]\n]*$", "", s)
    s = re.sub(r"\[[^\]\n]*\]\([^)\n]*$", "", s)
    return s.rstrip()


def omit_note(name):
    return f"（以下略：判定書の「{name}」）"


def cut_lines(text, limit, name, note=None):
    """The lines of `text` that fit in `limit` characters (a line is an item); the rest becomes the note 以下略 (default: omit_note(name)). A
    first line that is too long is cut inside (the note follows)."""
    lines = [l.rstrip() for l in (text or "").strip().splitlines()]
    out, n, cut = [], 0, False
    for l in lines:
        add = len(l) + 1
        if n + add > limit:
            if not out:
                out.append(cut_text(l, limit))
            cut = True
            break
        out.append(l)
        n += add
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out + ([note or omit_note(name)] if cut else []))


def linkify(line):
    """Bare URLs of one line become Markdown links (the review rows allow links only); code spans and existing links stay."""
    def one(seg):
        def sub(m):
            url = m.group(0)
            tail = ""
            while url and url[-1] in _URL_TAIL:
                tail, url = url[-1] + tail, url[:-1]
            return f"[{url}]({url})" + tail
        return _BARE_URL_RE.sub(sub, seg)
    return "".join(p if i % 2 else one(p) for i, p in enumerate(re.split(r"(`[^`\n]*`)", line)))


def table_rows(text):
    """Rows (lists of cell strings) of the first table in `text`: a Notion <table> or a pipe table. The header row is dropped when the
    table says header-row=true (pipe table: the row above the --- line)."""
    m = _TABLE_RE.search(text or "")
    if m:
        rows = [[re.sub(r"\s+", " ", re.sub(r"<br\s*/?>", " ", c)).strip() for c in _TD_RE.findall(r)] for r in _TR_RE.findall(m.group(0))]
        rows = [r for r in rows if r]
        return rows[1:] if 'header-row="true"' in m.group(0)[:m.group(0).find(">") + 1] else rows
    pipe = [l.strip() for l in (text or "").splitlines() if l.strip().startswith("|")]
    rows = [[c.strip() for c in l.strip("|").split("|")] for l in pipe]
    if len(rows) >= 2 and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in rows[1] if c.strip()):
        return rows[2:]
    return []


def flatten_tables(text):
    """Every <table> of a copied section becomes one bullet per row (cells joined with ／): a cut can never split a table."""
    def one(m):
        return "\n".join("- " + " ／ ".join(c for c in r if c) for r in table_rows(m.group(0)) if any(r))
    return _TABLE_RE.sub(one, text or "")


def status_word(cell):
    """達成 / 一部 / 未達 / 未実施 / 対象外 when the 状況 cell starts with one of them (markup ignored), else None."""
    s = re.sub(r"[*`_\s]", "", cell or "")
    return next((w for w in STATUS_WORDS if s.startswith(w)), None)


def count_status(rows):
    """({word: n, '不明': n}, achieved, denominator): rows are (condition, status cell, basis); the denominator leaves out 対象外."""
    counts = {w: 0 for w in STATUS_WORDS}
    counts["不明"] = 0
    for r in rows:
        w = status_word(r[1]) if len(r) > 1 else None
        counts[w or "不明"] += 1
    den = len(rows) - counts["対象外"]
    return counts, counts["達成"], den


def recommendation_word(section):
    """'承認' / '差し戻し' when the first non-empty line of the 推奨 chapter starts with 推奨：承認 / 推奨：差し戻し, else None."""
    for l in (section or "").splitlines():
        s = re.sub(r"^[-*・]\s*", "", l.strip()).replace("**", "").strip()
        if not s:
            continue
        m = re.match(r"^推奨\s*[:：]\s*(承認|差し戻し)", s)
        return m.group(1) if m else None
    return None


def heading_name(raw):
    """Chapter name of a heading text: color attribute, leading number ('1. ') and bold marks removed."""
    s = re.sub(r"\s*\{[^}]*\}\s*$", "", raw or "").replace("**", "").strip()
    return re.sub(r"^\s*[0-9０-９]+\s*[\.．、)）]\s*", "", s).strip()


def _headings(lines):
    out, fence = [], False
    for i, l in enumerate(lines):
        if l.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,3})[ \t]+(.*?)\s*$", l)
        if m:
            out.append((i, len(m.group(1)), heading_name(m.group(2))))
    return out


def _sections(md, names, stop_at_names=False):
    """{name: text under that heading}; a section ends at the next heading of the same or a higher level (stop_at_names: or at the next
    heading that is itself one of `names`). The first heading of a name wins. Never raises."""
    lines = (md or "").split("\n")
    heads = _headings(lines)
    out = {}
    for k, (i, lv, name) in enumerate(heads):
        if name not in names or name in out:
            continue
        end = len(lines)
        for j, lv2, n2 in heads[k + 1:]:
            if lv2 <= lv or (stop_at_names and n2 in names):
                end = j
                break
        out[name] = ("\n".join(lines[i + 1:end]).strip(), lv)
    return out


def judgement_sections(md, level=None):
    """({chapter: text}, [chapters that are missing]) of a release judgement page's markdown, cut at the exact chapter names of the level
    (judgement_chapters(level); a leading number is ignored; H1 or H2). A page of another shape only reports what is missing; nothing raises."""
    names = judgement_chapters(level)
    found = _sections(md, names)
    return {n: found[n][0] for n in found}, [n for n in names if n not in found]


def number_images(md):
    """(md, captions): every image line becomes the paragraph '［画像 n］<caption>' (ask_human.py puts the n-th image after it); copy-indicator
    lines are dropped."""
    out, caps = [], []
    for line in (md or "").split("\n"):
        if "copy_indicator" in line:
            continue
        m = _MD_IMAGE_RE.match(line)
        if m:
            caps.append(m.group(2))
            out.append((m.group(1) + IMAGE_MARK.format(n=len(caps)) + m.group(2)).rstrip())
        else:
            out.append(line)
    return "\n".join(out), caps


def copy_section(text, sec_level, base=2):
    """The mechanical copy of one section: tables flattened, headings moved below the review chapters' H1 (a sub-heading becomes H<base>..H3,
    colors dropped; H1 stays reserved for the nine chapters), bare URLs linked."""
    out = []
    for line in flatten_tables(text).split("\n"):
        h = _MD_HEADING_RE.match(line)
        if h:
            lv = max(2, min(3, base + len(h.group(1)) - sec_level - 1))
            attrs = re.sub(r'\s*color="[^"]*"', "", h.group(3) or "").strip()
            out.append("#" * lv + " " + h.group(2) + ("" if attrs in ("", "{}") else " " + attrs))
        else:
            out.append(linkify(line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def list_items(text):
    """Items of a chapter: its bullets / numbered lines, else its plain lines. 'なし…' alone is no item."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    items = [m.group(1) for m in (_ITEM_RE.match(l) for l in lines) if m] or [l.strip() for l in lines if not l.lstrip().startswith(("#", "<"))]
    return [] if len(items) == 1 and items[0].startswith("なし") else items


def checks_text(items, cap):
    """The 人が確かめること chapter from a list of items: at most 5, 120 characters each, `cap` in total; the rest is counted."""
    shown, n = [], 0
    for it in items[:CHECK_ITEMS_MAX]:
        it = cut_text(it, CHECK_ITEM_CHARS) + ("…" if len(it) > CHECK_ITEM_CHARS else "")
        if n + len(it) + 4 > cap:
            break
        shown.append(it)
        n += len(it) + 4
    lines = [f"{i}. {it}" for i, it in enumerate(shown, 1)]
    if len(items) > len(shown):
        lines.append(f"（ほか {len(items) - len(shown)} 件：判定書の「人が確かめること」）")
    return "\n".join(lines)


def _cell(s):
    return (s or "").replace("<", "＜").replace(">", "＞").replace("\n", " ").strip()


def conditions_table(rows, cap):
    """<table> of the first TABLE_ROWS_MAX rows (完了条件 / 状況 / 根拠): cells cut to CELL_CHARS, the 根拠 column shortened until the text fits `cap`."""
    shown = [(list(r) + ["", "", ""])[:3] for r in rows[:TABLE_ROWS_MAX]]
    basis = CELL_CHARS
    while basis > 40 and sum(len(cut_text(a, CELL_CHARS)) + len(cut_text(b, 60)) + len(cut_text(c, basis)) for a, b, c in shown) > cap:
        basis -= 20
    body = "\n".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (_cell(cut_text(a, CELL_CHARS)), _cell(cut_text(b, 60)), _cell(cut_text(c, basis)))
                     for a, b, c in shown)
    return ('<table fit-page-width="true" header-row="true">\n<colgroup><col width="320" /><col width="140" /><col width="440" /></colgroup>\n'
            '<tr><td>完了条件</td><td>状況</td><td>根拠</td></tr>\n' + body + "\n</table>")


def epic_condition_count(cell):
    """Number of the numbered completion conditions (①〜⑳) written in the epic's 完了条件 cell; 0 when none is numbered."""
    return len(set(re.findall(r"[①-⑳]", cell or "")))


def counts_line(counts):
    return "完了条件：" + "・".join(f"{w} {counts[w]}" for w in ("達成", "一部", "未達", "未実施")) + (f"・対象外 {counts['対象外']}" if counts["対象外"] else "")


def counts_brief(body):
    """'達成3・一部1・未実施1' from the line '完了条件：達成 3・一部 1・未達 0・未実施 1' of a review body (zeros dropped); '' when the row has none."""
    m = re.search(r"^完了条件：(.+)$", chapter_text(body, "推奨"), re.M)
    if not m:
        return ""
    return "・".join(f"{w}{n}" for w, n in re.findall(r"(達成|一部|未達|未実施|対象外) (\d+)", m.group(1)) if int(n))


def release_title(hub_name, page, rec, counts, ach, den):
    """Title of the release approval row: '<epic name, first 70 chars>: リリース承認（推奨 承認・条件 3/5 達成）'; （推奨 未確認） when the judgement has no
    readable 推奨, （判定書なし） when there is no judgement page."""
    head = f"{(hub_name or '').strip()[:TITLE_HEAD_CHARS]}: リリース承認"
    if not page:
        tail = "（判定書なし）"
    elif rec is None:
        tail = "（推奨 未確認）"
    elif counts is None or den <= 0:
        tail = f"（推奨 {rec}・条件 未確認）"
    else:
        tail = f"（推奨 {rec}・条件 {ach}/{den} 達成）"
    return (head + tail)[:TITLE_MAX]


def fmt_local(iso_or_epoch):
    """'YYYY-MM-DD HH:MM' (local time) of a Notion ISO timestamp or an epoch; '不明' when unreadable."""
    from datetime import datetime, timezone
    try:
        if isinstance(iso_or_epoch, (int, float)):
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(iso_or_epoch))
        dt = datetime.fromisoformat(str(iso_or_epoch).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(dt.timestamp()))
    except (ValueError, TypeError, OverflowError):
        return "不明"


def release_links(f, limit=LINKS_MAX):
    """[(label, url)] of the 根拠 chapter, at most LINKS_MAX: the judgement, the PR body draft, the branch and the epic always; the deliverables
    of the phase rows, then the phase rows, fill the rest."""
    def lab(s):
        return re.sub(r"\s+", " ", (s or "").replace("[", "［").replace("]", "］")).strip()[:80]
    page = f.get("page")
    first = [(lab(f"リリース判定書：{page['title']}"), page["url"])] if page else []
    first += [(lab(a), b) for a, b in f.get("pr_drafts") or []]
    last = []
    if f.get("repo") and f.get("branch"):
        last.append((lab(f"ブランチ {f['branch']}"), f"https://github.com/{f['repo']}/tree/{f['branch']}"))
    last.append(("エピック（目的・完了条件・管理ログ）", f["hub_url"]))
    room = max(0, limit - len(first) - len(last))
    mid = [(lab(a), b) for a, b in (f.get("deliverables") or []) + (f.get("phase_rows") or [])][:room]
    return first + mid + last


def _absent(f, secs, name):
    if f.get("md") is None:
        return "未確認：判定書がありません。" if not f.get("page") else "未確認：判定書の本文を読めませんでした。"
    return f"未確認：判定書の「{name}」の章が空です。" if name in secs else f"未確認：判定書に「{name}」の章がありません。"


def build_release_ask(f, now=None):
    """The release approval row from the facts `f` (collect_release_facts). Pure. -> dict(title, body, rec, counts, achieved, denominator, marks).

    f: hub_name, hub_url, key, repo, branch, page (dict title/url/edited or None), md (markdown of the judgement or None), cond_cell (the epic's
    完了条件 text), pr_drafts, deliverables, phase_rows ([(label, url)]), ui (the judgement is about a UI change). Every chapter that cannot be
    read is written as 未確認 and the row is still built. A body over REVIEW_BODY_MAX is rebuilt with shorter chapters, in SQUEEZE order."""
    caps = dict(CHAPTER_CAPS)
    while True:
        out = _compose(f, caps, time.time() if now is None else now)
        if visible_len(out["body"]) <= REVIEW_BODY_MAX:
            return out
        for name, step, floor in SQUEEZE:
            if caps[name] - step >= floor:
                caps[name] -= step
                break
        else:
            return out  # nothing left to shorten (cannot happen with the fixed parts: the floors leave room)


def _compose(f, caps, now):
    """One build of the nine chapters with the given chapter caps (see build_release_ask)."""
    md, images = number_images(f["md"]) if f.get("md") is not None else (None, [])
    secs_raw = _sections(md, judgement_chapters(f.get("level"))) if md is not None else {}
    secs = {n: v[0] for n, v in secs_raw.items()}
    lv = {n: v[1] for n, v in secs_raw.items()}
    hub = (f["hub_name"] or "").strip()[:HUB_NAME_CHARS]

    def copy(name, cap, base=2):
        return cut_lines(copy_section(secs[name], lv[name], base), cap, name) if secs.get(name) else _absent(f, secs, name)

    # 4 (first: the counts feed 2 and the title)
    rows = table_rows(secs.get("完了条件の達成状況", "")) if secs.get("完了条件の達成状況") else []
    counts, ach, den = count_status(rows) if rows else (None, 0, 0)
    cond_lines = []
    if rows:
        cond_lines.append(conditions_table(rows, caps["conditions"]))
        if len(rows) > TABLE_ROWS_MAX:
            cond_lines.append(omit_note("完了条件の達成状況") + f" 残り {len(rows) - TABLE_ROWS_MAX} 行")
        if counts["不明"]:
            cond_lines.append(f"未確認：状況の語が決まった語（{'・'.join(STATUS_WORDS)}）ではない行が {counts['不明']} 件あります。")
        n_epic = epic_condition_count(f.get("cond_cell"))
        if n_epic and n_epic != len(rows):
            cond_lines.append(f"未確認：判定書の表は {len(rows)} 行、エピックの完了条件は {n_epic} 件です。")
    else:
        cond_lines.append(_absent(f, secs, "完了条件の達成状況") if not secs.get("完了条件の達成状況")
                          else "未確認：判定書の「完了条件の達成状況」に表がありません。")
        if f.get("cond_cell"):
            cond_lines.append("エピックの完了条件（エピックの目的の表の写し）：\n" + cut_text(f["cond_cell"], EPIC_COND_CHARS))
    conditions = "\n\n".join(cond_lines)
    # 2
    rec = recommendation_word(secs.get("推奨", ""))
    rec_parts = [copy("推奨", caps["recommend"])]
    if secs.get("推奨") and rec is None:
        rec_parts.append("未確認：判定書の「推奨」の1行目が『推奨：承認』『推奨：差し戻し』の形ではありません。")
    if counts is not None:
        rec_parts.append(counts_line(counts))
        n_bad = den - ach
        if rec == "承認" and n_bad > 0:
            rec_parts.append(f"注意：達成でない完了条件が {n_bad} 件あります。")
    recommend = "\n\n".join(rec_parts)
    # 3
    if secs.get("人が確かめること"):
        items = list_items(copy_section(secs["人が確かめること"], lv["人が確かめること"]))
        checks = checks_text(items, caps["checks"]) if items else "なし（下の表と危険を読めば足ります）"
    else:
        items = [f"完了条件「{cut_text(r[0], 40)}」は「{cut_text(r[1], 20)}」です。判定書の根拠を読む" for r in rows
                 if (status_word(r[1]) if len(r) > 1 else None) not in ("達成", "対象外")] + ["判定書の「残っている危険」を読む"]
        checks = _absent(f, secs, "人が確かめること") + "\n\n" + checks_text(items, caps["checks"])
    # 1
    target = (f"`{f['repo']}` のブランチ `{f['branch']}`" if f.get("repo") and f.get("branch") else f"ブランチ `{f['branch']}`" if f.get("branch")
              else "未確認：ブランチが本文に見つかりません")
    judge = f"「{hub}」の成果（{target}）を、リリースしてよいと認めるかを決めてください。\n\n{release_meaning(**ship_meaning(f.get('repo'), f.get('pr_url'), f.get('pr')))}"
    # 5
    ships = [copy("何を出すか", caps["ships"])]
    pr = _PR_URL_RE.search(f.get("md") or "")
    if f.get("pr_url"):  # the draft PR that ship_approved.py prepare opened and read back
        ships.append(f"PR：[{f['pr_url']}]({f['pr_url']})（下書き。承認すると公開してマージします）")
    else:
        ships.append(f"PR：[{pr.group(0)}]({pr.group(0)})" if pr else "PR は未作成です（判定書にも PR の URL がありません）。")
    present = {int(n) for n in re.findall(r"［画像 (\d+)］", "\n".join(ships))}
    if f.get("ui") is True:  # a UI change keeps its before/after images even when the chapters copied above do not hold them
        for n, cap in enumerate(images, 1):
            if n not in present:
                ships.append(f"［画像 {n}］{cap}".rstrip())
    ships_text = "\n\n".join(ships)
    # 6
    half = caps["risks"] // 2
    names = judgement_chapters(f.get("level"))
    left = [n for n in names if PLACEHOLDER_RE.search(secs.get(n, ""))]
    unread = [_absent(f, secs, n) for n in names if n not in JUDGEMENT_COPIED and md is not None and not secs.get(n)]  # chapters of the level this row does not copy, still checked
    risks = ("## 残っている危険\n" + copy("残っている危険", half, base=3) + "\n\n## 未確認\n" + copy("未確認", caps["risks"] - half, base=3)
             + (f"\n\n未確認：判定書の「{'」「'.join(left)}」に、ひな形の書き置き（〈…〉）が残っています。" if left else "")
             + "".join(f"\n\n{u}" for u in unread))
    # 7
    links = "\n".join(f"- [{a}]({b})" for a, b in release_links(f, caps["links"]))
    edited = fmt_local((f.get("page") or {}).get("edited")) if f.get("page") else ""
    copied_at = (f"写した時刻 {fmt_local(now)}／判定書の最終更新 {edited}。食い違うときは判定書が正です。" if f.get("page")
                 else f"写した時刻 {fmt_local(now)}。判定書がないため、写した中身はありません。")
    vals = dict(judge=judge, recommend=recommend, checks=checks, conditions=conditions, ships=ships_text, risks=risks, links=links, copied_at=copied_at, key=f["key"])
    body = render_review(f.get("level"), **vals)
    # A level with fewer chapters (L2) drops the chapters that carry some 未確認 / 注意 lines (the judgement checks): they must not vanish from PROBLEMS
    # (finish_rows reads it), so they go to the end of 人が確かめること. Every chapter kept (L3): nothing is added, the body is what it was.
    shown = set(release_problems(body))
    extra = [p for p in release_problems(render("gate_ask", **vals)) if p not in shown]
    if extra:
        body = render_review(f.get("level"), **dict(vals, checks=checks + "\n\n" + "\n\n".join(extra)))
    marks = sorted({int(n) for n in re.findall(r"［画像 (\d+)］", body)})
    title = release_title(f["hub_name"], f.get("page"), rec, counts, ach, den)
    return dict(title=title, body=body + "\n", rec=rec, counts=counts, achieved=ach, denominator=den, marks=marks)

# ---- free-form asks (-done, -send-*, レビュー中 rows): the AI writes four headings, the other chapters read 該当なし
FREEFORM_HELP = ("自由文の依頼は、見出し「判断すること」「推奨」「人が確かめること」「根拠」の4つを書いてください（# か ## で、順番は問いません）。"
                 "「根拠」には Markdown のリンク（[名前](https://…)）を1つ以上入れます。決める内容を「判断すること」に、推奨を1つに決めて「推奨」に、"
                 "人が見る点を「人が確かめること」に書きます。")


def freeform_parts(text):
    """Placeholder values of gate_ask.md for a free-form ask. Raises ReviewAskError (the row is not created) when one of the four headings
    is missing or empty, or when the 根拠 has no Markdown link."""
    found = _sections(text, FREEFORM_CHAPTERS, stop_at_names=True)
    missing = [n for n in FREEFORM_CHAPTERS if n not in found or not found[n][0].strip()]
    if missing:
        raise ReviewAskError(f"見出しが足りません：{'・'.join(missing)}。{FREEFORM_HELP}")
    if not _LINK_RE.search(found["根拠"][0]):
        raise ReviewAskError(f"「根拠」に Markdown のリンクがありません。{FREEFORM_HELP}")

    def cp(name, cap):
        return cut_lines(copy_section(found[name][0], found[name][1]), cap, name, note=f"（以下略：依頼の「{name}」）")

    items = list_items(cp("人が確かめること", FREEFORM_CAPS["checks"] * 2))
    return dict(judge=cp("判断すること", FREEFORM_CAPS["judge"]), recommend=cp("推奨", FREEFORM_CAPS["recommend"]),
                checks=checks_text(items, FREEFORM_CAPS["checks"]) if items else "なし", conditions="該当なし", ships="該当なし", risks="該当なし",
                links=cp("根拠", FREEFORM_CAPS["links"]), copied_at="")


def review_markdown(key, text, epic_id=None, level=None):
    """Markdown body of a review row, the chapters of templates/gate_ask.md that levels.toml gives the epic's `level` (review_chapters). A body that
    already has them (build_release_ask) is used as is; a free-form text must carry the four headings of FREEFORM_CHAPTERS (else ReviewAskError)
    and fills 1, 2, 3 and 7."""
    body = (text or "").strip()
    if has_review_chapters(body):
        return body + "\n"
    return render_review(level, key=key, **freeform_parts(body)) + "\n"


# ---- the facts of a release ask (Notion reads; build_release_ask turns them into the row)

def table_cells(b):
    """Cell strings of a table_row block."""
    return ["".join(x.get("plain_text", "") for x in cell) for cell in (b.get("table_row") or {}).get("cells", [])]


def epic_condition_cell(blks):
    """Text of the 完了条件 row of the epic's 目的 table (blocks of the hub page), '' when it is not there."""
    for b in blks:
        if b.get("type") == "table_row":
            cells = table_cells(b)
            if cells and cells[0].strip() == "完了条件":
                return (cells[1] if len(cells) > 1 else "").strip()
    return ""


def rows_by_phase(rows):
    """Pure. Rows of a hub -> ({phase: row}, [names that more than one row claim]).

    A row is one card (行＝カード): every phase its title works (row_phases) maps to that row, so one row of a range stands for all its phases.
    Two rows with the same title are a DUP. When rows with different titles work the same phase, the one that is not finished (未着手 / 進行中 /
    レビュー中) is it; when none is open the newest; two open ones are a DUP (the epic is skipped)."""
    by_title, dups = {}, []
    for r in rows:
        nm = title_of(r, "タスク名").strip()
        ph = row_phases(nm)
        if ph is None:
            continue
        if nm in by_title:
            if nm not in dups:
                dups.append(nm)
            continue
        by_title[nm] = (r, ph)
    claims = {}
    for nm, (r, ph) in by_title.items():
        for p in ph:
            claims.setdefault(p, []).append(r)
    out = {}
    for p, rs in claims.items():
        if len(rs) == 1:
            out[p] = rs[0]
            continue
        open_rows = [r for r in rs if status_of(r) not in DONE_ROW]
        if len(open_rows) == 1:
            out[p] = open_rows[0]
        elif not open_rows:
            out[p] = max(rs, key=lambda r: r.get("created_time") or "")
        elif p not in dups:
            dups.append(p)
    return out, dups


def phase_rows(epic_row_id):
    """{phase: task row} of the epic (rows of the タスク DB linked to the epic; a row of a range stands for each phase it works)."""
    return rows_by_phase(query(TASK_DS, {"property": TASK_EPIC_PROP, "relation": {"contains": epic_row_id}}))[0]


def row_units(prows, order):
    """[(row title, phases, row | None)] in work order, each row once (a row of a range is one unit); a phase of `order` without a row is a
    unit (phase, (phase,), None)."""
    units, seen = [], set()
    for ph in order:
        if ph in seen:
            continue
        r = prows.get(ph)
        if r is None:
            units.append((ph, (ph,), None))
            seen.add(ph)
            continue
        title = title_of(r, "タスク名").strip()
        phases = row_phases(title) or (ph,)
        units.append((title, phases, r))
        seen.update(phases)
    return units


def distinct_rows(prows, phases):
    """The rows that work `phases`, in that order, each ONCE (a row of a range works several phases: reading it per phase reads it again)."""
    out, seen = [], set()
    for ph in phases:
        r = prows.get(ph)
        if r is not None and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def is_legacy(prows):
    """True for an epic whose rows are the old one-row-per-phase kind (every row is titled with a phase name; there is at least one): it runs
    as before (MERGE_PHASES: the ready rows join one card). A row of a range makes an epic new-form: one row, one card."""
    titles = {title_of(r, "タスク名").strip() for r in prows.values()}
    return bool(titles) and all(t in ORDER for t in titles)


_HEAD_RE = re.compile(r"^\s*(" + "|".join(ORDER) + r")\s*[：:]\s*(.*)$", re.S)


def item_head(text):
    """The phase an item text is headed with (`<phase>：...`), or None."""
    m = _HEAD_RE.match(text or "")
    return m.group(1) if m else None


def split_items(phases, texts):
    """({phase: [item text]}, [warnings]) of a row's checklist items. An item starting with `<phase>：` belongs to that phase (the head is cut);
    an item without a head belongs to the phase before it (the first one: the row's first phase). A head naming a phase the row does not work
    stays in the text, belongs to the current phase, and is one warning."""
    out = {p: [] for p in phases}
    cur, warns = phases[0], []
    for t in texts:
        m = _HEAD_RE.match(t)
        if m and m.group(1) in out:
            cur, t = m.group(1), m.group(2)
        elif m:
            warns.append(f"項目の頭 {m.group(1)}： は、この行（{'・'.join(phases)}）が受け持たない工程です: {t[:40]}")
        t = t.strip()
        if t:
            out[cur].append(t)
    return out, warns


def level_rows(level):
    """The row titles an epic of `level` gets (levels.toml `rows`); a level not set yet (None: an epic made before the levels) is made as L3."""
    return list(LEVEL_DEFS["L3" if level is None else level]["rows"])


def row_items(level, title):
    """The checklist items of the row `title` of `level`: the items of levels.toml whose head is a phase the row works, in the file's order."""
    phases = row_phases(title) or ()
    return [t for t in LEVEL_DEFS["L3" if level is None else level]["items"] if item_head(t) in phases]


_BROKEN_RANGE_RE = re.compile(r"\s*(" + "|".join(ORDER) + r")\s*[^\w\s]{1,3}\s*(" + "|".join(ORDER) + r")\s*")


def broken_row_title(title):
    """True when a title is shaped like a range of phases (<phase> <separator> <phase>, nothing else) but row_phases cannot read it: a typo
    (設計-リリース判定, リリース判定〜設計, 設計〜設計) that would make an ordinary task, which the intake would then start as a second card.
    A title with other words (a task about 設計 and 実装) is an ordinary title."""
    t = (title or "").strip()
    return row_phases(t) is None and _BROKEN_RANGE_RE.fullmatch(t) is not None


def page_url(pid):
    return "https://app.notion.com/p/" + pid.replace("-", "")


def collect_release_facts(epic_row_id, key, root=None):
    """Everything build_release_ask needs, read from Notion and git (read-only): the epic, the phase rows, the release judgement page and its
    markdown, the deliverables of the phases, the UI verdict. Never raises for a missing judgement; a Notion failure raises PipelineError."""
    epic = ntn(f"v1/pages/{epic_row_id}")
    epic_id = epic_row_id.replace("-", "")
    prows = phase_rows(epic_row_id)
    ref = decision_page_ref(prows)
    ev = collect_ui_evidence(prows, ref=ref or False, root=root)
    md = None
    if ref:
        try:
            md = (ntn(f"v1/pages/{ref['id']}/markdown") or {}).get("markdown", "")
        except PipelineError:
            md = None
    try:
        cond_cell = epic_condition_cell(iter_blocks(epic_row_id))
    except PipelineError:
        cond_cell = ""
    kids, pr_drafts, deliverables, row_links = {}, [], [], []
    units = [(t, r) for t, _, r in row_units(prows, ORDER) if r is not None]  # a row of a range works several phases: it is read ONCE
    for title, r in units:
        try:
            kids[r["id"]] = [(evidence_text(b), page_url(b["id"]), b["id"]) for b in blocks(r["id"]) if b.get("type") == "child_page"]
        except PipelineError:
            kids[r["id"]] = []
        row_links.append((f"{title}の行（作業メモ・完了レポート）", page_url(r["id"])))
    skip = {(ref or {}).get("id")}
    rel = prows.get("リリース判定")
    for title, url, bid in kids.get(rel["id"], []) if rel else []:
        if bid not in skip and re.search(r"PR\s*本文", title):
            pr_drafts.append((title, url))
            skip.add(bid)
    for title, r in units:
        deliverables += [(f"{title}：{t}", u) for t, u, bid in kids.get(r["id"], []) if bid not in skip]
    return dict(key=key, level=level_of(epic), hub_name=title_of(epic, "名前"), hub_url=page_url(epic_id), repo=ev["repo"], branch=ev["branch"], page=ref, md=md,
                cond_cell=cond_cell, pr_drafts=pr_drafts, deliverables=deliverables, phase_rows=row_links,
                ui=ev["verdict"]["ui"], verdict=ev["verdict"])


def review_page(key, text, title=None, epic_id=None, level=None):
    """`POST v1/pages` body that creates one レビュー row (status defaults to レビュー待ち).

    Properties written: 名前 / キー / 対象エピック (relation to the epic row). The body is the seven-chapter review template
    (`review_markdown`); the links the person opens to decide (the evidence) are in the 根拠 chapter, not in a property."""
    props = {"名前": {"title": [{"text": {"content": review_title(key, text, title)}}]},
             "キー": {"rich_text": [{"text": {"content": key}}]}}
    if epic_id:
        props["対象エピック"] = {"relation": [{"id": epic_id}]}
    return {"parent": {"data_source_id": REVIEW_DS}, "properties": props, "markdown": review_markdown(key, text, epic_id, level)}


def gate_key(epic_id, gate):
    """Review row key of a gate: <last 8 chars of the epic id>-承認 (the only gate today is the release approval at completion)."""
    return f"{epic_id[-8:]}-承認"


def legacy_gate_key(epic_id, gate):
    """The key format before the 種別 property was dropped (<epic8>-<gate name>). A row asked under it counts as asked: never ask twice."""
    return f"{epic_id[-8:]}-{gate}"


def release_review_keys(epic_id):
    """Review row keys of the release approval: the current one and the old <epic8>-リリース承認."""
    gate = GATE_ROWS[-1][1]
    return {gate_key(epic_id, gate), legacy_gate_key(epic_id, gate)}


def release_round_of(key, epic_id):
    """Round of a release-approval key of this epic: 1 for <epic8>-承認 (or the old key), N for <key>-rN (a re-ask after 差し戻し or a stopped ship); None otherwise."""
    for base in sorted(release_review_keys(epic_id)):
        if key == base:
            return 1
        m = re.fullmatch(re.escape(base) + r"-r(\d+)", key)
        if m:
            return int(m.group(1))
    return None


def release_approved_key(review_pages, epic_id):
    """Key of the approving row of the LATEST round of this epic's release approval (ステータス 承認), else None. An earlier round never counts once
    a later one exists (a re-ask replaces it)."""
    rows = []
    for p in review_pages:
        key = "".join(x.get("plain_text", "") for x in ((p.get("properties") or {}).get("キー") or {}).get("rich_text", [])).strip()
        n = release_round_of(key, epic_id)
        if n is not None:
            rows.append((n, key, status_of(p)))
    if not rows:
        return None
    top = max(n for n, _, _ in rows)
    return next((k for n, k, st in sorted(rows) if n == top and st == "承認"), None)


def release_approved(review_pages, epic_id):
    """True when the latest round's review row of the release approval has ステータス 承認 (the person's decision)."""
    return release_approved_key(review_pages, epic_id) is not None


# ---------------------------------------------------------------- ship (the approved release: draft PR -> merge -> deploy; scripts/ship_approved.py)

SHIP_STATE_FILE = "ship_approved.json"  # {review key: record} written by ship_approved.py (stage drafted/tested/readied/merged/deployed/done or a stop)
SHIP_MARK = "<!-- sf-ship -->"  # last line of the body of every draft PR the factory opens: a PR carrying it is not 'made without an instruction'
SHIP_DONE_STAGE = "done"


def ship_records():
    d = read_json(state_path(SHIP_STATE_FILE), {})
    return d if isinstance(d, dict) else {}


def ship_record(key):
    """The record of a review key (with `key` filled in), or None: a key without a record is an old-form approval (no PR, completes the epic)."""
    rec = ship_records().get(key)
    return dict(rec, key=key) if isinstance(rec, dict) else None


def ship_conf(repo):
    """The registration [ship.repos."owner/repo"] (test / deploy / cron_paths / describe), or None: such a repository is merged but never deployed."""
    return ((CFG.get("ship") or {}).get("repos") or {}).get(repo)


def ship_meaning(repo, pr_url=None, pr_number=None):
    """Values of ${pr} / ${deploy} of templates/release_meaning.md for a release ask about `repo`: the PR as a link when it exists, and the
    sentence of the registration's `describe` (a repository without a deploy registration says it stops at the merge, before the person approves)."""
    conf = ship_conf(repo) if repo else None
    pr = f"下書きの [PR #{pr_number}]({pr_url})" if pr_url and pr_number else (f"下書きの [PR]({pr_url})" if pr_url else RELEASE_MEANING_DEFAULTS["pr"])
    deploy = (conf.get("describe") or RELEASE_MEANING_DEFAULTS["deploy"]) if conf and conf.get("deploy") else "本番反映はしません（この repo には本番反映の登録がなく、マージまでです）"
    return dict(pr=pr, deploy=deploy)


def asked_keys():
    """Keys already asked (ask_human.json). A corrupt file is an error: it must never be moved aside (ask_human would repost)."""
    path = state_path(ASK_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return set()
    except ValueError as e:
        raise PipelineError(f"{path} is not valid JSON: {e}")
    if not isinstance(d, dict):
        raise PipelineError(f"{path} is not a JSON object")
    return set(d)


def live_phase_cards(cards, epic_id):
    """{phase: [live cards]} of one epic: same tenant and (phase idempotency key or '[phase]' title prefix)."""
    tenant = tenant_of(epic_id)
    out = {}
    for ph in ORDER:
        hit = [x for x in cards if x["tenant"] == tenant and x["status"] in LIVE_CARD_STATUSES
               and (is_phase_card_key(x["key"], epic_id, ph) or x["title"].startswith(f"[{ph}]"))]
        if hit:
            out[ph] = hit
    return out


def age_seconds(iso_ts, now):
    """Seconds between a Notion ISO timestamp and `now` (epoch); None when missing/unparseable."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt.timestamp()


def append_note(page_id, text):
    """Append one paragraph to a Notion page (used for the controller's own log lines)."""
    ntn(f"v1/blocks/{page_id}/children", "-X", "PATCH",
        data={"children": [{"object": "block", "type": "paragraph",
                            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}]})


def blocks(pid):
    """Top-level blocks of a page (paginated, no descent): list of block dicts."""
    out, cur = [], None
    while True:
        args = [f"v1/blocks/{pid}/children", "page_size==100"]
        if cur:
            args.append(f"start_cursor=={cur}")
        d = ntn(*args)
        out += d.get("results", [])
        if not d.get("has_more"):
            return out
        cur = d["next_cursor"]


def block_text(b):
    return "".join(x.get("plain_text", "") for x in b.get(b.get("type"), {}).get("rich_text", []))


def iter_blocks(pid, max_depth=2, max_extra_calls=30):
    """All blocks of a page incl. nested ones (depth-limited, call-budgeted), as a flat list of block dicts."""
    budget = [max_extra_calls]
    out = []

    def walk(block_id, depth):
        for b in blocks(block_id):
            out.append(b)
            if b.get("has_children") and depth < max_depth and b.get("type") not in _NO_DESCEND and budget[0] > 0:
                budget[0] -= 1
                walk(b["id"], depth + 1)

    walk(pid, 0)
    return out


CHECKLIST_HEADING = "チェックリス"  # matches the chapter やることチェックリスト


def checklist_items(blks):
    """Texts of the to_do items under the chapter やることチェックリスト of a task row's top-level blocks, in document order.

    The chapter is the heading (heading_1/2/3) whose text holds CHECKLIST_HEADING; it ends at the next heading of the same or a higher level.
    Blank to_do lines (template placeholders) are not items; a row without the chapter has no items."""
    out, level = [], None
    for b in blks:
        t = b.get("type") or ""
        if t in ("heading_1", "heading_2", "heading_3"):
            lv = int(t[-1])
            if level is None and CHECKLIST_HEADING in block_text(b):
                level = lv
            elif level is not None and lv <= level:
                level = None
        elif t == "to_do" and level is not None and block_text(b).strip():
            out.append(block_text(b).strip())
    return out


def is_phase_row(p):
    """A row of the pipeline (title is a phase name or a range of phases, linked to a hub): started by the controller, never by the intake."""
    pr = p["properties"]
    title = "".join(x["plain_text"] for x in pr.get("タスク名", {}).get("title", []))
    return bool(pr.get(TASK_EPIC_PROP, {}).get("relation")) and row_phases(title) is not None


def write_text(path, text):
    """Atomic text file write (readers never see a half-written file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- templates

def load_template(name):
    """Template text from templates/<name>.md without the single trailing newline of the file."""
    text = (TEMPLATE_DIR / f"{name}.md").read_text(encoding="utf-8")
    text = Template(text).safe_substitute(sfconfig.template_vars(_CFG_RAW))  # environment names (${live}, ${owner}, ...); runtime ${var} stay
    return text[:-1] if text.endswith("\n") else text


def render(name, **values):
    """Strict render: a missing ${var} (KeyError) or a stray '$' (ValueError) raises."""
    return Template(load_template(name)).substitute(**values)


def release_meaning(**values):
    """Chapter 1's fixed sentences (what 承認 / 差し戻し / 取り下げ do), rendered from templates/release_meaning.md. The sentences live only in that
    file; ${pr} and ${deploy} take RELEASE_MEANING_DEFAULTS unless the caller passes them (the ship epic replaces the template and the values)."""
    return render("release_meaning", **{**RELEASE_MEANING_DEFAULTS, **values})


def template_fields(name):
    """Set of placeholder names used by a template."""
    return {m.group("named") or m.group("braced") for m in Template.pattern.finditer(load_template(name))
            if m.group("named") or m.group("braced")}


# ---------------------------------------------------------------- state files

def state_path(name):
    return Path(STATE_DIR) / name


def atomic_write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default=None):
    """Parsed JSON file; `default` when missing. A corrupt file is moved aside (…corrupt-<ts>) and `default` returned."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return default


def read_status():
    return read_json(state_path(STATUS_FILE), None)


def write_status(ok, error=None, **extra):
    """Controller heartbeat. last_run advances on every run; last_success only when the run was clean."""
    now = int(time.time())
    st = read_status() or {}
    st.update(extra)
    st.update(last_run=now, ok=bool(ok), error=(str(error)[:300] if error else None))
    if ok:
        st["last_success"] = now
    atomic_write_json(state_path(STATUS_FILE), st)
    return st


def load_journal():
    j = read_json(state_path(JOURNAL_FILE), None)
    return j if isinstance(j, dict) else {}


def save_journal(j):
    atomic_write_json(state_path(JOURNAL_FILE), j)


def journal_key(epic_id, ph):
    return f"{epic_id}:{ph}"


class RunLock:
    """Non-blocking exclusive lock so two controller runs never overlap. `with RunLock() as lk: lk.acquired`."""

    def __init__(self, name=LOCK_FILE):
        self.path = state_path(name)
        self.f = None
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.acquired:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        return False


# ---------------------------------------------------------------- kanban (read-only sqlite)

def kanban_cards(db=None):
    """All non-archived cards of the board, read-only. Order = `kanban list` order (priority desc, created asc)."""
    path = Path(db or KANBAN_DB)
    if not path.exists():
        raise PipelineError(f"kanban db not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        con.execute("PRAGMA query_only=1")
        rows = con.execute(
            "SELECT id, status, assignee, tenant, idempotency_key, title, "
            "COALESCE((SELECT MAX(created_at) FROM task_events e WHERE e.task_id = tasks.id "
            "AND e.kind NOT IN ('heartbeat', 'respawn_guarded')), created_at) FROM tasks "
            "WHERE status != 'archived' ORDER BY priority DESC, created_at ASC").fetchall()
    except sqlite3.Error as e:
        raise PipelineError(f"kanban db read failed: {e}")
    finally:
        con.close()
    return [dict(id=r[0], status=r[1], assignee=r[2] or "(unassigned)", tenant=r[3] or "", key=r[4] or "", title=(r[5] or "").strip(), idle_since=r[6])
            for r in rows]


REVIEWER_PROFILE = "reviewer"  # the profile of the independent verifier (task_runs.profile): its run with outcome completed is the pass, changes_requested a send-back


def kanban_reviews(db=None):
    """All non-archived cards of the board, read-only, with the review facts the gate controller needs: dicts(id, status, tenant, key, title, body,
    requests = how many times the worker called kanban_request_review (runs with outcome review_requested),
    passed = the id of the reviewer's run that passed the card (outcome completed) when it is later than the last request, else 0).
    A card kanban_complete'd by the worker itself has no reviewer run: passed = 0. Order = `kanban list` order (priority desc, created asc)."""
    path = Path(db or KANBAN_DB)
    if not path.exists():
        raise PipelineError(f"kanban db not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        con.execute("PRAGMA query_only=1")
        rows = con.execute(
            "SELECT t.id, t.status, t.tenant, t.idempotency_key, t.title, t.body, "
            "(SELECT COUNT(*) FROM task_runs r WHERE r.task_id = t.id AND COALESCE(r.profile, '') != ? AND r.outcome = 'review_requested'), "
            "COALESCE((SELECT MAX(r.id) FROM task_runs r WHERE r.task_id = t.id AND r.profile = ? AND r.outcome = 'completed'), 0), "
            "COALESCE((SELECT MAX(r.id) FROM task_runs r WHERE r.task_id = t.id AND COALESCE(r.profile, '') != ? AND r.outcome = 'review_requested'), 0) "
            "FROM tasks t WHERE t.status != 'archived' ORDER BY t.priority DESC, t.created_at ASC",
            (REVIEWER_PROFILE, REVIEWER_PROFILE, REVIEWER_PROFILE)).fetchall()
    except sqlite3.Error as e:
        raise PipelineError(f"kanban db read failed: {e}")
    finally:
        con.close()
    return [dict(id=r[0], status=r[1], tenant=r[2] or "", key=r[3] or "", title=(r[4] or "").strip(), body=r[5] or "", requests=r[6],
                 passed=r[7] if r[7] > r[8] else 0) for r in rows]


def read_alerts():
    """{key: state} of what the gate controller already announced with an ALERT line (a corrupt file is moved aside by read_json: it then says things again, which is safe)."""
    d = read_json(state_path(ALERT_FILE), {})
    return d if isinstance(d, dict) else {}


def save_alerts(alerts):
    atomic_write_json(state_path(ALERT_FILE), alerts)


_REPORT_PLACEHOLDERS = {"", "未実施", "なし", "-", "ー", "未記入", "n/a"}  # a 完了レポート line that says nothing (same as epic_pipeline_health._PLACEHOLDERS)


def rich_text(rt):
    """The text of a Notion rich_text list; the address of a link that is only in the link (href) is added, so a bullet that links its row counts as carrying the URL."""
    out = []
    for x in rt or []:
        t = x.get("plain_text", "")
        href = x.get("href") or ""
        out.append(t + (f" {href}" if href and href not in t else ""))
    return "".join(out)


def close_state(blks):
    """What a row's page says about being finished: dict(items = [(text, ticked)] of the checklist (the to_do items under the heading that holds やることチェックリスト;
    every to_do when the page has none), report = the 完了レポート chapter as Markdown (headings as `##`, list items as `- `; what scripts/remaining_work.py reads),
    report_filled = the chapter holds a line that is not a placeholder). Pure: takes the top-level + nested blocks of the page (iter_blocks)."""
    has_list = any(b.get("type") == "heading_1" and CHECKLIST_HEADING in rich_text((b.get("heading_1") or {}).get("rich_text")) for b in blks)
    chapter, items, lines, filled = "", [], [], False
    for b in blks:
        t = b.get("type")
        body = b.get(t) or {}
        text = rich_text(body.get("rich_text"))
        if t == "heading_1":
            if "完了レポート" in chapter and "未確認・残件" in text:  # the section written as a chapter of its own, right after the report
                lines.append(f"# {text}")
                continue
            chapter = text
        elif t in ("heading_2", "heading_3"):
            if "完了レポート" in chapter:
                lines.append("#" * int(t[-1]) + f" {text}")
        elif t == "to_do" and (not has_list or CHECKLIST_HEADING in chapter):
            items.append((text.strip(), bool(body.get("checked"))))
        elif "完了レポート" in chapter and t in ("paragraph", "bulleted_list_item", "numbered_list_item", "table_row", "quote", "to_do"):
            if t == "table_row":
                text = " ".join(rich_text(c) for c in body.get("cells", []))
            lines.append(("- " if t in ("bulleted_list_item", "numbered_list_item", "to_do") else "") + text)
            if text.strip().lower() not in _REPORT_PLACEHOLDERS:
                filled = True
    return dict(items=items, report="\n".join(lines), report_filled=filled)


CONTEXT_FILE = "CONTEXT.md"  # per-card context file in the card's scratch workspace (phase_card.md tells the executor to keep it)


def kanban_context_cards(db=None, profiles=None):
    """Live cards of the board whose body asks for CONTEXT_FILE, read-only, with their workspace and the latest finished run of
    a worker profile (PHASE_ROLE values, i.e. executor): dicts(id, status, kind, workspace, run, run_started, run_ended).
    run* are None when no such run has finished yet."""
    path = Path(db or KANBAN_DB)
    if not path.exists():
        raise PipelineError(f"kanban db not found: {path}")
    profiles = sorted(set(profiles or PHASE_ROLE.values()))
    marks = ",".join("?" * len(profiles))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        con.execute("PRAGMA query_only=1")
        rows = con.execute(
            "SELECT t.id, t.status, t.workspace_kind, t.workspace_path, r.id, r.started_at, r.ended_at FROM tasks t "
            "LEFT JOIN task_runs r ON r.id = (SELECT MAX(id) FROM task_runs x WHERE x.task_id = t.id AND x.ended_at IS NOT NULL "
            f"AND x.profile IN ({marks})) "
            f"WHERE t.status IN ({','.join('?' * len(LIVE_CARD_STATUSES))}) AND instr(COALESCE(t.body, ''), ?) > 0 ORDER BY t.id",
            (*profiles, *LIVE_CARD_STATUSES, CONTEXT_FILE)).fetchall()
    except sqlite3.Error as e:
        raise PipelineError(f"kanban db read failed: {e}")
    finally:
        con.close()
    return [dict(id=r[0], status=r[1], kind=r[2] or "", workspace=r[3] or "", run=r[4], run_started=r[5], run_ended=r[6])
            for r in rows]


def kanban_card_texts(root=None):
    """Live cards of EVERY board (default + boards/*, read-only) as dicts(status, text = title + body).
    Used to tell whether a 進行中 task row still has a card working on it (the card body holds the row's Notion URL).
    A board db that cannot be read is skipped: this only suppresses an alert, it never raises one."""
    root = Path(root or KANBAN_ROOT)
    dbs = [root / "kanban.db"] + sorted((root / "boards").glob("*/kanban.db"))
    out = []
    for path in dbs:
        if not path.exists() or path.parent.name == "_archived":
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            try:
                con.execute("PRAGMA query_only=1")
                for st, title, body in con.execute("SELECT status, title, body FROM tasks WHERE status != 'archived'"):
                    out.append(dict(status=st, text=f"{title or ''}\n{body or ''}"))
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return out


_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}", re.I)


def recent_message_ids(now, window, root=None):
    """32-hex ids (lower case, no dashes) that appear in agent session messages written during the last `window` seconds,
    read-only from <root>/state.db and <root>/profiles/*/state.db. A row whose Notion page id shows up there is being worked on
    in a session even though no kanban card mentions it (a direct Slack request has no card). A db that cannot be read is
    skipped: this only suppresses an alert, it never raises one."""
    root = Path(root or HERMES_ROOT)
    dbs = [root / "state.db"] + sorted((root / "profiles").glob("*/state.db"))
    ids = set()
    for path in dbs:
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            try:
                con.execute("PRAGMA query_only=1")
                for content, calls in con.execute("SELECT content, tool_calls FROM messages WHERE timestamp >= ?", (now - window,)):
                    for text in (content, calls):
                        if text:
                            ids.update(m.replace("-", "").lower() for m in _UUID_RE.findall(str(text)))
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return ids


def registry_hubs(extra_filters=()):
    """エピック rows of kind プロダクト開発 that are not 完了/中止 (plus optional extra filters)."""
    return query(REGISTRY_DS, {"and": [
        {"property": "ステータス", "status": {"does_not_equal": "完了"}},
        {"property": "ステータス", "status": {"does_not_equal": "中止"}},
        *extra_filters,
        {"property": "種別", "select": {"equals": "プロダクト開発"}}]})


# ---------------------------------------------------------------- UI evidence (release approval)
# A release approval of a UI change is asked only when the release decision page (判定書) shows the UI before AND after the change
# (at least one image each). ui_evidence_verdict() is the ONE function that decides it, for the decision page and for the review row.

UI_PATH_PREFIXES = ("pages/", "src/livechat/ui/")  # a changed path starting with one of these = UI change
UI_BEFORE, UI_AFTER = "変更前", "変更後"
UI_DECLARED_RE = re.compile(r"UI\s*変更\s*[:：]?\s*(あり|なし)")  # "UI 変更：あり" / "UI 変更：なし" in the decision page (text or table row)
DECISION_PAGE_WORD = "判定書"  # the child page of the リリース判定 row whose title holds this word is the decision page
UI_HOLD_FILE = "ui_evidence_hold.json"  # release asks held for missing UI images: {key: {reason, edited, since}}
GIT_TIMEOUT = 30
IMAGE_MARK = "［画像 {n}］"  # paragraph that stands for the n-th image of the decision page in the transcript (ask_human.py puts the image after it)
IMAGE_MARK_RE = re.compile(r"^［画像 (\d+)］")


def evidence_text(b):
    """Text of one block for the UI evidence checks: rich text, table row cells joined with '：', image caption, child page title."""
    t = b.get("type") or ""
    if t == "table_row":
        return "：".join("".join(x.get("plain_text", "") for x in cell) for cell in b[t].get("cells", []))
    if t == "image":
        return "".join(x.get("plain_text", "") for x in (b[t].get("caption") or []))
    if t == "child_page":
        return b[t].get("title", "")
    return block_text(b)


def _before_after(text):
    """'変更前' / '変更後' / '' of a caption or heading; '変更前後' (both) counts as neither."""
    t = (text or "").replace("変更前後", "")
    before, after = UI_BEFORE in t, UI_AFTER in t
    return UI_BEFORE if before and not after else UI_AFTER if after and not before else ""


def ui_evidence_verdict(changed_paths, blks, assume_ui=None, diff_error=None):
    """THE check of UI evidence (pure). -> dict(ui, basis, ui_paths, declared, before, after, ok, reason).

    changed_paths: changed file paths of the epic's branch (None = unknown); blks: blocks of the page (decision page or review row),
    nested ones included, in document order (iter_blocks). assume_ui=True: the caller already knows it is a UI change (review row check).
    ui: True when a changed path starts with UI_PATH_PREFIXES or the page declares 'UI 変更：あり'; False when the diff has no UI path
    (and no 'あり'), or there is no diff and the page declares 'なし'; None when neither is known (never held: a wrong hold delays the approval).
    An image counts as 変更前 / 変更後 by its caption, else by the nearest heading above it. ok: not a UI change, or >= 1 image of each."""
    heading, before, after, declared = "", 0, 0, None
    for b in blks:
        t, txt = b.get("type") or "", evidence_text(b)
        if t.startswith("heading_"):
            heading = txt
        if t == "image":
            side = _before_after(txt) or _before_after(heading)
            before += side == UI_BEFORE
            after += side == UI_AFTER
            continue
        m = UI_DECLARED_RE.search(txt)
        if m and declared is None:
            declared = m.group(1) == "あり"
    ui_paths = sorted(p for p in (changed_paths or []) if p.startswith(UI_PATH_PREFIXES))
    if assume_ui is not None:
        ui, basis = bool(assume_ui), "given"
    elif ui_paths:
        ui, basis = True, "diff"
    elif declared is True:
        ui, basis = True, "declared"
    elif changed_paths is not None:
        ui, basis = False, "diff"
    elif declared is False:
        ui, basis = False, "declared"
    else:
        ui, basis = None, "unknown"
    ok = ui is not True or (before >= 1 and after >= 1)
    if basis == "diff" and ui:
        why = f"差分：{ui_paths[0]}" + (f" ほか {len(ui_paths) - 1} 件" if len(ui_paths) > 1 else "")
    elif basis == "diff":
        why = f"差分 {len(changed_paths or [])} 件に {'・'.join(UI_PATH_PREFIXES)} が無い"
    elif basis == "declared":
        why = "判定書の「UI 変更：" + ("あり" if ui else "なし") + "」"
    elif basis == "given":
        why = "ゲート制御の判定"
    else:
        why = (diff_error or "差分を取れず、判定書に「UI 変更：あり／なし」も無い")
    label = {True: "あり", False: "なし", None: "判定できず"}[ui]
    if ok:
        reason = f"UI 変更の判定：{label}（{why}）。変更前の画像 {before} 枚、変更後の画像 {after} 枚"
    else:
        missing = "・".join(s for s, n in ((UI_BEFORE, before), (UI_AFTER, after)) if n < 1)
        reason = f"UI 変更の判定：あり（{why}）なのに、{missing}の画像がありません（変更前 {before} 枚、変更後 {after} 枚）"
    return dict(ui=ui, basis=basis, ui_paths=ui_paths, declared=declared, before=before, after=after, ok=ok, reason=reason)


def repos_root():
    """Where repositories live (<root>/<owner>/<repo>): two levels above paths.repo (the ghq layout). No extra setting."""
    return Path(CFG["paths"]["repo"]).parent.parent


_REPO_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]*[A-Za-z0-9_-])")


def repo_and_branch(texts, root=None):
    """(owner/repo, branch) from page texts: the branch is the first word starting with BRANCH_PREFIX, the repository the first
    owner/repo that exists under the repositories root. (None, None) parts when not found."""
    root = Path(root or repos_root())
    branch_re = re.compile(re.escape(BRANCH_PREFIX) + r"[A-Za-z0-9._/-]*[A-Za-z0-9_-]")
    repo = branch = None
    for t in texts:
        if branch is None:
            m = branch_re.search(t or "")
            branch = m.group(0) if m else None
        if repo is None:
            repo = next((f"{m.group(1)}/{m.group(2)}" for m in _REPO_RE.finditer(t or "")
                         if (root / m.group(1) / m.group(2) / ".git").exists()), None)
        if repo and branch:
            break
    return repo, branch


def ui_changed_paths(repo_dir, branch):
    """(changed paths, None) of origin/<branch> against origin's default branch, or (None, reason). Never raises."""
    git = which_tool("git")

    def g(*a):
        r = _sh([git, "-C", str(repo_dir), *a], timeout=GIT_TIMEOUT)
        if r.returncode != 0:
            raise PipelineError(((r.stderr or "").strip() or (r.stdout or "").strip())[:120] or f"git exit {r.returncode}")
        return r.stdout or ""

    try:
        g("fetch", "--quiet", "origin", branch)
        try:
            base = g("rev-parse", "--abbrev-ref", "origin/HEAD").strip() or "origin/main"
        except PipelineError:
            base = "origin/main"
        out = g("diff", "--name-only", f"{base}...origin/{branch}")
    except (PipelineError, subprocess.TimeoutExpired, OSError) as e:
        return None, f"{branch} の差分を取れませんでした（{type(e).__name__}: {str(e)[:80]}）"
    return [l.strip() for l in out.splitlines() if l.strip()], None


def decision_page_ref(prows):
    """dict(id, title, url, edited) of the release decision page (child page of the リリース判定 row whose title holds 判定書), or None."""
    rel = prows.get("リリース判定")
    if not rel:
        return None
    cp = next((b for b in blocks(rel["id"]) if b.get("type") == "child_page" and DECISION_PAGE_WORD in evidence_text(b)), None)
    if not cp:
        return None
    page = ntn(f"v1/pages/{cp['id']}")
    return dict(id=cp["id"], title=evidence_text(cp), url="https://app.notion.com/p/" + cp["id"].replace("-", ""),
                edited=page.get("last_edited_time"))


def collect_ui_evidence(prows, ref=None, root=None):
    """Everything the release ask needs: dict(page = decision_page_ref or None, blocks, repo, branch, verdict).

    The repository and branch are read from the decision page, then the 実装 / リリース判定 / 設計 rows (概要 table 対象)."""
    ref = ref if ref is not None else decision_page_ref(prows)
    dblocks = iter_blocks(ref["id"]) if ref else []
    texts = ["\n".join(evidence_text(b) for b in dblocks)]
    repo, branch = repo_and_branch(texts, root)
    for r in distinct_rows(prows, ("実装", "リリース判定", "設計")):  # a row of a range works several of them: read it once (iter_blocks is up to 30 calls)
        if repo and branch:
            break
        texts.append("\n".join(evidence_text(b) for b in iter_blocks(r["id"])))
        repo, branch = repo_and_branch(texts, root)
    paths, err = None, None
    if repo and branch:
        paths, err = ui_changed_paths(Path(root or repos_root()) / repo, branch)
    else:
        err = "対象のリポジトリかブランチが本文に見つかりませんでした"
    verdict = ui_evidence_verdict(paths, dblocks, diff_error=err if paths is None else None)
    return dict(page=ref, blocks=dblocks, repo=repo, branch=branch, verdict=verdict)


_MD_IMAGE_RE = re.compile(r"^(\s*)!\[([^\]]*)\]\([^)]*\)\s*(?:\{[^}]*\})?\s*$")
_MD_HEADING_RE = re.compile(r"^(#{1,3}) (.*?)(\s*\{[^}]*\})?\s*$")


def append_to_chapter(page_id, heading, children):
    """Insert blocks at the end of the H1 chapter `heading` of a page (after its last block, before the next H1)."""
    blks = blocks(page_id)
    idx = next((i for i, b in enumerate(blks) if b.get("type") == "heading_1" and block_text(b).strip() == heading), None)
    if idx is None:
        raise PipelineError(f"chapter {heading} not found on page {page_id}")
    last = blks[idx]
    for b in blks[idx + 1:]:
        if b.get("type") == "heading_1":
            break
        last = b
    ntn(f"v1/blocks/{page_id}/children", "-X", "PATCH",
        data={"children": children, "position": {"type": "after_block", "after_block": {"id": last["id"]}}})


def text_block(kind, text, **extra):
    """A paragraph / to_do / ... block with one plain rich text (cut at 1900 characters)."""
    return {"object": "block", "type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}], **extra}}


def release_problems(body):
    """The lines of a release ask body that say something is not as expected: 未確認：… and 注意：… (what `ask_human.py --release --dry-run`
    lists, and what the judgement's author and the reviewer must bring to zero before the judgement is finished)."""
    return [l.strip() for l in (body or "").splitlines() if l.strip().startswith(("未確認：", "注意："))]
