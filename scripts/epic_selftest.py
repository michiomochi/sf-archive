#!/usr/bin/env python3
"""Offline self-test of the michiomochi epic pipeline scripts (no network, no Notion, no kanban, temp state dir).

Run: python3 -W error epic_gate_controller.py --selftest   (also: epic_pipeline_health.py --selftest, or this file directly)
Everything runs with Python warnings recorded and any warning fails the suite; all scripts are compiled from source
with warnings as errors first (a SyntaxWarning such as a missing comma between strings is a failure).
"""
import contextlib
import gc
import io
import json
import os
import re
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import epic_common as C  # noqa: E402

TESTS = []


def _repo_roots():
    """Where the repo files (notion/, prompts/, skills/, scripts/templates/) are read from, best first: the checkout `make stage` recorded in
    <staging>/repo_root (so a deploy-check compares the code under test with ITS OWN files, not with another checkout), this checkout (HERE.parent,
    for `make test`), the configured paths.repo (a deployed copy has no notion/ next to it)."""
    out = []
    marker = HERE / "repo_root"
    if marker.is_file():
        out.append(Path(marker.read_text(encoding="utf-8").strip()))
    return out + [HERE.parent, Path(C.CFG["paths"]["repo"])]


PIPELINE_PY = ("epic_common.py", "epic_selftest.py", "epic_gate_controller.py", "epic_gate_apply.py", "epic_planner_status.py",
               "notion_pending_tasks.py", "epic_pipeline_health.py", "ask_human.py", "create_row.py", "create_card.py", "check_orphan_tasks.py", "check_review_pairs.py", "apply_review_decisions.py", "request_review.py", "review_inbox_status.py", "ship_approved.py", "ship_selftest.py", "remaining_work.py")


def check(fn):
    TESTS.append(fn)
    return fn


def legacy_phases(fn):
    """Run a check under one-card-per-phase rules (MERGE_PHASES off, a parallel split of 要件定義 into A and B on)."""
    import functools

    @functools.wraps(fn)
    def run(*a, **kw):
        ctl = sys.modules.get("epic_gate_controller")
        old = (C.PARALLEL, getattr(ctl, "PARALLEL", None), getattr(ctl, "MERGE_PHASES", None))
        par = {"要件定義": {"B": {"CS確認", "マーケ確認", "PR確認"}}}
        C.PARALLEL = par
        if ctl is not None:
            ctl.PARALLEL, ctl.MERGE_PHASES = par, False
        try:
            return fn(*a, **kw)
        finally:
            C.PARALLEL = old[0]
            if ctl is not None:
                ctl.PARALLEL, ctl.MERGE_PHASES = old[1], old[2]
    return run


def expect(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    raise AssertionError(f"{getattr(exc, '__name__', exc)} not raised")


# ---------------------------------------------------------------- fakes

def _title(text):
    return {"title": [{"plain_text": text}]}


def _status(name):
    return {"status": {"name": name}}


def _id(n):
    h = f"{n:08x}"
    return f"{h}-0000-4000-8000-{h}{h[:4]}"  # 8-4-4-4-12 hex


def _epic_id(n):
    return _id(n).replace("-", "")


class World:
    """Fake Notion + fake `hermes kanban create` with the semantics the controller relies on."""

    def __init__(self):
        self.hubs, self.rows, self.row_blocks = [], {}, {}
        self.pages = {}            # page id -> page dict (anything PATCHable / GETable)
        self.fail_patch, self.break_epic = set(), set()  # break_epic: hub ids whose phase-row query fails
        self.fail_create = set()   # idempotency keys that fail
        self.kanban = {}           # key -> dict(id, created_at)
        self.board = []            # fake kanban board: dicts like C.kanban_cards() returns
        self.context_cards = []    # dicts like C.kanban_context_cards() returns (health: context)
        self.hub_blocks = {}       # hub id -> block list of the hub page
        self.review_rows = []      # rows of the review DB (health: review-unreflected)
        self.recent_rows, self.pending_rows = [], []  # michiomochiタスク rows: created in the last 30 min / 未着手
        self.notes = []            # (page id, text) appended through blocks/children PATCH
        self.asks = []             # (key, epic row id) of fake ask_release calls
        self.ask_epics = []        # the 対象エピック (epic row id) of each of them
        self.fail_ask = set()      # keys whose ask fails
        self.calls = []            # (kind, ...) in order
        self.decision_pages = {}   # page id -> dict(title, blocks, markdown, edited): release decision pages (child pages of リリース判定 rows)
        self.comments = []         # (card id, text) of fake `hermes kanban comment`
        self.made_rows = []        # (hub row id, title, items) of fake make_row calls (the rows the gate controller made)
        self.fail_make_row = {}    # row title -> error text (None page) | (error text, page id): make_row fails once for that title
        self.hide_new_rows = False # True: a row made by make_row is not listed by the query (Notion lagging)
        self.hidden_rows = []
        self.create_argv = []      # the whole argv of every `kanban ... create` (as build_args made it)
        self.child_pages = []      # (parent page id, title, page id) of the pages made with POST v1/pages under a page (deliverables)
        self.fail_child_page = set()  # titles whose creation fails
        self.bodies = {}           # card id -> the body the card was made with (stdin of `kanban create`)
        self.reviews = {}          # card id -> dict(requests = kanban_request_review calls, passed = id of the reviewer's passing run after the last request, 0 = none)
        self.blocks = []           # (card id, reason) of fake `hermes kanban block`
        self.fail_block = set()    # card ids whose block fails
        self.release_problems = {} # epic row id -> the PROBLEMS lines of `ask_human --release --dry-run` (default none)
        self.release_calls = []    # (epic row id, key) of every fake dry run
        self.row_reads = []        # the page id of every read of a row's blocks
        self.n = 0
        self.t0 = int(time.time())

    def add_epic(self, n, rows=(), items=None, hub_status="要件定義"):
        """rows: [(phase, status)]; items: {phase: [checklist item texts]} (default: two items per row; [] = an empty checklist).

        The epic id is the hub row id without dashes. Returns (hub, epic_id)."""
        hub_id = _id(0x200 + n)
        hub = {"id": hub_id, "url": f"https://www.notion.so/{hub_id}", "properties": {
            "名前": _title(f"ハブ{n}"), "新規作成の停止": {"checkbox": False}, "ステータス": _status(hub_status)}}
        self.hubs.append(hub)
        self.pages[hub_id] = hub
        self.rows[hub_id] = []
        for i, (ph, st) in enumerate(rows):
            rid = _id(0x2000 * n + 0x10 + i)
            r = {"id": rid, "url": f"https://www.notion.so/{rid}", "last_edited_time": "2026-01-01T00:00:00.000Z",
                 "properties": {"タスク名": _title(ph), "ステータス": _status(st)}}
            self.rows[hub_id].append(r)
            self.pages[rid] = r
            texts = (items or {}).get(ph, [f"{ph}の作業1", f"{ph}の作業2"])
            self.row_blocks[rid] = ([_block("heading_1", "概要"), _block("paragraph", "text"), _block("heading_1", "やることチェックリスト")]
                                    + [_block("to_do", t) for t in texts] + [_block("heading_1", "作業メモ"), _block("to_do", "memo todo")])
        return hub, hub_id.replace("-", "")

    def row(self, hub, phase):
        return next(r for r in self.rows[hub["id"]] if C.title_of(r, "タスク名") == phase)

    # --- Notion
    def ntn(self, *args, data=None):
        path = args[0]
        method = args[args.index("-X") + 1] if "-X" in args else ("POST" if data is not None else "GET")
        if path == "v1/pages" and method == "POST" and ((data or {}).get("parent") or {}).get("page_id"):  # a child page of a page
            parent, title = data["parent"]["page_id"], data["properties"]["title"]["title"][0]["text"]["content"]
            if title in self.fail_child_page:
                raise C.PipelineError("simulated Notion failure")
            pid = _id(0x7000 + len(self.child_pages))
            self.child_pages.append((parent, title, pid))
            self.row_blocks.setdefault(parent, []).append({"id": pid, "type": "child_page", "child_page": {"title": title}})
            return {"id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}"}
        m = re.fullmatch(r"v1/data_sources/([0-9a-f-]+)/query", path)
        if m:
            ds, flt = m.group(1), (data or {}).get("filter") or {}
            if ds == C.REGISTRY_DS:
                return {"results": list(self.hubs), "has_more": False}
            if ds == C.REVIEW_DS:
                return {"results": list(self.review_rows), "has_more": False}
            if ds == C.TASK_DS:
                if "relation" in flt:
                    if flt["relation"]["contains"] in self.break_epic:
                        raise C.PipelineError("simulated epic read failure")
                    return {"results": list(self.rows.get(flt["relation"]["contains"], [])), "has_more": False}
                if flt.get("timestamp") == "created_time":
                    self.calls.append(("query_recent", flt))
                    return {"results": list(self.recent_rows), "has_more": False}
                self.calls.append(("query_pending", flt))
                return {"results": list(self.pending_rows), "has_more": False}
            raise AssertionError(f"unexpected data source {ds}")
        m = re.fullmatch(r"v1/pages/([0-9a-f-]+)", path)
        if m:
            pid = m.group(1)
            if method == "PATCH":
                if pid in self.fail_patch:
                    raise C.PipelineError("simulated Notion failure")
                name = data["properties"]["ステータス"]["status"]["name"]
                self.pages[pid]["properties"]["ステータス"] = _status(name)
                self.calls.append(("patch", pid, name))
                return self.pages[pid]
            return self.pages[pid]
        m = re.fullmatch(r"v1/pages/([0-9a-f-]+)/markdown", path)
        if m and m.group(1) in self.decision_pages:
            return {"markdown": self.decision_pages[m.group(1)]["markdown"]}
        m = re.fullmatch(r"v1/blocks/([0-9a-f-]+)/children", path)
        if m and method == "PATCH":
            if m.group(1) in self.fail_patch:
                raise C.PipelineError("simulated Notion failure")
            first = data["children"][0]
            self.notes.append((m.group(1), first[first["type"]]["rich_text"][0]["text"]["content"]))
            self.calls.append(("note", m.group(1), first["type"], data.get("position")))
            return {"results": []}
        if m and m.group(1) in self.decision_pages:
            return {"results": self.decision_pages[m.group(1)]["blocks"], "has_more": False}
        if m and m.group(1) in self.row_blocks:
            self.row_reads.append(m.group(1))
            return {"results": self.row_blocks[m.group(1)], "has_more": False}
        if m and m.group(1) in self.hub_blocks:
            self.calls.append(("blocks", m.group(1)))
            return {"results": self.hub_blocks[m.group(1)], "has_more": False}
        if m:
            return {"results": [], "has_more": False}
        raise AssertionError(f"unexpected ntn call {args}")

    # --- kanban
    def make_row(self, epic_row_id, title, items, reason):
        """Fake of epic_gate_controller.make_row (create_row.py): one 未着手 row with its checklist under the epic."""
        if title in self.fail_make_row:
            err = self.fail_make_row.pop(title)
            raise C.PipelineError(err if isinstance(err, str) else f"{err[0]} page={err[1]}")
        self.made_rows.append((epic_row_id, title, list(items)))
        rid = _id(0x9000 + len(self.pages))
        r = {"id": rid, "url": f"https://www.notion.so/{rid}", "last_edited_time": "2026-01-01T00:00:00.000Z",
             "properties": {"タスク名": _title(title), "ステータス": _status("未着手")}}
        (self.hidden_rows if self.hide_new_rows else self.rows[epic_row_id]).append(r)
        self.pages[rid] = r
        self.row_blocks[rid] = ([_block("heading_1", "概要"), _block("paragraph", "text"), _block("heading_1", "やることチェックリスト")]
                                + [_block("to_do", t) for t in items] + [_block("heading_1", "作業メモ")])
        return rid

    def hermes(self, *args, stdin=None):
        args = list(args)
        if args[:1] == ["kanban"]:  # build_args gives the whole argv: kanban --board <slug> create ...
            if "create" in args:
                self.create_argv.append(list(args))
            args = args[3:]
        if args[0] == "comment":
            self.comments.append((args[1], args[2]))
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "block":  # hermes kanban block [--kind K] <id> <reason>: running / ready -> blocked (a card in review cannot be blocked)
            # argparse (`task_id` then `reason*`): an option AFTER the id leaves the reason unrecognized, so the option goes before the id
            if args[1:2] != ["--kind"] or len(args) != 5:
                return subprocess.CompletedProcess(args, 2, "", f"block: error: unrecognized arguments (the options go before the task id): {args[1:]}")
            card = next((c for c in self.board if c["id"] == args[3]), None)
            if card is None or args[3] in self.fail_block or card["status"] not in ("running", "ready"):
                return subprocess.CompletedProcess(args, 1, "", f"cannot block {args[3]}")
            card["status"] = "blocked"
            self.blocks.append((args[3], args[4]))
            return subprocess.CompletedProcess(args, 0, f"Blocked {args[3]}", "")
        key = args[args.index("--idempotency-key") + 1]
        if key in self.fail_create:
            return subprocess.CompletedProcess(args, 1, "", "boom: simulated kanban failure")
        if key not in self.kanban:
            self.n += 1
            self.kanban[key] = dict(id=f"t_{self.n:08x}", created_at=int(time.time()), status="ready")
            self.board.append(dict(id=self.kanban[key]["id"], status="ready", assignee=args[args.index("--assignee") + 1],
                                   tenant=args[args.index("--tenant") + 1], key=key, title=args[1]))
            self.calls.append(("create", key, args, stdin))
            self.bodies[self.kanban[key]["id"]] = stdin
        else:  # idempotent re-hit: same id, old created_at
            self.calls.append(("dedup", key, args, stdin))
            self.kanban[key] = dict(self.kanban[key], created_at=self.kanban[key]["created_at"])
        k = self.kanban[key]
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": k["id"], "created_at": k["created_at"], "status": k.get("status", "ready")}), "")

    def kanban_cards(self, db=None):
        return [dict(c) for c in self.board]

    def kanban_context_cards(self, db=None, profiles=None):
        return [dict(c) for c in self.context_cards]

    def kanban_reviews(self, db=None):
        """Like C.kanban_reviews(): the board cards with their body and review facts."""
        return [dict(c, body=self.bodies.get(c["id"], ""), requests=self.reviews.get(c["id"], {}).get("requests", 0),
                     passed=self.reviews.get(c["id"], {}).get("passed", 0)) for c in self.board]

    def card_of(self, key):
        return next(c for c in self.board if c["key"] == key)

    def request_review(self, key, n):
        """The executor called kanban_request_review for the n-th time: the card waits in review."""
        c = self.card_of(key)
        c["status"] = "review"
        self.reviews[c["id"]] = dict(requests=n, passed=0)

    def pass_review(self, key, n=1):
        """The reviewer passed the card (kanban done, a reviewer run with outcome completed after the n-th request)."""
        c = self.card_of(key)
        c["status"] = "done"
        self.kanban[key]["status"] = "done"
        self.reviews[c["id"]] = dict(requests=n, passed=100 + n)

    def release_judgement_problems(self, epic_row_id, key):
        self.release_calls.append((epic_row_id, key))
        return list(self.release_problems.get(epic_row_id, [])), True

    def finish_cards(self, *keys):
        """Mark cards done (all of them when no key is given), like the reviewer approving them."""
        for c in self.board:
            if not keys or c["key"] in keys:
                c["status"] = "done"
                self.kanban[c["key"]]["status"] = "done"

    def ask_release(self, key, epic_row_id):
        if key in self.fail_ask:
            return False, "simulated review DB failure"
        self.asks.append((key, epic_row_id))
        self.ask_epics.append(epic_row_id)
        path = C.state_path(C.ASK_STATE_FILE)  # ask_human.py records every posted key in this file
        st = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        st[key] = "1.0"
        path.write_text(json.dumps(st), encoding="utf-8")
        return True, "CREATED"

    def creates(self):
        return [c for c in self.calls if c[0] in ("create", "dedup")]


@contextlib.contextmanager
def sandbox(world=None):
    """Temp state dir + fakes installed into the modules; everything restored afterwards."""
    import epic_gate_controller as ctl
    tmp = tempfile.TemporaryDirectory()
    saved = (C.STATE_DIR, C.ntn, ctl.hermes, C._sleep, C._sh, C.kanban_cards, ctl.ask_release, C.KANBAN_ROOT, C.HERMES_ROOT,
             C.kanban_context_cards, ctl.make_row, C.kanban_reviews, ctl.release_judgement_problems)
    C.STATE_DIR = Path(tmp.name)
    C.KANBAN_ROOT = Path(tmp.name) / "no-kanban"  # the suite never reads the real boards
    C.HERMES_ROOT = Path(tmp.name) / "no-hermes"  # ... nor the real agent session databases
    if world:
        C.ntn, ctl.hermes, C.kanban_cards, ctl.ask_release = world.ntn, world.hermes, world.kanban_cards, world.ask_release
        C.kanban_context_cards = world.kanban_context_cards
        ctl.make_row = world.make_row
        C.kanban_reviews, ctl.release_judgement_problems = world.kanban_reviews, world.release_judgement_problems
    C._sleep = lambda s: None
    errbuf = contextlib.redirect_stderr(io.StringIO())  # expected tracebacks of failure-injection tests stay quiet
    errbuf.__enter__()
    try:
        yield ctl, Path(tmp.name)
    finally:
        errbuf.__exit__(None, None, None)
        (C.STATE_DIR, C.ntn, ctl.hermes, C._sleep, C._sh, C.kanban_cards, ctl.ask_release, C.KANBAN_ROOT, C.HERMES_ROOT,
         C.kanban_context_cards, ctl.make_row, C.kanban_reviews, ctl.release_judgement_problems) = saved
        tmp.cleanup()


def base_world():
    """Two-phase epic: 要件定義 and 設計 (the 要件定義 checklist has CS/マーケ/PR items => parallel split B under legacy_phases)."""
    w = World()
    items = {"要件定義": ["要件書の作成", "ユースケースの整理", "CS確認", "マーケ確認", "PR確認"], "設計": ["設計書の作成", "見積もりの作成"]}
    hub, eid = w.add_epic(1, rows=[("要件定義", "未着手"), ("設計", "未着手")], items=items)
    return w, hub, eid


def no_merge(fn):
    """Run a check with one card per phase (MERGE_PHASES off, no parallel split)."""
    import functools

    @functools.wraps(fn)
    def run(*a, **kw):
        ctl = sys.modules.get("epic_gate_controller")
        old = getattr(ctl, "MERGE_PHASES", None)
        if ctl is not None:
            ctl.MERGE_PHASES = False
        try:
            return fn(*a, **kw)
        finally:
            if ctl is not None:
                ctl.MERGE_PHASES = old
    return run


# ---------------------------------------------------------------- checks: static

# Copy of hermes-agent's cron runtime scanner patterns for the assembled prompt (tools/cronjob_prompt_scan.py,
# _CRON_SKILL_ASSEMBLED_PATTERNS). A skill or cron prompt that matches one blocks EVERY cron job that loads it
# ('Status: BLOCKED ... deception_hide'), which silently stopped the pipeline monitor once.
_CRON_SCAN = [(r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
              (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
              (r"system\s+prompt\s+override", "sys_prompt_override"),
              (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules")]


@check
def skills_and_cron_prompts_do_not_trip_the_cron_injection_scanner():
    """Regression: a skill sentence 'do not tell the user ...' made the cron scanner BLOCK the health monitor's agent runs."""
    root = HERE.parent
    if not (root / "Makefile").exists():  # run from ~/.hermes/scripts (deploy gate): HERE.parent holds every installed skill, not the repo's
        return
    files = sorted((root / "skills").glob("**/*.md")) + sorted((root / "prompts").glob("*.txt"))
    hits = [f"{p.relative_to(root)}: {name}" for p in files for rx, name in _CRON_SCAN if re.search(rx, p.read_text(encoding="utf-8"), re.I)]
    expect(not hits, f"cron scanner would block: {hits}")
    for rx, name in _CRON_SCAN[1:2]:  # the pattern itself must keep matching the phrase that broke the monitor
        expect(re.search(rx, "Report it; do not tell the user the next step", re.I), name)


@check
def compile_all_from_source_warnings_as_errors():
    files = [HERE / n for n in PIPELINE_PY] + sorted((HERE / "tools").glob("*.py"))
    missing = [str(p) for p in files if not p.exists()]
    expect(not missing, f"pipeline files missing: {missing}")
    for p in files:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with open(p, encoding="utf-8") as f:
                compile(f.read(), str(p), "exec")


@check
def compile_check_really_catches_the_missing_comma_bug():
    src = 'x = 1\ns = ("a "\n     "b "(f"{x}"))\n'
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        raises((SyntaxError, SyntaxWarning), compile, src, "probe.py", "exec")


@check
def templates_load_and_have_expected_fields():
    want = {"phase_card": {"epic_url", "hub_name", "ph", "prow_url", "parts", "parallel_note"},
            "phase_card_process": {"full", "text"},
            "phase_card_parallel_note": {"n_groups", "group_names", "g"},
            "phase_card_merged_note": {"phases", "rows"},
            "phase_card_range_note": {"links"},
            "gate_ask": {"judge", "recommend", "checks", "conditions", "ships", "risks", "links", "copied_at", "key"},
            "release_meaning": {"pr", "deploy"},
            "phase_row_cancel_note": {"reason"}}
    for name, fields in want.items():
        expect(C.template_fields(name) == fields, f"{name}: {C.template_fields(name)} != {fields}")
    body = C.load_template("phase_card")
    for needle in ("HARD RULE: do NOT create, publish or merge a GitHub pull request", "ship_approved.py publishes, merges and deploys it",
                   "やることチェックリスト"):
        expect(needle in body, f"HARD RULE text lost: {needle}")
    for name in want:  # the light version has no company side
        text = C.load_template(name)
        for gone in ("approval-tool", "Slack", "Company task", "company epic", "会社エピック", "案件承認", "リリース判定書の承認"):
            expect(gone not in text, f"{name} still mentions {gone!r}")
    expect("判断すること" in C.render("gate_ask", judge="j", recommend="r", checks="c", conditions="d", ships="s", risks="k", links="l", copied_at="t", key="k"), "gate_ask renders")
    expect("チェックリスト" in C.load_template("phase_row_cancel_note"), "G4 note names the checklist")


@check
def template_with_missing_variable_or_stray_dollar_fails_loudly():
    saved = C.TEMPLATE_DIR
    with tempfile.TemporaryDirectory() as d:
        C.TEMPLATE_DIR = Path(d)
        try:
            (Path(d) / "t.md").write_text("hello ${who} and ${other}\n", encoding="utf-8")
            raises(KeyError, C.render, "t", who="x")
            (Path(d) / "u.md").write_text("price $5 for ${who}\n", encoding="utf-8")
            raises(ValueError, C.render, "u", who="x")
        finally:
            C.TEMPLATE_DIR = saved


@check
def rendered_bodies_equal_the_original_code_golden_file():
    import epic_gate_controller as ctl
    with open(HERE / "tests" / "golden_bodies.json", encoding="utf-8") as f:
        cases = json.load(f)
    expect(len(cases) >= 20, "golden file too small")
    for c in cases:
        body = ctl.render_body(c["ph"], c["g"], c["n_groups"], c["group_names"], c["epic_url"], c["hub_name"], c["prow_url"],
                               c["procs"], c["ordered"], lambda v: c["texts"][v["id"]])
        expect(body == c["body"], f"body differs for {c['ph']} group {c['g']}")
    expect(any(c["n_groups"] == 2 for c in cases) and any(c["ph"] == "要件定義" for c in cases), "golden lacks split/要件定義 cases")
    expect({c["ph"] for c in cases} == set(C.ORDER), "golden covers the 5 phases")
    expect(not any("Company task" in c["body"] or "approval-tool" in c["body"] for c in cases), "golden still carries company wording")
    # the task has four chapters (概要 / やることチェックリスト / 作業メモ / 完了レポート); review requests go to `hermes kanban request-review`, deliverable links to the 完了レポート
    expect(all("レビュー中の内容" not in c["body"] and "関連ドキュメント" not in c["body"] for c in cases), "card body still names a removed task chapter")
    expect(all("hermes kanban request-review" in c["body"] and "(作業メモ, 完了レポート)" in c["body"] for c in cases), "card body lacks the request-review / 完了レポート wording")
    # The repo files are read from the checkout `make stage` recorded, this checkout (HERE.parent) or the configured paths.repo (see _repo_roots).
    root = next((r for r in _repo_roots() if (r / "notion" / "templates" / "task.md").is_file()), None)
    if root is None:
        print("skip task template / prompt wording checks: no repo checkout with notion/templates/task.md (HERE.parent or paths.repo)")
        return
    tpl = (root / "notion" / "templates" / "task.md").read_text(encoding="utf-8")
    heads = re.findall(r"^(#{1,2}) (\S+)", tpl, re.M)
    expect([h for lv, h in heads if lv == "#"] == ["概要", "やることチェックリスト", "作業メモ", "完了レポート"], f"task template chapters: {heads}")
    expect([h for lv, h in heads if lv == "##"] == ["実施内容", "結果", "確認した方法", "未確認・残件", "次のタスク"], f"task template 完了レポート sections: {heads}")
    for name in ("scripts/templates/phase_card.md", "prompts/intake.txt", "prompts/health.txt", "prompts/review_apply.txt"):
        expect("レビュー中の内容" not in (root / name).read_text(encoding="utf-8"), f"{name} still names the removed task chapter")
    # the 概要 table has four rows (目的 / 完了条件 / 対象外 / 対象); card ID, assignee, epic and duplicate-check record are not rows
    overview = tpl.split("# やることチェックリスト", 1)[0]
    rows = [r for r in re.findall(r"<tr><td>(.*?)</td>", overview) if r != "項目"]
    expect(rows == ["目的", "完了条件", "対象外", "対象"], f"task template 概要 rows: {rows}")
    gone = ("実行キューのカード", "属するエピック", "重複確認の記録", "対象リポジトリ", "作業ブランチ")
    for name in ("notion/templates/task.md", "prompts/intake.txt", "prompts/planner.txt", "scripts/templates/phase_card.md",
                 "skills/autonomous-task-execution/SKILL.md", "skills/product-development-process/SKILL.md",
                 "skills/software-factory-ops/SKILL.md", "hermes/profiles/executor/SOUL.md"):
        if (root / name).is_file():
            t = (root / name).read_text(encoding="utf-8")
            expect(not any(g in t for g in gone), f"{name} still names a removed 概要 row: {[g for g in gone if g in t]}")
    expect(not any(g in c["body"] for c in cases for g in gone), "card body still names a removed 概要 row")
    expect(all("first line of its 作業メモ" in c["body"] for c in cases), "card body lacks the card ID in the 作業メモ first line rule")
    expect("first line of the page's 作業メモ" in (root / "prompts" / "intake.txt").read_text(encoding="utf-8"), "intake lacks the card ID rule")
    # the duplicate check stays a mandatory step even though it has no record row
    skill = root / "skills" / "autonomous-task-execution" / "SKILL.md"
    if skill.is_file():
        expect("Duplicate check before creating anything (mandatory)" in skill.read_text(encoding="utf-8"), "duplicate check is no longer mandatory in the skill")
    expect("Duplicate check" in (root / "prompts" / "planner.txt").read_text(encoding="utf-8") and "Duplicate check first" in (root / "prompts" / "intake.txt").read_text(encoding="utf-8"),
           "planner/intake lost the mandatory duplicate check")
    # the epic has six chapters; the three removed ones (打ち手の一覧 / ゲートの現在地 / レビュー中の内容) are in no template and no instruction
    epic_tpl = root / "notion" / "templates" / "epic.md"
    if not epic_tpl.is_file():
        print("skip epic template chapter check: no notion/templates/epic.md")
        return
    etxt = epic_tpl.read_text(encoding="utf-8")
    eheads = re.findall(r"^(#{1,2}) (\S+)", etxt, re.M)
    expect([h for lv, h in eheads if lv == "#"] == ["目的", "候補", "計測ログ", "管理ログ", "関連ドキュメント", "完了レポート"], f"epic template chapters: {eheads}")
    expect([h for lv, h in eheads if lv == "##"] == ["実施内容", "結果", "確認した方法", "未確認・残件", "次のタスク"], f"epic template 完了レポート sections: {eheads}")
    removed = ("打ち手の一覧", "ゲートの現在地", "レビュー中の内容", "承認パック")
    for name in ("notion/templates/epic.md", "scripts/templates/phase_card.md", "scripts/templates/gate_ask.md", "prompts/planner.txt",
                 "prompts/review_apply.txt", "prompts/health.txt", "prompts/intake.txt"):
        t = (root / name).read_text(encoding="utf-8")
        expect(not any(r in t for r in removed), f"{name} still names a removed epic chapter: {[r for r in removed if r in t]}")
    expect(not any(r in c["body"] for c in cases for r in removed), "card body still names a removed epic chapter")

@check
def no_approval_is_asked_for_要件承認_only_the_release_gate_exists():
    import epic_gate_controller as ctl
    expect(C.GATE_NAMES == ("リリース承認",), f"gate names {C.GATE_NAMES}")
    expect(not hasattr(C, "GATE_RECOMMENDATION") and not hasattr(ctl, "GATE_WHY") and not hasattr(ctl, "gate_text"), "the fixed recommendation / why are gone")
    expect("要件承認" not in C.build_release_ask(_facts())["body"], "要件承認 is not in the release ask")
    for ph in C.ORDER[:-1]:  # every phase before リリース判定: finished, and still nobody is asked
        expect(C.gates_due({ph: "完了"}) == [], f"{ph} 完了 must not make a gate due")
    w = World()
    hub, eid = w.add_epic(12, rows=[(ph, "完了") for ph in C.ORDER[:-1]] + [("リリース判定", "進行中")])
    with sandbox(w) as (ctl2, tmp):
        lines, errors = ctl2.run(True)
        expect(not errors and w.asks == [] and not any("ASK" in l for l in lines), f"four phases 完了: no human ask ({lines} {w.asks})")
        w.row(hub, "リリース判定")["properties"]["ステータス"] = _status("完了")
        ctl2.run(True)
        ctl2.run(True)
        expect([a[0] for a in w.asks] == [f"{eid[-8:]}-承認"], f"リリース判定 完了: the one approval, once: {w.asks}")
    # the repo files name no 要件承認 / "two approvals" (notion/schema.json keeps the DB option for old rows). Skipped without a checkout (staging).
    root = next((r for r in _repo_roots() if (r / "notion" / "templates" / "task.md").is_file()), None)
    if root is None:
        print("skip 要件承認 wording check: no repo checkout (HERE.parent or paths.repo)")
        return
    names = ["scripts/templates/gate_ask.md", "scripts/templates/phase_card.md", "scripts/tests/golden_bodies.json", "prompts/planner.txt",
             "prompts/review_apply.txt", "prompts/health.txt", "prompts/intake.txt", "notion/templates/review.md", "docs/design.md", "README.md",
             "hermes/profiles/executor/SOUL.md", "hermes/profiles/reviewer/SOUL.md"]
    names += [str(f.relative_to(root)) for d in ("skills/autonomous-task-execution", "skills/product-development-process", "skills/software-factory-ops")
              for f in sorted((root / d).rglob("*.md"))]
    stale = ("要件承認", "two human approvals", "two gate", "2つの承認", "承認は2つ", "承認2つ")
    for name in names:
        if (root / name).is_file():
            text = (root / name).read_text(encoding="utf-8")
            expect(not any(s in text for s in stale), f"{name} still names a requirements approval / two approvals: {[s for s in stale if s in text]}")
    expect("要件承認" not in (HERE / "tests" / "golden_bodies.json").read_text(encoding="utf-8"), "golden bodies name 要件承認")


@check
def cards_are_told_never_to_run_make_deploy_and_the_skills_say_deploy_is_main_only():
    """Cards (the template and every rendered golden body) and the two skills carry the 'deploy only from the main working tree' rule."""
    sentence = "Never run `make deploy` from a card: deploy runs once, from the main working tree, run by ship_approved.py after the approved squash merge"
    with open(HERE / "tests" / "golden_bodies.json", encoding="utf-8") as f:
        cases = json.load(f)
    expect(all(sentence in c["body"] for c in cases), "a golden card body lacks the 'never run make deploy from a card' rule")
    # the template next to this file: scripts/templates (checkout) or templates (staging / live, already rendered)
    tpl = HERE / "templates" / "phase_card.md"
    expect(tpl.is_file() and sentence in tpl.read_text(encoding="utf-8"), "phase_card.md lacks the deploy rule")
    # the rule is held by the Makefile, not written in the skills (stage 10): `make deploy` and `make sync` start with tools/require_main.sh.
    # The Makefile exists only in a checkout (HERE.parent), not in staging; paths.repo is not used (it may still be the old main during deploy-check)
    mk = HERE.parent / "Makefile"
    if mk.is_file():
        text = mk.read_text(encoding="utf-8")
        expect(re.search(r"^deploy:\n\tbash tools/require_main\.sh\n", text, re.M) and "SF_REQUIRE_MAIN_LABEL=sync bash tools/require_main.sh" in text, "the Makefile's deploy / sync no longer start with require_main.sh")


@check
def the_ui_image_rule_is_written_in_the_card_template_every_golden_body_and_the_skill():
    """The rule 'UI change without 変更前/変更後 images in the 判定書 -> no release ask' is stated where the agents read it (so it cannot vanish silently)."""
    sentence = "UI CHANGE (the branch changes a path under pages/ or src/livechat/ui/"
    with open(HERE / "tests" / "golden_bodies.json", encoding="utf-8") as f:
        cases = json.load(f)
    expect(all(sentence in c["body"] and "UI 変更：あり" in c["body"] for c in cases), "a golden card body lacks the UI image rule")
    tpl = HERE / "templates" / "phase_card.md"
    expect(tpl.is_file() and sentence in tpl.read_text(encoding="utf-8"), "phase_card.md lacks the UI image rule")
    skill = HERE.parent / "skills" / "product-development-process" / "SKILL.md"
    if skill.is_file():
        expect("ui_evidence_verdict" in skill.read_text(encoding="utf-8"), "the skill lacks the UI image rule")


@check
def instructions_and_skills_no_longer_name_the_dropped_ask_human_flags_or_review_kinds():
    root = next((r for r in _repo_roots() if (r / "notion" / "templates" / "review.md").is_file()), None)
    if root is None:
        print("skip review wording check: no repo checkout (HERE.parent or paths.repo)")
        return
    review = json.loads((root / "notion" / "schema.json").read_text(encoding="utf-8"))["databases"]["review"]["properties"]
    expect(list(review) == ["名前", "キー", "ステータス", "対象エピック", "反映済み", "作成日時"], f"review schema: {list(review)}")
    expect(review["対象エピック"] == {"type": "relation", "relation": {"target": "epic", "mode": "single_property"}}, review["対象エピック"])
    tpl = (root / "notion" / "templates" / "review.md").read_text(encoding="utf-8")
    expect(not any(f"<td>{w}</td>" in tpl for w in ("種別", "優先度", "レビュー先", "対象タスク")), "review template rows")
    files = ["prompts/review_apply.txt", "prompts/planner.txt", "prompts/health.txt", "prompts/intake.txt", "scripts/templates/gate_ask.md"]
    files += [str(f.relative_to(root)) for d in ("skills/autonomous-task-execution", "skills/product-development-process", "skills/software-factory-ops")
              for f in sorted((root / d).rglob("*.md"))]
    for name in files:
        t = (root / name).read_text(encoding="utf-8")
        ask_lines = [l for l in t.splitlines() if "ask_human.py" in l]  # (the kanban card's own --priority is another thing)
        bad = [w for w in ("--kind", "--url", "--priority", "--task ") if any(w in l for l in ask_lines)]
        bad += [w for w in ("REVIEW_KINDS", "完了の確認", "外部送信の指示", "PRレビュー", "その他の判断") if w in t]
        expect(not bad, f"{name} still names a dropped ask_human flag / review kind: {bad}")


@check
def constants_keep_the_agreed_pipeline_policy():
    import epic_gate_controller as ctl
    expect(C.ORDER == ["要件定義", "設計", "実装", "テスト", "リリース判定"], "ORDER changed")
    expect(C.GATE_ROWS == (("リリース判定", "リリース承認"),) and C.GATE_NAMES == ("リリース承認",), "one human gate: リリース承認 at completion")
    expect(C.HUB_PHASE_STATUS == {"要件定義": "要件定義", "設計": "設計", "実装": "実装", "テスト": "テスト", "リリース判定": "承認待ち"}, "HUB_PHASE_STATUS")
    expect(C.HUB_STATUSES == ("要件定義", "設計", "実装", "テスト", "承認待ち", "完了", "中止") and C.HUB_KEEP_STATUSES == {"完了", "中止"}, "the hub's 7 statuses")
    expect("効果測定" not in C.HUB_STATUSES and not hasattr(C, "POST_RELEASE") and not hasattr(C, "HUB_EFFECT_STATUS") and not hasattr(C, "phase_order") and not hasattr(C, "ALL_PHASE_ROWS"), "効果測定 / リリース後作業 are gone")
    expect((C.REGISTRY_DS, C.TASK_DS, C.REVIEW_DS) == ("51a4bd0e-3f79-00a1-c2fa-dae61bdc22f0", "1fc17149-3110-bac2-b3e9-008de0515786",
                                                      "31cd1b97-b525-0c17-3e66-508b9f72b88a"), "data source ids")
    expect(C.BOARD == "dev" and C.KANBAN_DB.parts[-4:] == ("kanban", "boards", "dev", "kanban.db"), "board dev")
    sonnet, prov = "claude-sonnet-5-5[1m]", "claude-subscription-directsdk-experimental"
    expect(set(C.PHASE_MODEL) == set(C.ORDER), "a model entry for every phase")
    expect(all(e["provider"] == prov and e["reasoning"] == "high" for e in C.PHASE_MODEL.values()), "provider / reasoning")
    expect(all(e["model"] == sonnet for e in C.PHASE_MODEL.values()) and C.MODEL_DESIGN == C.MODEL_SONNET == sonnet,
           "Sonnet for every phase, 設計 included ([models].design = worker)")
    expect(not hasattr(C, "MODEL_OPUS"), "no Opus pin for 設計 any more")
    cards = (["要件定義"], ["設計"], ["要件定義", "設計", "実装"], ["実装", "テスト", "リリース判定"])
    expect(all(C.model_for(p)["model"] == sonnet and C.card_model_args(p) == [] for p in cards),
           "model_for is Sonnet and no phase card is pinned (a pinned model would also drive the review run)")
    expect(C.PARALLEL == {} and C.MERGE_PHASES is True, "one merged card per epic, no parallel split")
    expect(C.phase_key("E", "実装") == "phase-E-実装" and C.phase_key("E", "実装", "B") == "phase-E-実装-B", "idempotency key")
    expect(C.tenant_of("a" * 24 + "12345678") == "epic-12345678", "tenant")
    expect(ctl.ORDER is C.ORDER and ctl.BOARD == "dev", "controller re-exports")
    for gone in ("COMPANY_TASK_DS", "AGENT", "PRIVATE_DS", "OPEN", "PHASES", "DONE_STATUSES", "TRIAL_MODEL", "TRIAL_PROVIDER", "LEAK_CACHE_FILE"):
        expect(not hasattr(C, gone), f"epic_common still defines {gone}")
    expect(not hasattr(C, "REVIEW_KINDS") and not hasattr(C, "review_kind"), "the review row has no 種別 any more")
    expect(C.gate_key("a" * 24 + "12345678", "リリース承認") == "12345678-承認" and C.legacy_gate_key("a" * 24 + "12345678", "リリース承認") == "12345678-リリース承認", "gate key")


@check
def light_version_has_no_company_epic_or_company_task_dependency():
    """Design ch.11: no company epic, no company task DB, no old board in any pipeline script or template."""
    names = [n for n in PIPELINE_PY if n != "epic_selftest.py"] + ["epic_gate_apply.py", "michiomochi_task_events.py"]
    for n in names:
        p = HERE / n
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for gone in ("COMPANY_TASK_DS", "会社エピック", "work-card", "michiomochi-card"):
            expect(gone not in text, f"{n} still mentions {gone}")
        expect("michiomochiエピック" not in text, f"{n} still uses the old relation name michiomochiエピック")
    for p in sorted((HERE / "templates").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        expect("会社エピック" not in text and "michiomochi-card" not in text and "michiomochiエピック" not in text, f"{p.name}")


# ---------------------------------------------------------------- checks: pure functions

@check
@legacy_phases
def pure_decision_functions():
    import epic_gate_controller as ctl
    w = World()
    hub, eid = w.add_epic(2, rows=[("設計", "未着手"), ("設計", "未着手"), ("実装", "未着手")])
    rows, dups = ctl.parse_rows(w.rows[hub["id"]])
    expect(dups == ["設計"] and set(rows) == {"設計", "実装"}, "parse_rows reports duplicate phase rows")
    procs = ctl.phase_procs("設計", ["設計書の作成", "見積もりの作成", "見積もりの作成"])
    expect(list(procs) == ["[設計] 設計書の作成", "[設計] 見積もりの作成", "[設計] 見積もりの作成 (2)"], f"unique labels in checklist order: {list(procs)}")
    expect(ctl.order_group({"[設計] 見積もりの作成", "[設計] 設計書の作成"}, procs) == ["[設計] 設計書の作成", "[設計] 見積もりの作成"], "checklist order, not alphabetical")
    pr = ctl.phase_procs("要件定義", ["要件書の作成", "CS確認", "PR確認"])
    expect(ctl.split_groups("要件定義", pr) == {"A": {"[要件定義] 要件書の作成"}, "B": {"[要件定義] CS確認", "[要件定義] PR確認"}}, "split_groups")
    expect(list(ctl.split_groups("要件定義", ctl.phase_procs("要件定義", ["CS確認"]))) == ["A"], "only-B items stay one card")
    expect(list(ctl.split_groups("実装", ctl.phase_procs("実装", ["実装メモ"]))) == ["A"], "no parallel config outside 要件定義")
    # checklist chapter reader
    expect(C.checklist_items(_page_blocks([True, False], memo_todos=2)) == ["item0", "item1"], "only the やることチェックリスト chapter counts")
    blks = [_block("heading_2", "やることチェックリスト"), _block("to_do", "a"), _block("to_do", "  "), _block("heading_3", "小見出し"), _block("to_do", "b"),
            _block("heading_2", "作業メモ"), _block("to_do", "c"), _block("heading_1", "完了レポート"), _block("to_do", "d")]
    expect(C.checklist_items(blks) == ["a", "b"], "blank items are no items; a deeper heading stays inside; the next heading of the same level ends it")
    expect(C.checklist_items([_block("to_do", "x"), _block("heading_1", "概要"), _block("to_do", "y")]) == [], "no checklist chapter: no items")
    expect(C.checklist_items([_block("heading_1", "やることチェックリスト"), _block("to_do", "")]) == [] and C.checklist_items([]) == [], "empty chapter / empty page")


@check
def next_action_orders_phases_and_waits_for_rows():
    import epic_gate_controller as ctl
    w, hub, eid = base_world()
    prows, _ = ctl.parse_rows(w.rows[hub["id"]])
    items_of = lambda ph, prow: ["x"]
    set_row = lambda ph, st: prows[ph]["properties"].__setitem__("ステータス", _status(st))
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect(act and act["ph"] == "要件定義" and canc == [] and [x["ph"] for x in act.get("extra", [])] == ["設計"], f"starts the first phase, 設計 joins the card: {act}")
    expect(list(act["procs"]) == ["[要件定義] x"], "procs come from the checklist items")
    set_row("要件定義", "進行中")
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect(act is None and msgs == [] and canc == [], "running phase blocks later phases")
    set_row("要件定義", "レビュー中")  # a person must decide: NOT finished
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect(act is None and canc == [], "レビュー中 is not finished: the next phase must wait")
    set_row("要件定義", "完了")
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect(act and act["ph"] == "設計" and not act.get("extra"), "next phase after the finished one (no later row exists: nothing to merge)")
    set_row("要件定義", "キャンセル")
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect(act and act["ph"] == "設計", "キャンセル counts as finished")
    # a live card of an earlier phase blocks the next phase even though its row is 完了
    set_row("要件定義", "完了")
    live = C.live_phase_cards([dict(id="t1", status="running", assignee="executor", tenant=C.tenant_of(eid), key=C.phase_key(eid, "要件定義"), title="x")], eid)
    msgs, act, canc = ctl.next_action(items_of, prows, "H", live)
    expect(act is None and msgs and msgs[0].startswith("ORDER [設計]") and "live cards: 要件定義" in msgs[0], f"live earlier card blocks: {msgs}")
    live_title = C.live_phase_cards([dict(id="t1", status="ready", assignee="executor", tenant=C.tenant_of(eid), key="", title="[要件定義] 手動カード")], eid)
    expect(live_title.get("要件定義"), "'[phase]' title prefix of the same tenant counts too")
    other = C.live_phase_cards([dict(id="t1", status="running", assignee="executor", tenant="epic-ffffffff", key="", title="[要件定義] x"),
                                dict(id="t2", status="done", assignee="executor", tenant=C.tenant_of(eid), key=C.phase_key(eid, "要件定義"), title="x")], eid)
    expect(other == {}, "other tenants and done cards are not live")
    set_row("要件定義", "未着手")
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect(act["ph"] == "要件定義", "unfinished earlier phase starts first")
    # the planner creates all rows of an epic at once: a missing row means waiting for it
    msgs, act, canc = ctl.next_action(items_of, {"設計": prows["設計"]}, "H")
    expect(act is None and msgs and msgs[0].startswith("NOROW [要件定義]"), f"NOROW message: {msgs}")
    msgs, act, canc = ctl.next_action(items_of, {}, "H")
    expect(act is None and msgs and msgs[0].startswith("NOROW [要件定義]"), f"NOROW message: {msgs}")
    rows = {ph: {"id": ph, "url": "u", "properties": {"ステータス": _status("完了")}} for ph in C.ORDER}
    msgs, act, canc = ctl.next_action(items_of, rows, "H")
    expect(act is None and canc == [] and msgs == [], "all finished: nothing to do")


@check
@no_merge
def merge_off_starts_exactly_one_phase():
    import epic_gate_controller as ctl
    w, hub, eid = base_world()
    prows, _ = ctl.parse_rows(w.rows[hub["id"]])
    msgs, act, canc = ctl.next_action(lambda ph, prow: ["x"], prows, "H")
    expect(act["ph"] == "要件定義" and "extra" not in act, f"{act}")


@check
def g4_a_row_without_checklist_items_is_cancelled_not_skipped():
    import epic_gate_controller as ctl

    def mk():  # テスト row 未着手 with an EMPTY checklist (one blank to_do only); リリース判定 has items
        w = World()
        hub, eid = w.add_epic(4, rows=[("要件定義", "完了"), ("設計", "完了"), ("実装", "完了"), ("テスト", "未着手"), ("リリース判定", "未着手")],
                              items={"テスト": [""], "リリース判定": ["判定書の草稿", "PR本文の下書き"]})
        return w, hub, eid
    w, hub, eid = mk()
    prows, _ = ctl.parse_rows(w.rows[hub["id"]])
    items_of = lambda ph, prow: {"テスト": [], "リリース判定": ["判定書の草稿"]}[ph]
    msgs, act, canc = ctl.next_action(items_of, prows, "H")
    expect([c[0] for c in canc] == ["テスト"] and act and act["ph"] == "リリース判定", f"cancel + continue: {canc} {act and act['ph']}")
    # a row edited a moment ago waits (the planner may still be writing the checklist)
    prows["テスト"]["last_edited_time"] = "2026-11-07T00:00:00.000Z"
    now = C.age_seconds("2026-11-07T00:00:00.000Z", 0) * -1 + 60
    msgs, act, canc = ctl.next_action(items_of, prows, "H", now=now)
    expect(act is None and canc == [] and msgs and msgs[0].startswith("WAIT [テスト]"), f"fresh row waits: {msgs}")
    msgs, act, canc = ctl.next_action(items_of, prows, "H", now=now + 3600)
    expect([c[0] for c in canc] == ["テスト"], "old enough: cancelled")
    # a row WITH items is never cancelled by G4, whatever else is in its body
    msgs, act, canc = ctl.next_action(lambda ph, prow: ["項目"], prows, "H")
    expect(canc == [], "rows with items are not cancelled")
    # apply: note first, then キャンセル; the next phase of the same run starts; dry run only announces
    w0, _, _ = mk()
    with sandbox(w0) as (ctl2, tmp):
        lines, errors = ctl2.run(False)
        expect(any(l.startswith("WOULD CANCEL [テスト]") for l in lines) and any(l.startswith("WOULD CREATE [リリース判定]") for l in lines)
               and not w0.notes and not [c for c in w0.calls if c[0] == "patch"] and w0.asks == [], f"dry: {lines}")
    w, hub, eid = mk()
    with sandbox(w) as (ctl2, tmp):
        lines, errors = ctl2.run(True)
        expect(not errors, f"{lines}")
        trow = w.row(hub, "テスト")
        expect(C.status_of(trow) == "キャンセル", "row cancelled")
        expect(w.notes and w.notes[0][0] == trow["id"] and "キャンセル" in w.notes[0][1] and "チェックリスト" in w.notes[0][1], f"note on the row: {w.notes}")
        order = [c[0] for c in w.calls]
        expect(order.index("note") < order.index("patch"), f"note before status: {order}")
        expect(any(l.startswith("CANCELLED [テスト]") for l in lines) and any(l.startswith("CREATED [リリース判定]") for l in lines), f"{lines}")
        body = _creates_of(w)[0][3]
        expect("[リリース判定] 判定書の草稿" in body and "[リリース判定] PR本文の下書き" in body, "each checklist item is a section of the card body")


# ---------------------------------------------------------------- checks: controller flow (fakes)

def _creates_of(w, kind="create"):
    return [c for c in w.calls if c[0] == kind]


@check
@legacy_phases
def apply_happy_path_split_cards_row_patched_last():
    w, hub, eid = base_world()
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors, f"errors: {errors}")
        expect(any(l.startswith("EPIC ") for l in lines) and sum(1 for l in lines if l.startswith("CREATED [要件定義]")) == 2, f"lines: {lines}")
        expect(sum(1 for l in lines if l.startswith("SPLIT ")) == 1, f"lines {lines}")
        c = _creates_of(w)
        expect([x[1] for x in c] == [f"phase-{eid}-要件定義", f"phase-{eid}-要件定義-B"], f"keys {[x[1] for x in c]}")
        args, stdin = c[0][2], c[0][3]
        for pair in (("--tenant", f"epic-{eid[-8:]}"), ("--assignee", "executor")):
            expect(args[args.index(pair[0]) + 1] == pair[1], f"arg {pair}")
        expect("--priority" not in args, f"the card priority is left to the kanban default {args}")
        expect("--model" not in args and "--provider" not in args, f"no model pin: executor and reviewer run on their profile's model {args}")
        expect(args[1] == "[要件定義] ハブ1" and "--body-file" in args and "-" in args, "title/body-file")
        expect("HARD RULE: do NOT create, publish or merge" in stdin and "[要件定義] 要件書の作成" in stdin and "CS確認" not in stdin, "body of group A")
        bodyB = c[1][3]
        expect("PARALLEL SPLIT" in bodyB and "this card is group B" in bodyB and "[要件定義] CS確認" in bodyB and "要件書の作成" not in bodyB, "B body")
        order = [x[0] for x in w.calls]
        expect(order == ["create", "create", "patch"], f"call order {order}")  # both cards, then the phase row once
        expect(w.calls[2][1] == w.row(hub, "要件定義")["id"] and w.calls[2][2] == "進行中", "phase row patched to 進行中 after both cards")
        j = C.load_journal()
        e = j[f"{eid}:要件定義"]
        expect(e["row"] == "done" and sorted(e["cards"]) == ["A", "B"] and "procs_pending" not in e, f"journal: {e}")
        with open(C.state_path(C.MODEL_TRIAL_FILE), encoding="utf-8") as f:
            logged = [json.loads(l) for l in f]
        expect(len(logged) == 2 and all(x["model"] == "claude-sonnet-5-5[1m]" and x["reasoning"] == "high" for x in logged)
               and [x["group"] for x in logged] == ["A", "B"], f"trial log {logged}")
        # next phase: the 要件定義 cards are still live, so 設計 waits; 要件定義 完了 asks nobody
        w.row(hub, "要件定義")["properties"]["ステータス"] = _status("完了")
        w.calls.clear()
        lines, errors = ctl.run(True)
        expect(not errors and _creates_of(w) == [], f"the 要件定義 cards are still live: the next phase must wait ({lines})")
        expect(any(l.startswith("ORDER [設計]") and "live cards" in l for l in lines), f"ORDER line expected: {lines}")
        expect(C.status_of(hub) == "設計" and w.asks == [], f"hub status / no approval is asked for 要件定義: {C.status_of(hub)} {w.asks}")
        w.finish_cards()  # the reviewer approved: cards done
        w.calls.clear()
        lines, errors = ctl.run(True)
        expect(not errors, f"errors: {errors}")
        keys = [x[1] for x in _creates_of(w)]
        expect(keys == [f"phase-{eid}-設計"], f"keys {keys}")
        args = _creates_of(w)[0][2]
        expect("--model" not in args and "--provider" not in args, f"the 設計 card is not pinned (executor's Sonnet, reviewer's own model) {args}")
        kinds = [x[0] for x in w.calls]
        expect(kinds == ["create", "patch"], f"order {kinds}")  # card, then the phase row (the hub status was already set while waiting)
        expect(w.calls[1][1] == w.row(hub, "設計")["id"], "the phase row is patched right after the card")
        expect(w.asks == [], f"no approval is asked between the early phases: {w.asks}")


@check
def merged_card_covers_consecutive_phases_in_one_session():
    w, hub, eid = base_world()
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors, f"errors: {errors}")
        c = _creates_of(w)
        expect(len(c) == 1, f"exactly one card: {[x[1] for x in c]}")
        key, args, stdin = c[0][1], c[0][2], c[0][3]
        expect(key.startswith(f"phase-{eid}-要件定義+") and C.is_phase_card_key(key, eid, "要件定義") and C.is_phase_card_key(key, eid, "設計"), f"merged key {key}")
        expect(not C.is_phase_card_key(key, eid, "実装"), "key only matches the phases it lists")
        expect("ONE CARD FOR SEVERAL PHASES" in stdin and "要件定義 -> 設計" in stdin, "merged note in body")
        expect("[要件定義] 要件書の作成" in stdin and "[設計] 設計書の作成" in stdin, "the items of both phases are in the body")
        expect(stdin.index("[要件定義] 要件書の作成") < stdin.index("[設計] 設計書の作成"), "phase by phase, checklist order")
        expect("--model" not in args and "--provider" not in args, f"a merged card that covers 設計 is not pinned {args}")
        expect(all(C.status_of(w.row(hub, p)) == "進行中" for p in key.split("-")[-1].split("+")), "every merged row is 進行中")
        expect(any("merged phases" in l for l in lines), f"CREATED line names the merged phases: {lines}")
        w.calls.clear()
        lines, errors = ctl.run(True)
        expect(not errors and _creates_of(w) == [], f"no duplicate card on the next run: {lines}")


@check
@no_merge
def model_table_no_pin_for_every_phase_card_design_pin_follows_config():
    def model_of(phases_done):
        w = World()
        rows = [(ph, "完了" if ph in phases_done else "未着手") for ph in C.ORDER]
        hub, eid = w.add_epic(7, rows=rows)
        with sandbox(w) as (ctl, tmp):
            lines, errors = ctl.run(True)
            expect(not errors, f"{lines}")
            args = _creates_of(w)[0][2]
            flag = lambda f: args[args.index(f) + 1] if f in args else None  # noqa: E731
            return flag("--model"), flag("--provider"), args[1]
    opus, prov = "claude-opus-5-5[1m]", "claude-subscription-directsdk-experimental"
    # default config ([models].design = worker): no card is pinned, so the executor works it on its own Sonnet and the reviewer
    # reviews it on its own model (a card's --model drives every run of the card, the review run included)
    for done, title in (((), "[要件定義] ハブ7"), (("要件定義",), "[設計] ハブ7"), (("要件定義", "設計"), "[実装] ハブ7"),
                        (("要件定義", "設計", "実装"), "[テスト] ハブ7"), (("要件定義", "設計", "実装", "テスト"), "[リリース判定] ハブ7")):
        expect(model_of(done) == (None, None, title), f"{title}: no --model / --provider")
    # the knob still works: a different [models].design pins only the cards that cover 設計
    saved = C.PHASE_MODEL["設計"]
    C.PHASE_MODEL["設計"] = dict(saved, model=opus)
    try:
        expect(model_of(("要件定義",)) == (opus, prov, "[設計] ハブ7"), "設計 pinned when [models].design differs from worker")
        expect(model_of(("要件定義", "設計")) == (None, None, "[実装] ハブ7"), "実装 stays unpinned")
    finally:
        C.PHASE_MODEL["設計"] = saved


@check
def kanban_create_output_parsing():
    import epic_gate_controller as ctl
    expect(ctl.parse_created('{"id": "t_ab12", "created_at": 1793986735, "status": "ready"}') == ("t_ab12", 1793986735, "ready"), "plain json")
    expect(ctl.parse_created('notice line\n{\n  "id": "t_ab12",\n  "created_at": 5\n}\n') == ("t_ab12", 5, None), "notice before json")
    expect(ctl.parse_created("Created t_cd34  (ready, assignee=pdm)") == ("t_cd34", None, "ready"), "text fallback")
    expect(ctl.parse_created("Created t_cd34") == ("t_cd34", None, None), "text fallback without status")
    raises(C.PipelineError, ctl.parse_created, "something else")
    raises(C.PipelineError, ctl.parse_created, "")


@check
def dry_run_changes_nothing():
    w, hub, eid = base_world()
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(False)
        expect(not errors and any(l.startswith("WOULD CREATE [要件定義]") for l in lines), f"{lines}")
        expect(w.creates() == [] and not [c for c in w.calls if c[0] == "patch"], "dry run wrote something")
        expect(list(tmp.iterdir()) == [], f"dry run wrote state: {list(tmp.iterdir())}")


@check
@legacy_phases
def group_b_failure_is_resumed_without_duplicates_or_early_row_patch():
    w, hub, eid = base_world()
    keyB = f"phase-{eid}-要件定義-B"
    with sandbox(w) as (ctl, tmp):
        w.fail_create = {keyB}
        lines, errors = ctl.run(True)
        expect(errors and any(l.startswith("FAIL ") for l in lines), f"FAIL expected: {lines}")
        e = C.load_journal()[f"{eid}:要件定義"]
        expect(e["row"] == "pending" and list(e["cards"]) == ["A"], f"journal {e}")
        expect(C.status_of(w.row(hub, "要件定義")) == "未着手" and not [c for c in w.calls if c[0] == "patch" and c[1] != hub["id"]], "row must stay 未着手")
        w.fail_create = set()
        w.calls.clear()
        lines, errors = ctl.run(True)
        expect(not errors, f"{errors}")
        expect([c[1] for c in w.creates()] == [keyB], f"resume must create only B: {[c[1] for c in w.creates()]}")
        expect(C.status_of(w.row(hub, "要件定義")) == "進行中", "row patched after all cards")
        expect(C.load_journal()[f"{eid}:要件定義"]["row"] == "done", "journal done")
        with open(C.state_path(C.MODEL_TRIAL_FILE), encoding="utf-8") as f:
            expect(len(f.readlines()) == 2, "each real card logged once")


@check
@legacy_phases
def crash_after_create_before_journal_is_idempotent_and_not_double_logged():
    w, hub, eid = base_world()
    keyB = f"phase-{eid}-要件定義-B"
    with sandbox(w) as (ctl, tmp):
        w.fail_create = {keyB}
        ctl.run(True)
        C.state_path(C.JOURNAL_FILE).unlink()  # simulate a crash that lost the journal
        w.fail_create = set()
        w.calls.clear()
        time.sleep(1.1)  # the re-hit of card A has an older created_at than this run
        lines, errors = ctl.run(True)
        expect(not errors, f"{errors}")
        kinds = {c[1]: c[0] for c in w.creates()}
        expect(kinds[f"phase-{eid}-要件定義"] == "dedup" and kinds[keyB] == "create", f"kinds {kinds}")
        expect(len({v["id"] for v in w.kanban.values()}) == len(w.kanban) == 2, "no duplicate cards")
        with open(C.state_path(C.MODEL_TRIAL_FILE), encoding="utf-8") as f:
            expect(len(f.readlines()) == 2, "A and B logged once each (the re-hit of A is not logged again)")


@check
def row_patch_failure_resumes_without_recreating_cards():
    w, hub, eid = base_world()
    prow = w.row(hub, "要件定義")
    with sandbox(w) as (ctl, tmp):
        w.fail_patch = {prow["id"]}
        lines, errors = ctl.run(True)
        expect(errors and any(l.startswith("ERROR epic") for l in lines), f"{lines}")
        expect(C.load_journal()[f"{eid}:要件定義"]["row"] == "pending", "pending after failed row patch")
        w.fail_patch = set()
        before = len(w.creates())
        lines, errors = ctl.run(True)
        expect(not errors and len(w.creates()) == before, "no second kanban create call on resume")
        expect(C.status_of(prow) == "進行中" and C.load_journal()[f"{eid}:要件定義"]["row"] == "done", "resumed")
        expect(C.status_of(w.row(hub, "設計")) == "進行中", "the merged 設計 row moves with it")


@check
def per_epic_error_boundary_and_exit_code_and_heartbeat():
    w, hub1, eid1 = base_world()
    hub2, eid2 = w.add_epic(2, rows=[("要件定義", "未着手")])
    w.break_epic = {hub1["id"]}
    with sandbox(w) as (ctl, tmp):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ctl._run_and_report(True)
        text = out.getvalue()
        expect(rc == 1 and f"ERROR epic {hub1['id'].replace('-', '')[-8:]}" in text, f"rc={rc} out={text!r}")
        expect(any(c[1] == f"phase-{eid2}-要件定義" for c in w.creates()), "second epic must still be processed")
        st = C.read_status()
        expect(st["ok"] is False and "last_success" not in st and st["last_run"] >= w.t0, f"status {st}")
        w.break_epic = set()
        w.row(hub2, "要件定義")["properties"]["ステータス"] = _status("完了")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ctl._run_and_report(True)
        st = C.read_status()
        expect(rc == 0 and st["ok"] is True and st["last_success"] >= st["last_run"] - 1 and st["error"] is None, f"rc={rc} status={st}")


@check
def registry_failure_is_loud():
    w, hub, eid = base_world()
    with sandbox(w) as (ctl, tmp):
        def boom(*a, **k):
            raise C.PipelineError("notion down")
        C.ntn = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ctl._run_and_report(True)
        expect(rc == 1 and out.getvalue().startswith("ERROR PipelineError: notion down"), out.getvalue())
        expect(C.read_status()["ok"] is False, "heartbeat records the failure")


@check
@legacy_phases
def duplicate_phase_rows_skip_the_epic():
    w, hub, eid = base_world()
    w.add_epic(3, rows=[("要件定義", "未着手"), ("要件定義", "未着手")])
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(any(l.startswith("DUP [要件定義]") for l in lines), f"{lines}")
        expect(sorted(c[1] for c in w.creates()) == [f"phase-{eid}-要件定義", f"phase-{eid}-要件定義-B"], "only the clean epic gets cards")


@check
def run_time_budget_stops_new_work():
    w, hub, eid = base_world()
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True, deadline=time.time() - 1)
        expect(not errors and any(l.startswith("WARN run time budget") for l in lines) and w.creates() == [], f"{lines}")


@check
def controller_argument_parsing_is_strict_and_lock_is_exclusive():
    import epic_gate_controller as ctl
    for argv in (["--aply"], ["--app"], ["--apply", "--assume-status=x"], ["--bogus"]):
        with contextlib.redirect_stderr(io.StringIO()):  # argparse usage text
            e = raises(SystemExit, ctl.main, argv)
        expect(e.code == 2, f"{argv} -> {e.code}")
    with sandbox() as (ctl2, tmp):
        with C.RunLock() as a:
            expect(a.acquired, "first lock")
            with C.RunLock() as b:
                expect(not b.acquired, "second lock must not be acquired")
        with C.RunLock() as c:
            expect(c.acquired, "lock released")


# ---------------------------------------------------------------- checks: helpers

class _Seq:
    """Fake C._sh returning a scripted sequence of results (or raising)."""

    def __init__(self, items):
        self.items, self.calls = list(items), []

    def __call__(self, cmd, input=None, timeout=90):
        self.calls.append(cmd)
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        rc, out, err = it
        return subprocess.CompletedProcess(cmd, rc, out, err)


@check
def ntn_retries_transient_errors_but_not_permanent_ones():
    with sandbox() as (ctl, tmp):
        C._sh = _Seq([(1, "", "HTTP 429 rate limited"), (1, "", "502 bad gateway"), (0, '{"ok": 1}', "")])
        expect(C.ntn("v1/x") == {"ok": 1} and len(C._sh.calls) == 3, "recovered after 2 transient failures")
        C._sh = _Seq([(1, "", "error: Failed to execute public API request"), (1, "", "nodename nor servname provided"), (0, '{"ok": 2}', "")])
        expect(C.ntn("v1/x") == {"ok": 2} and len(C._sh.calls) == 3, "network blips (ntn 'Failed to execute public API request') are retried")
        C._sh = _Seq([(1, "", "HTTP 404 object not found")])
        raises(C.PipelineError, C.ntn, "v1/x")
        expect(len(C._sh.calls) == 1, "permanent error must not be retried")
        C._sh = _Seq([subprocess.TimeoutExpired("ntn", 60)] * 3)
        e = raises(C.PipelineError, C.ntn, "v1/x")
        expect("timeout" in str(e) and len(C._sh.calls) == 3, f"timeout handling: {e}")
        C._sh = _Seq([(0, "not json", "")] * 3)
        raises(C.PipelineError, C.ntn, "v1/x")


@check
def query_and_page_text_paginate_and_descend():
    with sandbox() as (ctl, tmp):
        pages = [{"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}, {"results": [{"id": "b"}], "has_more": False}]
        seen = []

        def fake(*args, data=None):
            seen.append((args, data))
            return pages.pop(0)
        C.ntn = fake
        out = C.query("ds", {"f": 1})
        expect([r["id"] for r in out] == ["a", "b"], "query follows next_cursor")
        expect(seen[0][1] == {"page_size": 100, "filter": {"f": 1}} and seen[1][1]["start_cursor"] == "c1", f"bodies {seen}")
        blk = lambda t, ch=False, bid="x": {"id": bid, "type": "paragraph", "has_children": ch, "paragraph": {"rich_text": [{"plain_text": t}]}}
        script = {("v1/blocks/p/children", None): {"results": [blk("one", True, "k"), blk("")], "has_more": True, "next_cursor": "n1"},
                  ("v1/blocks/p/children", "n1"): {"results": [blk("three")], "has_more": False},
                  ("v1/blocks/k/children", None): {"results": [blk("child")], "has_more": False}}
        calls = []

        def fake2(*args, data=None):
            cur = next((a.split("==", 1)[1] for a in args if a.startswith("start_cursor==")), None)
            calls.append(args)
            return script[(args[0], cur)]
        C.ntn = fake2
        expect(C.page_text("p") == "one\n  child\nthree", repr(C.page_text("p")))
        expect(all("page_size==100" in c for c in calls), "page_size query parameter")


@check
def state_files_are_atomic_and_corrupt_files_are_quarantined():
    with sandbox() as (ctl, tmp):
        C.atomic_write_json(C.state_path("x.json"), {"a": 1})
        expect(json.loads(C.state_path("x.json").read_text()) == {"a": 1} and [p.name for p in tmp.iterdir()] == ["x.json"], "atomic write leaves no tmp")
        C.state_path("bad.json").write_text("{not json", encoding="utf-8")
        expect(C.read_json(C.state_path("bad.json"), "dflt") == "dflt" and any(p.name.startswith("bad.json.corrupt-") for p in tmp.iterdir()), "quarantine")
        expect(C.read_status() is None, "no status yet")
        C.write_status(False, "e1")
        s1 = C.read_status()
        expect(s1["ok"] is False and "last_success" not in s1, s1)
        C.write_status(True)
        expect(C.read_status()["last_success"] >= s1["last_run"], "success sets last_success")


# ---------------------------------------------------------------- checks: health

def _make_kanban_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,"
                " created_at INTEGER NOT NULL, tenant TEXT, idempotency_key TEXT)")
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at INTEGER)")
    return con


@check
def kanban_reader_is_read_only_and_matches_cards_by_idempotency_key():
    with sandbox() as (ctl, tmp):
        db = tmp / "kanban.db"
        eid = _epic_id(0x101)
        con = _make_kanban_db(db)
        rows = [("t_1", "[実装] x", "executor", "running", 20, 1, f"epic-{eid[-8:]}", C.phase_key(eid, "実装")),
                ("t_2", "[設計] x", "executor", "ready", 20, 2, f"epic-{eid[-8:]}", C.phase_key(eid, "設計")),
                ("t_3", "[設計] x", "executor", "running", 20, 3, f"epic-{eid[-8:]}", C.phase_key(eid, "設計", "B")),
                ("t_4", "unrelated title 実装", None, "done", 10, 4, f"epic-{eid[-8:]}", None),
                ("t_5", "old", "executor", "archived", 10, 5, f"epic-{eid[-8:]}", C.phase_key(eid, "テスト")),
                ("t_6", "[実装] other", "executor", "running", 10, 6, "epic-ffffffff", C.phase_key("f" * 32, "実装"))]
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        sha = subprocess.run(["shasum", str(db)], capture_output=True, text=True).stdout
        cards = C.kanban_cards(db)
        expect(subprocess.run(["shasum", str(db)], capture_output=True, text=True).stdout == sha, "db modified")
        expect([c["id"] for c in cards] == ["t_1", "t_2", "t_3", "t_4", "t_6"], f"archived hidden, list order: {[c['id'] for c in cards]}")
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        raises(sqlite3.OperationalError, ro.execute, "INSERT INTO tasks VALUES ('x','x',NULL,'x',0,0,NULL,NULL)")
        ro.close()
        mine = [c for c in cards if c["tenant"] == f"epic-{eid[-8:]}"]
        expect({c["id"] for c in mine if C.is_phase_card_key(c["key"], eid, "設計")} == {"t_2", "t_3"}, "-B suffix matches")
        expect(not any(C.is_phase_card_key(c["key"], eid, "実装") for c in cards if c["id"] == "t_6"), "other epic's key does not match")
        raises(C.PipelineError, C.kanban_cards, tmp / "missing.db")


@check
def health_snapshot_flags_stuck_rows_and_stays_timestamp_free():
    import epic_pipeline_health as H
    w, hub, eid = base_world()
    rows = w.rows[hub["id"]]
    rows[0]["properties"]["ステータス"] = _status("進行中")
    cards = [dict(id="t_1", status="done", assignee="reviewer", tenant=C.tenant_of(eid), key=C.phase_key(eid, "設計"), title="x")]
    with sandbox() as (ctl, tmp):
        now = 1_800_000_000
        C.write_status(True)
        st = C.read_status()
        st["last_success"] = now - 30
        C.atomic_write_json(C.state_path(C.STATUS_FILE), st)
        alerts, lines = H.snapshot(now, cards, w.hubs, lambda h: rows)
        text = "\n".join(lines)
        e8 = eid[-8:]
        expect(lines[0].startswith("EPIC ") and f"ALERT {e8} 要件定義: row 進行中 but no live card (stuck)" in text, text)
        expect(alerts == [f"ALERT {e8} 要件定義: row 進行中 but no live card (stuck)"], f"alerts are self-contained: {alerts}")
        expect("設計: row=未着手 cards=done:reviewer" in text, text)
        expect(H.snapshot(now + 5, cards, w.hubs, lambda h: rows) == (alerts, lines), "output must be stable")
        expect(not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\b\d{9,}\b", text), f"timestamps in output: {text}")
        rows[0]["properties"]["ステータス"] = _status("レビュー中")
        a2, _ = H.snapshot(now, cards, w.hubs, lambda h: rows)
        expect(a2 == [f"ALERT {e8} 要件定義: row レビュー中 (a person must decide; later phases wait)"], f"レビュー中 alert: {a2}")


@check
def health_order_alert_when_a_later_phase_is_live_before_the_earlier_one_is_complete():
    import epic_pipeline_health as H
    w, hub, eid = base_world()
    rows = w.rows[hub["id"]]
    rows[0]["properties"]["ステータス"] = _status("進行中")   # 要件定義
    rows[1]["properties"]["ステータス"] = _status("進行中")   # 設計
    mk = lambda i, ph, st: dict(id=i, status=st, assignee="executor", tenant=C.tenant_of(eid), key=C.phase_key(eid, ph), title="x")
    cards = [mk("t_1", "要件定義", "running"), mk("t_2", "設計", "ready")]
    with sandbox() as (ctl, tmp):
        C.write_status(True)
        alerts, _ = H.snapshot(int(time.time()), cards, w.hubs, lambda h: rows)
        expect(f"ALERT order: 設計 live while 要件定義 not 完了 (epic {eid[-8:]})" in alerts, f"{alerts}")
        rows[0]["properties"]["ステータス"] = _status("完了")
        cards[0]["status"] = "done"
        alerts, _ = H.snapshot(int(time.time()), cards, w.hubs, lambda h: rows)
        expect(not any("order" in a for a in alerts), f"no order alert once the earlier phase is 完了: {alerts}")
        rows[0]["properties"]["ステータス"] = _status("レビュー中")  # レビュー中 is not 完了
        alerts, _ = H.snapshot(int(time.time()), cards, w.hubs, lambda h: rows)
        expect(any(a.startswith("ALERT order: 設計 live while 要件定義 not 完了") for a in alerts), f"レビュー中 is not 完了: {alerts}")
        rows[0]["properties"]["ステータス"] = _status("キャンセル")
        alerts, _ = H.snapshot(int(time.time()), cards, w.hubs, lambda h: rows)
        expect(not any("order" in a for a in alerts), f"キャンセル counts as finished: {alerts}")


@check
def health_flags_a_ready_card_that_nobody_claims():
    import epic_pipeline_health as H
    w, hub, eid = base_world()
    rows = w.rows[hub["id"]]
    now = 1_800_000_000
    def card(status, idle):
        return dict(id="t_9", status=status, assignee="executor", tenant=C.tenant_of(eid), key=C.phase_key(eid, "要件定義"), title="x", idle_since=now - idle)
    with sandbox() as (ctl, tmp):
        C.write_status(True)
        st = C.read_status(); st["last_success"] = now - 30
        C.atomic_write_json(C.state_path(C.STATUS_FILE), st)
        e8 = eid[-8:]
        want = f"ALERT {e8} card t_9 ready but not started (stalled)"
        expect(want in H.snapshot(now, [card("ready", 1000)], w.hubs, lambda h: rows)[0], "a ready card idle for 1000s must alert")
        expect(want not in H.snapshot(now, [card("ready", 100)], w.hubs, lambda h: rows)[0], "a freshly ready card must not alert")
        expect(want not in H.snapshot(now, [card("running", 5000)], w.hubs, lambda h: rows)[0], "a running card is not a stall alert")


@check
def health_intake_alert_for_tasks_left_未着手_longer_than_10_minutes():
    import epic_pipeline_health as H
    now = 1_800_000_000
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - s))
    mk = lambda i, title, rel, age: {"id": f"{i:08x}-0000-4000-8000-{i:012x}", "last_edited_time": iso(age),
                                      "properties": {"タスク名": _title(title), "エピック": {"relation": rel}}}
    pages = [mk(1, "古い通常タスク", [], 700), mk(2, "新しい通常タスク", [], 100), mk(3, "実装", [{"id": "h"}], 9999), mk(4, "古いエピックタスク", [{"id": "h"}], 7200)]
    out = H.intake_alerts(pages, now)
    expect(out == ["ALERT intake: task 00000001 未着手 for more than 10 minutes [stage 1]: 古い通常タスク",
                   "ALERT intake: task 00000004 未着手 for more than 10 minutes [stage 3]: 古いエピックタスク"], f"{out}")
    expect(H.intake_alerts(pages, now + 5) == out, "stable text (no ages)")
    expect(H.intake_alerts([mk(5, "ちょうど10分", [], 600)], now) == [], "exactly 10 minutes is not yet 'more than'")


def _row(i, title, status, age, now, rel=()):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - age))
    return {"id": f"{i:08x}-0000-4000-8000-{i:012x}", "last_edited_time": iso,
            "properties": {"タスク名": _title(title), "ステータス": _status(status), "エピック": {"relation": list(rel)}}}


@check
def health_orphan_alert_for_進行中_rows_idle_over_15_minutes_without_a_live_card():
    """Regression: a task row stayed 進行中 after its worker session ended, no card existed, and nothing noticed."""
    import epic_pipeline_health as H
    now = 1_800_000_000
    pages = [_row(1, "止まった作業", "進行中", 1000, now), _row(2, "更新直後", "進行中", 100, now),
             _row(3, "15分未満", "進行中", 800, now), _row(4, "未着手は対象外", "未着手", 9999, now),
             _row(5, "完了は対象外", "完了", 9999, now), _row(6, "実装", "進行中", 9999, now, [{"id": "h"}]),
             _row(7, "カードが生きている", "進行中", 9999, now), _row(8, "カードは完了済み", "進行中", 9999, now)]
    cards = [dict(status="running", text="[x]\nhttps://app.notion.com/p/" + pages[6]["id"].replace("-", "") + "?pvs=204"),
             dict(status="done", text=pages[7]["id"].replace("-", ""))]
    out = H.orphan_alerts(pages, cards, now)
    expect(out == ["ALERT orphan: task 00000001 進行中 but no live card and no update for more than 15 minutes [stage 1]: 止まった作業",
                   "ALERT orphan: task 00000008 進行中 but no live card and no update for more than 15 minutes [stage 3]: カードは完了済み"], f"{out}")
    expect(H.orphan_alerts(pages, cards, now + 5) == out, "stable text (no ages)")
    expect(H.orphan_alerts([pages[0]], [dict(status="review", text=pages[0]["id"])], now) == [], "a card in review counts as live; a dashed id matches")
    expect(H.orphan_alerts([_row(9, "ちょうど15分", "進行中", 900, now)], [], now) == [], "exactly 15 minutes is not yet 'more than'")
    # a card-less direct Slack row is 'working' while a session message mentions its page id
    expect(H.orphan_alerts([pages[0]], [], now, active_ids={pages[0]["id"].replace("-", "")}) == [], "recent session activity: not an orphan")
    expect(H.orphan_alerts([pages[0]], [], now, skip_ids={pages[0]["id"].replace("-", "")}) == [], "covered by a more specific alert")


@check
def health_alert_text_is_staged_so_an_unfixed_alert_changes_and_wakes_the_monitor_again():
    """Regression: the monitor cron wakes its agent only when stdout changes, so the same unfixed ALERT never woke it twice."""
    import epic_pipeline_health as H
    base = 900
    stages = [H.stage_of(base + d, base) for d in (1, 1799, 1800, 5399, 5400, 12599, 12600, 12600 + 14399, 12600 + 14400, 12600 + 28800)]
    expect(stages == [1, 1, 2, 2, 3, 3, 4, 4, 5, 6], f"{stages}")
    now = 1_800_000_000
    texts = {H.orphan_alerts([_row(1, "t", "進行中", age, now)], [], now)[0] for age in (901, 1500, 2699)}
    expect(len(texts) == 1, f"text is stable inside a stage: {texts}")
    expect(H.orphan_alerts([_row(1, "t", "進行中", 2701, now)], [], now) != H.orphan_alerts([_row(1, "t", "進行中", 901, now)], [], now),
           "the next stage changes the text")


def _block(kind, text="", checked=False, cells=None):
    if kind == "table_row":
        return {"type": kind, kind: {"cells": [[{"plain_text": c}] for c in cells]}}
    body = {"rich_text": [{"plain_text": text}]}
    if kind == "to_do":
        body["checked"] = checked
    return {"type": kind, kind: body}


def _page_blocks(ticks, report=("未実施",), memo_todos=0):
    """Blocks of a task page: the checklist (ticks = list of bool), a 作業メモ chapter and a 完了レポート chapter (the task chapters: 概要 / やることチェックリスト / 作業メモ / 完了レポート)."""
    blks = [_block("heading_1", "概要"), _block("paragraph", "text"), _block("heading_1", "やることチェックリスト")]
    blks += [_block("to_do", f"item{i}", t) for i, t in enumerate(ticks)]
    blks += [_block("heading_1", "作業メモ")] + [_block("to_do", "memo item", False) for _ in range(memo_todos)]
    blks += [_block("heading_1", "完了レポート"), _block("heading_2", "実施内容")]
    blks += [_block("paragraph", r) for r in report]
    blks += [_block("callout", "template guidance text")]
    return blks


@check
def health_checklist_state_counts_only_the_checklist_chapter_and_a_real_完了レポート():
    import epic_pipeline_health as H
    expect(H.checklist_state(_page_blocks([True, True, False], memo_todos=2)) == (3, 2, False), "作業メモ chapter to_dos do not count; placeholder report")
    expect(H.checklist_state(_page_blocks([True, True], report=("未実施", "なし"))) == (2, 2, False), "placeholders are not a report")
    expect(H.checklist_state(_page_blocks([True], report=("Opus に点検させました",))) == (1, 1, True), "text is a report")
    blks = _page_blocks([True]) + [_block("table_row", cells=["結果", "OK"])]
    expect(H.checklist_state(blks) == (1, 1, True), "a table row in the report chapter counts")
    expect(H.checklist_state([_block("to_do", "x", True), _block("to_do", "y", False)]) == (2, 1, False), "no checklist heading: every to_do counts")
    expect(H.checklist_state([]) == (0, 0, False), "empty page")


@check
def health_closeable_and_nearly_done_alerts_for_進行中_rows_whose_work_is_over():
    """Regression: rows with every checklist item ticked and a filled 完了レポート stayed 進行中 (the closing step was skipped).
    Spec: closeable after 5 idle minutes (all ticked + filled report), nearly-done after 10 (exactly one unticked + filled report);
    rows with several unticked items, a placeholder report, no checklist, a live card, a recent session or a phase row never alert."""
    import epic_pipeline_health as H
    now = 1_800_000_000
    pid = lambda p: p["id"].replace("-", "")
    rows = [_row(1, "全部チェック済み", "進行中", 400, now), _row(2, "未チェック複数でレポート記入済み", "進行中", 9999, now),
            _row(3, "あと1つ", "進行中", 700, now), _row(4, "まだ途中", "進行中", 9999, now),
            _row(5, "更新直後", "進行中", 200, now), _row(6, "カードあり", "進行中", 9999, now),
            _row(7, "作業中のセッションあり", "進行中", 9999, now), _row(8, "実装", "進行中", 9999, now, [{"id": "h"}]),
            _row(9, "全部チェック済みだがレポート未記入", "進行中", 9999, now), _row(10, "あと1つだが7分", "進行中", 420, now),
            _row(11, "あと1つだがレポート未記入", "進行中", 9999, now), _row(12, "チェックリストなし", "進行中", 9999, now)]
    states = {pid(rows[0]): (3, 3, True), pid(rows[1]): (4, 1, True), pid(rows[2]): (3, 2, True), pid(rows[3]): (3, 1, False),
              pid(rows[4]): (2, 2, True), pid(rows[5]): (2, 2, True), pid(rows[6]): (2, 2, True), pid(rows[7]): (2, 2, True),
              pid(rows[8]): (2, 2, False), pid(rows[9]): (2, 1, True), pid(rows[10]): (3, 2, False), pid(rows[11]): (0, 0, True)}
    cards = [dict(status="running", text=pid(rows[5]))]
    out, covered = H.close_alerts(rows, cards, now, states, active_ids={pid(rows[6])})
    expect(out == ["ALERT closeable: task 00000001 進行中 but every checklist item is ticked and the 完了レポート is filled [stage 1]: 全部チェック済み",
                   "ALERT nearly-done: task 00000003 進行中 but the 完了レポート is filled and only one checklist item is left [stage 1]: あと1つ"], f"{out}")
    expect(covered == {pid(rows[0]), pid(rows[2])}, f"unticked-many / placeholder-report / no-checklist / carded / active / phase rows never alert: {covered}")
    expect(H.close_alerts(rows, cards, now + 5, states, {pid(rows[6])})[0] == out, "stable text (no ages)")
    # the generic orphan alert is replaced by the specific one for the same row, and still covers the rest
    orph = H.orphan_alerts(rows, cards, now, {pid(rows[6])}, covered)
    expect(pid(rows[0])[-8:] not in "".join(orph) and pid(rows[2])[-8:] not in "".join(orph), f"{orph}")
    one = lambda age, st: H.close_alerts([_row(9, "x", "進行中", age, now)], [], now, {pid(_row(9, "", "進行中", 0, now)): st})[0]
    expect(one(300, (1, 1, True)) == [] and len(one(301, (1, 1, True))) == 1, "closeable: exactly 5 minutes is not yet 'more than'")
    expect(one(600, (2, 1, True)) == [] and len(one(601, (2, 1, True))) == 1, "nearly-done: exactly 10 minutes is not yet 'more than'")
    expect(one(9999, (0, 0, True)) == [], "an empty checklist is not closeable")
    expect(one(9999, (1, 0, True)) != [] and one(9999, (2, 0, True)) == [], "one of one unticked with a report is nearly-done; two unticked never")


@check
def health_row_states_reads_blocks_only_for_candidates_and_caches_per_edit_time():
    import epic_pipeline_health as H
    now = 1_800_000_000
    done, fresh, carded = _row(1, "終わった", "進行中", 700, now), _row(2, "更新直後", "進行中", 100, now), _row(3, "カードあり", "進行中", 700, now)
    reads = []
    saved = C.iter_blocks
    C.iter_blocks = lambda pid, *a, **k: (reads.append(pid), _page_blocks([True, True], report=("完了しました",)))[1]
    try:
        with sandbox() as (ctl, tmp):
            live = [carded["id"].replace("-", "")]
            st = H.row_states([done, fresh, carded], now, live, set())
            expect(st == {done["id"].replace("-", ""): (2, 2, True)} and reads == [done["id"]], f"{st} {reads}")
            H.row_states([done], now + 60, live, set())
            expect(reads == [done["id"]], "same edit time: served from the cache")
            done["last_edited_time"] = "2026-01-01T00:00:00.000Z"
            H.row_states([done], now + 120, live, set())
            expect(reads == [done["id"]] * 2, "a new edit time re-reads the blocks")
            expect(set(C.read_json(C.state_path(C.CLOSE_CACHE_FILE))) == {done["id"].replace("-", "")}, "cache keeps only live candidates")
    finally:
        C.iter_blocks = saved


@check
def health_review_unreflected_alert_for_レビュー中_rows_without_a_review_db_row():
    """Regression: a row was set レビュー中 without running ask_human.py, so nobody was ever asked."""
    import epic_pipeline_health as H
    now = 1_800_000_000
    pid = lambda p: p["id"].replace("-", "")
    rows = [_row(1, "依頼なし", "レビュー中", 700, now), _row(2, "関連で依頼済み", "レビュー中", 700, now), _row(3, "URLで依頼済み", "レビュー中", 700, now),
            _row(4, "更新直後", "レビュー中", 200, now), _row(5, "レビューカードあり", "レビュー中", 700, now),
            _row(6, "実装", "レビュー中", 9999, now, [{"id": "h"}]), _row(7, "進行中は対象外", "進行中", 9999, now)]
    review_rows = [{"properties": {"対象タスク": {"relation": [{"id": rows[1]["id"]}]}}},  # old layout: still counts
                   {"properties": {"レビュー先": {"url": "https://app.notion.com/p/" + pid(rows[2]) + "?pvs=4"}, "対象タスク": {"relation": []}}}]
    cards = [dict(status="review", text=pid(rows[4]))]
    out = H.review_unreflected_alerts(rows, cards, review_rows, now)
    expect(out == ["ALERT review-unreflected: task 00000001 レビュー中 but no row in the review DB asks for it [stage 1]: 依頼なし"], f"{out}")
    expect(H.review_unreflected_alerts(rows, cards, review_rows, now + 5) == out, "stable text (no ages)")
    # new layout: a review row of the task's epic, or a key starting with the task id8, asks about it; no old property needed
    epic_id = "e" * 8 + "-0000-4000-8000-" + "e" * 12
    new_rows = [_row(1, "依頼なし", "レビュー中", 700, now), _row(2, "エピックで依頼済み", "レビュー中", 700, now, [{"id": epic_id}]),
                _row(3, "キーで依頼済み", "レビュー中", 700, now), _row(4, "別のエピック", "レビュー中", 700, now, [{"id": "f" * 8 + "-0000-4000-8000-" + "f" * 12}])]
    new_review = [{"properties": {"対象エピック": {"relation": [{"id": epic_id}]}, "キー": {"rich_text": [{"plain_text": "eeeeeeee-承認"}]}}},
                  {"properties": {"対象エピック": {"relation": []}, "キー": {"rich_text": [{"plain_text": pid(new_rows[2])[-8:] + "-review"}]}}}]
    got = H.review_unreflected_alerts(new_rows, [], new_review, now)
    expect([re.search(r"task (\w+)", g).group(1) for g in got] == ["00000001", "00000004"] and len(H.review_unreflected_alerts(new_rows, [], [], now)) == 4, f"{got}")
    expect(H.review_unreflected_alerts([_row(1, "ちょうど5分", "レビュー中", 300, now)], [], [], now) == [], "exactly 5 minutes is not yet 'more than'")
    expect(len(H.review_unreflected_alerts([_row(1, "5分超", "レビュー中", 301, now)], [], [], now)) == 1, "just over 5 minutes alerts")


@check
def health_context_alert_when_an_executor_run_ends_without_keeping_CONTEXT_md():
    """Regression: a card sent back by the reviewer started a fresh session that re-read what the previous run had read (no
    context survives between runs). The card text now says to keep CONTEXT.md in the workspace; the monitor catches a run that
    ended without writing it, before the next run starts."""
    import epic_pipeline_health as H
    now = 1_800_000_000
    ws = "/ws"
    card = lambda cid, **kw: dict(dict(id=cid, status="review", kind="scratch", workspace=f"{ws}/{cid}", run=7,
                                       run_started=now - 3000, run_ended=now - 600), **kw)
    files = {f"{ws}/t_fresh/CONTEXT.md": now - 700, f"{ws}/t_stale/CONTEXT.md": now - 4000,
             f"{ws}/t_exact/CONTEXT.md": now - 3000}
    mtime = files.get
    cards = [card("t_missing"), card("t_stale"), card("t_fresh"), card("t_exact"),
             card("t_short", run_started=now - 800),           # a 200 s run (crash at start-up): not expected to have written it
             card("t_grace", run_ended=now - 100),             # just ended: inside the 120 s grace
             card("t_norun", run=None, run_started=None, run_ended=None),  # first run still going
             card("t_done", status="done"), card("t_dir", kind="dir"), card("t_nows", workspace="")]
    out = H.context_alerts(cards, now, mtime)
    expect(out == ["ALERT context: card t_missing CONTEXT.md missing after executor run 7 [stage 1]",
                   "ALERT context: card t_stale CONTEXT.md not updated during executor run 7 [stage 1]"], f"{out}")
    expect(H.context_alerts(cards, now + 10, mtime) == out, "stable text (no ages)")
    later = H.context_alerts(cards[:2], now - 600 + H.CONTEXT_GRACE_SECONDS + 1800, mtime)
    expect(len(later) == 2 and all(l.endswith("[stage 2]") for l in later), f"the stage grows so the monitor wakes again: {later}")
    expect(H.context_alerts([card("t_edge", run_started=now - 600 - H.CONTEXT_MIN_RUN_SECONDS)], now, mtime) != [],
           "a run of exactly the minimum length counts")
    expect(H.context_alerts([card("t_g", run_ended=now - H.CONTEXT_GRACE_SECONDS)], now, mtime) == [], "exactly the grace is not yet over")
    real = H.context_alerts([card("t_real", workspace="/nonexistent-dir-for-selftest")], now)
    expect(len(real) == 1 and "missing" in real[0], f"default mtime: a missing file is missing, no error: {real}")
    # the template asks for it (the marker the reader filters on), and the golden bodies carry the same paragraph
    body = C.load_template("phase_card")
    expect("CONTEXT FILE" in body and C.CONTEXT_FILE in body and "$" not in body.split("CONTEXT FILE", 1)[1].split("Follow skills", 1)[0],
           "phase_card.md carries the CONTEXT FILE paragraph (without ${...} placeholders)")
    for h in ("決めたこと", "捨てた案", "触ったファイル", "次にやること", "検証コマンドと結果"):
        expect(h in body, f"CONTEXT.md heading {h} missing from the card text")


@check
def kanban_context_cards_reads_live_cards_that_ask_for_CONTEXT_md_with_their_last_executor_run():
    with sandbox() as (ctl, tmp):
        db = tmp / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT NOT NULL, workspace_kind TEXT, workspace_path TEXT)")
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, started_at INTEGER, ended_at INTEGER)")
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", [
            ("t_a", "a", "keep CONTEXT.md", "review", "scratch", "/w/t_a"),
            ("t_b", "b", "old card text", "review", "scratch", "/w/t_b"),     # no marker: never alerted
            ("t_c", "c", "keep CONTEXT.md", "done", "scratch", "/w/t_c"),     # finished
            ("t_d", "d", "keep CONTEXT.md", "running", "scratch", "/w/t_d")])
        con.executemany("INSERT INTO task_runs VALUES (?,?,?,?,?,?)", [
            (1, "t_a", "executor", "review", 100, 900), (2, "t_a", "reviewer", "ready", 950, 1200),  # reviewer runs never count
            (3, "t_a", "executor", "running", 1300, None),                                           # still running: not finished
            (4, "t_d", "executor", "running", 50, None)])
        con.commit()
        con.close()
        sha = subprocess.run(["shasum", str(db)], capture_output=True, text=True).stdout
        got = C.kanban_context_cards(db)
        expect(got == [dict(id="t_a", status="review", kind="scratch", workspace="/w/t_a", run=1, run_started=100, run_ended=900),
                       dict(id="t_d", status="running", kind="scratch", workspace="/w/t_d", run=None, run_started=None, run_ended=None)], f"{got}")
        expect(subprocess.run(["shasum", str(db)], capture_output=True, text=True).stdout == sha, "db modified")
        raises(C.PipelineError, C.kanban_context_cards, tmp / "missing.db")


@check
def common_recent_message_ids_reads_session_dbs_read_only_inside_the_window():
    now = 1_800_000_000
    with sandbox() as (ctl, tmp):
        def mk(path, rows):
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT, tool_calls TEXT, timestamp REAL)")
            con.executemany("INSERT INTO messages (content, tool_calls, timestamp) VALUES (?,?,?)", rows)
            con.commit()
            con.close()
        a, b, c, d = _epic_id(1), _epic_id(2), _epic_id(3), _epic_id(4)
        dashed = lambda h: f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        root = tmp / "hermes"
        mk(root / "state.db", [(f"working on https://app.notion.com/p/{a}?pvs=204", None, now - 60), ("old", None, now - 5000),
                               (f"old {c}", None, now - 5000)])
        mk(root / "profiles" / "engineer" / "state.db", [(None, '{"page_id": "%s"}' % dashed(b), now - 100)])
        (root / "profiles" / "broken").mkdir(parents=True)
        (root / "profiles" / "broken" / "state.db").write_text("not sqlite")
        sha = subprocess.run(["shasum", str(root / "state.db")], capture_output=True, text=True).stdout
        got = C.recent_message_ids(now, 900, root)
        expect(got == {a, b}, f"{got}")
        expect(d not in got and c not in got, "ids outside the window are ignored")
        expect(subprocess.run(["shasum", str(root / "state.db")], capture_output=True, text=True).stdout == sha, "db modified")
        expect(C.recent_message_ids(now, 900, tmp / "missing") == set(), "no dbs: empty, no error")


@check
def kanban_card_texts_reads_every_board_read_only_and_skips_archived_and_broken_dbs():
    with sandbox() as (ctl, tmp):
        def mk(path, rows):
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT NOT NULL)")
            con.executemany("INSERT INTO tasks VALUES (?,?,?,?)", rows)
            con.commit()
            con.close()
        root = tmp / "kb"
        mk(root / "kanban.db", [("a", "default card", "notion abc", "running"), ("b", "gone", "x", "archived")])
        mk(root / "boards" / "one" / "kanban.db", [("c", "t", None, "ready")])
        mk(root / "boards" / "_archived" / "kanban.db", [("d", "old board", "x", "running")])
        (root / "boards" / "broken").mkdir(parents=True)
        (root / "boards" / "broken" / "kanban.db").write_text("not sqlite")
        sha = subprocess.run(["shasum", str(root / "kanban.db")], capture_output=True, text=True).stdout
        got = C.kanban_card_texts(root)
        expect(sorted((c["status"], c["text"]) for c in got) == [("ready", "t\n"), ("running", "default card\nnotion abc")], f"{got}")
        expect(subprocess.run(["shasum", str(root / "kanban.db")], capture_output=True, text=True).stdout == sha, "db modified")
        expect(C.kanban_card_texts(tmp / "missing") == [], "no boards: empty, no error")


@check
def health_main_prints_only_alerts_or_ok_and_writes_the_snapshot_file():
    import epic_pipeline_health as H
    w, hub, eid = base_world()
    w.rows[hub["id"]][0]["properties"]["ステータス"] = _status("完了")
    w.rows[hub["id"]][1]["properties"]["ステータス"] = _status("完了")
    with sandbox(w) as (ctl, tmp):
        C.write_status(True)
        C.ntn_saved = C.ntn
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = H.main([])
        expect(rc == 0 and out.getvalue() == "ok\n", f"clean run prints exactly ok: {out.getvalue()!r}")
        snap = C.state_path(C.SNAPSHOT_FILE).read_text(encoding="utf-8")
        expect(snap.startswith("EPIC ") and "設計: row=完了" in snap and snap.rstrip().endswith("ok"), snap)
        w.rows[hub["id"]][1]["properties"]["ステータス"] = _status("進行中")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = H.main([])
        e8 = eid[-8:]
        expect(rc == 0 and out.getvalue() == f"ALERT {e8} 設計: row 進行中 but no live card (stuck)\n", f"alert only, no EPIC lines: {out.getvalue()!r}")
        expect("EPIC " in C.state_path(C.SNAPSHOT_FILE).read_text(encoding="utf-8"), "snapshot keeps the full picture")


@check
def health_heartbeat_grace_right_after_a_deploy():
    import epic_pipeline_health as H
    with sandbox() as (ctl, tmp):
        now = 1_800_000_000
        C.atomic_write_json(C.state_path(C.DEPLOY_FILE), now - 100)
        expect(H.controller_alerts(now) == [], "deployed 100 s ago and the controller has not run yet: no alert")
        C.atomic_write_json(C.state_path(C.DEPLOY_FILE), now - 700)
        expect(len(H.controller_alerts(now)) == 1, "grace is over")


@check
def health_alerts_on_stale_or_missing_heartbeat_and_stuck_journal():
    import epic_pipeline_health as H
    with sandbox() as (ctl, tmp):
        now = 1_800_000_000
        a = H.controller_alerts(now)
        expect(len(a) == 1 and "heartbeat missing" in a[0], a)
        C.atomic_write_json(C.state_path(C.STATUS_FILE), {"last_success": now - 601, "last_run": now - 5, "ok": False, "error": "ntn timeout"})
        a1, a2 = H.controller_alerts(now), H.controller_alerts(now + 77)
        expect(len(a1) == 1 and "heartbeat stale" in a1[0] and "ntn timeout" in a1[0] and a1 == a2, f"stale alert must be stable: {a1} {a2}")
        C.atomic_write_json(C.state_path(C.STATUS_FILE), {"last_success": now - 599, "last_run": now, "ok": True, "error": None})
        expect(H.controller_alerts(now) == [], "fresh heartbeat: no alert")
        J = {"E1:実装": dict(row="pending", started=now - 700, updated=now - 100, epic="e" * 24 + "12345678", phase="実装", groups={"A": [], "B": []}, cards={"A": "t_1"}),
             "E2:実装": dict(row="pending", started=now - 100, updated=now - 100, epic="e" * 24 + "22222222", phase="実装", groups={"A": []}, cards={}),
             "E3:テスト": dict(row="done", started=now - 900, updated=now - 800, epic="e" * 24 + "33333333", phase="テスト", groups={"A": []}, cards={"A": "t"}),
             "E4:テスト": dict(row="done", started=now - 900, updated=now - 800, epic="e" * 24 + "44444444", phase="テスト", groups={"A": [], "B": []}, cards={"A": "t"})}
        C.save_journal(J)
        a = "\n".join(H.controller_alerts(now))
        expect("12345678 [実装] partial creation pending (cards 1/2)" in a, a)
        expect("22222222" not in a, "fresh pending entry is not an alert yet")
        expect("33333333" not in a and "44444444 [テスト] row started but only 1/2 cards" in a, a)


@check
def health_main_exits_nonzero_on_its_own_errors():
    import epic_pipeline_health as H
    with sandbox() as (ctl, tmp):
        saved = C.KANBAN_DB
        C.KANBAN_DB = tmp / "nope.db"
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = H.main([])
        finally:
            C.KANBAN_DB = saved
        expect(rc == 1 and out.getvalue().startswith("ERROR PipelineError"), f"rc={rc} {out.getvalue()!r}")


# ---------------------------------------------------------------- checks: wrapper, planner, intake

@check
def apply_wrapper_exits_nonzero_on_controller_failure_and_timeout():
    import epic_gate_apply as A
    with sandbox() as (ctl, tmp):
        def runner(rc, out="", err=""):
            return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, out, err)

        def go(fn):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                return A.main(fn), buf.getvalue()
        rc, out = go(runner(0, "EPIC x\nCREATED [実装] x [A] -> executor (1 items)\nWARN soft\nSKIP y\n"))
        expect(rc == 0 and out == "", f"clean success is silent: {out!r}")
        rc, out = go(runner(1, "EPIC x\nFAIL [実装] x: boom\n", "Traceback...\nValueError: bad"))
        expect(rc == 1 and out.startswith("ゲート制御（michiomochi）\nFAIL [実装] x: boom\nERROR controller exit 1: ValueError: bad"), out)
        rc, out = go(runner(0, "ERROR epic 12345678: X\n"))
        expect(rc == 1 and "ERROR epic 12345678" in out, "ERROR line alone fails the run")
        rc, out = go(runner(2, "", ""))
        expect(rc == 1 and "controller exit 2" in out, out)

        def timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 240)
        rc, out = go(timeout)
        expect(rc == 1 and "timed out" in out and C.read_status()["ok"] is False, f"timeout: rc={rc} {out!r}")

        def cannot(cmd, **kw):
            raise FileNotFoundError("python")
        rc, out = go(cannot)
        expect(rc == 1 and "cannot run controller" in out, out)
        # a held release ask (UI change without before/after images) is posted once, but is not a failure
        rc, out = go(runner(0, "EPIC x\nHELD 12345678-承認: UI 変更の判定：あり（差分：pages/a.tsx）なのに、変更後の画像がありません\nHOLD 9-承認 (unchanged): r\n"))
        expect(rc == 0 and "HELD 12345678-承認" in out and "HOLD 9-承認" not in out, f"HELD is posted, HOLD is not, exit 0: {rc} {out!r}")


def _planner_world():
    w = World()
    hub, eid = w.add_epic(1, rows=[("要件定義", "完了"), ("実装", "未着手")])
    hub["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}})
    hub["last_edited_time"] = "2026-11-06T17:07:00.000Z"
    return w, hub, eid


def _todo(text, checked=False):
    return {"id": "b" + str(abs(hash(text)) % 10**6), "type": "to_do", "to_do": {"checked": checked, "rich_text": [{"plain_text": text}]}}


def _heading(text, level=1):
    return {"id": "h" + text, "type": f"heading_{level}", f"heading_{level}": {"rich_text": [{"plain_text": text}]}}


def _planner_out(w):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runpy.run_path(str(HERE / "epic_planner_status.py"), run_name="__main__")
    return buf.getvalue().strip()


@check
def planner_status_format_and_nonzero_exit_on_error():
    w, hub, eid = _planner_world()
    with sandbox(w) as (ctl, tmp):
        out = _planner_out(w)
        expect(re.fullmatch(r"EPIC [0-9a-f]{8} ハブ1 status=要件定義 kind=プロダクト開発 tasks=0 settled=no "
                            r"allow=all reasons=- tasks_hash=[0-9a-f]{8} watch_hash=[0-9a-f]{8} gates_due=-", out), out)
        expect("missing_phase_rows" not in out, "the planner makes no row: the line no longer says which rows are missing")
        expect("hub_edited" not in out and "T17:07" not in out, "the hub edit timestamp is gone")

        def boom(*a, **k):
            raise C.PipelineError("notion down")
        C.ntn = boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            e = raises(SystemExit, runpy.run_path, str(HERE / "epic_planner_status.py"), run_name="__main__")
        expect(e.code == 1 and buf.getvalue().startswith("ERROR PipelineError: notion down"), f"{e.code} {buf.getvalue()!r}")


@check
def planner_status_gates_due_come_from_the_rows():
    w, hub, eid = _planner_world()
    hub2, eid2 = w.add_epic(2, rows=[("要件定義", "完了"), ("設計", "完了"), ("リリース判定", "完了")])
    hub3, eid3 = w.add_epic(3, rows=[("要件定義", "レビュー中"), ("設計", "進行中")])
    hub4, eid4 = w.add_epic(4, rows=[("設計", "完了")])  # only the 設計 row is 完了: no gate
    hub5, eid5 = w.add_epic(5, rows=[("リリース判定", "完了")])
    for h in (hub2, hub3, hub4, hub5):
        h["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}})
    with sandbox(w) as (ctl, tmp):
        lines = {re.search(r"EPIC ([0-9a-f]{8})", l).group(1): l for l in _planner_out(w).splitlines()}
        g = lambda hub: re.search(r"gates_due=(\S+)", lines[hub["id"].replace("-", "")[-8:]]).group(1)
        expect(g(hub) == "-", g(hub))  # 要件定義 完了 is not a gate
        expect(g(hub2) == "リリース承認", g(hub2))
        expect(g(hub3) == "-", "レビュー中 / 進行中 rows are not due")
        expect(g(hub4) == "-", g(hub4))
        expect(g(hub5) == "リリース承認", g(hub5))
        expect("company" not in "".join(lines.values()), "no company fields in the planner status line")


@check
def planner_status_watch_hash_wakes_only_on_real_changes():
    w, hub, eid = _planner_world()
    blocks = [_heading("候補"), _todo("候補1 未チェック"), _todo("候補2 済", True), _heading("管理ログ"), _todo("管理ログのto_doは対象外")]
    w.hub_blocks[hub["id"]] = blocks
    get = lambda: re.search(r"watch_hash=([0-9a-f]{8})", _planner_out(w)).group(1)
    with sandbox(w) as (ctl, tmp):
        h0 = get()
        hub["last_edited_time"] = "2026-11-07T09:09:09.000Z"
        expect(get() == h0, "an edit timestamp alone must not change the hash")
        blocks[2]["to_do"]["checked"] = False       # an already checked 候補 line turned unchecked? it is a real change
        h1 = get()
        expect(h1 != h0, "unchecked 候補 set changed")
        blocks[2]["to_do"]["checked"] = True
        expect(get() == h0, "back to the same set")
        blocks[4]["to_do"]["checked"] = True        # to_do outside 候補
        blocks.append(_todo("管理ログに追記"))
        expect(get() == h0, "other sections do not count")
        w.row(hub, "実装")["properties"]["ステータス"] = _status("進行中")
        expect(get() != h0, "a phase row status change must wake the planner")


@check
def pending_tasks_paginates_filters_phase_rows_and_fails_loudly():
    import notion_pending_tasks as P
    with sandbox() as (ctl, tmp):
        mk = lambda i, title, rel, st="未着手": {"id": f"id{i}", "url": f"https://n/{i}", "properties": {"タスク名": _title(title), "エピック": {"relation": rel}, "ステータス": {"status": {"name": st}}}}
        hub = [{"id": "edfee791-668c-e99f-bc1f-7ca6f354edab"}]
        pages = [{"results": [mk(2, "通常タスク", []), mk(1, "実装", hub)], "has_more": True, "next_cursor": "c"},
                 {"results": [mk(3, "別の通常タスク", hub), mk(0, "リリース判定", hub)], "has_more": False}]
        bodies, gets = [], []

        def fake(*args, data=None):
            if args[0].startswith("v1/pages/"):
                gets.append(args[0])
                return {}
            bodies.append(data)
            if data["filter"].get("timestamp") == "created_time":
                return {"results": [], "has_more": False}
            return pages.pop(0)
        C.ntn = fake
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = P.main()
        expect(rc == 0 and buf.getvalue() == "id2 https://n/2 tenant=adhoc\nid3 https://n/3 tenant=epic-f354edab\n", repr(buf.getvalue()))
        expect(gets == [], f"the tenant comes from the hub row id: no page read: {gets}")
        expect(bodies[0]["filter"]["timestamp"] == "created_time" and bodies[1]["page_size"] == 100 and bodies[1]["filter"] == P.FLT
               and bodies[2]["start_cursor"] == "c", f"{bodies}")

        def boom(*a, **k):
            raise C.PipelineError("down")
        C.ntn = boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = P.main()
        expect(rc == 1 and buf.getvalue().startswith("ERROR PipelineError: down"), buf.getvalue())


@check
def pending_tasks_runaway_guard_prints_one_alert_and_lists_nothing():
    import notion_pending_tasks as P
    with sandbox() as (ctl, tmp):
        mk = lambda i, title, rel, st="未着手": {"id": f"id{i}", "url": f"https://n/{i}", "properties": {"タスク名": _title(title), "エピック": {"relation": rel}, "ステータス": {"status": {"name": st}}}}
        recent6 = [mk(i, f"t{i}", []) for i in range(6)]
        recent5_plus_phase_rows = [mk(i, f"t{i}", []) for i in range(5)] + [mk(9, "実装", [{"id": "h"}]), mk(10, "テスト", [{"id": "h"}])]
        seen = []

        def mkfake(recent):
            def fake(*args, data=None):
                seen.append(data["filter"])
                if data["filter"].get("timestamp") == "created_time":
                    return {"results": recent, "has_more": False}
                return {"results": [mk(1, "通常", [])], "has_more": False}
            return fake
        C.ntn = mkfake(recent6)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = P.main(now=1_800_000_000)
        expect(rc == 0 and buf.getvalue() == P.RUNAWAY_ALERT + "\n" and P.RUNAWAY_ALERT.startswith("ALERT runaway"), repr(buf.getvalue()))
        expect(all(f.get("timestamp") == "created_time" for f in seen), "the pending list is not even read")
        f = seen[0]["created_time"]["on_or_after"]
        expect(f == time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(1_800_000_000 - 1800)), f"30 minute window: {f}")
        C.ntn = mkfake([mk(i, f"t{i}", [], "進行中") for i in range(6)])  # 6 rows but already taken: not a runaway
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = P.main(now=1_800_000_000)
        expect(rc == 0 and buf.getvalue() == "id1 https://n/1 tenant=adhoc\n", repr(buf.getvalue()))
        C.ntn = mkfake(recent5_plus_phase_rows)  # exactly 5 non-phase rows (+ phase rows made by the planner): not a runaway
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = P.main(now=1_800_000_000)
        expect(rc == 0 and buf.getvalue() == "id1 https://n/1 tenant=adhoc\n", repr(buf.getvalue()))


@check
def task_events_ignore_priority_with_or_without_the_property():
    """The タスク DB loses 優先度 (a card's priority is left to the kanban default): rows with or without it work and a priority change is silent."""
    import michiomochi_task_events as T
    with tempfile.TemporaryDirectory() as d:
        saved = (T.ntn, T.STATE)
        T.STATE = os.path.join(d, "snap.json")
        rows = {}

        def row(i, title, status, prio=None):
            pr = {"タスク名": {"type": "title", "title": [{"plain_text": title}]}, "ステータス": {"status": {"name": status}}, "エピック": {"relation": []}}
            if prio is not None:  # production keeps the property until a person deletes it
                pr["優先度"] = {"select": {"name": prio}}
            return {"id": f"id{i}", "url": f"https://n/{i}", "properties": pr}

        def fake(path, *a, data=None):
            return {"results": [] if T.EPIC_DS in path else list(rows.values()), "has_more": False}
        T.ntn = fake
        try:
            def run():
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    T.main()
                return buf.getvalue()
            rows[1] = row(1, "あ", "未着手", "P1")
            expect(run() == "", "first run only records the baseline")
            rows[1] = row(1, "あ", "未着手", "P3")
            expect(run() == "", "a priority change on a task is not reported")
            del rows[1]["properties"]["優先度"]
            expect(run() == "", "the property being deleted is not an event")
            rows[2] = row(2, "い", "未着手")
            out = run()
            expect("タスク作成" in out and "（未着手）" in out and "P" not in out.split("（未着手）")[1], out)
            rows[2] = row(2, "い", "進行中")
            expect("未着手 → 進行中" in run(), "status change still reported")
        finally:
            T.ntn, T.STATE = saved


@check
def priority_property_is_gone_from_schema_prompts_and_skills():
    root = next((r for r in _repo_roots() if (r / "notion" / "schema.json").is_file()), None)
    if root is None:
        return
    dbs = json.loads((root / "notion" / "schema.json").read_text(encoding="utf-8"))["databases"]
    expect("優先度" not in dbs["task"]["properties"], "task schema still defines 優先度")
    expect(all("優先度" not in dbs[k]["properties"] for k in dbs), "no DB schema defines 優先度")
    intake = (root / "prompts" / "intake.txt").read_text(encoding="utf-8")
    expect("--priority <N>" not in intake and "30 for P1" not in intake, "intake must leave the card priority to the kanban default")
    planner = (root / "prompts" / "planner.txt").read_text(encoding="utf-8")
    expect("優先度 = the hub's" not in planner and "優先度 (inherit the hub's)" not in planner, "planner still writes 優先度 on task rows")
    expect(not hasattr(C, "PRIO"), "epic_common.PRIO is gone: no epic priority maps to a card priority")
    import inspect
    import epic_gate_controller as ctl
    expect("prio" not in inspect.signature(ctl.card_args).parameters, "card_args takes no priority")


# ---------------------------------------------------------------- checks: second-review flow fixes

@check
def hub_status_follows_the_earliest_unfinished_row_and_never_the_forbidden_values():
    expect(C.hub_target_status({"要件定義": "完了", "設計": "進行中"}) == "設計", "earliest unfinished row")
    expect(C.hub_target_status({"要件定義": "完了", "設計": "レビュー中"}) == "設計", "レビュー中 is unfinished")
    expect(C.hub_target_status({"要件定義": "キャンセル", "設計": "完了", "実装": "完了", "テスト": "未着手"}) == "テスト", "テスト -> テスト")
    expect(C.hub_target_status({ph: "完了" for ph in C.ORDER}) is None, "all finished: leave alone")
    expect(C.hub_target_status({"実装": "進行中"}) == "実装", "phases without a row are ignored")
    expect(set(C.HUB_PHASE_STATUS) == set(C.ORDER) and not (set(C.HUB_PHASE_STATUS.values()) & (C.HUB_APPROVAL_STATUSES | C.HUB_KEEP_STATUSES)),
           "the mapping never contains approval or protected values")
    expect(set(C.HUB_PHASE_STATUS.values()) <= set(C.HUB_STATUSES), "every mapped value is one of the hub's 7 statuses")
    expect(C.HUB_PHASE_STATUS == {"要件定義": "要件定義", "設計": "設計", "実装": "実装", "テスト": "テスト", "リリース判定": "承認待ち"}, "mapping as agreed")
    for protected in ("完了", "中止"):
        w = World()
        hub, eid = w.add_epic(5, rows=[("要件定義", "完了"), ("実装", "未着手")], hub_status=protected)
        with sandbox(w) as (ctl, tmp):
            ctl.run(True)
            expect(C.status_of(hub) == protected, f"{protected} must never be overwritten")
    w = World()
    hub, eid = w.add_epic(6, rows=[("要件定義", "完了"), ("設計", "完了"), ("実装", "未着手")], hub_status="要件定義")
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(False)
        expect(any(l.startswith("WOULD SET hub ステータス 要件定義 -> 実装") for l in lines) and C.status_of(hub) == "要件定義", f"dry run only announces: {lines}")
        lines, errors = ctl.run(True)
        expect(C.status_of(hub) == "実装" and any(l.startswith("HUBSTATUS ") for l in lines), f"{lines}")
        w.calls.clear()
        ctl.run(True)
        expect(not [c for c in w.calls if c[0] == "patch" and c[1] == hub["id"]], "no write when the hub already shows the right value")
        # refuses to write a forbidden value even if the mapping were corrupted
        saved = dict(C.HUB_PHASE_STATUS)
        C.HUB_PHASE_STATUS["実装"] = "完了"
        try:
            hub["properties"]["ステータス"] = _status("要件定義")
            lines, errors = ctl.run(True)
            expect(errors and any("refusing to write hub status" in l for l in lines) and C.status_of(hub) == "要件定義", f"{lines}")
        finally:
            C.HUB_PHASE_STATUS.clear()
            C.HUB_PHASE_STATUS.update(saved)
    w = World()
    hub, eid = w.add_epic(7, rows=[("要件定義", "未着手")])
    with sandbox(w) as (ctl, tmp):
        w.fail_patch = {hub["id"]}
        hub["properties"]["ステータス"] = _status("実装")
        lines, errors = ctl.run(True)
        expect(errors and any(l.startswith("ERROR epic") for l in lines) and any(c[0] == "create" for c in w.calls),
               f"a failing hub write is loud but does not block the card creation: {lines}")


@check
def there_is_no_効果測定_and_no_post_release_phase_and_the_release_gate_is_asked():
    import epic_gate_controller as ctl2
    expect(len(C.ORDER) == 5 and "効果測定" not in C.HUB_STATUSES, "five phases, seven hub statuses")
    expect(not any(hasattr(m, n) for m in (C, ctl2) for n in ("POST_RELEASE", "HUB_EFFECT_STATUS", "phase_order", "ALL_PHASE_ROWS")), "no post-release code left")
    expect(not C.is_phase_row({"properties": {"タスク名": _title("リリース後作業"), "エピック": {"relation": [{"id": "x"}]}}}), "リリース後作業 is an ordinary task title now")
    rows = [(ph, "完了") for ph in C.ORDER]
    for st in ("承認待ち", "リリース判定"):  # an old/other hub value is never a trigger for more work: no card is ever made
        w = World()
        hub, eid = w.add_epic(8, rows=rows + [("リリース後作業", "未着手")], hub_status=st)
        with sandbox(w) as (ctl, tmp):
            lines, errors = ctl.run(True)
            expect(not errors and w.creates() == [], f"all rows finished: no card ({lines})")
            expect([a[0] for a in w.asks] == [f"{eid[-8:]}-承認"], f"only the release gate is asked once all rows are 完了: {w.asks}")
            expect(C.status_of(hub) == "承認待ち", f"all finished, not approved: 承認待ち ({lines})")


@check
def hub_status_has_three_stages_phase_then_承認待ち_then_完了():
    done = [(ph, "完了") for ph in C.ORDER]
    def case(n, rows, review_status, key="{e8}-承認", hub_status="承認待ち", live_card=False):
        w = World()
        hub, eid = w.add_epic(n, rows=rows, hub_status=hub_status)
        if review_status:
            w.review_rows.append(_review_page(n, review_status, epic=hub["id"], key=key.format(e8=eid[-8:])))
        if live_card:
            w.board.append(dict(id="t_live", status="running", assignee="executor", tenant=C.tenant_of(eid),
                                key=C.phase_key(eid, "リリース判定"), title="[リリース判定] live"))
        with sandbox(w) as (ctl, tmp):
            dry, _ = ctl.run(False)
            lines, errors = ctl.run(True)
        return w, hub, lines, dry, errors
    # all rows 完了 + 承認 -> 完了 (dry run only announces)
    w, hub, lines, dry, errors = case(41, done, "承認")
    expect(not errors and C.status_of(hub) == "完了" and any(l.startswith("HUBSTATUS ハブ41: 承認待ち -> 完了") for l in lines), f"{lines}")
    expect(any(l.startswith("WOULD SET hub ステータス 承認待ち -> 完了") for l in dry), f"dry run announces: {dry}")
    expect(w.creates() == [], "completing creates no card")
    # the hub that shows an older phase value still becomes 完了; キャンセル counts as finished; the old key counts
    w, hub, lines, dry, errors = case(42, [(ph, "キャンセル" if ph == "設計" else "完了") for ph in C.ORDER], "承認", key="{e8}-リリース承認", hub_status="設計")
    expect(not errors and C.status_of(hub) == "完了", f"キャンセル + old key: {lines}")
    # stage 2: all rows finished, release approval not 承認 -> 承認待ち (from any earlier hub value), never 完了; already 承認待ち: no write
    for st in ("レビュー待ち", "差し戻し", "取り下げ", None):
        for start in ("設計", "テスト"):
            w, hub, lines, dry, errors = case(43, done, st, hub_status=start)
            expect(not errors and C.status_of(hub) == "承認待ち" and any(l.startswith(f"HUBSTATUS ハブ43: {start} -> 承認待ち") for l in lines)
                   and any(l.startswith(f"WOULD SET hub ステータス {start} -> 承認待ち") for l in dry) and not any("-> 完了" in l for l in lines + dry), f"review {st} from {start}: {lines}")
        w, hub, lines, dry, errors = case(43, done, st)
        expect(not errors and C.status_of(hub) == "承認待ち" and not any(c[0] == "patch" and c[1] == hub["id"] for c in w.calls), f"review {st}: already shown, no write: {lines}")
    # 承認 of another epic or another key does not count -> still 承認待ち
    w, hub, lines, dry, errors = case(44, done, "承認", key="zzzzzzzz-承認", hub_status="テスト")
    expect(C.status_of(hub) == "承認待ち", f"other key: {lines}")
    # stage 1 -> 2 -> 3 on one hub: last row 完了 moves it to 承認待ち, the approval then moves it to 完了
    w = World()
    hub, eid = w.add_epic(51, rows=[(ph, "完了") for ph in C.ORDER[:-1]] + [("リリース判定", "進行中")], hub_status="テスト")
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        expect(C.status_of(hub) == "承認待ち", "stage 1: the リリース判定 row is still open -> 承認待ち, the last stage")
        w.row(hub, "リリース判定")["properties"]["ステータス"] = _status("完了")
        ctl.run(True)
        expect(C.status_of(hub) == "承認待ち", "stage 2: all finished, waiting for the person")
        w.review_rows.append(_review_page(51, "レビュー待ち", epic=hub["id"], key=f"{eid[-8:]}-承認"))
        ctl.run(True)
        expect(C.status_of(hub) == "承認待ち", "stage 2: review row open")
        w.review_rows[0]["properties"]["ステータス"] = _status("承認")
        ctl.run(True)
        expect(C.status_of(hub) == "完了", "stage 3: approved")
    # 承認 but a phase is not finished -> not 完了 (the usual earliest-unfinished-phase rule keeps working)
    for unfinished in ("未着手", "進行中", "レビュー中"):
        rows = [(ph, unfinished if ph == "テスト" else "完了") for ph in C.ORDER]
        w, hub, lines, dry, errors = case(45, rows, "承認", hub_status="実装")
        expect(not errors and C.status_of(hub) != "完了" and not any("-> 完了" in l for l in lines + dry), f"{unfinished}: {lines}")
    w, hub, lines, dry, errors = case(46, [(ph, "完了") for ph in C.ORDER[:-1]], "承認", hub_status="テスト")  # a row is missing
    expect(C.status_of(hub) == "テスト", f"missing phase row: {lines}")
    # 承認 + all rows finished, but a live phase card exists -> not 完了
    w, hub, lines, dry, errors = case(47, done, "承認", live_card=True)
    expect(C.status_of(hub) == "承認待ち" and not any("-> 完了" in l for l in lines), f"live card: {lines}")
    # human-decided values are never overwritten: 中止 and an already 完了 hub
    for keep in ("中止", "完了"):
        w, hub, lines, dry, errors = case(48, done, "承認", hub_status=keep)
        expect(C.status_of(hub) == keep and not any(c[0] == "patch" and c[1] == hub["id"] for c in w.calls), f"{keep} kept: {lines}")
    # the review DB is read only when it can matter (cheap conditions first)
    w = World()
    hub, eid = w.add_epic(50, rows=[("要件定義", "進行中")] + [(ph, "未着手") for ph in C.ORDER[1:]])
    seen = []
    orig = w.ntn
    w.ntn = lambda *a, **k: (seen.append(a[0]) if C.REVIEW_DS in a[0] else None, orig(*a, **k))[1]
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
    expect(seen == [], f"review DB not queried while a phase is unfinished: {seen}")


@check
def gates_due_are_asked_once_by_the_controller_in_japanese_with_the_hub_link():
    expect(C.gates_due({"要件定義": "完了"}) == [], "要件定義 完了 -> no gate")
    expect(C.gates_due({"設計": "完了", "実装": "完了", "テスト": "完了"}) == [], "only the release phase has a gate")
    expect(C.gates_due({"リリース判定": "完了"}) == ["リリース承認"], "リリース判定 完了 -> リリース承認")
    expect(C.gates_due({"リリース判定": "完了", "要件定義": "完了"}) == ["リリース承認"], "one gate, no duplicates")
    expect(C.gates_due({ph: st for ph in C.ORDER for st in ("進行中",)}) == [] and C.gates_due({"要件定義": "キャンセル"}) == [], "only 完了 rows trigger a gate")
    w = World()
    hub, eid = w.add_epic(10, rows=[("要件定義", "完了"), ("設計", "進行中"), ("リリース判定", "完了")])
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(False)
        expect([l for l in lines if l.startswith("WOULD ASK")] == [f"WOULD ASK {eid[-8:]}-承認"] and w.asks == [], f"dry run: {lines}")
        lines, errors = ctl.run(True)
        expect(not errors and [a[0] for a in w.asks] == [f"{eid[-8:]}-承認"], f"{lines} {w.asks}")
        expect(w.asks == [(f"{eid[-8:]}-承認", hub["id"])] and w.ask_epics == [hub["id"]], f"the ask goes through ask_human.py --release with the epic row: {w.asks}")
        n = len(w.asks)
        ctl.run(True)
        expect(len(w.asks) == n, "second run: nothing is asked again (key exists)")
        # allowed out of order: the 設計 row is still running, yet the release gate is asked as soon as its row is 完了 (and only once)
        ctl.run(True)
        expect(len(w.asks) == n == 1, "and only once")
    # a gate already asked under the old key (<epic8>-リリース承認, recorded in ask_human.json) is not asked a second time under the new key
    w = World()
    hub, eid = w.add_epic(13, rows=[("リリース判定", "完了")])
    with sandbox(w) as (ctl, tmp):
        C.state_path(C.ASK_STATE_FILE).write_text(json.dumps({f"{eid[-8:]}-リリース承認": "review-db"}), encoding="utf-8")
        lines, errors = ctl.run(True)
        expect(not errors and w.asks == [] and not any("ASK" in l for l in lines), f"old key counts as asked: {lines} {w.asks}")
    # a failing ask is loud (ERROR, exit 1) and is retried next run; a corrupt ask state is an error, never moved aside
    w = World()
    hub, eid = w.add_epic(11, rows=[("リリース判定", "完了"), ("実装", "進行中")])
    with sandbox(w) as (ctl, tmp):
        w.fail_ask = {f"{eid[-8:]}-承認"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ctl._run_and_report(True)
        expect(rc == 1 and f"ERROR gate ask {eid[-8:]}-承認: simulated review DB failure" in out.getvalue() and C.read_status()["ok"] is False, f"{rc} {out.getvalue()}")
        w.fail_ask = set()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ctl._run_and_report(True)
        expect(rc == 0 and [a[0] for a in w.asks] == [f"{eid[-8:]}-承認"], "retried and created")
        C.state_path(C.ASK_STATE_FILE).write_text("{broken", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = ctl._run_and_report(True)
        expect(rc == 1 and C.state_path(C.ASK_STATE_FILE).read_text(encoding="utf-8") == "{broken"
               and not [p for p in tmp.iterdir() if "corrupt" in p.name], "corrupt ask state: loud error, file untouched")


@check
def ask_release_wrapper_parses_the_script_result_and_calls_the_release_path():
    import epic_gate_controller as ctl
    with sandbox() as (ctl2, tmp):
        C._sh = _Seq([(0, "CREATED 411a\n", ""), (0, "DUP 411a\n", ""), (1, "ERROR channel_not_found\n", ""), (0, "weird\n", ""),
                      subprocess.TimeoutExpired("x", 60)])
        expect(ctl.ask_release("k", "e") == (True, "CREATED") and ctl.ask_release("k", "e") == (True, "DUP"), "CREATED / DUP are success")
        r = ctl.ask_release("k", "e")
        expect(r[0] is False and "channel_not_found" in r[1], r)
        expect(ctl.ask_release("k", "e")[0] is False, "unknown output is a failure")
        expect(ctl.ask_release("k", "e")[0] is False, "timeouts do not raise")
        cmd = C._sh.calls[0]
        expect(cmd[1].endswith("ask_human.py") and cmd[2:] == ["--release", "--key", "k", "--epic", "e"], cmd)
        expect(ctl.ASK_TIMEOUT == 150, "the ask runs at most 150 seconds")


_EPIC = "0123456789abcdef0123456789abcdef"
_EPIC_NAME = "あ" * 50  # longer than the 40 characters a thread name keeps
_ASK_TEXT = ("# 判断すること\n完了の確認をお願いします\n\n# 推奨\n承認: [判定書](https://example.test/doc) を確認済みです\n\n"
             "# 人が確かめること\n1. 判定書を読む\n2. 画面を見る\n\n# 根拠\n[判定書](https://example.test/doc)\n")


class _FakeDiscordWorld:
    """Fake Notion (ntn) + fake Discord REST (urllib.request.urlopen) for ask_human.py's notification. Records every call; no network."""

    def __init__(self, chat_url="", discord_error=None, patch_error=False, thread_error=None):
        self.chat_url, self.discord_error, self.patch_error = chat_url, discord_error, patch_error
        self.thread_error = thread_error  # Discord JSON code of a 400 answer to POST /channels/<ch>/messages/<msg>/threads only
        self.discord, self.patches, self.keys = [], [], set()

    def ntn(self, *args, data=None, **kw):
        path = args[0]
        method = args[args.index("-X") + 1] if "-X" in args else ("POST" if data is not None else "GET")
        if path.endswith("/query"):
            hit = [{"id": "row-" + k} for k in self.keys if data["filter"]["rich_text"]["equals"] == k]
            return {"results": hit, "has_more": False}
        if path == "v1/pages":
            key = data["properties"]["キー"]["rich_text"][0]["text"]["content"]
            self.keys.add(key)
            return {"id": "row-" + key, "url": "https://www.notion.so/row" + key}
        if path == f"v1/pages/{_EPIC}" and method == "GET":
            return {"properties": {"名前": {"type": "title", "title": [{"plain_text": _EPIC_NAME}]},
                                   "チャット": {"type": "url", "url": self.chat_url or None}}}
        if path == f"v1/pages/{_EPIC}" and method == "PATCH":
            if self.patch_error:
                raise C.PipelineError("simulated epic write failure")
            self.patches.append(data)
            self.chat_url = data["properties"]["チャット"]["url"]
            return {}
        raise AssertionError(f"unexpected ntn call {args}")

    def urlopen(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        self.discord.append((req.get_method(), req.full_url, body, req.get_header("Authorization")))
        if self.discord_error:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, self.discord_error, "x", {}, io.BytesIO(b'{"message": "Missing Access"}'))
        if self.thread_error and re.search(r"/messages/\d+/threads$", req.full_url):
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 400, "x", {}, io.BytesIO(json.dumps({"message": "x", "code": self.thread_error}).encode()))
        resp = io.BytesIO(json.dumps({"id": "9990001", "guild_id": "777"} if req.full_url.endswith("/threads") else {"id": "1"}).encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: None
        return resp


@contextlib.contextmanager
def _ask_sandbox(world, mention="199335018222053302", token_line="DISCORD_BOT_TOKEN=TESTTOKEN123\n"):
    import urllib.request
    import ask_human as ah
    with sandbox() as (_ctl, tmp):
        env = tmp / "fake.env"
        env.write_text("OTHER=1\n" + token_line, encoding="utf-8")
        chat = C.CFG["chat"]
        saved = (dict(chat), C.ntn, ah.token_env_path, urllib.request.urlopen)
        chat.pop("mention_user", None)
        if mention:
            chat["mention_user"] = mention
        chat["fallback_parent"] = "discord:555000"
        C.ntn, ah.token_env_path, urllib.request.urlopen = world.ntn, (lambda: env), world.urlopen
        try:
            yield ah
        finally:
            chat.clear()
            chat.update(saved[0])
            C.ntn, ah.token_env_path, urllib.request.urlopen = saved[1:]


def _run_ask(ah, key="k1", epic=_EPIC):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ah.main(["--key", key, "--text", _ASK_TEXT, "--epic", epic])
    return rc, out.getvalue(), err.getvalue()


@check
def ask_human_with_a_chat_url_mentions_in_that_channel():
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222")  # way 1: a thread (or channel) URL
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED"), (rc, out, err))
        expect(len(w.discord) == 1 and not w.patches, w.discord)  # no thread, no write-back
        method, url, body, auth = w.discord[0]
        expect(method == "POST" and url == "https://discord.com/api/v10/channels/222/messages" and auth == "Bot TESTTOKEN123", (method, url))
        lines = body["content"].split("\n")
        expect(len(lines) == 4 and lines[0].startswith("<@199335018222053302> 承認をお願いします: "), lines)
        expect(lines[1] == "推奨: 承認: 判定書 を確認済みです" and lines[2] == "確かめること: 判定書を読む（ほか 1 件）" and lines[3] == "https://www.notion.so/rowk1", lines)
        expect(all(len(l) <= 200 for l in lines[:3]) and len(body["content"]) <= 2000, lines)
        expect(body["allowed_mentions"] == {"users": ["199335018222053302"]}, body)
        expect("TESTTOKEN123" not in out + err, "the token never reaches stdout / stderr")
        rc, out, err = _run_ask(ah)  # the same key again: DUP, nothing is posted
        expect(rc == 0 and out.startswith("DUP") and len(w.discord) == 1, (out, w.discord))
    # a thread URL (channel id of the thread) works the same; a non-Discord URL falls back to a thread
    expect(__import__("ask_human").chat_channel_id("https://discord.com/channels/111/4242") == "4242", "thread url")
    expect(__import__("ask_human").chat_channel_id("https://example.com/channels/1/2/3") is None, "non-discord url")
    parse = __import__("ask_human").parse_chat_url
    expect(parse("https://discord.com/channels/111/4242") == ("111", "4242", None), "thread url: no message")
    expect(parse("https://discord.com/channels/111/222/333/") == ("111", "222", "333"), "message url")
    expect(parse("https://discord.com/channels/@me/222/333") == ("@me", "222", "333"), "message url outside a server")
    expect(parse("https://example.com/channels/1/2/3") is None and parse("") is None and parse(None) is None, "empty / non-discord")


@check
def ask_human_with_a_message_url_creates_a_thread_on_that_message_and_writes_it_back():
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222/333")  # way 2: the message the request came from
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah, key="k1")
        expect(rc == 0 and out.startswith("CREATED") and "WARN" not in err, (rc, out, err))
        expect(len(w.discord) == 2, w.discord)
        (m1, u1, b1, _), (m2, u2, b2, _) = w.discord
        expect(m1 == "POST" and u1 == "https://discord.com/api/v10/channels/222/messages/333/threads", u1)
        expect(b1 == {"name": "承認待ち: " + _EPIC_NAME[:40], "auto_archive_duration": 10080}, b1)
        expect(m2 == "POST" and u2 == "https://discord.com/api/v10/channels/9990001/messages", u2)
        expect(b2["content"].startswith("<@199335018222053302> 承認をお願いします: "), b2)
        expect(not any(u.endswith("/channels/222/messages") for _, u, _, _ in w.discord), "never posted in the channel itself")
        expect(w.patches == [{"properties": {"チャット": {"url": "https://discord.com/channels/111/9990001"}}}], w.patches)
        rc, out, err = _run_ask(ah, key="k2")  # the epic's チャット is now the thread URL: way 1, no second thread
        expect(rc == 0 and len(w.discord) == 3 and w.discord[2][1] == "https://discord.com/api/v10/channels/9990001/messages", w.discord[2:])
        expect(len(w.patches) == 1, w.patches)
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/@me/222/333")  # a message outside a server: posted, nothing written back
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and len(w.discord) == 2 and not w.patches and "WARN" not in err, (w.discord, w.patches, err))


@check
def ask_human_with_a_message_url_that_already_has_a_thread_posts_in_it():
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222/333", thread_error=160004)  # measured answer of Discord
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED") and "WARN" not in err, (rc, out, err))
        expect([u for _, u, _, _ in w.discord] == ["https://discord.com/api/v10/channels/222/messages/333/threads",
                                                   "https://discord.com/api/v10/channels/333/messages"], w.discord)  # the thread has the message's id
        expect(w.patches == [{"properties": {"チャット": {"url": "https://discord.com/channels/111/333"}}}], w.patches)
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222/333", thread_error=50024)  # the message is inside thread 222
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED") and "WARN" not in err, (rc, out, err))
        expect([u for _, u, _, _ in w.discord] == ["https://discord.com/api/v10/channels/222/messages/333/threads",
                                                   "https://discord.com/api/v10/channels/222/messages"], w.discord)
        expect(w.patches == [{"properties": {"チャット": {"url": "https://discord.com/channels/111/222"}}}], w.patches)
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222/333", thread_error=50001)  # any other 400: WARN, no post, row kept
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED") and "WARN" in err and "50001" in err and len(w.discord) == 1 and not w.patches, (rc, out, err, w.discord))


@check
def ask_human_without_a_chat_url_creates_a_thread_mentions_there_and_writes_it_back_to_the_epic():
    w = _FakeDiscordWorld(chat_url="")
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah, key="k1")
        expect(rc == 0 and out.startswith("CREATED") and "WARN" not in err, (rc, out, err))
        expect(len(w.discord) == 2, w.discord)
        (m1, u1, b1, _), (m2, u2, b2, _) = w.discord
        expect(m1 == "POST" and u1 == "https://discord.com/api/v10/channels/555000/threads", u1)
        expect(b1 == {"name": "承認待ち: " + _EPIC_NAME[:40], "auto_archive_duration": 10080, "type": 11}, b1)
        expect(u2 == "https://discord.com/api/v10/channels/9990001/messages" and b2["content"].startswith("<@199335018222053302> "), (u2, b2))
        # write-back: the epic's チャット is the new thread, so the next ask gathers in it (no second thread)
        expect(w.patches == [{"properties": {"チャット": {"url": "https://discord.com/channels/777/9990001"}}}], w.patches)
        rc, out, err = _run_ask(ah, key="k2")
        expect(rc == 0 and len(w.discord) == 3 and w.discord[2][1].endswith("/channels/9990001/messages"), w.discord[2:])
        expect(not any(u.endswith("/threads") for _, u, _, _ in w.discord[2:]), "no new thread on the second ask")
    # a failed write-back is only a WARN
    w = _FakeDiscordWorld(chat_url="", patch_error=True)
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED") and "WARN" in err and "チャット" in err and len(w.discord) == 2, (rc, out, err))
    # no mention_user configured: the post still happens, without a mention
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222")
    with _ask_sandbox(w, mention=None) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and w.discord[0][2]["content"].startswith("承認をお願いします: ") and "<@" not in w.discord[0][2]["content"], w.discord)


@check
def ask_human_keeps_the_row_and_exits_0_when_discord_fails():
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222/333", discord_error=403)
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED") and "row-k1" in w.keys | {"row-k1"} and "k1" in w.keys, (rc, out, err))
        expect("WARN" in err and "403" in err and "TESTTOKEN123" not in out + err, err)
        # a message URL: the failing call is the thread creation; nothing is posted and nothing written back
        expect(len(w.discord) == 1 and w.discord[0][1].endswith("/channels/222/messages/333/threads") and not w.patches, (w.discord, w.patches))
    w = _FakeDiscordWorld(chat_url="https://example.com/x")  # a non-Discord URL takes way 3 (a new thread under the fallback parent)
    with _ask_sandbox(w) as ah:
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and [u for _, u, _, _ in w.discord] == ["https://discord.com/api/v10/channels/555000/threads",
                                                               "https://discord.com/api/v10/channels/9990001/messages"], w.discord)
        expect(w.patches == [{"properties": {"チャット": {"url": "https://discord.com/channels/777/9990001"}}}], w.patches)
    w = _FakeDiscordWorld(chat_url="https://discord.com/channels/111/222")
    with _ask_sandbox(w, token_line="NOT_THE_TOKEN=1\n") as ah:  # no token in the .env: WARN too, no HTTP call
        rc, out, err = _run_ask(ah)
        expect(rc == 0 and out.startswith("CREATED") and "WARN" in err and not w.discord, (rc, out, err))


@check
@legacy_phases
def idempotency_hit_on_an_old_done_card_is_a_fail_not_a_phantom_phase():
    def mk():
        w = World()
        hub, eid = w.add_epic(1, rows=[("要件定義", "未着手")])
        return w, hub, eid
    w, hub, eid = mk()
    key = f"phase-{eid}-要件定義"
    w.kanban[key] = dict(id="t_old", created_at=1, status="done")  # an old finished card with the same key (not archived)
    w.board.append(dict(id="t_old", status="done", assignee="executor", tenant=C.tenant_of(eid), key=key, title="[要件定義] ハブ1"))
    with sandbox(w) as (ctl, tmp):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ctl._run_and_report(True)
        text = out.getvalue()
        expect(rc == 1 and f"FAIL [要件定義] ハブ1: idempotency key {key} matched card t_old which is already done" in text and "archive the old card" in text, text)
        expect(C.status_of(w.row(hub, "要件定義")) == "未着手" and not [c for c in w.calls if c[0] == "patch" and c[1] != hub["id"]], "row must stay 未着手")
        expect(C.load_journal()[f"{eid}:要件定義"]["cards"] == {}, "the old card is not recorded as ours")
        # the human archives the old card: the next run creates the real one
        w.kanban.pop(key)
        w.board[:] = [c for c in w.board if c["id"] != "t_old"]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ctl._run_and_report(True)
        expect(rc == 0 and C.status_of(w.row(hub, "要件定義")) == "進行中" and len(w.kanban) == 1, "recovered after the old card was archived")
    # a status-less create output is verified against the board
    w, hub, eid = mk()
    with sandbox(w) as (ctl, tmp):
        key = f"phase-{eid}-要件定義"
        orig = w.hermes

        def hermes_no_status(*args, stdin=None):
            r = orig(*args, stdin=stdin)
            d = json.loads(r.stdout)
            d.pop("status", None)
            return subprocess.CompletedProcess(r.args, 0, json.dumps(d), "")
        ctl.hermes = hermes_no_status
        w.kanban[key] = dict(id="t_old", created_at=1, status="done")
        w.board.append(dict(id="t_old", status="done", assignee="executor", tenant=C.tenant_of(eid), key=key, title="[要件定義] ハブ1"))
        lines, errors = ctl.run(True)
        expect(errors and any("already done" in l for l in lines), f"board lookup catches it: {lines}")


# ---------------------------------------------------------------- michiomochiレビュー DB (human decisions)

class _ReviewDB:
    """Fake michiomochiレビュー data source: query by キー, create page, list rows."""

    def __init__(self, rows=None):
        self.rows, self.created = list(rows or []), []
        self.reject_epic = False   # behave like the old DB schema: a create that writes 対象エピック fails
        self.level = None          # the レベル the epic row (GET v1/pages/<id>) answers with; None = an epic without one

    def ntn(self, *args, data=None, **kw):
        if args[0].endswith("/query"):
            flt = (data or {}).get("filter")
            if flt and flt["property"] == "キー":
                want = flt["rich_text"]["equals"]
                return {"results": [r for r in self.rows if r["key"] == want], "has_more": False}
            return {"results": [r["page"] for r in self.rows], "has_more": False}
        if re.fullmatch(r"v1/pages/[0-9a-f-]+", args[0]) and data is None:
            return {"properties": {C.LEVEL_PROP: {"select": {"name": self.level}}} if self.level else {}}
        if args[0] == "v1/pages":
            if self.reject_epic and "対象エピック" in data["properties"]:
                raise C.PipelineError("対象エピック is not a property that exists.")
            self.created.append(data)
            return {"id": _id(900 + len(self.created))}
        raise AssertionError(args)


def _review_page(i, status, epic=None, applied=False, key="k", edited="2026-11-07T00:00:00.000Z", created="2026-11-06T00:00:00.000Z", legacy=None):
    """A review row as the DB returns it now (名前 / ステータス / キー / 対象エピック / 反映済み); `legacy` adds properties of the old layout."""
    props = {"名前": _title(f"row{i}"), "ステータス": _status(status),
             "反映済み": {"checkbox": applied}, "キー": {"rich_text": [{"plain_text": key}]}}
    if epic is not False:  # epic=False: a row from before the property existed has no 対象エピック at all
        props["対象エピック"] = {"relation": [{"id": epic}] if epic else []}
    props.update(legacy or {})
    return {"id": _id(i), "url": f"https://app.notion.com/p/{_epic_id(i)}", "properties": props,
            "last_edited_time": edited, "created_time": created}


def _md_blocks(md):
    """Fake Notion: the block list the markdown of a create/update call turns into (enhanced markdown: `# H {color="yellow_bg"}`, <table>, paragraphs).
    heading color yellow_bg is stored as the API's yellow_background. An unparsed `{color=...}` left in a paragraph shows up as a paragraph (= the bug)."""
    blocks, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r'^# (.+?)(?: \{color="(\w+)"\})?\s*$', ln)
        if not ln.strip():
            i += 1
        elif m:
            blocks.append({"type": "heading_1", "text": m.group(1), "color": {"yellow_bg": "yellow_background"}.get(m.group(2), "default")})
            i += 1
        elif ln.startswith("<table"):
            j = i
            while lines[j] != "</table>":
                j += 1
            rows = [re.findall(r"<td>(.*?)</td>", l) for l in lines[i:j] if l.startswith("<tr>")]
            blocks.append({"type": "table", "rows": rows})
            i = j + 1
        else:
            blocks.append({"type": "paragraph", "text": ln})
            i += 1
    return blocks


def _expect_review_chapters(md, what, level=None):
    """The body has exactly the review template's seven H1 chapters (yellow_background), each followed by a paragraph or table, none empty."""
    blocks = _md_blocks(md)
    heads = [b for b in blocks if b["type"] == "heading_1"]
    expect([h["text"] for h in heads] == list(C.review_chapters(level)), f"{what}: chapters {[h['text'] for h in heads]}")
    expect(all(h["color"] == "yellow_background" for h in heads), f"{what}: heading colors {[h['color'] for h in heads]}")
    expect(not any("{color" in b.get("text", "") or b["type"] == "heading_1" and b["text"].startswith("{") for b in blocks), f"{what}: a literal {{color}} is left in the body")
    expect(blocks[0]["type"] == "heading_1", f"{what}: the first block is a heading, not a loose paragraph")
    for k, b in enumerate(blocks):
        if b["type"] == "heading_1":
            nxt = blocks[k + 1] if k + 1 < len(blocks) else None
            expect(nxt is not None and nxt["type"] in ("paragraph", "table") and (nxt.get("text") or nxt.get("rows")), f"{what}: chapter {b['text']} has no body")
    return blocks


@check
def review_row_payload_carries_key_epic_and_the_nine_chapters_and_none_of_the_dropped_properties():
    body = C.build_release_ask(_facts())["body"]
    page = C.review_page("1e311667-承認", body, title="ハブ: リリース承認", epic_id=_id(7))
    p = page["properties"]
    expect(page["parent"] == {"data_source_id": C.REVIEW_DS}, page["parent"])
    expect(set(p) == {"名前", "キー", "対象エピック"}, f"only 名前 / キー / 対象エピック are written: {sorted(p)}")
    expect(p["名前"]["title"][0]["text"]["content"] == "ハブ: リリース承認", p)
    expect(p["キー"]["rich_text"][0]["text"]["content"] == "1e311667-承認" and p["対象エピック"] == {"relation": [{"id": _id(7)}]}, p)
    expect(not any(k in p for k in ("種別", "優先度", "レビュー先", "対象タスク", "ステータス")), "no dropped property, status stays at the DB default (レビュー待ち)")
    md = page["markdown"]
    blocks = _expect_review_chapters(md, "gate_ask body")
    expect(md.startswith('# 判断すること {color="yellow_bg"}\n') and "k" in md, md[:80])
    chap = {}
    for b in blocks:  # chapter -> the text under it
        if b["type"] == "heading_1":
            cur = chap.setdefault(b["text"], [])
        else:
            cur.append(b)
    # chapter 1 = the intro line + the three lines of templates/release_meaning.md as rendered, whatever their wording (the swap test is below)
    expect("リリースしてよいと認めるか" in chap["判断すること"][0]["text"] and [b["text"] for b in chap["判断すること"][1:]] == _meaning().splitlines(),
           chap["判断すること"])
    expect(chap["推奨"][0]["text"].startswith("推奨：承認") and "ステータス" in chap["判断の結果"][0]["text"], chap["推奨"])
    expect("1e311667-承認" in chap["反映の記録"][0]["text"], chap["反映の記録"])
    # free-form ask text (-done, -send-*, レビュー中): the four headings are carried over, the other chapters read 該当なし
    free = C.review_page("k", _ASK_TEXT, epic_id=_id(7))
    fb = _expect_review_chapters(free["markdown"], "free-form body")
    expect(free["markdown"].count("該当なし") == 3 and "[判定書](https://example.test/doc)" in free["markdown"]
           and free["properties"]["名前"]["title"][0]["text"]["content"] == "完了の確認をお願いします", free["markdown"])
    expect(C.review_markdown("k", body) == body.strip() + "\n" and C.has_review_chapters(body) and not C.has_review_chapters("# 判断の対象\nx"), "a body that has the chapters is used as is")
    expect("対象エピック" not in C.review_page("k", _ASK_TEXT)["properties"], "without an epic id the relation is left out")
    expect(not hasattr(C, "first_link"), "the link lives in the body, no レビュー先 is derived from it")
    raises(TypeError, C.review_page, "k", _ASK_TEXT, priority="P1")
    expect(C.review_title("k", "") == "k" and len(C.review_title("k", "あ" * 300)) == 120, "title fallbacks")
    expect(C.review_title("k", body) not in ("", "# 判断すること") and not C.review_title("k", body).startswith(("#", "<")), C.review_title("k", body))
    # the template files and the review template of the Notion DB name the same chapters in the same order
    root = next((r for r in _repo_roots() if (r / "notion" / "templates" / "review.md").is_file()), None)
    if root is not None:
        for rel in ("notion/templates/review.md", "scripts/templates/gate_ask.md"):
            tpl = (root / rel).read_text(encoding="utf-8")
            expect(tuple(re.findall(r'^# (\S+) \{color="yellow_bg"\}', tpl, re.M)) == C.review_template_chapters(), f"{rel} chapters differ from templates/gate_ask.md")
        expect(C.review_template_chapters() == C.review_chapters("L3"), "levels.toml gives L3 every chapter of the template")


def _chapter1(body):
    """The text between the first heading (判断すること) and the second one."""
    return ("\n" + body).split("\n# ")[1]


@check
def chapter_1_fixed_sentences_live_in_release_meaning_md_only_and_a_swap_changes_the_row():
    base = C.build_release_ask(_facts())["body"]
    tpl = C.load_template("release_meaning")
    lines = tpl.splitlines()
    expect(len(lines) == 3 and C.template_fields("release_meaning") == {"pr", "deploy"}, f"release_meaning.md: 3 lines with ${{pr}} / ${{deploy}}: {lines}")
    expect(all(line in _chapter1(base) for line in _meaning().splitlines()), "chapter 1 carries the rendered template")
    # (1) the values ${pr} / ${deploy} change the rendered sentences
    old_lines = _meaning().splitlines()
    rendered = C.release_meaning(pr="下書きの PR の公開・マージ", deploy="本番反映まで")
    expect("下書きの PR の公開・マージ" in rendered and "本番反映まで" in rendered and rendered != _meaning(), rendered)
    # (2) swapping the template file (what the ship epic does) changes chapter 1; no constant holds the old sentences any more
    saved = C.TEMPLATE_DIR
    with tempfile.TemporaryDirectory() as d:
        for f in Path(saved).glob("*.md"):
            (Path(d) / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (Path(d) / "release_meaning.md").write_text("- 承認すると：甲を${pr}。${deploy}\n- 差し戻すと：乙\n- 取り下げると：丙\n", encoding="utf-8")
        C.TEMPLATE_DIR = Path(d)
        try:
            swapped = C.build_release_ask(_facts())["body"]
        finally:
            C.TEMPLATE_DIR = saved
    ch1 = _chapter1(swapped)
    expect("甲を" + C.ship_meaning("example-org/example-app")["pr"] in ch1 and "- 差し戻すと：乙" in ch1 and "- 取り下げると：丙" in ch1, ch1)
    expect(ch1 != _chapter1(base) and not any(line in swapped for line in old_lines), "after the swap none of the old sentences is left in the row")
    # (3) the sentences exist in no other file of the repo (scripts/, templates, prompts, skills, profiles, Notion templates)
    chunks = []
    for line in lines:  # the wording around the placeholders (read from the template: this file must not hold it)
        chunks += [c.strip() for c in re.split(r"\$\{\w+\}", line.split("：", 1)[1]) if len(c.strip()) >= 8]
    expect(len(chunks) >= 3, f"chunks to look for: {chunks}")
    root = next((r for r in _repo_roots() if (r / "notion" / "templates" / "review.md").is_file()), None)
    if root is None:
        print("skip single-location check of the chapter 1 sentences: no repo checkout (staging)")
        return
    found = []
    for sub in ("scripts", "prompts", "skills", "hermes", "notion"):
        for f in sorted((root / sub).rglob("*")):
            if not f.is_file() or f.suffix not in (".py", ".md", ".txt", ".json", ".toml", ".tmpl", ".sh") or "__pycache__" in f.parts:
                continue
            if f.name == "release_meaning.md" and f.parent.name == "templates":
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            found += [f"{f.relative_to(root)}: {c}" for c in chunks if c in text]
    expect(not found, f"the chapter 1 sentences are written somewhere besides templates/release_meaning.md: {found}")


@check
def ask_human_creates_one_row_per_key_and_never_posts_to_slack():
    import ask_human as ah
    db = _ReviewDB()
    saved = (C.ntn, C.STATE_DIR, ah.notify)
    notified = []
    with tempfile.TemporaryDirectory() as tmp:
        C.STATE_DIR = Path(tmp)
        C.ntn = db.ntn
        ah.notify = lambda *a, **k: notified.append((a, k))  # the Discord notification has its own checks (never the real API here)
        try:
            expect(ah.ask("k1", _ASK_TEXT) == ("CREATED", _id(901)) and len(db.created) == 1, db.created)
            _expect_review_chapters(db.created[0]["markdown"], "row created by ask_human (free-form text)")
            db.rows.append(dict(key="k1", page=_review_page(1, "レビュー待ち", key="k1"), id=_id(1)))
            db.rows[-1]["page"]["id"] = _id(1)
            expect(ah.ask("k1", _ASK_TEXT) == ("DUP", _id(1)) and len(db.created) == 1, "the same key never creates a second row")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--key", "k2", "--text", _ASK_TEXT, "--epic", _id(5),
                              "--kind", "外部送信の指示", "--priority", "P1", "--url", "https://example.com/p", "--task", _id(6)])  # old flags: accepted, never written
            expect(rc == 0 and out.getvalue().startswith("CREATED "), out.getvalue())
            expect(json.loads(C.state_path(C.ASK_STATE_FILE).read_text(encoding="utf-8")).get("k2") == "review-db", "key cached")
            props = db.created[-1]["properties"]
            _expect_review_chapters(db.created[-1]["markdown"], "row created by ask_human --text")
            import epic_gate_controller as ctl
            gate_md = C.build_release_ask(_facts())["body"]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ah.main(["--key", "1e311667-承認", "--text", gate_md, "--title", "ハブ: リリース承認", "--epic", _id(5)])
            expect(rc == 0, "gate body")
            gb = _expect_review_chapters(db.created[-1]["markdown"], "row created by ask_human with the gate_ask body")
            expect(db.created[-1]["markdown"] == gate_md.strip() + "\n" and sum(b["type"] == "table" for b in gb) == 1, db.created[-1]["markdown"])
            expect(set(props) == {"名前", "キー", "対象エピック"} and props["対象エピック"]["relation"] == [{"id": _id(5)}], db.created[-1])
            # the live DB has not got 対象エピック yet: the row is still created, without the relation, with a warning on stderr
            db.reject_epic = True
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
                rc = ah.main(["--key", "k4", "--text", _ASK_TEXT, "--epic", _id(5)])
            expect(rc == 0 and "対象エピック" not in db.created[-1]["properties"] and "WARN" in err.getvalue(), (db.created[-1], err.getvalue()))
            db.reject_epic = False
            C.ntn = lambda *a, **k: (_ for _ in ()).throw(C.PipelineError("boom"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--key", "k3", "--text", _ASK_TEXT])
            expect(rc == 1 and out.getvalue().startswith("ERROR "), out.getvalue())
        finally:
            C.ntn, C.STATE_DIR, ah.notify = saved
    src = (HERE / "ask_human.py").read_text(encoding="utf-8")
    expect("slack.com" not in src and "chat.postMessage" not in src, "ask_human.py must not post to Slack any more")


@check
def review_inbox_lists_only_decided_unapplied_rows_and_flags_stale_ones():
    import review_inbox_status as rs
    now = time.mktime(time.strptime("2026-11-07", "%Y-%m-%d")) + 12 * 3600
    old, fresh = "2026-11-07T00:00:00.000Z", "2026-11-07T02:59:00.000Z"
    old_layout = {"種別": {"select": {"name": "要件承認"}}, "レビュー先": {"url": "https://example.com/x"}, "優先度": {"select": {"name": "P1"}},
                  "対象タスク": {"relation": [{"id": _id(50)}]}}
    pages = [_review_page(1, "レビュー待ち", epic=_id(11)),
             _review_page(2, "承認", epic=False, key="a-要件承認", edited=fresh, legacy=old_layout),  # a row from before the change still reads fine
             _review_page(3, "差し戻し", epic=_id(13), applied=True), _review_page(4, "取り下げ", epic=_id(14), key="b-承認-r2", edited=old)]
    lines = rs.lines_for(pages, rs._epoch("2026-11-07T03:00:00.000Z"))
    expect(len([l for l in lines if l.startswith("DECIDED")]) == 2, lines)
    expect(lines[0].startswith(f"DECIDED {_id(2)} 承認 key=a-要件承認 epic=- ") and f"DECIDED {_id(4)} 取り下げ key=b-承認-r2 epic={_id(14)} " in lines[1], lines)
    expect(not any(w in l for l in lines for w in ("種別", "要件承認 key", "example.com")), "kind / link are not reported")
    expect([l for l in lines if l.startswith("ALERT")] == [f"ALERT unapplied review decision older than 30 minutes: {_id(4)} 取り下げ row4"], lines)
    expect(rs.lines_for([_review_page(1, "レビュー待ち")], now) == [], "a waiting row alone wakes nobody")
    snap = rs.snapshot_text(pages, rs._epoch("2026-11-07T03:00:00.000Z"))
    expect(snap.startswith("WAITING ") and f"epic={_id(11)} " in snap and snap.count("WAITING") == 1, "waiting rows go to the snapshot only: " + snap)
    saved = (C.ntn, C.STATE_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        C.STATE_DIR = Path(tmp)
        db = _ReviewDB([dict(key="a", page=p, id=p["id"]) for p in pages])
        C.ntn = lambda *a, **k: {"results": [], "has_more": False} if C.TASK_DS in a[0] else db.ntn(*a, **k)  # no レビュー中 task
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = rs.main(now=rs._epoch("2026-11-07T03:00:00.000Z"))
            expect(rc == 0 and out.getvalue().count("DECIDED") == 2 and (Path(tmp) / rs.SNAPSHOT_FILE).exists(), out.getvalue())
            C.ntn = lambda *a, **k: (_ for _ in ()).throw(C.PipelineError("boom"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = rs.main(now=0)
            expect(rc == 1 and out.getvalue().startswith("ERROR "), out.getvalue())
        finally:
            C.ntn, C.STATE_DIR = saved


def _unreflected_world():
    """レビュー中 tasks 1-7 and the review rows of a round: (task pages still レビュー中, review pages). Task 4 is 完了 and task 5 キャンセル already (not in the list)."""
    import check_review_pairs as cp
    t = {n: _pair_task(n) for n in range(1, 8)}
    key = lambda n, sfx="": cp.task_key(t[n]["id"]) + sfx
    reviews = [_review_page(21, "承認", key=key(1), applied=True),                          # 反映済み ticked, task still レビュー中: the real incident
               _review_page(22, "差し戻し", key=key(2), applied=True),
               _review_page(23, "取り下げ", key=key(3)),                                    # not ticked either: DECIDED and UNREFLECTED
               _review_page(24, "承認", key=key(4), applied=True),                          # task is 完了
               _review_page(25, "取り下げ", key=key(5), applied=True),                      # task is キャンセル
               _review_page(26, "差し戻し", key=key(6), applied=True), _review_page(27, "レビュー待ち", key=key(6, "-r2")),  # re-asked: waiting
               _review_page(28, "承認", key="a-承認", applied=True)]                        # not a task key
    return [t[n] for n in (1, 2, 3, 6, 7)], reviews


@check
def review_inbox_flags_decided_rows_whose_task_is_still_レビュー中_whatever_反映済み_says():
    import review_inbox_status as rs
    tasks, reviews = _unreflected_world()
    now = rs._epoch("2026-11-07T00:10:00.000Z")  # 10 minutes after the rows were edited: no ALERT
    lines = rs.lines_for(reviews, now, tasks)
    un = [l for l in lines if l.startswith("UNREFLECTED")]
    expect(len(un) == 3, lines)  # tasks 1, 2, 3 only: 4 / 5 are closed, 6 is re-asked, 7 has no row, the "a-承認" row is no task's
    for l, row, status, to in zip(un, (21, 22, 23), ("承認", "差し戻し", "取り下げ"), ("完了", "進行中", "キャンセル")):
        expect(f"review={_id(row)} " in l and f"決定={status} " in l and f"ステータスを「{to}」" in l and f"task={_id(0xA00 + row - 20)} " in l, l)
    expect("反映済み=済" in un[0] and "反映済み=未" in un[2], un)
    expect(sum(l.startswith("DECIDED") for l in lines) == 1 and lines.index(un[0]) > lines.index(f"DECIDED {_id(23)} 取り下げ key={reviews[2]['properties']['キー']['rich_text'][0]['plain_text']} epic=- {reviews[2]['url']}"), lines)
    expect(not [l for l in lines if l.startswith("ALERT")], lines)
    expect(rs.lines_for(reviews, now) == [l for l in lines if not l.startswith("UNREFLECTED")], "without the task list nothing changes")
    expect(not [l for l in rs.lines_for(reviews, now, []) if l.startswith("UNREFLECTED")], "no レビュー中 task: nothing to say")
    late = rs.lines_for(reviews, rs._epoch("2026-11-07T01:00:00.000Z"), tasks)
    expect(sum(l.startswith("ALERT") for l in late) == 3 and all(f"{_id(r)} " in " ".join(late) for r in (21, 22, 23)), late)  # an unreflected row older than 30 minutes is alerted once, 23 not twice


@check
def review_inbox_output_is_stable_and_main_reads_the_レビュー中_tasks_too():
    import review_inbox_status as rs
    tasks, reviews = _unreflected_world()
    now = rs._epoch("2026-11-07T00:10:00.000Z")
    first = rs.lines_for(reviews, now, tasks)
    expect(rs.lines_for(list(reversed(reviews)), now + 60, list(reversed(tasks))) == first == rs.lines_for(reviews, now, tasks), "same state, same text")
    saved = (C.ntn, C.STATE_DIR)
    seen = []

    def ntn(*a, data=None, **k):
        seen.append((a[0], (data or {}).get("filter")))
        return {"results": tasks if C.TASK_DS in a[0] else reviews, "has_more": False}
    with tempfile.TemporaryDirectory() as tmp:
        C.STATE_DIR, C.ntn = Path(tmp), ntn
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = rs.main(now=now)
        finally:
            C.ntn, C.STATE_DIR = saved
    expect(rc == 0 and out.getvalue().splitlines() == first, out.getvalue())
    expect(any(C.TASK_DS in u and f == {"property": "ステータス", "status": {"equals": "レビュー中"}} for u, f in seen), seen)


# ---------------------------------------------------------------- create_row.py (one command makes a row; the template is applied and checked)

def _template_fixture(kind):
    """The default template body of the live DB as GET v1/pages/<id>/markdown returned it on 2026-11-09 (scripts/tests/notion_template_<kind>.md)."""
    return (HERE / "tests" / f"notion_template_{kind}.md").read_text(encoding="utf-8").rstrip("\n")


class _TemplateNotion:
    """Fake Notion with default templates: POST v1/pages with `template` gives the page the template body (as the live API does);
    GET/PATCH markdown (update_content: a replacement that matches nothing is dropped silently while another one matched, as measured),
    blocks, page properties. Failure injection: hidden (reads before the body shows), drop (indexes of replacements Notion drops),
    patch_error / patch_error_after_apply, no_default."""

    TASK_TPL, EPIC_TPL = _id(0x701), _id(0x702)

    def __init__(self):
        self.pages = {self.TASK_TPL: dict(md=_template_fixture("task")), self.EPIC_TPL: dict(md=_template_fixture("epic"))}
        self.calls, self.posts, self.patches, self.children = [], [], [], {}
        self.hidden, self.drop, self.no_default, self.no_epic = 0, set(), False, set()
        self.patch_error = self.patch_error_after_apply = False
        self.italics = False  # Notion rewrites _x_ to *x* (measured)

    def ntn(self, *args, data=None, **kw):
        a0, method = args[0], (args[args.index("-X") + 1] if "-X" in args else "GET")
        self.calls.append((method, a0))
        m = re.fullmatch(r"v1/data_sources/(.+)/templates", a0)
        if m:
            tpl = self.TASK_TPL if m.group(1) == C.TASK_DS else self.EPIC_TPL
            return {"templates": [] if self.no_default else [{"id": tpl, "is_default": True, "name": "ひな形"}], "has_more": False}
        m = re.fullmatch(r"v1/pages/([^/]+)/markdown", a0)
        if m:
            page = self.pages[m.group(1)]
            if method == "PATCH":
                return self.update_content(page, data)
            if page.get("hidden", 0) > 0:
                page["hidden"] -= 1
                return {"markdown": ""}
            return {"markdown": page["md"]}
        if a0 == "v1/pages" and method == "POST":
            self.posts.append(data)
            pid = _id(0x800 + len(self.posts))
            tpl = data["template"]["template_id"]
            self.pages[pid] = dict(md=self.pages[tpl]["md"], props=json.loads(json.dumps(data["properties"])), hidden=self.hidden)
            return {"id": pid, "url": f"https://app.notion.com/p/{pid.replace('-', '')}"}
        m = re.fullmatch(r"v1/pages/([^/]+)", a0)
        if m and m.group(1) not in self.pages and method == "GET":  # an existing row that was not made here (the epic of a task): present unless listed in `no_epic`
            if m.group(1) in self.no_epic:
                raise C.PipelineError("object_not_found: Could not find page")
            return {"id": m.group(1), "properties": {}}
        if m:
            page = self.pages[m.group(1)]
            if method == "PATCH":
                page["props"].update(json.loads(json.dumps(data["properties"])))
                self.patches.append(("props", m.group(1), data))
            return {"id": m.group(1), "properties": page["props"]}
        m = re.fullmatch(r"v1/blocks/(.+)/children", a0)
        if m:
            return {"results": self.blocks(m.group(1)), "has_more": False}
        raise AssertionError(args)

    def update_content(self, page, data):
        ups = data["update_content"]["content_updates"]
        self.patches.append(("md", ups))
        if self.patch_error:
            raise C.PipelineError("ntn timeout after 90s")
        md, hit, miss = page["md"], 0, 0
        for i, u in enumerate(ups):
            if i in self.drop or md.count(u["old_str"]) == 0:
                miss += 1
            elif md.count(u["old_str"]) > 1:
                raise C.PipelineError("validation_error: Multiple matches")
            else:
                md, hit = md.replace(u["old_str"], u["new_str"]), hit + 1
        if hit == 0:
            raise C.PipelineError("validation_error: No matches found")  # all of them miss: an error; one miss among hits: silent
        page["md"] = md.replace("_g_", "*g*") if self.italics else md
        if self.patch_error_after_apply:
            raise C.PipelineError("ntn timeout after 90s")
        return {"markdown": page["md"]}

    def blocks(self, bid):
        if bid in self.children:
            return self.children[bid]
        md = self.pages[bid]["md"]
        out, lines, i = [], md.split("\n"), 0
        color = {"yellow_bg": "yellow_background", "blue_bg": "blue_background"}
        plain_text = lambda s: re.sub(r"\\(.)", r"\1", re.sub(r"`", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)))
        rt = lambda s: [{"plain_text": plain_text(s)}]
        while i < len(lines):
            ln = lines[i]
            h = re.match(r'^(#{1,2}) (.+?)(?: \{color="(\w+)"\})?\s*$', ln)
            if h:
                t = f"heading_{len(h.group(1))}"
                out.append({"id": f"{bid}-b{len(out)}", "type": t, t: {"rich_text": rt(h.group(2)), "color": color.get(h.group(3), "default")}})
            elif ln.startswith("<table"):
                j = i
                while lines[j] != "</table>":
                    j += 1
                rows = [re.findall(r"<td>(.*?)</td>", r, re.S) for r in re.findall(r"<tr>(.*?)</tr>", "\n".join(lines[i:j]), re.S)]
                tid = f"{bid}-t{len(out)}"
                self.children[tid] = [{"type": "table_row", "table_row": {"cells": [rt(c) for c in r]}} for r in rows]
                out.append({"id": tid, "type": "table", "has_children": True, "table": {"has_column_header": True}})
                i = j
            elif re.match(r"^- \[[ xX]\]", ln):
                out.append({"id": f"{bid}-b{len(out)}", "type": "to_do", "to_do": {"rich_text": rt(ln[6:].strip()), "checked": ln[3] in "xX"}})
            elif ln.startswith("- "):
                out.append({"id": f"{bid}-b{len(out)}", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt(ln[2:])}})
            elif ln.strip():
                out.append({"id": f"{bid}-b{len(out)}", "type": "paragraph", "paragraph": {"rich_text": rt(ln)}})
            i += 1
        return out


_TASK_SPEC = {"kind": "task", "title": "【SF】テスト", "epic": _epic_id(5),
              "overview": {"目的": "手で作ると崩れる行を1コマンドで作る。a < b & c | d _g_ も通る。", "完了条件": "CREATED が出る。", "対象外": "既存行の移行。", "対象": "example-org/software-factory、ブランチ michiomochi/x。README.md を読む。"},
              "checklist": ["scripts/create_row.py を作る", "テストを足す"], "memo": ["カード t_1 で起票"]}
_EPIC_SPEC = {"kind": "epic", "title": "【SF】エピックのテスト", "type": "プロダクト開発", "priority": "P1", "chat": "https://discord.com/channels/1/2",  # priority: an old spec key, accepted and never written
              "purpose": {"指標": "検査に失敗した行の割合", "定義": "H1 6章・表2つ・to_do", "基準値": "2026-11-09: 1件", "目標値": "0件", "期限": "2026-11-16（仮置き）", "完了条件": "create_row.py が通る"},
              "candidates": ["打ち手A：スクリプトを作る ／ 判断の宣言：切り替えるか ／ 感度の見積もり：動かない ／ 開始できるフェーズ：設計", "打ち手B：スキルを差し替える ／ 判断の宣言：x ／ 感度の見積もり：y ／ 開始できるフェーズ：実装"],
              "measurements": [["2026-11-09", "手組みの未適用", "1件", "本文の読み取り"], ["2026-11-10", "手組みの未適用", "0件", "監視"]],
              "log": ["2026-11-09 起票。", "2026-11-09 重複確認の結果。"], "docs": ["[リポジトリ](https://github.com/example-org/software-factory)"]}


def _run_create_row(fake, spec, *flags, raw=None):
    """-> (exit code, stdout). Notion is `fake`; the waits are skipped."""
    import create_row as cr
    saved = (C.ntn, C._sleep)
    C.ntn, C._sleep = fake.ntn, lambda s: None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = cr.main(["--spec-json", raw if raw is not None else json.dumps(spec, ensure_ascii=False), *flags])
    finally:
        C.ntn, C._sleep = saved
    return rc, out.getvalue()


def _last_page(fake):
    pid = _id(0x800 + len(fake.posts))
    return pid, fake.pages[pid]


@check
def create_row_makes_a_task_from_the_default_template_in_a_waiting_state_and_ends_in_the_spec_status():
    f = _TemplateNotion()
    rc, out = _run_create_row(f, _TASK_SPEC)
    pid, page = _last_page(f)
    expect(rc == 0 and out.startswith(f"CREATED task {pid} "), (rc, out))
    post = f.posts[0]
    expect(post["template"] == {"type": "template_id", "template_id": f.TASK_TPL} and post["parent"] == {"data_source_id": C.TASK_DS}, post)
    expect("markdown" not in post and "children" not in post, "the body is the template's: nothing else is sent with the POST")
    expect(post["properties"]["ステータス"] == {"status": {"name": "進行中"}}, "created in the waiting state: the intake cron takes 未着手 rows only")
    expect(post["properties"]["エピック"] == {"relation": [{"id": _id(5)}]}, post["properties"])  # the 32-hex id of the spec is sent in the 8-4-4-4-12 form
    expect(len([p for p in f.patches if p[0] == "md"]) == 1, "all holes are filled by ONE update_content call")
    md = page["md"]
    expect("手で作ると崩れる行を1コマンドで作る" in md and "誰のどんな困りごとを" not in md and "要件書の子ページを作る" not in md, "template text is gone")
    expect("- [ ] `scripts/create_row.py` を作る" in md and "`README.md` を読む" in md and "- カード t_1 で起票" in md, md)
    expect(page["props"]["ステータス"] == {"status": {"name": "未着手"}}, "the final status is the spec's (default 未着手)")
    expect(f.calls[-1][1].startswith("v1/pages/") and f.calls[-1][0] == "GET", "the last call reads the properties back")
    blocks_reads = [i for i, c in enumerate(f.calls) if "blocks" in c[1]]
    status_patch = [i for i, c in enumerate(f.calls) if c == ("PATCH", f"v1/pages/{pid}")]
    expect(blocks_reads and status_patch and status_patch[0] > max(blocks_reads), "the final state is set only after the read-back inspection")
    rc, out = _run_create_row(_TemplateNotion(), dict(_TASK_SPEC, status="キャンセル"))
    expect(rc == 0 and "CREATED task" in out, out)


@check
def create_row_read_back_ignores_notion_auto_links():
    """Notion auto-links a token that looks like a hostname (`ai.calls_per_session.dev` -> `session.dev`).
    A replacement that landed must not be reported as missing just because the page shows a link."""
    import create_row as cr
    upd = [("<td>指標</td>\n<td>x</td>", "<td>指標</td>\n<td>ai.calls_per_session.dev が悪化</td>")]
    linked = '<td>指標</td>\n<td>[ai.calls_per_session.dev](http://session.dev) が悪化</td>'
    expect(cr.confirm_updates(linked, upd) == [], "an auto-linked replacement counts as landed")
    expect(cr.confirm_updates('<td>指標</td>\n<td>ai.calls_per_session.dev が悪化</td>', upd) == [], "plain text counts as landed")
    expect(cr.confirm_updates("<td>指標</td>\n<td>別の文</td>", upd) != [], "a replacement that really is missing still fails")


@check
def create_row_makes_an_epic_and_holds_it_with_新規作成の停止_until_the_inspection_passed():
    f = _TemplateNotion()
    rc, out = _run_create_row(f, _EPIC_SPEC)
    pid, page = _last_page(f)
    expect(rc == 0 and out.startswith(f"CREATED epic {pid} "), (rc, out))
    post = f.posts[0]
    expect(post["template"]["template_id"] == f.EPIC_TPL and post["parent"] == {"data_source_id": C.REGISTRY_DS} and "markdown" not in post, post)
    p = post["properties"]
    expect(p["新規作成の停止"] == {"checkbox": True}, "the gate controller and the planner skip a held epic: no phase row or card from a half-filled page")
    expect(p["種別"] == {"select": {"name": "プロダクト開発"}} and "優先度" not in p and p["チャット"] == {"url": "https://discord.com/channels/1/2"}, p)
    expect("ステータス" not in p, "the epic status is the gate controller's, never written here")
    expect(page["props"]["新規作成の停止"] == {"checkbox": False}, page["props"])
    md = page["md"]
    expect("何を測るか" not in md and "<td>検査に失敗した行の割合</td>" in md and "<td>プロダクト開発</td>" in md, "purpose cells (種別 filled from type)")
    expect("- [ ]\n" not in md and md.count("- [ ] 打ち手") == 2, "both 候補 lines, no empty to_do")
    expect("<td>2026-11-10</td>" in md and "<td></td>" not in md and "- [リポジトリ](https://github.com/example-org/software-factory)" in md and "2026-11-09 重複確認の結果。" in md, md)
    rc, out = _run_create_row(_TemplateNotion(), dict(_EPIC_SPEC, stop_new=True, measurements=[]))
    expect(rc == 0, out)


@check
def create_row_rejects_a_wrong_spec_with_exit_2_before_it_touches_notion():
    bad = {
        "task without overview key": dict(_TASK_SPEC, overview={k: v for k, v in _TASK_SPEC["overview"].items() if k != "対象外"}),
        "task overview with a fifth row": dict(_TASK_SPEC, overview=dict(_TASK_SPEC["overview"], 備考="x")),
        "task unknown key": dict(_TASK_SPEC, titel="x"),
        "task without checklist": dict(_TASK_SPEC, checklist=[]),
        "task empty checklist item": dict(_TASK_SPEC, checklist=["a", " "]),
        "task bad status": dict(_TASK_SPEC, status="終わり"),
        "task bad epic": dict(_TASK_SPEC, epic="not an id"),
        "task multi-line cell": dict(_TASK_SPEC, overview=dict(_TASK_SPEC["overview"], 目的="a\nb")),
        "epic bad type": dict(_EPIC_SPEC, type="開発"),
        "epic candidate without the four words": dict(_EPIC_SPEC, candidates=["打ち手だけ"]),
        "epic purpose 種別 differs from type": dict(_EPIC_SPEC, purpose=dict(_EPIC_SPEC["purpose"], 種別="調査")),
        "epic purpose key missing": dict(_EPIC_SPEC, purpose={k: v for k, v in _EPIC_SPEC["purpose"].items() if k != "期限"}),
        "epic measurement with 3 cells": dict(_EPIC_SPEC, measurements=[["a", "b", "c"]]),
        "epic chat is not a URL": dict(_EPIC_SPEC, chat="あとで"),
        "epic stop_new is not a boolean": dict(_EPIC_SPEC, stop_new="yes"),
        "unknown kind": {"kind": "review"},
        "task without epic": {k: v for k, v in _TASK_SPEC.items() if k != "epic"},
        "task with epic null": dict(_TASK_SPEC, epic=None),
        "task L2 without epic": {k: v for k, v in dict(_TASK_SPEC, level="L2").items() if k != "epic"},
        "task bad level": dict(_TASK_SPEC, level="L4"),
        "task L1 with an epic (L1 makes its own)": dict(_TASK_SPEC, level="L1"),
        "epic bad level": dict(_EPIC_SPEC, level="L9"),
    }
    for what, spec in bad.items():
        f = _TemplateNotion()
        rc, out = _run_create_row(f, spec)
        expect(rc == 2 and out.startswith("ERROR spec: ") and not f.calls, f"{what}: rc={rc} out={out!r} calls={f.calls}")
    f = _TemplateNotion()
    rc, out = _run_create_row(f, None, raw="{not json")
    expect(rc == 2 and out.startswith("ERROR spec: ") and not f.calls, out)


@check
def create_row_stops_before_creating_when_the_template_is_missing_or_does_not_fit():
    f = _TemplateNotion()
    f.no_default = True
    rc, out = _run_create_row(f, _TASK_SPEC)
    expect(rc == 1 and "no default template" in out and not f.posts, (rc, out))
    f = _TemplateNotion()
    f.pages[f.TASK_TPL]["md"] = f.pages[f.TASK_TPL]["md"].replace("<td>対象外</td>", "<td>除外</td>")  # the DB's template lost a row of the 概要 table
    rc, out = _run_create_row(f, _TASK_SPEC)
    expect(rc == 1 and "対象外" in out and not f.posts, f"a template that does not have the rows creates nothing: {rc} {out}")


@check
def create_row_leaves_a_failed_row_in_its_waiting_state_and_prints_its_page_id():
    # the template body never shows up
    f = _TemplateNotion()
    f.hidden = 99
    rc, out = _run_create_row(f, _TASK_SPEC)
    pid, page = _last_page(f)
    expect(rc == 1 and f"page={pid}" in out and "did not appear" in out, (rc, out))
    expect(page["props"]["ステータス"] == {"status": {"name": "進行中"}} and not any(c[0] == "DELETE" for c in f.calls), "left waiting, never deleted")
    # Notion silently drops one replacement while the others land (measured): the confirmation catches it, in both kinds
    for spec, drop in ((_TASK_SPEC, 0), (_TASK_SPEC, 4), (_EPIC_SPEC, 3), (_EPIC_SPEC, 8)):
        f = _TemplateNotion()
        f.drop = {drop}
        rc, out = _run_create_row(f, spec)
        pid, page = _last_page(f)
        expect(rc == 1 and f"page={pid}" in out and ("did not land" in out or "still in the page" in out), f"drop {drop}: {rc} {out}")
        expect(page["props"].get("ステータス", {}).get("status", {}).get("name", "進行中") == "進行中" and page["props"].get("新規作成の停止", {"checkbox": True}) == {"checkbox": True}, "still waiting")
    # the update call times out but was applied: the read-back decides, the row is fine
    f = _TemplateNotion()
    f.patch_error_after_apply = True
    rc, out = _run_create_row(f, _TASK_SPEC)
    expect(rc == 0 and out.startswith("CREATED task"), (rc, out))
    # the update call times out and nothing was applied: failure with the page id
    f = _TemplateNotion()
    f.patch_error = True
    rc, out = _run_create_row(f, _TASK_SPEC)
    pid, _ = _last_page(f)
    expect(rc == 1 and f"page={pid}" in out, (rc, out))
    # a failed create: no page id, and a hint that the row may exist
    f = _TemplateNotion()
    orig = f.ntn
    f.ntn = lambda *a, **k: (_ for _ in ()).throw(C.PipelineError("ntn timeout after 60s")) if a[0] == "v1/pages" else orig(*a, **k)
    rc, out = _run_create_row(f, _TASK_SPEC)
    expect(rc == 1 and "page=" not in out and "may exist" in out, (rc, out))


@check
def create_row_inspection_finds_the_defects_of_a_hand_built_row():
    """The incident of 2026-11-09 (an epic built by hand: heading colors as span tags, a purpose table without column widths, report sub-headings
    without color) and a row made from the template's default checklist: every one of them must fail the read-back inspection."""
    import create_row as cr
    f = _TemplateNotion()
    rc, out = _run_create_row(f, _EPIC_SPEC)
    epic_pid, epic_page = _last_page(f)
    expect(rc == 0, out)
    rc, out = _run_create_row(f, _TASK_SPEC)
    task_pid, task_page = _last_page(f)
    expect(rc == 0, out)
    tpl = {"epic": f.pages[f.EPIC_TPL]["md"], "task": f.pages[f.TASK_TPL]["md"]}
    good = {"epic": (epic_pid, epic_page, cr.validate(_EPIC_SPEC)), "task": (task_pid, task_page, cr.validate(_TASK_SPEC))}

    def problems(kind, edit):
        pid, page, spec = good[kind]
        saved, saved_ntn = page["md"], C.ntn
        page["md"], f.children, C.ntn = edit(saved), {}, f.ntn
        try:
            return cr.inspect(spec, cr.read_back(pid), tpl[kind])
        finally:
            page["md"], f.children, C.ntn = saved, {}, saved_ntn
    expect(problems("epic", lambda m: m) == [] and problems("task", lambda m: m) == [], "the good rows pass")
    cases = [
        ("epic", lambda m: m.replace('# 目的 {color="yellow_bg"}', "# 目的"), "color"),
        ("epic", lambda m: m.replace('## 結果 {color="blue_bg"}', "## 結果"), "H2"),
        ("epic", lambda m: m.replace('## 確認した方法 {color="blue_bg"}\n', ""), "完了レポート H2"),
        ("epic", lambda m: re.sub(r'<table fit-page-width="true" header-row="true">\n<tr>\n<td>日付.*?</table>', "", m, flags=re.S), "table"),
        ("epic", lambda m: m.replace("<td>検査に失敗した行の割合</td>", "<td>何を測るか</td>"), "指標"),
        ("epic", lambda m: m.replace("- [ ] 打ち手A", "- [ ]\n- [ ] 打ち手A"), "empty to_do"),
        ("epic", lambda m: m.replace('# 管理ログ {color="yellow_bg"}', '# 管理ログ {color="yellow_bg"}\n# 余分な章 {color="yellow_bg"}'), "H1 chapters"),
        ("epic", lambda m: m.replace("2026-11-09 起票。", ""), "管理ログ"),
        ("task", lambda m: m.replace("- [ ] テストを足す", "- [ ] 要件書の子ページを作る\n- [ ] テストを足す"), "items are not the spec items"),
        ("task", lambda m: m.replace("<td>CREATED が出る。</td>", "<td>何がどうなっていれば完了か。確認する方法も書く</td>"), "完了条件"),
        ("task", lambda m: m.replace('# 作業メモ {color="yellow_bg"}', "# 作業メモ"), "color"),
        ("task", lambda m: m.replace("<td>対象外</td>", "<td>除外</td>"), "rows are"),
    ]
    for kind, edit, word in cases:
        got = problems(kind, edit)
        expect(got and any(word in g for g in got), f"{kind} / {word}: {got}")
    # a spec that carries the template's own placeholder text as a value leaves the template text in the row: the inspection refuses it
    f2 = _TemplateNotion()
    rc, out = _run_create_row(f2, dict(_TASK_SPEC, overview=dict(_TASK_SPEC["overview"], 目的="誰のどんな困りごとを、どう変えるか")))
    expect(rc == 1 and "still holds the template text" in out and "page=" in out, (rc, out))


@check
def create_row_tolerates_what_notion_rewrites_in_values_but_not_a_lost_replacement():
    import create_row as cr
    expect(cr.plain("a < b & c | d _g_ `x` [t](http://u)") == cr.plain("a \\< b & c \\| d *g* x t"), "escapes, emphasis and links are normalised on both sides")
    f = _TemplateNotion()
    f.italics = True  # the live API stores _g_ as *g* (measured 2026-11-09)
    rc, out = _run_create_row(f, _TASK_SPEC)
    expect(rc == 0, out)
    expect(cr.protect_names("scripts/create_row.py と README.md を読む。`x.py` は守る。[a.md](https://e.com/b.md) https://e.com/c.py も。日本語create_row.pyの後ろ")
           == "`scripts/create_row.py` と `README.md` を読む。`x.py` は守る。[a.md](https://e.com/b.md) https://e.com/c.py も。日本語`create_row.py`の後ろ", "names are wrapped, code spans / links / URLs are not")


@check
def create_row_dry_run_writes_nothing_and_prints_the_plan():
    for spec in (_TASK_SPEC, _EPIC_SPEC):
        f = _TemplateNotion()
        rc, out = _run_create_row(f, spec, "--dry-run")
        expect(rc == 0 and out.startswith("DRY-RUN "), (rc, out))
        plan = json.loads(out[len("DRY-RUN "):])
        expect(not f.posts and not f.patches and all(c[0] == "GET" for c in f.calls), f.calls)
        expect(plan["post"]["template"]["template_id"] in (f.TASK_TPL, f.EPIC_TPL) and len(plan["updates"]) >= 6, plan["updates"])
        expect(all(u["old"] in f.pages[plan["post"]["template"]["template_id"]]["md"] for u in plan["updates"]), "every replacement hits the template body")


_L1_SPEC = {k: v for k, v in dict(_TASK_SPEC, level="L1").items() if k != "epic"}


@check
def create_row_refuses_a_task_without_an_epic_and_says_to_use_level_L1():
    f = _TemplateNotion()
    rc, out = _run_create_row(f, {k: v for k, v in _TASK_SPEC.items() if k != "epic"})
    expect(rc == 2 and out.startswith("ERROR spec: ") and "エピック配下" in out and "--level L1" in out, (rc, out))
    expect(not f.calls, "nothing is read or written: " + str(f.calls))
    # the epic id is there but no such row in Notion: also exit 2 before anything is created
    f = _TemplateNotion()
    f.no_epic = {_id(5)}
    rc, out = _run_create_row(f, _TASK_SPEC)
    expect(rc == 2 and "ERROR spec: " in out and not f.posts and not f.patches, (rc, out, f.posts))


@check
def create_row_level_L1_makes_one_epic_and_one_task_in_one_call():
    f = _TemplateNotion()
    rc, out = _run_create_row(f, _L1_SPEC)
    expect(rc == 0 and len(f.posts) == 2, (rc, out, len(f.posts)))
    epic_post, task_post = f.posts
    eid, tid = _id(0x801), _id(0x802)
    m = re.fullmatch(r"CREATED epic (\S+) (\S+) task (\S+) (\S+)\n", out)
    expect(m and m.group(1) == eid and m.group(3) == tid and m.group(2).startswith("https://") and m.group(4).startswith("https://"), out)
    ep = epic_post["properties"]
    expect(epic_post["parent"] == {"data_source_id": C.REGISTRY_DS} and epic_post["template"]["template_id"] == f.EPIC_TPL, epic_post)
    expect(ep["名前"]["title"][0]["text"]["content"] == _L1_SPEC["title"] and ep["種別"] == {"select": {"name": "プロダクト開発"}} and ep["レベル"] == {"select": {"name": "L1"}}, ep)
    expect("ステータス" not in ep, "the epic status is the gate controller's (the DB has no 進行中 for epics)")
    expect(f.pages[eid]["props"]["新規作成の停止"] == {"checkbox": False}, "an L1 epic is not held by the flag: levels.toml says it has no phase rows (レベル L1 is read by the gate controller and the planner)")
    md = f.pages[eid]["md"]
    expect(_L1_SPEC["overview"]["完了条件"] in md.replace("\\", "") and "何を測るか" not in md and "- [ ]\n" not in md, "purpose: 目的 and 完了条件 of the task, no template text")
    expect(task_post["parent"] == {"data_source_id": C.TASK_DS} and task_post["properties"]["エピック"] == {"relation": [{"id": eid}]}, task_post)
    expect(f.pages[tid]["props"]["ステータス"] == {"status": {"name": "未着手"}}, "the task ends in the spec status")
    expect(not any(c[0] == "DELETE" for c in f.calls), "nothing is deleted")


@check
def create_row_level_L1_dry_run_plans_two_rows_and_writes_nothing():
    f = _TemplateNotion()
    rc, out = _run_create_row(f, _L1_SPEC, "--dry-run")
    expect(rc == 0 and out.startswith("DRY-RUN "), (rc, out))
    plan = json.loads(out[len("DRY-RUN "):])
    expect(not f.posts and not f.patches and all(c[0] == "GET" for c in f.calls), f.calls)
    expect(plan["plan"] == "L1: エピック1行＋作業1行" and plan["epic"]["post"]["parent"] == {"data_source_id": C.REGISTRY_DS}
           and plan["task"]["post"]["parent"] == {"data_source_id": C.TASK_DS} and plan["epic"]["updates"] and plan["task"]["updates"], plan)
    # the level also comes from the command line (--level L1); both together must agree
    rc, out = _run_create_row(_TemplateNotion(), {k: v for k, v in _TASK_SPEC.items() if k != "epic"}, "--dry-run", "--level", "L1")
    expect(rc == 0 and '"plan": "L1' in out, (rc, out))
    rc, out = _run_create_row(_TemplateNotion(), dict(_L1_SPEC, level="L2"), "--dry-run", "--level", "L1")
    expect(rc == 2 and "level" in out, (rc, out))


@check
def create_row_level_L1_failure_keeps_what_exists_and_names_it():
    # the epic is made but the task page cannot be: exit 1, the epic id is printed, nothing is deleted, no second epic on a retry hint
    f = _TemplateNotion()
    orig = f.ntn
    f.ntn = lambda *a, **k: (_ for _ in ()).throw(C.PipelineError("ntn timeout after 60s")) if a[0] == "v1/pages" and a[-1] == "POST" and f.posts else orig(*a, **k)
    rc, out = _run_create_row(f, _L1_SPEC)
    eid = _id(0x801)
    expect(rc == 1 and f"page={eid}" in out and "epic" in out and "task" in out and len(f.posts) == 1 and not any(c[0] == "DELETE" for c in f.calls), (rc, out))
    # the task page exists but fails its inspection: both ids are in the message
    f = _TemplateNotion()
    f.drop = {0}
    orig = f.ntn
    n = {"posts": 0}

    def ntn(*a, **k):  # only the task page (the second POST) loses a replacement
        if a[0] == "v1/pages" and a[-1] == "POST":
            n["posts"] += 1
            f.drop = {0} if n["posts"] == 2 else set()
        return orig(*a, **k)
    f.ntn = ntn
    rc, out = _run_create_row(f, _L1_SPEC)
    expect(rc == 1 and f"page={_id(0x802)}" in out and f"epic={_id(0x801)}" in out, (rc, out))
    expect(f.pages[_id(0x801)]["props"]["新規作成の停止"] == {"checkbox": False} and f.pages[_id(0x802)]["props"]["ステータス"] == {"status": {"name": "進行中"}}, "the epic is complete, the task stays waiting")


def _orphan_world(rows):
    """Fake Notion for check_orphan_tasks: the タスク DS query. Returns what `rows` holds whatever the filter says (the script must not trust the filter alone)."""
    sent = []

    def ntn(*a, data=None, **k):
        sent.append((a, data))
        if "/query" in a[0]:
            return {"results": rows, "has_more": False}
        raise AssertionError(a)
    return ntn, sent


def _task_row(n, status, epics, title="t"):
    return {"id": _id(0xA00 + n), "url": f"https://app.notion.com/p/{_id(0xA00 + n).replace('-', '')}",
            "properties": {"タスク名": {"title": [{"plain_text": f"{title}{n}"}]}, "ステータス": {"status": {"name": status}},
                           C.TASK_EPIC_PROP: {"relation": [{"id": e} for e in epics]}}}


def _run_orphans(rows):
    import check_orphan_tasks as co
    ntn, sent = _orphan_world(rows)
    saved = C.ntn
    C.ntn = ntn
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = co.main([])
    finally:
        C.ntn = saved
    return rc, out.getvalue(), sent


@check
def check_orphan_tasks_lists_only_open_tasks_without_an_epic():
    rows = [_task_row(1, "未着手", []), _task_row(2, "進行中", []), _task_row(3, "レビュー中", []),
            _task_row(4, "完了", []), _task_row(5, "キャンセル", []),      # closed rows with no epic (the 12 old ones) are not reported
            _task_row(6, "進行中", [_id(7)])]                                 # open, has an epic
    rc, out, sent = _run_orphans(rows)
    lines = [l for l in out.splitlines() if l.startswith("ORPHAN ")]
    expect(rc == 1 and len(lines) == 3 and all(f"t{n}" in l for l, n in zip(lines, (1, 2, 3))), (rc, out))
    expect("t4" not in out and "t5" not in out and "t6" not in out and "https://app.notion.com/p/" in lines[0], out)
    expect(all(a[0].endswith("/query") for a, _ in sent) and not any("PATCH" in a or "POST" in a and not a[0].endswith("/query") for a, _ in sent), "reads only")
    flt = json.dumps(sent[0][1]["filter"], ensure_ascii=False)
    expect(C.TASK_EPIC_PROP in flt and "is_empty" in flt and "完了" not in flt, flt)
    rc, out, _ = _run_orphans([_task_row(4, "完了", []), _task_row(5, "キャンセル", []), _task_row(6, "未着手", [_id(7)])])
    expect(rc == 0 and "ORPHAN" not in out and "0" in out, (rc, out))
    rc, out, _ = _run_orphans([])
    expect(rc == 0, (rc, out))


# ---------------------------------------------------------------- レビュー中 task <-> review row pairs (check_review_pairs.py / request_review.py)

def _pair_task(n, status="レビュー中", title=None, epic=True):
    r = _task_row(n, status, [_id(7)] if epic else [], title or "t")
    return r


def _pair_review(task_id, status, suffix="", dashed=False):
    key = "task-" + (task_id if dashed else task_id.replace("-", "")) + suffix
    return _review_page(1, status, key=key)


def _run_pairs(tasks, reviews, fail=False):
    import check_review_pairs as cp
    sent = []

    def ntn(*a, data=None, **k):
        sent.append((a, data))
        if fail:
            raise C.PipelineError("notion down")
        rows = tasks if C.TASK_DS in a[0] else reviews if C.REVIEW_DS in a[0] else None
        if rows is None or not a[0].endswith("/query"):
            raise AssertionError(a)
        return {"results": rows, "has_more": False}
    saved, out = C.ntn, io.StringIO()
    C.ntn = ntn
    try:
        with contextlib.redirect_stdout(out):
            rc = cp.main([])
    finally:
        C.ntn = saved
    return rc, out.getvalue(), sent


@check
def check_review_pairs_lists_レビュー中_tasks_without_a_pending_review_row():
    import check_review_pairs as cp
    a, b, c, d, e, f = (_pair_task(n, title=f"t{n}") for n in range(1, 7))
    reviews = [_pair_review(b["id"], "レビュー待ち"),                         # asked: not reported
               _pair_review(c["id"], "取り下げ"), _pair_review(d["id"], "差し戻し"),  # withdrawn / sent back but the task is still レビュー中: STALE
               _pair_review(e["id"], "承認"),                               # approved but the task is still レビュー中: STALE
               _pair_review(f["id"], "差し戻し"), _pair_review(f["id"], "レビュー待ち", "-r2", dashed=True)]  # re-asked (-r2, dashed id): asked
    missing, stale = cp.find_pairs([a, b, c, d, e, f, _pair_task(7, "進行中"), _pair_task(8, "完了")], reviews)
    expect([m[0] for m in missing] == [a["id"]] and [s[0] for s in stale] == [c["id"], d["id"], e["id"]], (missing, stale))
    expect(cp.task_key(a["id"]) == "task-" + a["id"].replace("-", "") and len(cp.task_key(a["id"])) == 37, cp.task_key(a["id"]))
    i32 = a["id"].replace("-", "")
    expect(cp.key_task_id(f"task-{i32}") == cp.key_task_id(f"TASK-{a['id']}-r3") == i32, "hyphens, case and -rN do not matter")
    expect(cp.key_task_id("task-1234") is None and cp.key_task_id(f"{i32[:8]}-承認") is None and cp.key_task_id("") is None, "other keys are not task keys")
    phase = _task_row(9, "レビュー中", [_id(7)], "")
    phase["properties"]["タスク名"] = {"title": [{"plain_text": "設計"}]}
    expect(cp.find_pairs([phase], []) == ([], []), "phase rows are reviewed by the AI reviewer, not asked of a person")


@check
def check_review_pairs_prints_MISSING_and_STALE_lines_and_exits_by_the_count_and_never_writes():
    a, e = _pair_task(1, title="落ちた"), _pair_task(5, title="承認済み")
    w = _pair_task(3, title="取り下げ済み")
    rc, out, sent = _run_pairs([a, e, w], [_pair_review(e["id"], "承認"), _pair_review(w["id"], "取り下げ")])
    expect(rc == 1 and f"MISSING {a['id']} 落ちた1 https://app.notion.com/p/" in out and f"STALE {e['id']} 承認済み5 " in out and f"STALE {w['id']} 取り下げ済み3 " in out, (rc, out))
    expect(out.splitlines()[-2:] == ["review pairs: 1 missing", "review pairs: 2 stale"], out)
    expect(all(x[0][0].endswith("/query") for x in sent), "reads only")
    rc, out, _ = _run_pairs([_pair_task(2)], [_pair_review(_pair_task(2)["id"], "レビュー待ち")])
    expect(rc == 0 and out == "review pairs: 0 missing\n", (rc, out))
    rc, out, _ = _run_pairs([], [], fail=True)
    expect(rc == 2 and out.startswith("ERROR "), (rc, out))


class _ApplyWorld:
    """Fake Notion for apply_review_decisions.py: the レビュー rows, the task pages, and every write in order."""

    def __init__(self, tasks, reviews):
        self.tasks, self.reviews, self.log = {t["id"]: t for t in tasks}, reviews, []

    def ntn(self, *a, data=None, **k):
        method = a[a.index("-X") + 1] if "-X" in a else "GET"
        if a[0].endswith("/query"):
            assert C.REVIEW_DS in a[0], a
            return {"results": self.reviews, "has_more": False}
        pid = a[0].rsplit("/", 1)[1]
        if method == "GET":
            return next(t for i, t in self.tasks.items() if i.replace("-", "") == pid.replace("-", ""))
        self.log.append((pid, data["properties"]))
        return {}


def _run_apply(w, *args):
    import apply_review_decisions as ar
    saved, out = C.ntn, io.StringIO()
    C.ntn = w.ntn
    try:
        with contextlib.redirect_stdout(out):
            rc = ar.main(list(args))
    finally:
        C.ntn = saved
    return rc, out.getvalue()


@check
def apply_review_decisions_completes_approved_tasks_once_and_only_with_apply():
    t1, t2, t3, t4 = (_pair_task(n, title=f"t{n}") for n in (1, 2, 3, 4))
    r1 = _pair_review(t1["id"], "承認", dashed=True)           # URL-style id with hyphens still hits
    r2 = _pair_review(t2["id"], "承認")
    r2["properties"]["反映済み"] = {"checkbox": True}            # already applied: nothing
    r3 = _pair_review(t3["id"], "差し戻し")                      # a person's call: untouched
    r4 = _pair_review(t4["id"], "取り下げ")
    r1["id"] = _id(11)
    w = _ApplyWorld([t1, t2, t3, t4], [r1, r2, r3, r4])
    rc, out = _run_apply(w)
    expect(rc == 0 and w.log == [] and f"WOULD APPLY {t1['id']} t11" in out and "t22" not in out, ("dry run writes nothing", rc, out, w.log))
    rc, out = _run_apply(w, "--apply")
    expect(out.splitlines()[0] == f"APPLIED {t1['id']} t11" and len(out.splitlines()) == 1, out)
    expect(w.log == [(t1["id"].replace("-", ""), {"ステータス": {"status": {"name": "完了"}}}), (_id(11), {"反映済み": {"checkbox": True}})], w.log)
    w2 = _ApplyWorld([_pair_task(1, "完了", title="t")], [_pair_review(_pair_task(1)["id"], "承認")])
    rc, out = _run_apply(w2, "--apply")
    expect([x[1] for x in w2.log] == [{"反映済み": {"checkbox": True}}] and "APPLIED" not in out, ("task already 完了: only the row is marked", w2.log, out))
    w3 = _ApplyWorld([_pair_task(1, "進行中", title="t")], [_pair_review(_pair_task(1)["id"], "承認")])
    rc, out = _run_apply(w3, "--apply")
    expect(w3.log == [] and rc == 0, ("a task that is neither レビュー中 nor 完了 is left alone", w3.log))


class _RequestWorld:
    """Fake Notion for request_review.py: one task page, the レビュー DB (query by キー / create), and the order of every write."""

    def __init__(self, status="進行中", rows=(), fail_create=False):
        self.task = _task_row(1, status, [_id(7)], "仕上げ")
        self.keys, self.log, self.fail_create = set(rows), [], fail_create

    def ntn(self, *a, data=None, **k):
        method = a[a.index("-X") + 1] if "-X" in a else "GET"
        if a[0].endswith("/query"):
            return {"results": [{"id": _id(2)} for kk in self.keys if kk == data["filter"]["rich_text"]["equals"]], "has_more": False}
        mine = a[0].replace("-", "") == "v1/pages/" + self.task["id"].replace("-", "")
        if mine and method == "GET":
            return self.task
        if mine and method == "PATCH":
            self.log.append(("PATCH", data["properties"]["ステータス"]["status"]["name"]))
            self.task["properties"]["ステータス"] = _status(data["properties"]["ステータス"]["status"]["name"])
            return self.task
        if a[0] == "v1/pages":
            if self.fail_create:
                raise C.PipelineError("create failed")
            self.log.append(("CREATE", data["properties"]["キー"]["rich_text"][0]["text"]["content"], data["markdown"]))
            self.keys.add(self.log[-1][1])
            return {"id": _id(900), "url": "https://www.notion.so/row"}
        if a[0] == f"v1/pages/{_id(7)}":
            return {"properties": {}}
        raise AssertionError(a)


def _run_request(w, args):
    import request_review as rr
    import ask_human as ah
    saved = (C.ntn, ah.notify, C.STATE_DIR)
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        C.STATE_DIR = Path(tmp)
        C.ntn, ah.notify = w.ntn, lambda *a, **k: w.log.append(("NOTIFY",))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = rr.main([w.task["url"], "--recommend", "承認", "--check", "画面を見る", "--why", "テスト緑", *args])
        finally:
            C.ntn, ah.notify, C.STATE_DIR = saved
    return rc, out.getvalue()


@check
def request_review_creates_the_row_first_then_sets_レビュー中_and_is_idempotent():
    import check_review_pairs as cp
    w = _RequestWorld()
    rc, out = _run_request(w, [])
    key = cp.task_key(w.task["id"])
    expect(rc == 0 and [x[0] for x in w.log] == ["CREATE", "NOTIFY", "PATCH"] and w.log[0][1] == key and w.log[2] == ("PATCH", "レビュー中"), (rc, w.log, out))
    for h in ("# 判断すること", "# 推奨", "# 人が確かめること", "# 根拠"):
        expect(h in w.log[0][2], (h, w.log[0][2]))
    expect("画面を見る" in w.log[0][2] and "テスト緑" in w.log[0][2] and "仕上げ" in w.log[0][2], w.log[0][2])
    n = len(w.log)
    rc, out = _run_request(w, [])  # again: the row exists, the state already matches
    expect(rc == 0 and len(w.log) == n and "既にある" in out, (rc, w.log, out))
    w2 = _RequestWorld(rows=[cp.task_key(_id(0xA01))])  # the row exists, the task is not レビュー中 yet: only the state is set
    rc, out = _run_request(w2, [])
    expect(rc == 0 and w2.log == [("PATCH", "レビュー中")] and "既にある" in out, (rc, w2.log, out))


@check
def request_review_changes_nothing_when_the_row_cannot_be_made_or_on_dry_run():
    w = _RequestWorld(fail_create=True)
    rc, out = _run_request(w, [])
    expect(rc == 1 and w.log == [] and w.task["properties"]["ステータス"] == _status("進行中"), (rc, w.log, out))
    w = _RequestWorld()
    rc, out = _run_request(w, ["--dry-run"])
    expect(rc == 0 and w.log == [] and "task-" in out and "仕上げ" in out and "# 判断すること" in out, (rc, w.log, out))
    w = _RequestWorld(status="完了")
    rc, out = _run_request(w, [])
    expect(rc == 1 and w.log == [], (rc, w.log, out))


@check
def create_row_is_deployed_compiled_and_the_hand_built_procedures_are_gone():
    root = HERE.parent
    expect("create_row.py" in PIPELINE_PY, "compile check list")
    sd = (root / "tools" / "safe_deploy.sh") if (root / "tools" / "safe_deploy.sh").exists() else None
    if sd is None or not (root / "Makefile").exists():  # run from ~/.hermes/scripts (deploy gate): only the repo has tools/, skills/, prompts/
        return
    text = sd.read_text(encoding="utf-8")
    files = re.search(r'DEFAULT_FILES="([^"]*)"', text).group(1).split()
    expect({"create_row.py", "tests/notion_template_task.md", "tests/notion_template_epic.md"} <= set(files), "safe_deploy.sh must deploy create_row.py and its fixtures")
    for name in ("create_row.py", "tests/notion_template_task.md", "tests/notion_template_epic.md"):
        expect((HERE / name).is_file(), f"missing: {name}")
    # the instructions point at the script; none of them teaches the hand-built way (a template_id POST, notion_create_pages with a template) any more
    must_name = ["skills/autonomous-task-execution/SKILL.md", "skills/autonomous-task-execution/references/epic-creation-from-request.md",
                 "skills/product-development-process/SKILL.md", "skills/product-development-process/references/notion-api-notes.md",
                 "skills/software-factory-ops/SKILL.md", "prompts/planner.txt", "prompts/intake.txt", "notion/templates/README.md"]
    for n in must_name:
        expect("create_row.py" in (root / n).read_text(encoding="utf-8"), f"{n} must send the reader to create_row.py")
    old = [r"cannot apply the DB's default template", r"cannot apply the DB's template", r"API では既定テンプレートを適用できない",
           r"スクリプトができるまでは", r"notion_create_pages with parent data_source_id", r"with template_id \(no content together"]
    hits = [f"{p.relative_to(root)}: {rx}" for p in sorted((root / "skills").glob("**/*.md")) + sorted((root / "prompts").glob("*.txt")) + [q for q in sorted((root / "scripts").glob("*.py")) if q.name != "epic_selftest.py"]
            + sorted((root / "hermes").glob("**/*")) + [root / "notion" / "templates" / "README.md"] if p.is_file()
            for rx in old if re.search(rx, p.read_text(encoding="utf-8"))]
    expect(not hits, f"outdated hand-built procedure left: {hits}")


# ---------------------------------------------------------------- checks: UI evidence of the release ask

def _img(i, caption="", kind="file"):
    return {"id": _id(0x9000 + i), "type": "image", "has_children": False,
            "image": {"type": kind, kind: {"url": f"https://files.example.test/s/{i}/shot{i}.png?X-Amz-Expires=3600"},
                      "caption": [{"plain_text": caption}] if caption else []}}


def _hb(kind, text, i):
    """A block with an id (the controller inserts after it)."""
    b = _block(kind, text)
    b["id"] = _id(0x8000 + i)
    return b


def _hub_page_blocks():
    return [_hb("heading_1", "目的", 1), _hb("paragraph", "p", 2), _hb("heading_1", "候補", 3), _hb("to_do", "打ち手A", 4),
            _hb("heading_1", "計測ログ", 5), _hb("heading_1", "管理ログ", 6), _hb("paragraph", "2026-11-09 起票", 7),
            _hb("heading_1", "関連ドキュメント", 8), _hb("heading_1", "完了レポート", 9)]


_DECISION_MD = ("# 1. 判定の案 {color=\"yellow_bg\"}\n- 推奨：条件つきでリリースしてよい\n- 対象：example-org/example-app、ブランチ michiomochi/example-app-text-search\n"
                "<unknown url=\"x\" alt=\"copy_indicator\"/>\n## 変更前後のUI（2026-11-09 実測）\n![変更前（main）375px 幅](https://files.example.test/s/1/shot1.png?sig=1)\n"
                "![変更後（a566497）375px 幅](https://files.example.test/s/2/shot2.png?sig=2)\n### 細目\n#### 4段目")


def _add_decision(w, hub, n, images, edited="2026-11-09T12:00:00.000Z", markdown=_DECISION_MD, text="対象：example-org/example-app、ブランチ michiomochi/example-app-text-search"):
    """A リリース判定書 child page under the epic's リリース判定 row. Returns its page id."""
    pid = _id(0x7000 + n)
    rel = w.row(hub, "リリース判定")
    w.row_blocks[rel["id"]] = w.row_blocks[rel["id"]] + [{"id": pid, "type": "child_page", "child_page": {"title": f"リリース判定書の草稿：案件{n}"}},
                                                         {"id": _id(0x7100 + n), "type": "child_page", "child_page": {"title": "PR 本文の下書き"}}]
    w.pages[pid] = {"id": pid, "last_edited_time": edited, "properties": {}}
    w.decision_pages[pid] = dict(blocks=[_hb("heading_1", "1. 判定の案", 0x100 + n), _hb("bulleted_list_item", text, 0x200 + n),
                                         _hb("heading_2", "変更前後のUI（2026-11-09 実測）", 0x300 + n)] + images,
                                 markdown=markdown)
    return pid


@contextlib.contextmanager
def _fake_repo(paths_of):
    """Repositories root with example-org/example-app and a fake diff: paths_of[branch] -> changed paths (None = diff fails)."""
    saved = (C.repos_root, C.ui_changed_paths)
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "example-org" / "example-app" / ".git").mkdir(parents=True)
        calls = []

        def diff(repo_dir, branch):
            calls.append((str(repo_dir), branch))
            p = paths_of.get(branch)
            return (list(p), None) if p is not None else (None, f"{branch} の差分を取れませんでした（simulated）")
        C.repos_root, C.ui_changed_paths = (lambda: Path(root)), diff
        try:
            yield calls
        finally:
            C.repos_root, C.ui_changed_paths = saved


@check
def ui_evidence_verdict_decides_the_ui_change_and_counts_before_and_after_images():
    V = C.ui_evidence_verdict
    both = [_hb("heading_2", "変更前後のUI", 1), _img(1, "変更前（main）"), _img(2, "変更後（branch）")]
    v = V(["pages/livechat/search.tsx", "src/livechat/ui/Card.tsx", "src/x.ts"], both)
    expect(v["ui"] is True and v["basis"] == "diff" and v["ok"] and (v["before"], v["after"]) == (1, 1), v)
    expect(v["ui_paths"] == ["pages/livechat/search.tsx", "src/livechat/ui/Card.tsx"] and "ほか 1 件" in v["reason"], v)
    v = V(["pages/a.tsx"], both[:2])
    expect(not v["ok"] and "変更後の画像がありません" in v["reason"] and "変更前 1 枚、変更後 0 枚" in v["reason"], v)
    v = V(["pages/a.tsx"], [])
    expect(not v["ok"] and "変更前・変更後の画像がありません" in v["reason"], v)
    v = V(["scripts/epic_common.py"], [])
    expect(v["ui"] is False and v["ok"] and v["basis"] == "diff", "no UI path in the diff: not a UI change")
    v = V(["src/livechat/uix.ts", "app/pages/a.tsx"], [])
    expect(v["ui"] is False, "only the prefixes count (src/livechat/uix, a nested pages/ do not)")
    # no diff: the declaration decides; a table row 'UI 変更：あり' counts; images by the nearest heading when the caption says nothing
    decl = [{"type": "table_row", "table_row": {"cells": [[{"plain_text": "UI 変更"}], [{"plain_text": "あり"}]]}},
            _hb("heading_3", "変更前の画面", 2), _img(3), _hb("heading_3", "変更後の画面", 3), _img(4), _img(5)]
    v = V(None, decl)
    expect(v["ui"] is True and v["basis"] == "declared" and (v["before"], v["after"]) == (1, 2) and v["ok"], v)
    v = V(None, [_block("paragraph", "UI 変更：なし")])
    expect(v["ui"] is False and v["basis"] == "declared" and v["ok"], v)
    v = V(["scripts/a.py"], [_block("paragraph", "UI 変更：あり")])
    expect(v["ui"] is True and v["basis"] == "declared" and not v["ok"], "'あり' adds a UI change the paths do not catch")
    v = V(["pages/a.tsx"], [_block("paragraph", "UI 変更：なし")])
    expect(v["ui"] is True and v["basis"] == "diff", "'なし' never overrides a UI path of the diff")
    v = V(None, [], diff_error="simulated")
    expect(v["ui"] is None and v["ok"] and "判定できず" in v["reason"] and "simulated" in v["reason"], "unknown: never held, the reason says so")
    v = V(["pages/a.tsx"], [_hb("heading_2", "変更前後のUI", 1), _img(1), _img(2)])
    expect(not v["ok"] and (v["before"], v["after"]) == (0, 0), "a heading naming both (変更前後) does not count a caption-less image")
    expect(V(None, both[:2], assume_ui=True)["ok"] is False and V(None, both, assume_ui=True)["ok"] is True, "review row check")
    expect("UI 変更なら変更前後" not in "" and V(None, [_block("paragraph", "UI 変更なら変更前後の画像を入れる")])["declared"] is None,
           "a sentence about UI changes is not a declaration")
    # the shape of the real decision page that started the epic (example-app search, 2026-11-09): 2 before + 3 after images
    real = [_hb("heading_1", "1. 判定の案", 1), _hb("heading_2", "変更前後のUI（2026-11-09 実測）", 2), _img(1, "変更前（main 0731313）375px 幅"),
            _img(2, "変更前（main 0731313）1280px 幅"), _img(3, "変更後（a566497）375px 幅"), _img(4, "変更後（a566497）1280px 幅"),
            _img(5, "変更後：「待ってます」で検索")]
    v = V(["pages/livechat/search.tsx", "pages/livechat/search.server.ts", "src/livechat/ui/PerformerCard.tsx"], real)
    expect(v["ok"] and (v["before"], v["after"]) == (2, 3), v)
    expect(not V(["pages/livechat/search.tsx"], real[:2])["ok"], "the same page before the images were added is held")


@check
def repo_and_branch_come_from_the_page_text_and_only_existing_repositories():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "example-org" / "example-app" / ".git").mkdir(parents=True)
        texts = ["対象：example-org/example-app、ブランチ michiomochi/example-app-text-search、コミット a566497"]
        expect(C.repo_and_branch(texts, root) == ("example-org/example-app", "michiomochi/example-app-text-search"), C.repo_and_branch(texts, root))
        expect(C.repo_and_branch(["https://github.com/example-org/example-app だけ"], root) == (None, None), "URL paths are not read as owner/repo")
        expect(C.repo_and_branch(["ブランチ michiomochi/x", "example-org/example-app（ghq）"], root) == ("example-org/example-app", "michiomochi/x"), "texts in order")
        expect(C.repo_and_branch(["example-org/unknown michiomochi/y"], root) == (None, "michiomochi/y"), "a repository that is not cloned is not used")
    with sandbox() as (ctl, tmp):
        C._sh = _Seq([(0, "", ""), (0, "origin/main\n", ""), (0, "pages/a.tsx\nsrc/b.ts\n", "")])
        paths, err = C.ui_changed_paths("/r", "michiomochi/x")
        expect(paths == ["pages/a.tsx", "src/b.ts"] and err is None, (paths, err))
        expect(C._sh.calls[2][-2:] == ["--name-only", "origin/main...origin/michiomochi/x"], C._sh.calls)
        C._sh = _Seq([(128, "", "fatal: couldn't find remote ref")])
        paths, err = C.ui_changed_paths("/r", "michiomochi/gone")
        expect(paths is None and "差分を取れませんでした" in err, "a failing git is a reason, never an exception")
        C._sh = _Seq([subprocess.TimeoutExpired("git", 30)])
        expect(C.ui_changed_paths("/r", "b")[0] is None, "timeout")


def _ui_world():
    """Three epics whose リリース判定 rows are 完了: 31 = UI change with only the 変更前 image, 32 = UI change with both, 33 = no UI change."""
    w = World()
    rows = [(ph, "完了") for ph in C.ORDER]
    out = {}
    for n, imgs, branch in ((31, [_img(1, "変更前（main）")], "michiomochi/ui-a"),
                            (32, [_img(1, "変更前（main）"), _img(2, "変更後（a566497）")], "michiomochi/ui-b"),
                            (33, [], "michiomochi/plain")):
        hub, eid = w.add_epic(n, rows=rows, hub_status="承認待ち")
        w.hub_blocks[hub["id"]] = _hub_page_blocks()
        pid = _add_decision(w, hub, n, imgs, text=f"対象：example-org/example-app、ブランチ {branch}",
                            markdown=_DECISION_MD if imgs else "# 1. 判定の案 {color=\"yellow_bg\"}\n- UI は変えない")
        w.board.append(dict(id=f"t_rel{n}", status="done", assignee="executor", tenant=C.tenant_of(eid),
                            key=C.merged_phase_key(eid, ["設計", "実装", "テスト", "リリース判定"]), title=f"[設計] ハブ{n}"))
        out[n] = (hub, eid, pid)
    paths = {"michiomochi/ui-a": ["pages/livechat/search.tsx"], "michiomochi/ui-b": ["src/livechat/ui/Card.tsx"], "michiomochi/plain": ["scripts/a.py"]}
    return w, out, paths


@check
def release_ask_is_held_without_ui_images_and_asked_with_them_and_a_non_ui_change_is_asked():
    w, ep, paths = _ui_world()
    (hub31, e31, p31), (hub32, e32, p32), (hub33, e33, p33) = ep[31], ep[32], ep[33]
    k31, k32, k33 = (f"{e[-8:]}-承認" for e in (e31, e32, e33))
    with sandbox(w) as (ctl, tmp), _fake_repo(paths) as diffs:
        lines, errors = ctl.run(False)
        expect(not errors and any(l.startswith(f"WOULD HOLD {k31}: UI 変更の判定：あり") for l in lines)
               and f"WOULD ASK {k32}" in lines and f"WOULD ASK {k33}" in lines and w.asks == [] and not w.notes, f"dry run: {lines}")
        lines, errors = ctl.run(True)
        expect(not errors, lines)
        asked = {a[0]: a[1] for a in w.asks}
        # 1. image missing -> no ask; 管理ログ line, 候補 item, card comment, HELD line, hold state
        expect(k31 not in asked, f"UI change without the 変更後 image is not asked: {list(asked)}")
        held = [l for l in lines if l.startswith(f"HELD {k31}: ")]
        expect(len(held) == 1 and "変更後の画像がありません" in held[0], lines)
        notes = [(c[2], c[3]["after_block"]["id"]) for c in w.calls if c[0] == "note" and c[1] == hub31["id"]]
        expect(notes == [("paragraph", _id(0x8007)), ("to_do", _id(0x8004))], f"管理ログ after its last line, 候補 after its last item: {notes}")
        texts = [t for pid, t in w.notes if pid == hub31["id"]]
        expect("ゲート制御：リリース承認の依頼を止めました" in texts[0] and texts[1].startswith("打ち手：リリース判定書の草稿に変更前・変更後の画像を入れる")
               and "開始できるフェーズ：リリース判定" in texts[1] and "判断の宣言：" in texts[1] and "感度の見積もり：" in texts[1], texts)
        expect(w.comments == [("t_rel31", texts[0])], f"the reason is left on the epic's リリース判定 card: {w.comments}")
        hold = json.loads(C.state_path(C.UI_HOLD_FILE).read_text(encoding="utf-8"))
        expect(set(hold) == {k31} and hold[k31]["edited"] == "2026-11-09T12:00:00.000Z", hold)
        # 2. images present -> asked with the copy and the images; 3. not a UI change -> asked (no images needed)
        expect(asked[k32] == hub32["id"] and asked[k33] == hub33["id"], f"the two epics that may be asked are asked through ask_human.py --release: {asked}")
        # next runs: the held key is not re-checked (no git, no new notes) until the decision page changes
        n_notes, n_diffs = len(w.notes), len(diffs)
        lines, errors = ctl.run(True)
        expect(not errors and any(l.startswith(f"HOLD {k31} (unchanged)") for l in lines) and not any(l.startswith("HELD") for l in lines)
               and len(w.notes) == n_notes and len(diffs) == n_diffs and len(w.asks) == 2, f"unchanged: {lines}")
        # the 変更後 image is added (the page is edited) -> asked with the images, the hold is dropped, nothing is written to the hub again
        w.decision_pages[p31]["blocks"].append(_img(2, "変更後（branch）"))
        w.pages[p31]["last_edited_time"] = "2026-11-09T13:00:00.000Z"
        lines, errors = ctl.run(True)
        expect(not errors and w.asks[-1][0] == k31, f"{lines} {w.asks[-1:]}")
        expect(len(w.notes) == n_notes and k31 not in json.loads(C.state_path(C.UI_HOLD_FILE).read_text(encoding="utf-8")), "hold dropped")
        lines, errors = ctl.run(True)
        expect(len(w.asks) == 3 and not errors, "asked once")
    # a changed reason (still held) logs again, but adds no second 候補 item
    w, ep, paths = _ui_world()
    hub31, e31, p31 = ep[31]
    k31 = f"{e31[-8:]}-承認"
    with sandbox(w) as (ctl, tmp), _fake_repo(paths):
        ctl.run(True)
        w.decision_pages[p31]["blocks"][:] = [b for b in w.decision_pages[p31]["blocks"] if b["type"] != "image"]
        w.pages[p31]["last_edited_time"] = "2026-11-09T14:00:00.000Z"
        lines, errors = ctl.run(True)
        kinds = [c[2] for c in w.calls if c[0] == "note" and c[1] == hub31["id"]]
        expect(not errors and kinds == ["paragraph", "to_do", "paragraph"] and any(l.startswith(f"HELD {k31}: ") and "変更前・変更後" in l for l in lines), (kinds, lines))
    # the old gate tests' shape (no decision page, no repository): not held, asked as before
    w = World()
    hub, eid = w.add_epic(34, rows=[("リリース判定", "完了")])
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and [a[0] for a in w.asks] == [f"{eid[-8:]}-承認"], lines)


class _ImgNotion:
    """Fake Notion for ask_human.py --images-from: the review DB, the created row as blocks (from its markdown), the decision page's images,
    block inserts after a block; plus fake downloads and `ntn files create`."""

    def __init__(self, src_images, fail_insert_once=False):
        self.src, self.rows, self.uploads, self.inserts = src_images, {}, [], []
        self.fail_insert_once = fail_insert_once
        self.n = 0

    def ntn(self, *args, data=None, **kw):
        path = args[0]
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        if path.endswith("/query"):
            key = data["filter"]["rich_text"]["equals"]
            return {"results": [{"id": rid, "url": f"https://www.notion.so/{rid}"} for rid, r in self.rows.items() if r["key"] == key], "has_more": False}
        if path == "v1/pages" and method == "POST":
            rid = _id(0x6000 + len(self.rows))
            blks = []
            for i, ln in enumerate(l for l in data["markdown"].split("\n") if l.strip()):
                kind = "heading_1" if ln.startswith("# ") else "paragraph"
                blks.append({"id": f"{rid}-{i}", "type": kind, "parent": {"type": "page_id", "page_id": rid}, kind: {"rich_text": [{"plain_text": ln.lstrip("# ")}]}})
            self.rows[rid] = dict(key=data["properties"]["キー"]["rich_text"][0]["text"]["content"], blocks=blks)
            return {"id": rid, "url": f"https://www.notion.so/{rid}"}
        m = re.fullmatch(r"v1/blocks/([^/]+)/children", path)
        if m and method == "PATCH":
            if self.fail_insert_once:
                self.fail_insert_once = False
                raise C.PipelineError("simulated insert failure")
            rid, after = m.group(1), data["position"]["after_block"]["id"]
            blks = self.rows[rid]["blocks"]
            i = next(j for j, b in enumerate(blks) if b["id"] == after)
            new = dict(data["children"][0], id=f"img-{len(self.inserts)}")
            new["image"] = dict(new["image"], caption=[{"plain_text": x["text"]["content"]} for x in new["image"]["caption"]])
            blks.insert(i + 1, new)
            self.inserts.append((rid, after, new["image"]["file_upload"]["id"]))
            return {"results": [new]}
        if m and m.group(1) == "SRC":
            return {"results": self.src, "has_more": False}
        if m and m.group(1) in self.rows:
            return {"results": self.rows[m.group(1)]["blocks"], "has_more": False}
        raise AssertionError(f"unexpected ntn call {args}")

    def sh_bytes(self, cmd, data, timeout=120):
        expect(cmd[1:3] == ["files", "create"] and data.startswith(b"PNG:"), cmd)
        self.n += 1
        self.uploads.append((cmd[cmd.index("--filename") + 1], data))
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": f"up-{self.n}", "status": "uploaded"}).encode(), b"")

    def urlopen(self, req, timeout=None):
        resp = io.BytesIO(b"PNG:" + req.full_url.encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: None
        return resp


@contextlib.contextmanager
def _img_sandbox(world):
    import urllib.request
    import ask_human as ah
    with sandbox() as (_ctl, tmp):
        saved = (C.ntn, C._sh_bytes, urllib.request.urlopen, ah.notify)
        notified = []
        C.ntn, C._sh_bytes, urllib.request.urlopen = world.ntn, world.sh_bytes, world.urlopen
        ah.notify = lambda *a, **k: notified.append(a[0])
        try:
            yield ah, notified
        finally:
            C.ntn, C._sh_bytes, urllib.request.urlopen, ah.notify = saved


_IMG_ASK = ("# 判断すること\n画像を確かめてください\n# 推奨\n承認\n# 人が確かめること\n1. 画像を見る\n"
            "# 根拠\n[判定書](https://example.test/doc)\n［画像 1］変更前（main）\n本文\n［画像 2］変更後（branch）")


@check
def ask_human_copies_the_decision_images_after_the_marks_verifies_them_and_repairs_a_partial_row():
    def run(ah, key="u1", extra=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ah.main(["--key", key, "--text", _IMG_ASK, "--images-from", "SRC", *extra])
        return rc, out.getvalue()
    w = _ImgNotion([_img(1, "変更前（main）"), _img(2, "変更後（branch）")])
    with _img_sandbox(w) as (ah, notified):
        rc, out = run(ah, extra=["--require-ui-images"])
        expect(rc == 0 and out.startswith("CREATED") and notified == ["u1"], (rc, out, notified))
        expect([n for n, _ in w.uploads] == ["shot1.png", "shot2.png"] and [u for _, _, u in w.inserts] == ["up-1", "up-2"], (w.uploads, w.inserts))
        rid = next(iter(w.rows))
        blks = w.rows[rid]["blocks"]
        marks = [i for i, b in enumerate(blks) if C.evidence_text(b).startswith("［画像")]
        expect(all(blks[i + 1]["type"] == "image" for i in marks) and blks[marks[1] + 1]["image"]["caption"][0]["plain_text"] == "変更後（branch）",
               "each image right after its mark, with the decision page's caption")
        expect(json.loads(C.state_path(C.ASK_STATE_FILE).read_text(encoding="utf-8")).get("u1"), "key cached after the images are in")
        rc, out = run(ah)  # the same key again: DUP, nothing missing, no upload, no mention
        expect(rc == 0 and out.startswith("DUP") and len(w.uploads) == 2 and notified == ["u1"], (out, w.uploads, notified))
    # a failed insert: ERROR, no cache, no mention; the next run (DUP) adds only what is missing and announces once
    w = _ImgNotion([_img(1, "変更前（main）"), _img(2, "変更後（branch）")], fail_insert_once=True)
    with _img_sandbox(w) as (ah, notified):
        rc, out = run(ah, key="u2", extra=["--require-ui-images"])
        expect(rc == 1 and out.startswith("ERROR images of the review row") and notified == [] and not C.state_path(C.ASK_STATE_FILE).exists(), (rc, out))
        rc, out = run(ah, key="u2", extra=["--require-ui-images"])
        expect(rc == 0 and out.startswith("DUP") and notified == ["u2"] and len(w.inserts) == 2, (rc, out, notified, w.inserts))
    # a UI change whose copied images have no 変更後: the row is incomplete -> ERROR (the controller retries; it never announces it)
    w = _ImgNotion([_img(1, "変更前（main）"), _img(2, "変更前（desktop）")])
    with _img_sandbox(w) as (ah, notified):
        rc, out = run(ah, key="u3", extra=["--require-ui-images"])
        expect(rc == 1 and "変更後の画像がありません" in out and notified == [], (rc, out))
    # the decision page lost an image since the copy was made: nothing is created
    w = _ImgNotion([_img(1, "変更前（main）")])
    with _img_sandbox(w) as (ah, notified):
        rc, out = run(ah, key="u4")
        expect(rc == 1 and out.startswith("ERROR") and not w.rows and notified == [], (rc, out, w.rows))


# ---------------------------------------------------------------- checks: the release row is the release judgement

_STATUSES = ("達成", "達成", "達成", "一部", "未実施")
_CHECKS = ("画面を見る：[画面](https://example.test/a)（5分）", "ログを見る：[ログ](https://example.test/l)（3分）")
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _judgement(statuses=_STATUSES, rec="推奨：承認", checks=_CHECKS, extra_ships="", risks="- 本番反映は未確認", unknown="- 上限は未確認", basis="make test が緑"):
    """A release judgement page in the fixed shape of scripts/templates/release_judgement.md (the markdown the Notion API returns)."""
    rows = [f"<tr><td>{_CIRCLED[i % 20]} 条件{i + 1}</td><td>{st}</td><td>根拠{i + 1}：{basis}</td></tr>" for i, st in enumerate(statuses)]
    return "\n".join([
        '# 推奨 {color="yellow_bg"}', rec, "条件つきで承認してよい。残りは別案件で扱う。", "",
        '# 人が確かめること {color="yellow_bg"}', *[f"{i}. {c}" for i, c in enumerate(checks, 1)], "",
        '# 何を出すか {color="yellow_bg"}', "- リポジトリ：example-org/example-app", "- ブランチ：michiomochi/example-app-text-search", "- PR：未作成", extra_ships, "",
        '# 完了条件の達成状況 {color="yellow_bg"}', '<table fit-page-width="true" header-row="true">', "<tr><td>完了条件</td><td>状況</td><td>根拠</td></tr>", *rows,
        "</table>", "",
        '# 残っている危険 {color="yellow_bg"}', risks, "",
        '# 未確認 {color="yellow_bg"}', unknown, "",
        '# 承認のあとに起きること {color="yellow_bg"}', "1. make deploy", "",
        '# 戻し方 {color="yellow_bg"}', "backup から戻す"])


def _facts(md="__typed__", **kw):
    """The facts build_release_ask works from (what collect_release_facts reads)."""
    md = _judgement() if md == "__typed__" else md
    f = dict(key="1e311667-承認", hub_name="ハブ", hub_url="https://app.notion.com/p/abc", repo="example-org/example-app", branch="michiomochi/example-app-text-search",
             page=dict(id=_id(1), title="リリース判定書の草稿", url="https://app.notion.com/p/def", edited="2026-11-10T01:00:00.000Z") if md is not None else None,
             md=md, cond_cell="\n".join(f"{_CIRCLED[i]} 条件{i + 1}" for i in range(5)), pr_drafts=[("PR本文の下書き", "https://app.notion.com/p/pr")],
             deliverables=[("要件定義：要件書", "https://app.notion.com/p/req"), ("設計：設計書", "https://app.notion.com/p/des")],
             phase_rows=[(f"{ph}の行", f"https://app.notion.com/p/row{i}") for i, ph in enumerate(C.ORDER)], ui=False)
    f.update(kw)
    return f


@contextlib.contextmanager
def fake_prepare(**over):
    """ship_approved.prepare replaced by a fake that returns an opened-and-read-back draft PR (or what `over` says); yields the list of its calls."""
    import ship_approved as S
    res = dict(status="ok", code="", reason="", pr=31, pr_url="https://github.com/example-org/example-app/pull/31", head="a" * 40, title="t")
    res.update(over)
    calls, saved = [], S.prepare
    S.prepare = lambda epic, key, dry_run=False, repo=None, branch=None: calls.append((epic, key, dry_run, repo, branch)) or dict(res)
    try:
        yield calls
    finally:
        S.prepare = saved


def _meaning(repo="example-org/example-app", **kw):
    """The three fixed lines of chapter 1 as a row about `repo` renders them."""
    return C.release_meaning(**C.ship_meaning(repo, **kw))


_REAL_HEADINGS = {  # the chapter headings of the six release judgements that existed on 2026-11-10 (6 shapes)
    "22c86466": ["## 1. 判定の推奨", "## 2. 目標値と実測の差", "## 3. 打ち手B の採否", "## 4. 本番反映の手順（指示のあとに行う）", "## 5. 戻し方", "## 6. 危険と未確認・残件"],
    "056cdb35": ["# 1. 判定の草稿（結論）", "# 2. 完了条件①〜⑥の達成状況", "# 3. 残っている危険と未確認", "# 4. 本番反映の手順（michiomochi の指示の後）", "# 5. 戻し方（ロールバック）"],
    "ee772d9c": ["# 1. 判定の案", "# 2. 完了条件ごとの判定と根拠", "# 3. 残っている危険", "# 4. リリースの手順（michiomochi の指示のあとに実行するもの）", "# 5. 戻し方（ロールバック）", "# 6. 未確認事項"],
    "840df19b": ["# 判定の対象と推奨", "# 完了条件の達成状況", "# 指標と目標値の見立て", "# 残っている危険", "# 本番反映の手順（michiomochi の指示後）", "# 戻し方", "# 未確認"],
    "a86e9802": ["# 判定（草稿）", "# 完了条件の達成状況", "# 残る危険", "# 本番反映の手順（michiomochi の指示のあとだけ行う）", "# 戻し方"],
    "fixed": ["## 1. 何を出すか", "## 2. 完了条件の達成状況", "## 3. 本番反映の手順（人が PR を指示したあと）", "## 4. 残っている危険", "## 5. 戻し方", "## 6. 未確認", "## 7. 推奨"],
}


def _real_doc(heads):
    return "\n".join(f"{h} {{color=\"blue_bg\"}}\nこの章の本文です。\n" for h in heads)


def _links_of(body):
    return re.findall(r"^- \[", C.chapter_text(body, "根拠（開くページ）"), re.M)


@check
def the_judgement_is_cut_at_its_chapter_names_and_a_page_of_another_shape_only_reports_what_is_missing():
    secs, missing = C.judgement_sections(_judgement())
    expect(missing == [] and list(secs) == list(C.judgement_chapters()) and secs["推奨"].startswith("推奨：承認") and "<table" in secs["完了条件の達成状況"], (missing, list(secs)))
    expect("本番反映" in secs["残っている危険"] and "上限" in secs["未確認"] and "上限" not in secs["残っている危険"], "a chapter ends where the next one starts")
    secs, missing = C.judgement_sections("## 7. 推奨 {color=\"x\"}\n推奨：差し戻し\n### 小見出し\nx\n## 8. 戻し方\ny")
    expect(secs["推奨"].endswith("x") and "戻し方" not in secs["推奨"] and secs["戻し方"] == "y" and "人が確かめること" in missing, (secs, missing))
    expect(C.judgement_sections("") == ({}, list(C.judgement_chapters())) and C.judgement_sections(None)[1] == list(C.judgement_chapters()), "empty input")
    expect(C.judgement_sections("```\n# 推奨\n```\n")[1][0] == "推奨", "a heading inside a code fence is no chapter")
    for name, heads in _REAL_HEADINGS.items():  # the six real shapes: reported, never an exception
        secs, missing = C.judgement_sections(_real_doc(heads))
        expect(missing and set(secs) | set(missing) == set(C.judgement_chapters()), (name, missing))
    secs, missing = C.judgement_sections(_real_doc(_REAL_HEADINGS["fixed"]))
    expect(missing == ["人が確かめること", "承認のあとに起きること"], f"numbers are ignored, H2 chapters are found: {missing}")


@check
def the_judgement_template_is_the_fixed_eight_chapters_and_its_hints_are_found_when_left_in():
    text = C.load_template("release_judgement")
    secs, missing = C.judgement_sections(text)
    expect(missing == [] and [n for _, _, n in C._headings(text.split("\n"))] == list(C.judgement_chapters()), (missing, list(secs)))
    expect(C.template_fields("release_judgement") == set() and C.PLACEHOLDER_RE.search(text), "no variables; the writing hints are 〈…〉")
    expect(len(C.table_rows(secs["完了条件の達成状況"])) == 1, "the template table has one example row")
    out = C.build_release_ask(_facts(md=text))
    expect(any("ひな形の書き置き" in p for p in C.release_problems(out["body"])), "a judgement that still holds the hints is reported as 未確認")
    expect(not any("ひな形の書き置き" in p for p in C.release_problems(C.build_release_ask(_facts())["body"])), "a finished judgement has none")
    expect("release_judgement" in (HERE.parent / "tools" / "safe_deploy.sh").read_text(encoding="utf-8") if (HERE.parent / "tools" / "safe_deploy.sh").is_file() else True,
           "safe_deploy.sh deploys the judgement template")


@check
def status_words_are_counted_and_the_title_and_the_注意_line_follow_from_them():
    counts, ach, den = C.count_status([("a", "達成"), ("b", "**一部**"), ("c", "未達成"), ("d", "未実施（理由）"), ("e", "対象外"), ("f", "満たす")])
    expect(counts == {"達成": 1, "一部": 1, "未達": 1, "未実施": 1, "対象外": 1, "不明": 1} and (ach, den) == (1, 5), (counts, ach, den))
    out = C.build_release_ask(_facts())
    expect(out["title"] == "ハブ: リリース承認（推奨 承認・条件 3/5 達成）" and (out["achieved"], out["denominator"], out["rec"]) == (3, 5, "承認"), out["title"])
    rec = C.chapter_text(out["body"], "推奨")
    expect("完了条件：達成 3・一部 1・未達 0・未実施 1" in rec and "注意：達成でない完了条件が 2 件あります。" in rec, rec)
    expect(C.counts_brief(out["body"]) == "達成3・一部1・未実施1", C.counts_brief(out["body"]))
    out = C.build_release_ask(_facts(md=_judgement(statuses=("達成", "達成", "対象外"))))
    expect(out["title"].endswith("（推奨 承認・条件 2/2 達成）") and "注意：" not in out["body"] and "・対象外 1" in out["body"], out["title"])
    out = C.build_release_ask(_facts(md=_judgement(rec="推奨：差し戻し")))
    expect(out["rec"] == "差し戻し" and out["title"].endswith("（推奨 差し戻し・条件 3/5 達成）") and "注意：" not in out["body"], "no 注意 line for 差し戻し")
    expect(C.recommendation_word("- **推奨：承認** 理由") == "承認" and C.recommendation_word("推奨: 差し戻し") == "差し戻し"
           and C.recommendation_word("推奨：条件つきで承認") is None and C.recommendation_word("") is None and C.recommendation_word("\n\n理由\n推奨：承認") is None,
           "only the first non-empty line, exactly 推奨：承認 / 推奨：差し戻し")
    expect(C.release_title("あ" * 200, dict(a=1), "承認", {}, 3, 5).startswith("あ" * 70 + ": リリース承認（") and len(C.release_title("あ" * 200, dict(a=1), None, None, 0, 0)) <= 120, "title length")
    expect(C.release_title("ハブ", dict(a=1), "承認", None, 0, 0) == "ハブ: リリース承認（推奨 承認・条件 未確認）", "a table that cannot be read")


@check
def the_release_row_has_nine_chapters_in_order_the_first_three_are_filled_and_the_body_stays_within_5000_characters():
    out = C.build_release_ask(_facts(md=_judgement(extra_ships="- 画面：https://example.test/shot")))
    body = out["body"]
    expect(C.has_review_chapters(body) and len(re.findall(r"^# ", body, re.M)) == 9, "nine H1 chapters, the copy adds none")
    for name in C.review_chapters()[:3]:
        expect(C.chapter_text(body, name).strip(), f"chapter {name} is filled")
    expect("リリースしてよいと認めるか" in body and "`example-org/example-app` のブランチ `michiomochi/example-app-text-search`" in body, "1st sentence names the target")
    expect(not re.search(r"(?<![(\[])https?://", re.sub(r"`[^`]*`", "", body)) and "[https://example.test/shot](https://example.test/shot)" in body, "bare URLs become links")
    expect("承認(判定書の草稿とPR本文の下書きを確認)" not in body and "リリース判定の工程が完了し" not in body, "the old fixed recommendation / why are gone")
    expect("写した時刻 " in body and "判定書の最終更新 " in body and "食い違うときは判定書が正です。" in body, "copy time and the rule")
    expect(C.visible_len(body) <= C.REVIEW_BODY_MAX and len(_links_of(body)) <= 12 and out["marks"] == [], C.visible_len(body))
    expect(len(C.chapter_text(body, "判断すること")) <= 400 and len(C.chapter_text(body, "推奨")) <= 400 + 120 and len(C.chapter_text(body, "人が確かめること")) <= 700, "chapter limits 1-3")
    expect("[リリース判定書：リリース判定書の草稿](https://app.notion.com/p/def)" in body and "PR本文の下書き" in body and "https://github.com/example-org/example-app/tree/michiomochi/example-app-text-search" in body, "direct links")
    expect("PR は未作成です" in C.chapter_text(body, "何を出すか"), "no PR url in the judgement")
    pr = C.build_release_ask(_facts(md=_judgement(extra_ships="- PR：https://github.com/example-org/example-app/pull/7")))["body"]
    expect("PR：[https://github.com/example-org/example-app/pull/7]" in C.chapter_text(pr, "何を出すか"), "a PR url of the judgement is shown")
    # a very large material: every chapter is cut at an item boundary with the note, the whole stays within the limit
    big = _judgement(statuses=["達成"] * 30, checks=["あ" * 300] * 9, risks="\n".join(f"- 危険{i}：" + "い" * 100 for i in range(60)),
                     unknown="\n".join(f"- 未確認{i}：" + "え" * 100 for i in range(60)), extra_ships="\n".join(f"- 中身{i}：" + "う" * 80 for i in range(40)), basis="お" * 400)
    f = _facts(md=big, hub_name="エ" * 200, deliverables=[(f"工程{i}：" + "成" * 100, f"https://app.notion.com/p/{i}") for i in range(30)])
    out = C.build_release_ask(f)
    expect(C.has_review_chapters(out["body"]) and C.visible_len(out["body"]) <= 5000 and len(out["title"]) <= 120, (C.visible_len(out["body"]), len(out["title"])))
    expect("（以下略：判定書の「" in out["body"] and len(_links_of(out["body"])) <= 12 and "未確認：判定書の表は 30 行、エピックの完了条件は 5 件です。" in out["body"], "cut with the note")
    expect(C.chapter_text(out["body"], "人が確かめること").count("\n") <= 5 and "（ほか " in C.chapter_text(out["body"], "人が確かめること"), "at most 5 checks, the rest counted")
    cells = re.findall(r"<td>(.*?)</td>", C.chapter_text(out["body"], "完了条件の達成状況"))
    expect(max(len(c) for c in cells) <= 200 and len(cells) <= 3 * 11, "cells up to 200 characters, 10 rows and the header")
    expect(C.cut_text("あ[リンク](https://example.test/x)い", 12) == "あ" and C.cut_text("あ[リンク](https://example.test/x)い", 40) == "あ[リンク](https://example.test/x)い", "a cut never splits a link")


@check
def a_missing_or_foreign_judgement_still_makes_the_row_and_the_gaps_read_未確認_with_no_default_recommendation():
    out = C.build_release_ask(_facts(md=None))
    expect(out["title"] == "ハブ: リリース承認（判定書なし）" and out["rec"] is None and C.has_review_chapters(out["body"]), out["title"])
    probs = C.release_problems(out["body"])
    expect(len(probs) >= 5 and all("判定書がありません" in p or "ありません" in p or "未確認" in p for p in probs), probs)
    expect("推奨：承認" not in out["body"] and "推奨 承認" not in out["title"] and "判定書がありません" in C.chapter_text(out["body"], "推奨"), "never defaults to 承認")
    expect("エピックの完了条件（エピックの目的の表の写し）" in C.chapter_text(out["body"], "完了条件の達成状況"), "fallbacks")
    expect("判定書の「残っている危険」を読む" in C.chapter_text(out["body"], "人が確かめること"), "a machine-made check")
    expect("ブランチ" in C.chapter_text(C.build_release_ask(_facts(md=None, repo=None, branch=None))["body"], "判断すること") and
           "未確認：ブランチが本文に見つかりません" in C.chapter_text(C.build_release_ask(_facts(md=None, repo=None, branch=None))["body"], "判断すること"), "no branch")
    for name, heads in _REAL_HEADINGS.items():  # the six real shapes of 2026-11-10: a row is made, the title says 推奨 未確認, the missing chapters are named
        out = C.build_release_ask(_facts(md=_real_doc(heads)))
        probs = C.release_problems(out["body"])
        expect(out["title"] == "ハブ: リリース承認（推奨 未確認）" and C.has_review_chapters(out["body"]) and C.visible_len(out["body"]) <= 5000, (name, out["title"]))
        expect(any("判定書に「推奨」の章がありません" in p or "1行目が『推奨：承認』" in p for p in probs) and not re.search(r"^推奨：承認", out["body"], re.M), (name, probs))
    out = C.build_release_ask(_facts(md=_judgement(statuses=("達成", "満たす", "達成", "達成")), cond_cell="① a\n② b\n③ c\n④ d\n⑤ e\n⑥ f"))
    probs = C.release_problems(out["body"])
    expect(any("状況の語が決まった語" in p and "1 件" in p for p in probs) and any("判定書の表は 4 行、エピックの完了条件は 6 件です" in p for p in probs), probs)
    out = C.build_release_ask(_facts(md=_judgement().replace("<table", "<xtable").replace("</table>", "</xtable>")))
    expect(any("表がありません" in p for p in C.release_problems(out["body"])) and out["counts"] is None and out["title"].endswith("（推奨 承認・条件 未確認）"), out["title"])
    out = C.build_release_ask(_facts(md=_judgement(checks=())))
    expect("未確認：判定書の「人が確かめること」の章が空です。" in out["body"], "an empty chapter is not a filled one")
    out = C.build_release_ask(_facts(md=_judgement(checks=("なし",))))
    expect("なし（下の表と危険を読めば足ります）" in C.chapter_text(out["body"], "人が確かめること"), "0 checks")


@check
def a_ui_changes_images_stay_in_the_row_even_when_the_copied_chapters_do_not_hold_them():
    md = _judgement(extra_ships="![変更前（main）](https://files.example.test/a.png)") + "\n# 補足\n![変更後（branch）](https://files.example.test/b.png)\n"
    out = C.build_release_ask(_facts(md=md, ui=True))
    expect(out["marks"] == [1, 2] and "［画像 1］変更前（main）" in out["body"] and "［画像 2］変更後（branch）" in out["body"] and "files.example.test" not in out["body"], out["body"][-1500:])
    out = C.build_release_ask(_facts(md=md, ui=False))
    expect(out["marks"] == [1], "not a UI change: only the images the copied chapters hold")


def _notify_body():
    return C.build_release_ask(_facts())["body"]


@check
def the_discord_message_has_four_lines_with_the_recommendation_the_first_check_and_the_url():
    import ask_human as ah
    lines = ah.notify_lines("ハブ", "https://www.notion.so/row", _notify_body(), "199335018222053302", release=True)
    expect(len(lines) == 4 and lines[0] == "<@199335018222053302> リリース承認をお願いします: ハブ", lines)
    expect(lines[1] == "推奨: 承認（条件 達成3・一部1・未実施1）" and lines[2] == "確かめること: 画面を見る：画面（5分）（ほか 1 件）" and lines[3] == "https://www.notion.so/row", lines)
    expect(all(len(l) <= 200 for l in lines[:3]) and sum(len(l) for l in lines) + 3 <= 2000, "each line up to 200 characters, the message up to 2000")
    big = C.build_release_ask(_facts(md=_judgement(checks=["あ" * 300] * 5), hub_name="エ" * 300))["body"]
    lines = ah.notify_lines("エ" * 300, "https://www.notion.so/row", big, "1", release=True)
    expect(all(len(l) <= 200 for l in lines[:3]) and sum(len(l) for l in lines) + 3 <= 2000, [len(l) for l in lines])
    none = ah.notify_lines("ハブ", "u", C.build_release_ask(_facts(md=None))["body"], "", release=True)
    expect(none[0] == "リリース承認をお願いします: ハブ" and none[1].startswith("推奨: 未確認：判定書がありません") and none[2] == "確かめること: 判定書の「残っている危険」を読む", none)
    free = ah.notify_lines("名前", "u", C.review_markdown("k", _ASK_TEXT), "1")
    expect(free[0] == "<@1> 承認をお願いします: 名前" and free[1] == "推奨: 承認: 判定書 を確認済みです", free)


_OLD_SEVEN = ('# 判断の対象 {color="yellow_bg"}\nこの行は、「【SF】差し戻し・中断のあとも同じ文脈で続ける」のリリース承認の判断を待っています。同じキーでは2回作りません。\n'
              '<table fit-page-width="true" header-row="true">\n<tr>\n<td>項目</td>\n<td>内容</td>\n</tr>\n<tr>\n<td>キー</td>\n<td>22c86466-承認</td>\n</tr>\n</table>\n'
              '# 何を判断するか {color="yellow_bg"}\n「【SF】差し戻し・中断のあとも同じ文脈で続ける」のリリース承認をお願いします。\n'
              '# なぜ {color="yellow_bg"}\nリリース判定の工程が完了し、判定書の草稿とPR本文の下書きができました。リリースはまだ行いません。\n'
              '# 根拠 {color="yellow_bg"}\n確認する成果物(判定書の草稿とPR本文の下書き)は、ハブページの関連ドキュメントとリリース判定の行にあります：[【SF】差し戻し・中断のあとも同じ文脈で続ける](https://app.notion.com/p/00d31b1968d13aec3996c54c80b03a4a)\n'
              '# 推奨の選択肢 {color="yellow_bg"}\n承認(判定書の草稿とPR本文の下書きを確認)\n'
              '# 判断の結果 {color="yellow_bg"}\n決めるのは人だけです。AI はこの章を書き換えません。\n'
              '# 反映の記録 {color="yellow_bg"}\n反映ジョブが、決定を SSOT のどこに反映したかを1行書きます。\n')


@check
def an_old_seven_chapter_row_is_still_read_and_never_made_again():
    import ask_human as ah
    expect(tuple(re.findall(r'^# (\S+) \{color="yellow_bg"\}', _OLD_SEVEN, re.M)) == C.OLD_REVIEW_CHAPTERS and not C.has_review_chapters(_OLD_SEVEN), "the old shape")
    expect(ah.recommendation_line(_OLD_SEVEN) == "承認(判定書の草稿とPR本文の下書きを確認)", ah.recommendation_line(_OLD_SEVEN))
    expect(ah.notify_lines("名前", "u", _OLD_SEVEN, "1")[1:3] == ["推奨: 承認(判定書の草稿とPR本文の下書きを確認)", "確かめること: なし"], "the digest / notification read the old shape")
    err = raises(C.ReviewAskError, C.review_markdown, "k", _OLD_SEVEN)
    expect("判断すること" in str(err) and "人が確かめること" in str(err), "an old-shape text is refused with the expected shape")


@check
def free_form_asks_need_the_four_headings_and_a_markdown_link_else_no_row_is_made():
    import ask_human as ah
    ok = C.review_markdown("k", _ASK_TEXT)
    expect(C.has_review_chapters(ok) and ok.count("該当なし") == 3 and "# 人が確かめること" in ok, ok)
    expect(C.chapter_text(ok, "判断すること") == "完了の確認をお願いします" and C.chapter_text(ok, "根拠（開くページ）").startswith("[判定書](https://example.test/doc)"), "the four are carried over")
    deep = C.review_markdown("k", _ASK_TEXT.replace("# 判断すること", "## 判断すること").replace("# 推奨\n", "## 推奨\n### 強調された見出し\n"))
    expect(len(re.findall(r"^# ", deep, re.M)) == 9 and "## 強調された見出し" in deep, "headings inside the text are moved below the nine H1 chapters")
    e = raises(C.ReviewAskError, C.review_markdown, "k", _ASK_TEXT.replace("# 推奨\n", "# 推薦\n"))
    expect("推奨" in str(e) and "見出しが足りません" in str(e), e)
    e = raises(C.ReviewAskError, C.review_markdown, "k", _ASK_TEXT.replace("[判定書](https://example.test/doc)\n", "https://example.test/doc\n").replace(
        "承認: [判定書](https://example.test/doc)", "承認"))
    expect("リンク" in str(e), e)
    raises(C.ReviewAskError, C.review_markdown, "k", "ただの文章です")
    raises(C.ReviewAskError, C.review_markdown, "k", _ASK_TEXT.replace("1. 判定書を読む\n2. 画面を見る\n", ""))
    db = _ReviewDB()
    saved = (C.ntn, C.STATE_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        C.STATE_DIR = Path(tmp)
        C.ntn = db.ntn
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--key", "x-done", "--text", "リリース承認をお願いします", "--epic", _id(5)])
            expect(rc == 1 and out.getvalue().startswith("ERROR ReviewAskError") and db.created == [], (rc, out.getvalue(), db.created))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--key", "x-done", "--release", "--text", "x"])
            expect(rc == 1 and "exactly one" in out.getvalue(), out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--key", "x-done", "--release"])
            expect(rc == 1 and "--epic" in out.getvalue() and db.created == [], out.getvalue())
        finally:
            C.ntn, C.STATE_DIR = saved


def _verdict(ok=True, ui=False):
    return C.ui_evidence_verdict(["pages/a.tsx"] if ui else ["scripts/a.py"], [_img(1, "変更前"), _img(2, "変更後")] if ok else [])


@check
def the_release_path_dry_run_prints_title_message_and_body_and_writes_nothing_and_the_real_run_creates_one_row():
    import ask_human as ah
    saved = (C.ntn, C.STATE_DIR, C.collect_release_facts, ah.notify)
    db = _ReviewDB()
    notified = []
    with tempfile.TemporaryDirectory() as tmp, fake_prepare():
        C.STATE_DIR = Path(tmp)
        C.collect_release_facts = lambda epic, key, root=None: dict(_facts(key=key), verdict=_verdict())
        ah.notify = lambda *a, **k: notified.append(k)
        try:
            C.ntn = lambda *a, **k: (_ for _ in ()).throw(AssertionError("--dry-run must not call Notion"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "test", "--dry-run"])
            text = out.getvalue()
            expect(rc == 0 and text.startswith("TITLE: ハブ: リリース承認（推奨 承認・条件 3/5 達成）\nNOTIFY (4 lines,") and "BODY (" in text and "9 chapters" in text
                   and "PROBLEMS (1):\n- 注意：達成でない完了条件が 2 件あります。" in text and "(the row URL goes here)" in text, text[:600])
            expect(not Path(tmp, C.ASK_STATE_FILE).exists() and notified == [], "nothing remembered, nobody mentioned")
            C.ntn = db.ntn
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "1e311667-承認"])
            expect(rc == 0 and out.getvalue().startswith("CREATED "), out.getvalue())
            made = db.created[-1]
            expect(made["properties"]["名前"]["title"][0]["text"]["content"] == "ハブ: リリース承認（推奨 承認・条件 3/5 達成）" and made["properties"]["対象エピック"]["relation"] == [{"id": _id(5)}], made["properties"])
            _expect_review_chapters(made["markdown"], "the release row")
            expect(notified and notified[0].get("release") is True and notified[0].get("title") == "ハブ: リリース承認（推奨 承認・条件 3/5 達成）", notified)
            db.rows.append(dict(key="1e311667-承認", page=_review_page(1, "レビュー待ち", key="1e311667-承認"), id=_id(1)))
            db.rows[-1]["page"]["id"] = _id(1)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "1e311667-承認"])
            expect(rc == 0 and out.getvalue().startswith("DUP ") and len(db.created) == 1, "the same key never creates a second row (the re-ask after 差し戻し uses -r2)")
            # a UI change without both images: no row (the controller holds it before; the re-ask path is refused here)
            C.collect_release_facts = lambda epic, key, root=None: dict(_facts(key=key, ui=True), verdict=_verdict(ok=False, ui=True))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "1e311667-承認-r2"])
            expect(rc == 1 and "ERROR the release ask is not created" in out.getvalue() and len(db.created) == 1, out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "test", "--dry-run"])
            expect(rc == 0 and "HOLD: the ask would not be created" in out.getvalue(), "dry-run shows the hold")
        finally:
            C.ntn, C.STATE_DIR, C.collect_release_facts, ah.notify = saved


# ---------------------------------------------------------------- checks: the deliverables of a range row are made by the gate controller first

_RANGE_ROW = "要件定義〜リリース判定"


def _range_start(w, ctl, row, level="L2", title=_RANGE_ROW, apply=True, out=None):
    """start_phase for the row `title` (a card of a range of phases) of an epic of `level` -> the lines it wrote."""
    out = [] if out is None else out
    texts = C.checklist_items(w.row_blocks[row["id"]])
    act = dict(ph=C.row_phases(title)[0], row_title=title, procs=ctl.phase_procs(C.row_phases(title)[0], texts), prow=row)
    ctx = dict(now=int(time.time()), run_start=int(time.time()), deadline=None, cards=[], asked=set())
    ctl.start_phase(act, ctx, apply, C.load_journal() if apply else {}, dict(epic_id=_epic_id(1), url="https://app.notion.com/p/e", tenant="epic-1", level=level),
                    "ハブ1", out)
    return out


def _range_world(title=_RANGE_ROW):
    w = World()
    hub, eid = w.add_epic(1, rows=[(title, "未着手")], items={title: ["要件定義：要件書を書く", "リリース判定：判定書の草稿を書く"]})
    return w, w.row(hub, title)


def _card_body(w):
    return next(c for c in w.calls if c[0] == "create")[3]


@check
def the_gate_controller_makes_the_deliverables_of_a_range_row_first_and_puts_their_links_and_the_verify_command_in_the_card():
    want = C.LEVEL_DEFS["L2"]["deliverables"]
    w, row = _range_world()
    with sandbox(w) as (ctl, tmp):
        out = _range_start(w, ctl, row)
        expect([(p, t) for p, t, _ in w.child_pages] == [(row["id"], t) for t in want], f"one child page of the row per title of levels.toml, in its order: {w.child_pages}")
        body = _card_body(w)
        for _, t, pid in w.child_pages:
            expect(f"[{t}](https://app.notion.com/p/{pid.replace('-', '')})" in body, f"the card links {t}")
        expect(C.sfconfig.template_vars(C._CFG_RAW)["verify"] in body and "ops.py verify" in body, "the card says how to verify (${verify})")
        expect(body.index("ops.py verify") < body.index("CONTEXT FILE:") and body.index("[" + want[0] + "]") > body.index("Do the row's"), "the links end the ${parts} of the card")
        expect(sum(1 for c in w.calls if c[0] == "create") == 1 and any(l.startswith("CREATED ") for l in out), out)
        _range_start(w, ctl, row)  # a second run: nothing new
        expect(len(w.child_pages) == len(want), f"a second run makes no page: {len(w.child_pages)}")


@check
def an_l2_card_gets_exactly_three_deliverable_pages_and_the_other_three_are_not_pages():
    expect(list(C.LEVEL_DEFS["L2"]["deliverables"]) == ["要件書", "設計書", "判定書の草稿"], C.LEVEL_DEFS["L2"]["deliverables"])
    expect(len(C.LEVEL_DEFS["L3"]["deliverables"]) == 7, "L3 is not decided here: it keeps its list")
    expect("PR 本文" in C.judgement_chapters("L2") and "PR 本文" not in C.judgement_chapters("L3"), "the PR body is a chapter of L2's judgement only")
    items = "\n".join(C.LEVEL_DEFS["L2"]["items"])
    expect(all(w in items for w in ("見積もりは作業メモ", "実装メモは作業メモ", "テスト計画と結果を作業メモ", "「PR 本文」の章")), "the items say where the four things go")
    w, row = _range_world()
    with sandbox(w) as (ctl, tmp):
        _range_start(w, ctl, row)
        titles = [t for _, t, _ in w.child_pages]
        expect(len(titles) == 3 and not any(x in " ".join(titles) for x in ("見積もり", "実装メモ", "テスト計画", "PR 本文")), str(titles))
        body = _card_body(w)
        expect(body.count("](https://app.notion.com/p/") == 3 and "(no page)" in body and "'PR 本文'" in body, "the card links three pages and says where the rest goes")


@check
def the_deliverables_are_made_only_for_a_row_that_spans_the_levels_phases_and_never_for_an_old_row():
    for title, level in (("設計", "L2"), ("要件定義〜設計", "L3"), ("実装〜リリース判定", "L3"), ("実装〜リリース判定", None)):
        w, row = _range_world(title)
        with sandbox(w) as (ctl, tmp):
            _range_start(w, ctl, row, level=level, title=title)
            body = _card_body(w)
            expect(w.child_pages == [] and "ops.py verify" not in body and "DELIVERABLES" not in body, f"{title} / {level}: nothing is added ({w.child_pages})")
    w, row = _range_world("設計")  # the card of an old row is the card it always was: render_body without a tail is the golden body
    with sandbox(w) as (ctl, tmp):
        _range_start(w, ctl, row, title="設計")
        texts = C.checklist_items(w.row_blocks[row["id"]])
        procs = ctl.phase_procs("設計", texts)
        expect(_card_body(w) == ctl.render_body("設計", "A", 1, {"A"}, "https://app.notion.com/p/e", "ハブ1", row["url"], procs, list(procs), lambda v: ""), "same body")


@check
def a_failed_deliverable_stops_the_card_and_the_next_run_makes_only_what_is_missing():
    want = C.LEVEL_DEFS["L2"]["deliverables"]
    w, row = _range_world()
    w.fail_child_page = {want[2]}
    with sandbox(w) as (ctl, tmp):
        raises(C.PipelineError, _range_start, w, ctl, row)
        expect([t for _, t, _ in w.child_pages] == want[:2] and not any(c[0] == "create" for c in w.calls), "no card until every page exists")
        w.fail_child_page = set()
        _range_start(w, ctl, row)
        expect([t for _, t, _ in w.child_pages] == want and sum(1 for c in w.calls if c[0] == "create") == 1, f"each title once: {[t for _, t, _ in w.child_pages]}")
    w, row = _range_world()
    with sandbox(w) as (ctl, tmp):
        out = _range_start(w, ctl, row, apply=False)
        expect(w.child_pages == [] and not w.calls and any(l.startswith("WOULD CREATE DELIVERABLES") for l in out), out)


# ---------------------------------------------------------------- checks: the size of a range row's card, split_items, an epic raised from L1 to L2

# The card of L2's range row as the controller really writes it (next_action splits the items by head, start_phase renders phase_card.md with the
# ${parts} of every phase and the tail of phase_card_range_note.md), measured with an epic URL of real length and a hub name of HUB_NAME_CHARS:
# 4,828 characters (2026-11-13, levels.toml L2 items; 4,751 with a 3-character hub name). The 2,800 the first redesign asked for cannot hold: phase_card.md alone is 2,980 characters
# (skill_lint holds it at MAX_CARD_TEMPLATE 3,000), so the ceiling is the measured value rounded up. Lower the card, lower this too: the floor
# below fails when the card is more than RANGE_CARD_SLACK under the ceiling, so the ceiling cannot be left loose.
RANGE_CARD_MAX = 4900
RANGE_CARD_SLACK = 200


def _real_range_card(level="L2", title=_RANGE_ROW, texts=None, hub_name=None):
    """The card body the controller writes for the row `title` of an epic of `level` (the whole path: next_action, start_phase) -> (body, the lines it wrote)."""
    texts = C.row_items(level, title) if texts is None else texts
    hub_name = "ハ" * C.HUB_NAME_CHARS if hub_name is None else hub_name
    w = World()
    hub, eid = w.add_epic(1, rows=[(title, "未着手")], items={title: texts})
    with sandbox(w) as (ctl, tmp):
        prows, _ = ctl.parse_rows(w.rows[hub["id"]])
        msgs, act, canc = ctl.next_action(lambda t, r: texts, prows, hub_name)
        expect(act and not canc, (msgs, canc))
        out = list(msgs)
        ctx = dict(now=int(time.time()), run_start=int(time.time()), deadline=None, cards=[], asked=set())
        ctl.start_phase(act, ctx, True, C.load_journal(), dict(epic_id=_epic_id(1), url=C.page_url("e" * 32), tenant="epic-1", level=level), hub_name, out)
        return _card_body(w), out


@check
def the_card_of_the_l2_range_row_is_measured_and_held_under_its_ceiling():
    body, out = _real_range_card()
    n = len(body)
    expect(all(f"[{ph}] " in body for ph in C.ORDER) and body.count("](https://app.notion.com/p/") == 3 and "ops.py verify" in body, "the body is the range row's card: five phases, three links, verify")
    expect(n <= RANGE_CARD_MAX, f"the card of the L2 range row is {n} characters (ceiling {RANGE_CARD_MAX}): shorten phase_card.md / levels.toml items / phase_card_range_note.md")
    expect(n >= RANGE_CARD_MAX - RANGE_CARD_SLACK, f"the card is {n} characters, more than {RANGE_CARD_SLACK} under the ceiling {RANGE_CARD_MAX}: lower RANGE_CARD_MAX to the measured value")
    expect(len(C.load_template("phase_card")) < n, "the template is part of the card")
    long, _ = _real_range_card(texts=C.row_items("L2", _RANGE_ROW) + ["リリース判定：" + "あ" * 300])
    expect(len(long) > RANGE_CARD_MAX, f"the check sees a longer item ({len(long)})")


def _split(phases, texts):
    return C.split_items(phases, texts)


@check
def split_items_gives_each_item_to_the_phase_of_its_head_and_cuts_the_head():
    out, warns = _split(C.ORDER, ["要件定義：A", "設計：B", "設計: C", "実装：D", "テスト：E", "リリース判定：F"])
    expect(out == {"要件定義": ["A"], "設計": ["B", "C"], "実装": ["D"], "テスト": ["E"], "リリース判定": ["F"]} and warns == [], (out, warns))  # full-width and half-width colons
    out, warns = _split(("実装", "テスト"), ["  実装 ：  X  ", "テスト：Y\nZ"])
    expect(out == {"実装": ["X"], "テスト": ["Y\nZ"]} and warns == [], (out, warns))  # spaces around the head and the text are dropped, a text of several lines is kept
    out, _ = _split(("設計", "実装"), ["設計：", "実装：  ", "実装：やる"])
    expect(out == {"設計": [], "実装": ["やる"]}, f"an item with nothing after its head is dropped: {out}")
    expect(list(_split(("設計", "実装"), [])[0]) == ["設計", "実装"] and _split(("設計", "実装"), []) == ({"設計": [], "実装": []}, []), "no items: every phase of the row, empty")


@check
def split_items_puts_an_item_without_a_head_in_the_phase_before_it_and_the_first_in_the_first_phase():
    out, warns = _split(("設計", "実装", "テスト"), ["頭のない最初", "実装：A", "頭のない2つ目", "頭のない3つ目", "テスト：B", "頭のない4つ目"])
    expect(out == {"設計": ["頭のない最初"], "実装": ["A", "頭のない2つ目", "頭のない3つ目"], "テスト": ["B", "頭のない4つ目"]} and warns == [], (out, warns))
    out, _ = _split(("設計", "実装"), ["実装の準備：中に工程名を含む文", "設計について 実装：頭ではない"])
    expect(out == {"設計": ["実装の準備：中に工程名を含む文", "設計について 実装：頭ではない"], "実装": []}, f"a phase name that is not the head of the item is not a head: {out}")
    first = _split(("実装", "テスト"), ["先頭は頭なし", "テスト：B"])[0]
    expect(first["実装"] == ["先頭は頭なし"], f"the first item without a head belongs to the row's FIRST phase, not to ORDER's: {first}")


@check
def split_items_keeps_a_head_the_row_does_not_work_in_the_text_and_warns_once_per_item():
    out, warns = _split(("設計", "実装"), ["設計：A", "リリース判定：B", "テスト：C", "実装：D", "リリース判定：E"])
    expect(out == {"設計": ["A", "リリース判定：B", "テスト：C"], "実装": ["D", "リリース判定：E"]}, f"the foreign head stays in the text, in the current phase: {out}")
    expect(len(warns) == 3 and all("この行（設計・実装）が受け持たない工程です" in x for x in warns), warns)
    expect([x.split("： ")[0] for x in warns] == ["項目の頭 リリース判定", "項目の頭 テスト", "項目の頭 リリース判定"], warns)


@check
def split_items_over_the_items_of_every_level_gives_each_phase_of_a_row_exactly_its_items():
    for level, title in (("L2", "要件定義〜リリース判定"), ("L3", "要件定義〜設計"), ("L3", "実装〜リリース判定")):
        phases = C.row_phases(title)
        texts = C.row_items(level, title)
        out, warns = _split(phases, texts)
        expect(warns == [] and list(out) == list(phases), (level, title, warns, list(out)))
        expect(all(len(out[p]) == 1 and not out[p][0].startswith(p + "：") and out[p][0] for p in phases), f"{level} {title}: one item per phase, its head cut: {out}")
        expect(sum(len(v) for v in out.values()) == len(texts), (level, title))
    expect(C.row_items("L2", "設計") == [C.LEVEL_DEFS["L2"]["items"][1]], "a row of one phase takes the items of that phase only")


@check
def an_epic_raised_from_L1_to_L2_gets_the_L2_row_and_its_card_once_and_never_before():
    for stray in (False, True):  # an L1 epic made by create_row has one ordinary work row; one made by hand has none
        w = World()
        hub, eid = _product_hub(w, 80 + stray, "L1", rows=[("原因の調べ物", "進行中")] if stray else ())
        before = len(w.rows[hub["id"]])
        with sandbox(w) as (ctl, tmp):
            lines, errors = ctl.run(True)
            expect(not errors and len(w.rows[hub["id"]]) == before and w.made_rows == [] and w.creates() == [], f"L1 (stray={stray}): no row, no card: {lines} {w.made_rows}")
            expect(any(l.startswith("SKIP L1") and eid[-8:] in l for l in lines), lines)
            hub["properties"]["レベル"] = {"select": {"name": "L2"}}  # the person raises the epic
            dry, errors = ctl.run(False)
            expect(not errors and w.made_rows == [] and any(l.startswith("WOULD CREATE ROW") for l in dry), f"a dry run only says it would: {dry}")
            lines, errors = ctl.run(True)
            expect(not errors, errors)
            expect([m[1] for m in w.made_rows] == list(C.LEVEL_DEFS["L2"]["rows"]) and len(w.rows[hub["id"]]) == before + 1, f"L2 (stray={stray}): its one row is added: {w.made_rows}")
            row = next(r for r in w.rows[hub["id"]] if C.title_of(r, "タスク名") == C.LEVEL_DEFS["L2"]["rows"][0])
            expect(C.status_of(row) == "進行中" and [i.split("：")[0] for i in w.made_rows[0][2]] == list(C.ORDER), "its items are the L2 items, one per phase")
            expect(len(w.creates()) == 1 and len(w.child_pages) == len(C.LEVEL_DEFS["L2"]["deliverables"]), f"one card, three deliverable pages: {len(w.creates())} {len(w.child_pages)}")
            if stray:
                expect(C.status_of(next(r for r in w.rows[hub["id"]] if C.title_of(r, "タスク名") == "原因の調べ物")) == "進行中", "the work row that was there is not touched")
            w.calls.clear()
            lines, errors = ctl.run(True)
            expect(not errors and len(w.made_rows) == 1 and w.creates() == [] and not [c for c in w.calls if c[0] == "patch"] and not any("ROW" in l for l in lines),
                   f"the second run adds nothing: {lines}")


# ---------------------------------------------------------------- checks: the chapters follow the level (levels.toml)

def _filled(template, drop=None):
    """A judgement page made by filling the level's template as it stands: every 〈writing hint〉 becomes plain text; `drop` removes one chapter."""
    text = re.sub(r"〈([^〉\n]*)〉", r"\1", C.load_template(template))
    if drop:
        text = re.sub(r"(?ms)^# %s .*?(?=^# |\Z)" % re.escape(drop), "", text)
    return text


def _dry_run_release(level, md, **kw):
    """`ask_human.py --release --dry-run` on an epic of `level` whose judgement is `md` -> (exit code, stdout)."""
    import ask_human as ah
    saved = (C.ntn, C.STATE_DIR, C.collect_release_facts)
    with tempfile.TemporaryDirectory() as tmp, fake_prepare():
        C.STATE_DIR = Path(tmp)
        C.ntn = lambda *a, **k: (_ for _ in ()).throw(AssertionError("--dry-run of the release path must not call Notion"))
        C.collect_release_facts = lambda epic, key, root=None: dict(_facts(key=key, md=md, level=level, cond_cell="① 条件", **kw), verdict=_verdict())
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "test", "--dry-run"])
        finally:
            C.ntn, C.STATE_DIR, C.collect_release_facts = saved
    return rc, out.getvalue()


@contextlib.contextmanager
def _level_value(level, key, value):
    """levels.toml's `key` of `level` replaced for a moment (what the config says is what the code does)."""
    node = C.LEVEL_DEFS[level]
    old = node[key]
    node[key] = value
    try:
        yield
    finally:
        node[key] = old


@check
def the_chapters_of_the_judgement_and_of_the_release_row_are_read_from_levels_toml_by_level():
    expect(not hasattr(C, "JUDGEMENT_CHAPTERS") and not hasattr(C, "REVIEW_CHAPTERS"), "no hard-coded chapter list is left in epic_common")
    for lv in ("L2", "L3"):
        expect(C.judgement_chapters(lv) == tuple(C.LEVEL_DEFS[lv]["judgement_chapters"]) and C.review_chapters(lv) == tuple(C.LEVEL_DEFS[lv]["review_chapters"]), lv)
    expect(C.judgement_chapters("L1") == C.judgement_chapters("L3") == C.judgement_chapters(None) and C.review_chapters("L1") == C.review_chapters(None),
           "L1 has no chapters of its own and an epic without a level is made as L3: both read the L3 shape")
    raises(C.PipelineError, C.review_chapters, "L9")
    with _level_value("L2", "judgement_chapters", ["推奨", "人が確かめること"]):
        expect(C.judgement_chapters("L2") == ("推奨", "人が確かめること") and C.judgement_chapters("L3") == tuple(C.LEVEL_DEFS["L3"]["judgement_chapters"]), "one level's change leaves the other")
        secs, missing = C.judgement_sections(_judgement(), "L2")
        expect(list(secs) == ["推奨", "人が確かめること"] and missing == [], (list(secs), missing))
    full = C.render("gate_ask", judge="a", recommend="b", checks="c", conditions="d", ships="e", risks="f", links="g", copied_at="h", key="k")
    for lv in (None, "L1", "L3"):
        expect(C.render_review(lv, judge="a", recommend="b", checks="c", conditions="d", ships="e", risks="f", links="g", copied_at="h", key="k") == full,
               f"all the chapters of the template: the body is byte for byte what it was ({lv})")
    l2 = C.render_review("L2", judge="a", recommend="b", checks="c", conditions="d", ships="e", risks="f", links="g", copied_at="h", key="k")
    expect(re.findall(r'^# (\S+) \{color="yellow_bg"\}', l2, re.M) == list(C.review_chapters("L2")) and l2 != full, "L2 is the chapters of its own value only")


@check
def ask_human_makes_the_release_row_with_the_chapters_of_the_epics_level():
    md = _filled(C.LEVEL_DEFS["L2"]["judgement_template"].removesuffix(".md"))
    six = ["判断すること", "推奨", "人が確かめること", "根拠（開くページ）", "判断の結果", "反映の記録"]
    expect(list(C.LEVEL_DEFS["L2"]["review_chapters"]) == six and len(C.LEVEL_DEFS["L3"]["review_chapters"]) == 9, "levels.toml: L2 asks the person for six chapters, L3 for nine")
    rc, out = _dry_run_release("L2", md)
    expect(rc == 0 and "6 chapters" in out and "PROBLEMS (0):" in out, "L2 makes the six chapters from levels.toml as it stands: " + out[:300])
    with _level_value("L2", "review_chapters", list(C.review_template_chapters())):
        rc, out = _dry_run_release("L2", md)
        expect(rc == 0 and "9 chapters" in out, "L2 follows its review_chapters (all nine too): " + out[:300])
    rc, out = _dry_run_release("L2", md)
    body = out.split("BODY (", 1)[1].split("\nPROBLEMS (", 1)[0]
    expect(re.findall(r'^# (\S+) \{color="yellow_bg"\}', body, re.M) == six and "# 完了条件の達成状況" not in body, body[:200])
    if True:
        rc, out = _dry_run_release("L3", md)
        expect(rc == 0 and "9 chapters" in out, "L3 is not touched by L2's setting: " + out[:300])
        expect(C.has_review_chapters(C.render_review("L2", judge="a", recommend="b", checks="c", conditions="d", ships="e", risks="f", links="g", copied_at="h", key="k"), "L2"),
               "a body of the level's own chapters is a review body")
        chat = io.StringIO()
        import ask_human as ah
        saved = C.ntn
        C.ntn = lambda *a, **k: {"properties": {C.LEVEL_PROP: {"select": {"name": "L2"}}}}  # a free-form ask of an L2 epic
        try:
            expect(ah.epic_level(_id(5)) == "L2" and ah.epic_level(None) is None, "the level of the epic row")
            body = C.review_markdown("k", _ASK_TEXT, level=ah.epic_level(_id(5)))
        finally:
            C.ntn = saved
        expect(re.findall(r'^# (\S+) \{color="yellow_bg"\}', body, re.M) == six, "a free-form ask follows the level too")


@check
def the_filled_template_of_the_level_has_no_problem_and_a_chapter_taken_out_is_reported():
    for lv in ("L2", "L3"):
        name = C.LEVEL_DEFS[lv]["judgement_template"].removesuffix(".md")
        rc, out = _dry_run_release(lv, _filled(name))
        expect(rc == 0 and "PROBLEMS (0):" in out, f"{lv}: the filled template is clean: " + out.split("PROBLEMS (", 1)[1][:400])
        for gone in C.judgement_chapters(lv):  # every chapter of the judgement is checked, the ones the row copies and the ones it does not
            rc, out = _dry_run_release(lv, _filled(name, drop=gone))
            expect(f"- 未確認：判定書に「{gone}」の章がありません。" in out, f"{lv}: without {gone}: " + out.split("PROBLEMS (", 1)[1][:400])
    # a level with a shorter judgement is checked by ITS chapters only: L2 drops two chapters, a page without them is clean for L2 and not for L3
    name = C.LEVEL_DEFS["L2"]["judgement_template"].removesuffix(".md")
    short = [c for c in C.judgement_chapters("L2") if c not in ("承認のあとに起きること", "戻し方")]
    md = _filled(name, drop="承認のあとに起きること")
    md = re.sub(r"(?ms)^# 戻し方 .*?(?=^# |\Z)", "", md)
    with _level_value("L2", "judgement_chapters", short):
        rc, out = _dry_run_release("L2", md)
        expect(rc == 0 and "PROBLEMS (0):" in out, "L2 with its own shorter judgement: " + out.split("PROBLEMS (", 1)[1][:300])
        rc, out = _dry_run_release("L3", md)
        expect("- 未確認：判定書に「戻し方」の章がありません。" in out, "the same page is short for L3: " + out.split("PROBLEMS (", 1)[1][:300])


@check
def the_release_path_copies_the_judgements_images_after_their_marks():
    import ask_human as ah
    md = _judgement(extra_ships="![変更前（main）](https://files.example.test/a.png)\n![変更後（branch）](https://files.example.test/b.png)")
    w = _ImgNotion([_img(1, "変更前（main）"), _img(2, "変更後（branch）")])
    with _img_sandbox(w) as (ah, notified), fake_prepare():
        saved = C.collect_release_facts
        C.collect_release_facts = lambda epic, key, root=None: dict(_facts(md=md, key=key, ui=True, page=dict(id="SRC", title="判定書", url="https://app.notion.com/p/src", edited="2026-11-10T01:00:00.000Z")),
                                                                    verdict=_verdict(ui=True))
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = ah.main(["--release", "--epic", _id(5), "--key", "u1"])
            expect(rc == 0 and out.getvalue().startswith("CREATED") and notified == ["u1"], (rc, out.getvalue(), notified))
            expect([n for n, _ in w.uploads] == ["shot1.png", "shot2.png"] and len(w.inserts) == 2, (w.uploads, w.inserts))
        finally:
            C.collect_release_facts = saved


@check
def collect_release_facts_reads_the_epic_the_phase_rows_and_the_judgement_page():
    w = World()
    hub, eid = w.add_epic(41, rows=[(ph, "完了") for ph in C.ORDER])
    w.hub_blocks[hub["id"]] = _hub_page_blocks() + [{"id": _id(0x8100), "type": "table_row", "has_children": False,
                                                    "table_row": {"cells": [[{"plain_text": "完了条件"}], [{"plain_text": "① 条件1\n② 条件2"}]]}}]
    pid = _add_decision(w, hub, 41, [], markdown=_judgement(), text="対象：example-org/example-app、ブランチ michiomochi/example-app-text-search")
    rid = w.row(hub, "設計")["id"]
    w.row_blocks[rid] = w.row_blocks[rid] + [{"id": _id(0x7300), "type": "child_page", "has_children": False, "child_page": {"title": "設計書"}}]
    with sandbox(w) as (ctl, tmp), _fake_repo({"michiomochi/example-app-text-search": ["scripts/a.py"]}):
        f = C.collect_release_facts(hub["id"], "k-承認")
    expect(f["hub_name"] == "ハブ41" and f["hub_url"] == "https://app.notion.com/p/" + eid and f["key"] == "k-承認", (f["hub_name"], f["hub_url"]))
    expect((f["repo"], f["branch"]) == ("example-org/example-app", "michiomochi/example-app-text-search") and f["page"]["id"] == pid and f["md"] == _judgement(), f["page"])
    expect(f["cond_cell"] == "① 条件1\n② 条件2" and f["ui"] is False and f["verdict"]["ok"], f["cond_cell"])
    expect(f["pr_drafts"] == [("PR 本文の下書き", "https://app.notion.com/p/" + _epic_id(0x7100 + 41))], f["pr_drafts"])
    expect(("設計：設計書", "https://app.notion.com/p/" + _epic_id(0x7300)) in f["deliverables"] and not any("判定書" in a for a, _ in f["deliverables"]), f["deliverables"])
    expect([a for a, _ in f["phase_rows"]] == [f"{ph}の行（作業メモ・完了レポート）" for ph in C.ORDER], f["phase_rows"])
    out = C.build_release_ask(f)
    expect(C.has_review_chapters(out["body"]) and out["title"].startswith("ハブ41: リリース承認（推奨 承認・条件 3/5 達成）") and "未確認：判定書の表は" in out["body"], out["title"])
    w2 = World()  # an epic without a judgement page: the row can still be built
    hub2, eid2 = w2.add_epic(42, rows=[(ph, "完了") for ph in C.ORDER])
    with sandbox(w2) as (ctl, tmp), _fake_repo({}):
        f2 = C.collect_release_facts(hub2["id"], "k2-承認")
    expect(f2["page"] is None and f2["md"] is None and f2["cond_cell"] == "" and C.build_release_ask(f2)["title"] == "ハブ42: リリース承認（判定書なし）", f2["page"])


# ---------------------------------------------------------------- levels (scripts/levels.toml: the values of L1 / L2 / L3 in ONE file)

def _levels_file(tmp, text):
    p = Path(tmp) / "levels.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _good_levels_text():
    return (HERE / "levels.toml").read_text(encoding="utf-8")


@check
def levels_toml_says_L1_has_no_row_L2_one_row_L3_two_rows():
    lv = C.LEVEL_DEFS
    expect(sorted(lv) == ["L1", "L2", "L3"], sorted(lv))
    expect(lv["L1"]["rows"] == [] and C.makes_phase_rows("L1") is False, "L1 makes no phase row")
    expect(len(lv["L2"]["rows"]) == 1 and len(lv["L3"]["rows"]) == 2, (lv["L2"]["rows"], lv["L3"]["rows"]))
    expect(C.makes_phase_rows("L2") and C.makes_phase_rows("L3") and C.makes_phase_rows(None), "L2 / L3 / a level not set yet (old epics) keep their rows")
    # the rows are read as phase ranges, and together they cover the level's phases once each
    expect([ph for t in lv["L3"]["rows"] for ph in C.row_phases(t)] == list(C.ORDER), [C.row_phases(t) for t in lv["L3"]["rows"]])
    expect(C.row_phases("設計〜リリース判定") == ("設計", "実装", "テスト", "リリース判定") and C.row_phases("設計") == ("設計",), "a range and a single phase")
    expect(C.row_phases("設計-リリース判定") is None and C.row_phases("リリース判定〜設計") is None and C.row_phases("作業") is None, "a broken title is not read")
    raises(C.PipelineError, C.makes_phase_rows, "L4")


@check
def a_broken_levels_toml_stops_loading_with_the_reason():
    import sfconfig
    good = _good_levels_text()
    expect(sfconfig.load_levels()["L2"]["rows"] == C.LEVEL_DEFS["L2"]["rows"], "the real file loads")
    cases = [("a level is missing", re.sub(r"(?ms)^\[L3\].*?(?=^\[|\Z)", "", good), "L3"),
             ("rows is not a list", good.replace("rows = []", 'rows = "x"', 1), "rows"),
             ("an unknown key", good.replace("[L2]\n", "[L2]\nrowz = []\n", 1), "rowz"),
             ("not TOML", "[L1\n", "levels.toml")]
    with tempfile.TemporaryDirectory() as tmp:
        for why, text, word in cases:
            e = raises(sfconfig.ConfigError, sfconfig.load_levels, _levels_file(tmp, text))
            expect(word in str(e), (why, str(e)))
        # as a command (what the Makefile and the deploy run): exit 1 and the reason, never a half-working default
        env = dict(os.environ, SF_CONFIG=str(sfconfig.config_path()), SF_LEVELS=str(_levels_file(tmp, cases[1][1])))
        r = subprocess.run([sys.executable, str(HERE / "sfconfig.py"), "check"], capture_output=True, text=True, env=env)
        expect(r.returncode == 1 and "rows" in r.stderr, (r.returncode, r.stderr))
        r = subprocess.run([sys.executable, "-c", "import epic_common"], capture_output=True, text=True, env=env, cwd=str(HERE))
        expect(r.returncode == 1 and "levels" in r.stderr and "rows" in r.stderr, "every pipeline script stops at import when levels.toml is broken: " + repr((r.returncode, r.stderr)))


@check
def level_names_for_the_texts_come_from_levels_toml():
    import sfconfig
    v = sfconfig.template_vars(sfconfig.get(expand=False))
    expect(v["l1_rows"] == "なし" and v["l2_rows"] == "／".join(C.LEVEL_DEFS["L2"]["rows"]) and v["l3_rows"] == "／".join(C.LEVEL_DEFS["L3"]["rows"]), (v["l1_rows"], v["l2_rows"], v["l3_rows"]))
    expect(v["deliverables"] == "、".join(C.LEVEL_DEFS["L2"]["deliverables"]), v["deliverables"])


def _product_hub(w, n, level, **kw):
    """An epic of kind プロダクト開発 with NO row, at `level` (None = the レベル property is not set: an epic made before the levels)."""
    hub, eid = w.add_epic(n, **kw)
    hub["properties"]["種別"] = {"select": {"name": "プロダクト開発"}}
    if level:
        hub["properties"]["レベル"] = {"select": {"name": level}}
    return hub, eid


@check
def an_L1_epic_gets_no_row_and_no_card_while_an_L2_epic_gets_its_row_and_card_from_the_controller():
    w = World()
    hub1, _ = _product_hub(w, 61, "L1")
    hub2, _ = _product_hub(w, 62, "L2")
    for h in (hub1, hub2):
        expect(h["properties"]["新規作成の停止"] == {"checkbox": False}, "no flag on either epic")
    with sandbox(w) as (ctl, tmp):
        lines = {re.search(r"EPIC ([0-9a-f]{8})", l).group(1): l for l in _planner_out(w).splitlines()}
        l1, l2 = lines[hub1["id"].replace("-", "")[-8:]], lines[hub2["id"].replace("-", "")[-8:]]
        expect("allow=none reasons=level_L1" in l1 and "allow=all reasons=-" in l2, (l1, l2))
        expect("missing_phase_rows" not in l1 + l2, "the planner does not make rows any more")
        lines, errors = ctl.run(True)
        expect(not errors, errors)
        expect(w.rows[hub1["id"]] == [] and [m[1] for m in w.made_rows] == [C.LEVEL_DEFS["L2"]["rows"][0]], w.made_rows)  # L1: 0 rows
        made = [c for c in w.calls if c[0] in ("create", "patch")]
        expect(not any(f"{_epic_id(0x200 + 61)}" in str(c) for c in made), made)
        expect(any(l.startswith("SKIP L1") and _epic_id(0x200 + 61)[-8:] in l for l in lines) and not any(l.startswith(("EPIC", "CREATED")) and _epic_id(0x200 + 61)[-8:] in l for l in lines), lines)
        expect(any(c[0] == "create" and f"phase-{_epic_id(0x200 + 62)}" in c[1] for c in made), "the L2 epic gets its card")


@check
def an_L1_epic_with_stray_phase_rows_still_starts_no_card():
    w = World()
    hub, eid = w.add_epic(63, rows=[("要件定義", "未着手"), ("設計", "未着手")])
    hub["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}, "レベル": {"select": {"name": "L1"}}})
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and _creates_of(w) == [] and not [c for c in w.calls if c[0] == "patch"], (lines, w.calls))
        expect(any(l.startswith("SKIP L1") and eid[-8:] in l for l in lines), lines)
        expect(ctl.run(False)[0] == lines, "a dry run says the same")


# ---------------------------------------------------------------- rows made by the gate controller (stage 6: rows = cards, the planner makes no row)

def _rows(w, hub):
    return [C.title_of(r, "タスク名") for r in w.rows[hub["id"]]]


@check
def an_L2_epic_without_a_row_gets_one_row_and_one_card_and_the_second_run_makes_nothing():
    w = World()
    hub, eid = _product_hub(w, 71, "L2")
    with sandbox(w) as (ctl, tmp):
        dry, errors = ctl.run(False)
        expect(not errors and w.made_rows == [] and w.creates() == [] and any(l.startswith("WOULD CREATE ROW") for l in dry), (dry, w.made_rows))
        lines, errors = ctl.run(True)
        expect(not errors, errors)
        expect(len(w.made_rows) == 1 and len(w.rows[hub["id"]]) == 1, f"L2 is one row: {w.made_rows}")  # the literal 1: a 2-row L2 in levels.toml fails here
        expect(len(w.creates()) == 1, f"and one card: {w.calls}")
        expect(_rows(w, hub) == list(C.LEVEL_DEFS["L2"]["rows"]), _rows(w, hub))
        row = w.rows[hub["id"]][0]
        expect(C.status_of(row) == "進行中" and C.row_phases(C.title_of(row, "タスク名")) == tuple(C.ORDER), "the one row works all five phases and is 進行中 now")
        items = w.made_rows[0][2]
        expect([i.split("：")[0] for i in items] == list(C.ORDER), f"one checklist item per phase, headed by its phase: {items}")
        card = _creates_of(w)[0]
        expect(card[1] == f"phase-{eid}-" + "+".join(C.ORDER), card[1])
        expect(w.create_argv[0][:4] == ["kanban", "--board", C.BOARD, "create"] and w.create_argv[0][4] == f"[{_rows(w, hub)[0]}] ハブ71", w.create_argv[0][:5])
        body = card[3]
        expect(all(f"[{ph}] " in body for ph in C.ORDER) and "ONE CARD FOR SEVERAL PHASES" in body, "the card body carries every phase's items")
        w.calls.clear()
        lines2, errors2 = ctl.run(True)
        expect(not errors2 and w.made_rows and len(w.made_rows) == 1 and w.creates() == [] and not [c for c in w.calls if c[0] == "patch"],
               f"the second run makes nothing: {lines2} {w.calls}")
        expect(not any("ROW" in l for l in lines2), lines2)


@check
def an_L2_epic_gets_its_deliverable_pages_before_the_one_card_and_an_L3_epic_gets_none():
    want = C.LEVEL_DEFS["L2"]["deliverables"]
    w = World()
    hub, eid = _product_hub(w, 74, "L2")
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        row = w.rows[hub["id"]][0]
        expect(not errors and [(p, t) for p, t, _ in w.child_pages] == [(row["id"], t) for t in want], (errors, w.child_pages))
        expect(any(l.startswith("MADE DELIVERABLES") for l in lines) and not any(l.startswith("CREATED DELIVERABLES") for l in lines), lines)
        body = _creates_of(w)[0][3]
        expect(all(f"[{t}](" in body for t in want) and "ops.py verify" in body, "the one card links every deliverable")
        w.calls.clear()
        expect(ctl.run(True)[1] == [] and len(w.child_pages) == len(want) and w.creates() == [], "the second run adds nothing")
    w = World()
    hub, eid = _product_hub(w, 75, "L3")
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and w.child_pages == [] and len(w.creates()) == 1 and "ops.py verify" not in _creates_of(w)[0][3], (errors, w.child_pages))


@check
def an_L3_epic_gets_two_rows_and_the_first_card_the_second_row_waits_for_it():
    w = World()
    hub, eid = _product_hub(w, 72, "L3")
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors, errors)
        expect(len(w.made_rows) == 2 and len(w.rows[hub["id"]]) == 2, f"L3 is two rows: {w.made_rows}")
        expect(_rows(w, hub) == list(C.LEVEL_DEFS["L3"]["rows"]), _rows(w, hub))
        expect(len(w.creates()) == 1 and w.creates()[0][1] == f"phase-{eid}-" + "+".join(C.row_phases(_rows(w, hub)[0])), "one card: the first row's")
        sts = [C.status_of(r) for r in w.rows[hub["id"]]]
        expect(sts == ["進行中", "未着手"], sts)
        w.calls.clear()
        expect(ctl.run(True)[1] == [] and w.creates() == [] and len(w.made_rows) == 2, "nothing more while the first card runs")
        w.finish_cards()
        w.rows[hub["id"]][0]["properties"]["ステータス"] = _status("完了")  # the reviewer passed the first card
        lines, errors = ctl.run(True)
        expect(not errors and len(w.creates()) == 1 and len(w.made_rows) == 2, "now the second row's card (no third row)")
        expect(w.creates()[0][1] == f"phase-{eid}-" + "+".join(C.row_phases(_rows(w, hub)[1])), w.creates()[0][1])


@check
def an_epic_without_a_level_is_made_the_L3_rows_the_way_it_ran_before_the_levels():
    w = World()
    hub, eid = _product_hub(w, 73, None)
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and _rows(w, hub) == list(C.LEVEL_DEFS["L3"]["rows"]), (errors, _rows(w, hub)))


@check
def an_epic_that_already_has_rows_is_not_touched():
    w = World()
    legacy, _ = _product_hub(w, 74, "L3", rows=[("要件定義", "完了"), ("設計", "未着手")])           # the old one-row-per-phase epic
    part, _ = _product_hub(w, 75, "L2", rows=[("設計", "完了"), ("リリース判定", "未着手")])          # a few rows only
    rng, _ = _product_hub(w, 76, "L3", rows=[(C.LEVEL_DEFS["L3"]["rows"][0], "完了")])                # one of the two new rows
    with sandbox(w) as (ctl, tmp):
        before = {h["id"]: [(r["id"], C.status_of(r)) for r in w.rows[h["id"]]] for h in (legacy, part, rng)}
        lines, errors = ctl.run(True)
        expect(w.made_rows == [], f"no row is made for an epic that has one: {w.made_rows}")
        expect({h["id"]: len(w.rows[h["id"]]) for h in (legacy, part, rng)} == {legacy["id"]: 2, part["id"]: 2, rng["id"]: 1}, "no row added")
        expect(all(w.pages[i]["properties"]["ステータス"]["status"]["name"] in ("完了", "未着手", "進行中") for h in before.values() for i, _ in h), "rows kept")
        expect(not [c for c in w.calls if c[0] == "patch" and c[2] == "キャンセル"], "no row is cancelled")
        expect(ctl.run(False)[0] == ctl.run(False)[0], "stable")


@check
def a_row_the_query_does_not_show_yet_is_not_made_twice():
    w = World()
    hub, eid = _product_hub(w, 77, "L2")
    w.hide_new_rows = True  # Notion lags: the row made in this run is not listed by the next query
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        ctl.run(True)
        ctl.run(True)
        expect(len(w.made_rows) == 1, f"the journal remembers the row: {w.made_rows}")
        expect(C.load_journal()[f"{eid}:row:{C.LEVEL_DEFS['L2']['rows'][0]}"]["row"] == "done", C.load_journal())


@check
def create_epic_rows_called_twice_makes_the_rows_once():
    w = World()
    hub, eid = _product_hub(w, 78, "L3")
    with sandbox(w) as (ctl, tmp):
        J = {}
        ctx = dict(now=int(time.time()), run_start=int(time.time()), deadline=None, cards=[], asked=set())
        for _ in range(2):
            out = []
            rows = C.query(C.TASK_DS, {"property": C.TASK_EPIC_PROP, "relation": {"contains": hub["id"]}})
            ctl.create_epic_rows(hub, eid, rows, "ハブ78", J, ctx, True, out)
        expect(len(w.made_rows) == 2 and len(w.rows[hub["id"]]) == 2, w.made_rows)
        # even when the second call does not see the rows (stale list): the journal holds
        ctl.create_epic_rows(hub, eid, [], "ハブ78", J, ctx, True, [])
        expect(len(w.made_rows) == 2, w.made_rows)


@check
def a_row_that_failed_is_retried_unless_a_half_filled_page_exists():
    w = World()
    hub, eid = _product_hub(w, 79, "L3")
    first, second = C.LEVEL_DEFS["L3"]["rows"]
    w.fail_make_row = {second: "create_row.py failed (rc 1): boom"}
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(errors and any(f"ERROR epic {eid[-8:]}" in l for l in errors) and _rows(w, hub) == [first], (errors, _rows(w, hub)))
        lines, errors = ctl.run(True)
        expect(not errors and _rows(w, hub) == [first, second] and len(w.made_rows) == 2, f"the missing second row is made by the next run: {_rows(w, hub)}")
        # a failure that left a half-filled page is never retried
        hub2, eid2 = _product_hub(w, 80, "L2")
        w.fail_make_row = {C.LEVEL_DEFS["L2"]["rows"][0]: ("create_row.py failed (rc 1)", "abc")}
        lines, errors = ctl.run(True)
        expect(errors and "page=abc" in "".join(errors), errors)
        n = len(w.made_rows)
        lines, errors = ctl.run(True)
        expect(len(w.made_rows) == n, "never a second row after a half-filled one")


@check
def a_row_of_a_range_is_one_row_for_the_status_the_gate_and_the_intake():
    w = World()
    title = C.LEVEL_DEFS["L2"]["rows"][0]
    hub, eid = _product_hub(w, 81, "L2", rows=[(title, "進行中")], hub_status="要件定義")
    w.board.append(dict(id="t_1", status="running", assignee="executor", tenant=C.tenant_of(eid), key=f"phase-{eid}-" + "+".join(C.ORDER), title=f"[{title}] ハブ81"))
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and w.made_rows == [] and w.creates() == [] and w.asks == [], (lines, w.calls))
        row = w.rows[hub["id"]][0]
        row["properties"]["ステータス"] = _status("完了")
        w.board[0]["status"] = "done"
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(hub) == "承認待ち" and len(w.asks) == 1, f"the range row 完了 = every phase finished: gate asked once: {lines} {w.asks}")
        ctl.run(True)
        expect(len(w.asks) == 1, "asked once")
    import notion_pending_tasks as N
    page = {"id": row["id"], "properties": {"タスク名": _title(title), C.TASK_EPIC_PROP: {"relation": [{"id": hub["id"]}]}}}
    expect(N.is_phase_row(page) and C.is_phase_row(page), "the intake never takes a row of a range as a task")
    page["properties"]["タスク名"] = _title("設計-リリース判定")
    expect(not C.is_phase_row(page) and C.broken_row_title("設計-リリース判定") and C.broken_row_title("リリース判定〜設計") and not C.broken_row_title("設計と実装の見直し"), "a typo is not a row")


@check
def a_row_of_a_range_without_items_is_cancelled_with_its_title():
    w = World()
    title = C.LEVEL_DEFS["L2"]["rows"][0]
    hub, eid = _product_hub(w, 82, "L2", rows=[(title, "未着手")], items={title: []})
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and any(l.startswith(f"CANCELLED [{title}]") for l in lines) and w.creates() == [], lines)


@check
def create_row_refuses_a_title_shaped_like_a_broken_range():
    import create_row as R
    spec = {"kind": "task", "title": "設計-リリース判定", "epic": _id(0x500), "overview": {k: "x" for k in R.TASK_OVERVIEW}, "checklist": ["a"]}
    e = raises(R.SpecError, R.validate, spec)
    expect("範囲" in str(e) or "range" in str(e), str(e))
    ok = R.validate(dict(spec, title=C.LEVEL_DEFS["L2"]["rows"][0]))
    expect(ok["title"] == C.LEVEL_DEFS["L2"]["rows"][0], ok)


@check
def the_health_monitor_counts_a_row_of_a_range_once_and_says_nothing_missing():
    import epic_pipeline_health as H
    w = World()
    title = C.LEVEL_DEFS["L2"]["rows"][0]
    hub, eid = _product_hub(w, 83, "L2", rows=[(title, "進行中")])
    rows = w.rows[hub["id"]]
    key = f"phase-{eid}-" + "+".join(C.ORDER)
    now = int(time.time())
    live = [dict(id="t_1", status="running", assignee="executor", tenant=C.tenant_of(eid), key=key, title=f"[{title}] x", idle_since=now)]
    with sandbox(w) as (ctl, tmp):
        alerts, lines = H.snapshot(now, live, w.hubs, lambda h: rows)
        text = "\n".join(lines)
        expect("MISSING" not in text and f"  {title}: row=進行中 cards=running:executor" in text, text)
        expect(not [a for a in alerts if eid[-8:] in a], alerts)
        alerts, _ = H.snapshot(now, [], w.hubs, lambda h: rows)
        stuck = [a for a in alerts if "stuck" in a]
        expect(len(stuck) == 1 and title in stuck[0], f"the row is stuck ONCE, not once per phase: {alerts}")


@check
def the_planner_status_does_not_count_a_row_of_a_range_as_a_task_and_reads_its_gate():
    w = World()
    title = C.LEVEL_DEFS["L2"]["rows"][0]
    hub, eid = _product_hub(w, 84, "L2", rows=[(title, "完了")])
    tid = _id(0x7000)
    t = {"id": tid, "url": "u", "last_edited_time": "2026-01-01T00:00:00.000Z", "properties": {"タスク名": _title("候補から作ったタスク"), "ステータス": _status("未着手")}}
    w.rows[hub["id"]].append(t)
    w.pages[tid] = t
    with sandbox(w) as (ctl, tmp):
        line = next(l for l in _planner_out(w).splitlines() if eid[-8:] in l)
        expect("tasks=1 " in line and "gates_due=リリース承認" in line, line)


@check
def reading_the_rows_of_an_epic_reads_a_row_that_works_several_phases_once():
    w = World()
    title = C.LEVEL_DEFS["L2"]["rows"][0]
    hub, eid = _product_hub(w, 85, "L2", rows=[(title, "完了")])
    rid = w.rows[hub["id"]][0]["id"]
    reads = []
    with sandbox(w) as (ctl, tmp):
        real = C.ntn

        def counting(*args, data=None):
            if args[0] == f"v1/blocks/{rid}/children" and not (len(args) > 2 and "PATCH" in args):
                reads.append(args[0])
            return real(*args, data=data)
        C.ntn = counting
        try:
            prows = C.phase_rows(hub["id"])
            expect(sorted(prows) == sorted(C.ORDER) and len({r["id"] for r in prows.values()}) == 1, "one row stands for all five phases")
            C.collect_ui_evidence(prows, ref=False)  # no decision page, no repository in the text: it reads the rows for the branch
        finally:
            C.ntn = real
        expect(len(reads) == 1, f"the row was read {len(reads)} times: a row of a range is read once")


def _kanban_create_builders(directory):
    """(file, line) of every list/tuple literal that builds a `kanban ... create` command: it holds the word create and kanban or a create-only flag."""
    import ast
    out = []
    for p in sorted(Path(directory).glob("*.py")):
        if p.name in ("epic_selftest.py", "ship_selftest.py"):
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, (ast.List, ast.Tuple)):
                words = {e.value for e in n.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                if "create" in words and words & {"kanban", "--idempotency-key", "--assignee"}:
                    out.append((p.name, n.lineno))
    return out


@check
def only_create_card_builds_a_kanban_create_command():
    hits = _kanban_create_builders(HERE)
    expect(hits and {f for f, _ in hits} == {"create_card.py"} and len(hits) == 1, f"`kanban create` is built in create_card.build_args only: {hits}")
    with tempfile.TemporaryDirectory() as tmp:  # the detector itself: a second builder (any shape) is found, an unrelated `create` is not
        (Path(tmp) / "a.py").write_text('def f(t):\n    return ["create", t, "--assignee", "x"]\n', encoding="utf-8")
        (Path(tmp) / "b.py").write_text('cmd = ["kanban", "--board", "b", "create", "t"]\n', encoding="utf-8")
        (Path(tmp) / "c.py").write_text('cmd = ["gh", "pr", "create", "-R", "r", "--base", "main"]\ncmd2 = ["ntn", "files", "create", "--filename", "f"]\n', encoding="utf-8")
        expect([f for f, _ in _kanban_create_builders(tmp)] == ["a.py", "b.py"], _kanban_create_builders(tmp))
    import create_card
    import epic_gate_controller as ctl
    expect(not hasattr(ctl, "kanban_create_args"), "the controller has no builder of its own")
    args = create_card.build_args(C.BOARD, "t", "executor", 20, "epic-x", "-", ("s1",), "k", model_args=("--model", "m", "--provider", "p"))
    expect(args[:5] == ["kanban", "--board", C.BOARD, "create", "t"] and args[-1] == "--json" and "--model" in args and args[args.index("--model") + 1] == "m", args)


# ---------------------------------------------------------------- stage 8: the gate controller completes the rows, limits the reviews, closes the L1 epic

def _closing_blocks(items, ticked=None, report=("実施内容：全部やった。",), remaining=("なし",)):
    """The blocks of a row the executor finished: the checklist (`ticked` = list of bool, default all ticked), then the 完了レポート with its 未確認・残件 section.
    A `remaining` line starting with '- ' is a bullet."""
    ticked = [True] * len(items) if ticked is None else ticked
    blks = [_block("heading_1", "概要"), _block("paragraph", "text"), _block("heading_1", "やることチェックリスト")]
    blks += [_block("to_do", t, c) for t, c in zip(items, ticked)]
    blks += [_block("heading_1", "作業メモ"), _block("to_do", "memo todo"), _block("heading_1", "完了レポート"), _block("heading_2", "実施内容")]
    blks += [_block("paragraph", r) for r in report]
    blks += [_block("heading_2", "未確認・残件")]
    blks += [_block("bulleted_list_item", r[2:]) if r.startswith("- ") else _block("paragraph", r) for r in remaining]
    return blks


def _level_world(n, level, titles=None, statuses=None):
    """An epic of `level` with its rows (levels.toml's, or `titles`) 未着手 and each row's checklist = levels.toml's items for it."""
    w = World()
    titles = list(titles if titles is not None else C.level_rows(level))
    hub, eid = w.add_epic(n, rows=[(t, (statuses or {}).get(t, "未着手")) for t in titles], items={t: C.row_items(level, t) for t in titles})
    hub["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}, "レベル": {"select": {"name": level}}})
    return w, hub, eid


def _card_key(eid, title):
    return f"phase-{eid}-" + "+".join(C.row_phases(title))


def _row_of(w, hub, title):
    return w.row(hub, title)


def _touch(row, stamp):
    """The row was edited (Notion's last_edited_time moves)."""
    row["last_edited_time"] = f"2026-02-{stamp:02d}T00:00:00.000Z"


def _alerts(lines):
    return [l for l in lines if l.startswith("ALERT")]


def _patches(w, page_id=None):
    return [c for c in w.calls if c[0] == "patch" and (page_id is None or c[1] == page_id)]


@check
def levels_toml_gives_every_level_a_review_limit_one_for_L1_and_three_for_L2_and_L3():
    expect([C.max_reviews(lv) for lv in ("L1", "L2", "L3")] == [1, 3, 3], "L1 1, L2 3, L3 3 (the literals: a changed levels.toml fails here)")
    expect(C.max_reviews(None) == C.max_reviews("L3"), "an epic without a level is L3")
    raises(C.PipelineError, C.max_reviews, "L9")
    import sfconfig
    expect("max_reviews" in sfconfig.LEVEL_KEYS and sfconfig.LEVEL_KEYS["max_reviews"] == "int", sfconfig.LEVEL_KEYS)
    with tempfile.TemporaryDirectory() as tmp:
        text = (HERE / "levels.toml").read_text(encoding="utf-8").replace("max_reviews = 3\n", "", 1)
        raises(sfconfig.ConfigError, sfconfig.load_levels, _levels_file(tmp, text))  # a level without max_reviews is refused at load


@check
def a_row_is_completed_by_the_gate_controller_once_the_reviewer_passed_and_the_next_row_starts_in_the_same_run():
    w, hub, eid = _level_world(81, "L3")
    first, second = C.level_rows("L3")
    with sandbox(w) as (ctl, tmp):
        expect(not ctl.run(True)[1], "start")
        row1, row2 = _row_of(w, hub, first), _row_of(w, hub, second)
        key1 = _card_key(eid, first)
        w.row_blocks[row1["id"]] = _closing_blocks(C.row_items("L3", first))
        w.request_review(key1, 1)
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(row1) == "進行中" and not _alerts(lines) and not any(l.startswith("COMPLETED") for l in lines), (lines, "the card waits for the review: nothing yet"))
        w.pass_review(key1, 1)
        w.calls.clear()
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(row1) == "完了" and not _alerts(lines), (lines, errors))
        expect(sum(1 for l in lines if l.startswith("COMPLETED")) == 1 and f"[{first}]" in [l for l in lines if l.startswith("COMPLETED")][0], lines)
        expect(_patches(w, row1["id"]) == [("patch", row1["id"], "完了")] and w.calls[0][:3] == ("patch", row1["id"], "完了"), f"the row is completed first: {w.calls[:3]}")
        expect([c[1] for c in w.creates()] == [_card_key(eid, second)] and C.status_of(row2) == "進行中", "and the second row's card starts in the same run")
        expect(w.release_calls == [], "the first row has no リリース判定: the judgement is not read")
        w.calls.clear()
        lines, errors = ctl.run(True)
        expect(not errors and _patches(w, row1["id"]) == [] and not any(l.startswith("COMPLETED") for l in lines), (lines, "the second run completes nothing again"))


@check
def a_card_done_without_a_reviewer_pass_does_not_complete_its_row_and_alerts_once():
    w, hub, eid = _level_world(82, "L3")
    first = C.level_rows("L3")[0]
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        row1, key1 = _row_of(w, hub, first), _card_key(eid, first)
        w.row_blocks[row1["id"]] = _closing_blocks(C.row_items("L3", first))
        w.finish_cards(key1)  # kanban says done, but no reviewer run ever passed it (the executor completed it itself, or a person did)
        w.calls.clear()
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(not errors and len(al) == 1 and "reviewer" in al[0] and w.card_of(key1)["id"] in al[0] and eid[-8:] in al[0], (lines, errors))
        expect(C.status_of(row1) == "進行中" and _patches(w, row1["id"]) == [], "the row is not completed")
        for _ in range(3):
            lines, errors = ctl.run(True)
            expect(not errors and not _alerts(lines) and _patches(w, row1["id"]) == [], (lines, "no second alert, no second try"))
        w.request_review(key1, 1)  # a pass that came BEFORE the last request does not count either
        w.reviews[w.card_of(key1)["id"]] = dict(requests=2, passed=0)
        w.card_of(key1)["status"] = "done"
        expect(not ctl.run(True)[1] and C.status_of(row1) == "進行中", "still no pass after the last request")


@check
def a_row_with_an_unticked_or_headless_item_is_not_completed_and_alerts_once_until_the_row_changes():
    w, hub, eid = _level_world(83, "L3")
    first = C.level_rows("L3")[0]
    items = C.row_items("L3", first)
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        row1, key1 = _row_of(w, hub, first), _card_key(eid, first)
        w.pass_review(key1, 1)
        w.row_blocks[row1["id"]] = _closing_blocks(items, ticked=[True] + [False] * (len(items) - 1))
        w.row_reads.clear()
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(not errors and len(al) == 1 and "済んでいない" in al[0] and items[1].split("：")[1][:10] in al[0] and C.status_of(row1) == "進行中", (lines, errors))
        n = len(w.row_reads)
        expect(n >= 1, "the row was read once")
        for _ in range(2):
            lines, errors = ctl.run(True)
            expect(not _alerts(lines) and not errors, lines)
        expect(len(w.row_reads) == n, f"an unchanged row is not read again (no loop): {w.row_reads}")
        _touch(row1, 9)  # edited, but still the same unfinished item: looked at again, the same reason is not said again
        lines, errors = ctl.run(True)
        expect(not errors and not _alerts(lines) and len(w.row_reads) == n + 1 and C.status_of(row1) == "進行中", (lines, w.row_reads))
        # the executor edits the row: the head of an item is gone (all ticked) -> re-checked, another problem, another alert
        headless = [items[0], items[1].split("：", 1)[1]]
        w.row_blocks[row1["id"]] = _closing_blocks(headless)
        _touch(row1, 2)
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(len(al) == 1 and "頭" in al[0] and C.status_of(row1) == "進行中", (lines, "an item without its `<工程名>：` head keeps the row open"))
        expect(not _alerts(ctl.run(True)[0]), "once")
        # a head of a phase the row does not work is no head for it either
        w.row_blocks[row1["id"]] = _closing_blocks([items[0], "実装：この行の工程ではない"])
        _touch(row1, 3)
        expect(len(_alerts(ctl.run(True)[0])) == 1 and C.status_of(row1) == "進行中", "a foreign head")
        w.row_blocks[row1["id"]] = _closing_blocks(items)
        _touch(row1, 4)
        lines, errors = ctl.run(True)
        expect(not errors and not _alerts(lines) and C.status_of(row1) == "完了" and any(l.startswith("COMPLETED") for l in lines), (lines, "fixed: completed, no alert"))


@check
def a_row_with_an_empty_or_unfiled_完了レポート_is_not_completed_the_same_rule_as_remaining_work_check():
    w, hub, eid = _level_world(84, "L3")
    first = C.level_rows("L3")[0]
    items = C.row_items("L3", first)
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        row1, key1 = _row_of(w, hub, first), _card_key(eid, first)
        w.pass_review(key1, 1)
        for stamp, kw, why in ((2, dict(report=("未実施",)), "placeholder report"),
                               (4, dict(remaining=("- あとで直す（起票していない）",)), "an unfiled leftover"), (5, dict(remaining=()), "an empty 未確認・残件")):
            w.row_blocks[row1["id"]] = _closing_blocks(items, **kw)
            _touch(row1, stamp)
            lines, errors = ctl.run(True)
            expect(not errors and len(_alerts(lines)) == 1 and C.status_of(row1) == "進行中", (why, lines))
        w.row_blocks[row1["id"]] = _closing_blocks(items, remaining=("- 残りは [行](https://app.notion.com/p/abc123) に起票した",))
        _touch(row1, 6)
        lines, errors = ctl.run(True)
        expect(not errors and not _alerts(lines) and C.status_of(row1) == "完了", (lines, "a filed leftover (a Notion URL) passes, like remaining_work_check"))
    import remaining_work
    import epic_gate_controller as ctl2
    expect(ctl2.RW is remaining_work, "the gate controller calls scripts/remaining_work.py")
    tool = HERE.parent / "tools" / "remaining_work_check.py"
    if tool.is_file():  # a repo checkout (the staged copy of the scripts has no tools/)
        sys.path.insert(0, str(tool.parent))
        try:
            import remaining_work_check as R
        finally:
            sys.path.remove(str(tool.parent))
        expect(R.check is remaining_work.check, "one rule: the command an agent runs and the gate controller call the same function")


@check
def the_report_text_the_rule_reads_keeps_the_links_of_a_bullet_that_are_only_in_the_rich_text():
    link = {"plain_text": "起票した行", "href": "https://app.notion.com/p/abc"}
    blks = _closing_blocks(["a"], remaining=())
    blks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [link]}})
    st = C.close_state(blks)
    expect(st["items"] == [("a", True)], st["items"])
    import remaining_work
    expect(remaining_work.check(st["report"]) == [], (st["report"], "a link that lives in the href counts as the row's URL"))
    expect(st["report_filled"] is True, st)
    st2 = C.close_state(_closing_blocks(["a", "b"], ticked=[True, False], report=("未実施",)))
    expect(st2["items"] == [("a", True), ("b", False)] and st2["report_filled"] is False, st2)
    expect(C.close_state([])["items"] == [] and C.close_state([])["report"] == "", "an empty page")


@check
def the_judgement_problems_stop_the_row_that_holds_リリース判定_and_only_that_row():
    w, hub, eid = _level_world(85, "L3")
    first, second = C.level_rows("L3")
    key_of = {first: _card_key(eid, first), second: _card_key(eid, second)}
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        row1, row2 = _row_of(w, hub, first), _row_of(w, hub, second)
        w.row_blocks[row1["id"]] = _closing_blocks(C.row_items("L3", first))
        w.pass_review(key_of[first], 1)
        expect(not ctl.run(True)[1] and C.status_of(row1) == "完了" and C.status_of(row2) == "進行中", "the first row")
        w.row_blocks[row2["id"]] = _closing_blocks(C.row_items("L3", second))
        w.pass_review(key_of[second], 1)
        problem = "未確認：判定書に「戻し方」の章がありません。"
        w.release_problems[hub["id"]] = [problem]
        w.calls.clear()
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(not errors and len(al) == 1 and problem in al[0] and C.status_of(row2) == "進行中" and _patches(w, row2["id"]) == [], (lines, errors))
        expect(w.release_calls == [(hub["id"], f"{eid[-8:]}-承認")] and w.asks == [], (w.release_calls, "read once, with the release key; no approval is asked for an unfinished row"))
        for _ in range(2):
            lines, errors = ctl.run(True)
            expect(not _alerts(lines) and not errors, lines)
        expect(len(w.release_calls) == 1, f"not read again while the row is unchanged: {w.release_calls}")
        w.release_problems[hub["id"]] = []
        _touch(row2, 2)  # the judgement was fixed (its author edited the row)
        lines, errors = ctl.run(True)
        expect(not errors and not _alerts(lines) and C.status_of(row2) == "完了" and len(w.release_calls) == 2, (lines, w.release_calls))
        expect([a[0] for a in w.asks] == [f"{eid[-8:]}-承認"], f"and now the release approval is asked: {w.asks}")


@check
def a_dry_run_completes_nothing_and_remembers_nothing():
    w, hub, eid = _level_world(86, "L3")
    first = C.level_rows("L3")[0]
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        row1, key1 = _row_of(w, hub, first), _card_key(eid, first)
        w.row_blocks[row1["id"]] = _closing_blocks(C.row_items("L3", first))
        w.pass_review(key1, 1)
        w.calls.clear()
        lines, errors = ctl.run(False)
        expect(not errors and any(l.startswith("WOULD COMPLETE") for l in lines) and C.status_of(row1) == "進行中" and _patches(w) == [], (lines, w.calls))
        expect(ctl.run(False)[0] == lines, "a dry run says the same twice")
        w.row_blocks[row1["id"]] = _closing_blocks(C.row_items("L3", first), ticked=[False, False])
        before = sorted(p.name for p in Path(tmp).iterdir())
        lines, errors = ctl.run(False)
        expect(any(l.startswith("WOULD ALERT") for l in lines) and not _alerts(lines) and sorted(p.name for p in Path(tmp).iterdir()) == before, (lines, "a dry run does not write the alert state"))
        lines, errors = ctl.run(True)
        expect(len(_alerts(lines)) == 1, "the real run still alerts (the dry run remembered nothing)")


@check
def the_fourth_request_review_blocks_the_card_and_alerts_once_for_L2_and_L3():
    for n, level in ((87, "L2"), (88, "L3")):
        w, hub, eid = _level_world(n, level)
        first = C.level_rows(level)[0]
        key = _card_key(eid, first)
        with sandbox(w) as (ctl, tmp):
            ctl.run(True)
            for k in (1, 2, 3):
                w.request_review(key, k)
                w.card_of(key)["status"] = "running"  # the reviewer is on it
                lines, errors = ctl.run(True)
                expect(not errors and w.blocks == [] and not _alerts(lines), (level, k, lines))
            w.request_review(key, 4)  # the card waits in review: kanban cannot block it from there; the next run does when the reviewer claimed it
            lines, errors = ctl.run(True)
            expect(not errors and w.blocks == [] and not _alerts(lines), (level, lines, "review status: wait"))
            w.card_of(key)["status"] = "running"
            lines, errors = ctl.run(True)
            al = _alerts(lines)
            expect(not errors and len(w.blocks) == 1 and w.blocks[0][0] == w.card_of(key)["id"] and len(al) == 1 and w.card_of(key)["id"] in al[0], (level, lines, w.blocks))
            expect("4" in al[0] and "3" in al[0] and level in al[0], f"the alert says the count and the limit: {al[0]}")
            expect(w.card_of(key)["status"] == "blocked", "the card is blocked")
            for _ in range(3):
                lines, errors = ctl.run(True)
                expect(not errors and len(w.blocks) == 1 and not _alerts(lines), (level, lines, "once"))
            # a person unblocks it and lets the executor go on: the same 4 requests do not block it again; a 5th does
            w.card_of(key)["status"] = "ready"
            expect(not ctl.run(True)[1] and len(w.blocks) == 1, "no loop after the unblock")
            w.request_review(key, 5)
            w.card_of(key)["status"] = "running"
            lines, errors = ctl.run(True)
            expect(len(w.blocks) == 2 and len(_alerts(lines)) == 1, (lines, "a further request blocks again, one more alert"))


@check
def the_review_limit_of_an_L1_card_is_one_and_a_card_that_passed_is_never_blocked():
    w = World()
    hub, eid = w.add_epic(89, rows=[("ログを直す", "進行中")])
    hub["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}, "レベル": {"select": {"name": "L1"}}})
    row = w.rows[hub["id"]][0]
    w.board.append(dict(id="t_l1", status="running", assignee="executor", tenant=C.tenant_of(eid), key="", title="ログを直す"))
    w.bodies["t_l1"] = f"Follow the skills. Notion page: {row['url']}. Goal: fix the log."
    w.board.append(dict(id="t_other", status="running", assignee="executor", tenant="epic-ffffffff", key="", title="another epic"))
    w.reviews["t_other"] = dict(requests=9, passed=0)
    with sandbox(w) as (ctl, tmp):
        w.reviews["t_l1"] = dict(requests=1, passed=0)
        lines, errors = ctl.run(True)
        expect(not errors and w.blocks == [] and not _alerts(lines), (lines, "the first review is the one L1 gets; another epic's card is not this epic's business"))
        w.reviews["t_l1"] = dict(requests=2, passed=0)
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(not errors and [b[0] for b in w.blocks] == ["t_l1"] and len(al) == 1 and "L1" in al[0], (lines, w.blocks))
        expect(not _alerts(ctl.run(True)[0]) and len(w.blocks) == 1, "once")
    w = World()  # the 4th request was passed by the reviewer before the controller looked: it is done, there is nothing to block
    hub, eid = w.add_epic(90, rows=[("ログを直す", "進行中")])
    hub["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}, "レベル": {"select": {"name": "L2"}}})
    w.board.append(dict(id="t_d", status="done", assignee="executor", tenant=C.tenant_of(eid), key="k", title="x"))
    w.reviews["t_d"] = dict(requests=4, passed=104)
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and w.blocks == [], (lines, w.blocks))


@check
def a_block_that_kanban_refuses_is_one_alert_and_is_tried_again_by_the_next_run():
    w, hub, eid = _level_world(91, "L2")
    key = _card_key(eid, C.level_rows("L2")[0])
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        w.request_review(key, 4)
        w.card_of(key)["status"] = "running"
        w.fail_block = {w.card_of(key)["id"]}
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(not errors and len(al) == 1 and "止められ" in al[0] and w.blocks == [], (lines, errors))
        expect(not _alerts(ctl.run(True)[0]) and w.blocks == [], "the failure is not announced again")
        w.fail_block = set()
        lines, errors = ctl.run(True)
        expect(len(w.blocks) == 1 and len(_alerts(lines)) == 1, (lines, "retried: blocked, announced"))


@check
def an_L1_epic_is_closed_in_the_same_run_that_completes_its_work_row():
    def l1(n, rows, level="L1"):
        w = World()
        hub, eid = w.add_epic(n, rows=rows)
        hub["properties"].update({"種別": {"select": {"name": "プロダクト開発"}}, "レベル": {"select": {"name": level}}})
        return w, hub, eid
    items = ["ログの出力先を直す", "テストを足す"]

    def card(w, hub, eid, row, status, passed):
        w.board.append(dict(id="t_a", status=status, assignee="executor", tenant=C.tenant_of(eid), key="", title=row["properties"]["タスク名"]["title"][0]["plain_text"]))
        w.bodies["t_a"] = f"Notion page: {row['url']}"
        w.reviews["t_a"] = dict(requests=1, passed=passed)
    # done + reviewer pass + everything ticked: the row is 完了 and the epic is 完了, both in one run
    w, hub, eid = l1(92, [("ログを直す", "進行中")], )
    row = w.rows[hub["id"]][0]
    w.row_blocks[row["id"]] = _closing_blocks(items)
    card(w, hub, eid, row, "done", 101)
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(row) == "完了" and C.status_of(hub) == "完了", (lines, errors))
        order = [(c[1], c[2]) for c in _patches(w)]
        expect(order == [(row["id"], "完了"), (hub["id"], "完了")], f"the row first, then the epic: {order}")
        expect(any(l.startswith("CLOSED") and eid[-8:] in l for l in lines), lines)
        w.calls.clear()
        ctl.run(True)
        expect(_patches(w) == [], "nothing again")
    # the same, but the epic still has another open work row: it stays open
    w, hub, eid = l1(93, [("ログを直す", "進行中"), ("もう1つの作業", "未着手")])
    row = w.rows[hub["id"]][0]
    w.row_blocks[row["id"]] = _closing_blocks(items)
    card(w, hub, eid, row, "done", 101)
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(row) == "完了" and C.status_of(hub) != "完了" and not any(l.startswith("CLOSED") for l in lines), lines)
    # a row the reviewer already set 完了 (no card to look at) closes the epic too; an epic without any work row is never closed
    w, hub, eid = l1(94, [("ログを直す", "完了")])
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(hub) == "完了", (lines, "all work rows are 完了"))
    w, hub, eid = l1(95, [])
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and C.status_of(hub) != "完了" and _patches(w) == [], (lines, "the task row of create_row --level L1 may not exist yet"))
    # no pass: the row stays, the epic stays, one alert
    w, hub, eid = l1(96, [("ログを直す", "進行中")])
    row = w.rows[hub["id"]][0]
    w.row_blocks[row["id"]] = _closing_blocks(items)
    card(w, hub, eid, row, "done", 0)
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        expect(not errors and len(_alerts(lines)) == 1 and C.status_of(row) == "進行中" and C.status_of(hub) != "完了", (lines, errors))
        expect(not _alerts(ctl.run(True)[0]), "once")
    # a dry run only says what it would do
    w, hub, eid = l1(97, [("ログを直す", "進行中")])
    row = w.rows[hub["id"]][0]
    w.row_blocks[row["id"]] = _closing_blocks(items)
    card(w, hub, eid, row, "done", 101)
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(False)
        expect(not errors and C.status_of(hub) != "完了" and _patches(w) == [] and any(l.startswith("WOULD COMPLETE") for l in lines), lines)
    # an L2 epic whose rows are all done is NOT closed by this (a person's release approval decides) - only the L1 path closes the epic
    w, hub, eid = _level_world(98, "L2", statuses={C.level_rows("L2")[0]: "完了"})
    with sandbox(w) as (ctl, tmp):
        ctl.run(True)
        expect(not any(c[1] == hub["id"] and c[2] == "完了" for c in _patches(w)), "an L2 epic is closed by the release approval only")


@check
def lowering_the_level_from_L3_to_L2_alerts_once_and_changes_no_row():
    w, hub, eid = _level_world(99, "L3")  # its rows are L3's
    hub["properties"]["レベル"] = {"select": {"name": "L2"}}  # ... and the person lowered the level to L2
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(True)
        al = _alerts(lines)
        expect(not errors and len(al) == 1 and "L3→L2" in al[0] and eid[-8:] in al[0], (lines, errors))
        expect(w.made_rows == [] and not any(l.startswith(("CANCELLED", "CREATED ROW")) for l in lines), "no row is made or cancelled by the lowering")
        expect(sorted(_rows(w, hub)) == sorted(C.level_rows("L3")), _rows(w, hub))
        for _ in range(3):
            lines, errors = ctl.run(True)
            expect(not errors and not _alerts(lines), (lines, "once"))
        hub["properties"]["レベル"] = {"select": {"name": "L3"}}  # back to L3: nothing to say
        expect(not _alerts(ctl.run(True)[0]), "L3 on L3 rows")
    # no alert: the rows are the level's own, the level was raised (L2 rows under L3), an old-form epic, an epic without a level
    for n, level, titles in ((100, "L2", None), (101, "L3", C.level_rows("L2")), (102, "L2", list(C.ORDER)), (103, None, None)):
        w, hub, eid = _level_world(n, level or "L3", titles=titles)
        if level is None:
            del hub["properties"]["レベル"]
        with sandbox(w) as (ctl, tmp):
            lines, errors = ctl.run(True)
            expect(not errors and not _alerts(lines), (n, level, lines))
    w, hub, eid = _level_world(104, "L3")  # a dry run says WOULD ALERT and keeps no state
    hub["properties"]["レベル"] = {"select": {"name": "L2"}}
    with sandbox(w) as (ctl, tmp):
        lines, errors = ctl.run(False)
        expect(any(l.startswith("WOULD ALERT") and "L3→L2" in l for l in lines) and not _alerts(lines), lines)
        expect(len(_alerts(ctl.run(True)[0])) == 1, "the real run still says it")


@check
def the_cron_wrapper_posts_the_ALERT_lines_without_failing_the_run():
    import epic_gate_apply as A
    with sandbox() as (ctl, tmp):
        def go(out):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = A.main(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
            return rc, buf.getvalue()
        rc, out = go("EPIC x\nCOMPLETED [a] h: done\nALERT row-close 12345678 [a] h: no pass\nWOULD ALERT y\nALERT review-limit t_1: 4th\n")
        expect(rc == 0 and out.count("ALERT") == 2 and "COMPLETED" not in out and "WOULD" not in out, (rc, out))
        rc, out = go("EPIC x\nCOMPLETED [a] h: done\n")
        expect(rc == 0 and out == "", "completing a row is silent: Notion shows it")


@check
def kanban_reviews_reads_the_requests_and_the_reviewer_pass_from_the_task_runs_read_only():
    with sandbox() as (ctl, tmp):
        db = tmp / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,"
                    " created_at INTEGER NOT NULL, tenant TEXT, idempotency_key TEXT)")
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT, status TEXT NOT NULL, outcome TEXT, started_at INTEGER NOT NULL, ended_at INTEGER)")
        con.executemany("INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, tenant, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)", [
            ("t_1", "two reviews, passed", "page https://www.notion.so/aaaaaaaa", "reviewer", "done", 20, 1, "epic-11111111", "k1"),
            ("t_2", "four requests", "b", "reviewer", "review", 20, 2, "epic-11111111", "k2"),
            ("t_3", "a pass, then a new request", "c", "executor", "ready", 20, 3, "epic-11111111", "k3"),
            ("t_4", "done by the executor itself", "d", "executor", "done", 20, 4, "epic-22222222", "k4"),
            ("t_5", "archived", "e", "executor", "archived", 20, 5, "epic-22222222", "k5"),
            ("t_6", "never reviewed", None, "executor", "running", 20, 6, None, None)])
        runs = [("t_1", "executor", "review", "review_requested"), ("t_1", "reviewer", "ready", "changes_requested"), ("t_1", "executor", "review", "review_requested"), ("t_1", "reviewer", "done", "completed"),
                ("t_2", "executor", "review", "review_requested"), ("t_2", "reviewer", "ready", "changes_requested"), ("t_2", "executor", "review", "review_requested"), ("t_2", "reviewer", "ready", "changes_requested"),
                ("t_2", "executor", "review", "review_requested"), ("t_2", "reviewer", "ready", "changes_requested"), ("t_2", "executor", "review", "review_requested"),
                ("t_3", "executor", "review", "review_requested"), ("t_3", "reviewer", "done", "completed"), ("t_3", "executor", "review", "review_requested"),
                ("t_4", "executor", "done", "completed"), ("t_6", "executor", "rate_limited", "rate_limited"), ("t_6", "executor", "running", None)]
        con.executemany("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES (?,?,?,?,1,2)", runs)
        con.commit()
        con.close()
        sha = subprocess.run(["shasum", str(db)], capture_output=True, text=True).stdout
        got = {c["id"]: c for c in C.kanban_reviews(db)}
        expect(subprocess.run(["shasum", str(db)], capture_output=True, text=True).stdout == sha, "db modified")
        expect(sorted(got) == ["t_1", "t_2", "t_3", "t_4", "t_6"], f"archived hidden: {sorted(got)}")
        expect((got["t_1"]["requests"], got["t_1"]["passed"]) == (2, 4) and got["t_1"]["status"] == "done" and "aaaaaaaa" in got["t_1"]["body"], got["t_1"])
        expect((got["t_2"]["requests"], got["t_2"]["passed"]) == (4, 0), got["t_2"])
        expect((got["t_3"]["requests"], got["t_3"]["passed"]) == (2, 0), f"a pass older than the last request is none: {got['t_3']}")
        expect((got["t_4"]["requests"], got["t_4"]["passed"]) == (0, 0), f"an executor's own completion is no reviewer pass: {got['t_4']}")
        expect((got["t_6"]["requests"], got["t_6"]["passed"], got["t_6"]["tenant"], got["t_6"]["body"]) == (0, 0, "", ""), got["t_6"])
        raises(C.PipelineError, C.kanban_reviews, tmp / "missing.db")


# ---------------------------------------------------------------- runner

def main(argv=None, quiet=False):
    """Run every check. Returns 0 when all pass. Any Python warning raised during the run is a failure."""
    import ship_selftest  # the checks of ship_approved.py (they use the helpers of this module)
    ship_selftest.register(sys.modules[__name__])
    results = []

    def forbid(cmd, input=None, timeout=90):  # safety net: the suite must never run ntn / hermes / ask_human for real
        raise AssertionError(f"selftest tried to run an external command: {cmd[:3]}")
    import urllib.request
    net_attempts = []

    def no_network(req, *a, **k):  # safety net: the suite must never reach Discord (or anything else); the checks that need HTTP install their own fake
        net_attempts.append(getattr(req, "full_url", str(req)))
        raise AssertionError("selftest tried to open a network connection")
    saved_sh, saved_sh_bytes, saved_urlopen = C._sh, C._sh_bytes, urllib.request.urlopen
    C._sh = forbid
    C._sh_bytes = lambda cmd, input=None, timeout=120: forbid(cmd)  # file uploads (ntn files create) are faked by the checks that need them
    urllib.request.urlopen = no_network
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            for fn in TESTS:
                try:
                    fn()
                    results.append((fn.__name__, None))
                except Exception as e:  # report every failing check
                    results.append((fn.__name__, f"{type(e).__name__}: {e}"))
        finally:
            C._sh, C._sh_bytes = saved_sh, saved_sh_bytes
            urllib.request.urlopen = saved_urlopen
        gc.collect()
    failed = [(n, e) for n, e in results if e]
    failed += [("network", f"real network attempt: {u}") for u in net_attempts]
    for w in caught:
        failed.append(("python-warning", f"{w.category.__name__}: {w.message} ({w.filename}:{w.lineno})"))
    if not quiet:
        for n, e in results:
            print(("ok   " if not e else "FAIL ") + n + ("" if not e else f": {e}"))
        for n, e in failed:
            if n == "python-warning":
                print(f"FAIL {n}: {e}")
        print(f"selftest: {len(results)} checks, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
