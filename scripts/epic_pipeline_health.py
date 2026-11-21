#!/usr/bin/env python3
"""Pipeline health monitor (read-only, no LLM, stable output).

stdout is what the monitoring agent sees (the cron monitor wakes the agent only when stdout changes):
  * one line per problem: `ALERT ...` (or `ERROR ...` when the monitor itself failed, exit code 1)
  * the fixed line `ok` when nothing is wrong
  * no timestamps, ages or counters that change every run
The full snapshot (EPIC lines, phase rows, cards, all alerts) is written to ~/.hermes/state/epic_health_snapshot.txt.

Per managed epic: the rows (private task rows titled with a phase or a range of phases <first>〜<last>; a row is one card) and the kanban cards of the
epic's phase cards (matched by idempotency key phase-<epic_id>-<phase>[-B] or phase-<epic_id>-<ph1>+<ph2>...; epic_id = the hub row id; the kanban sqlite
is opened read-only). Every row ALERT (stuck, blocked, レビュー中, live card while 未着手, duplicate, more than one live card) is made ONCE PER ROW,
named by the row title: a row of a range that works five phases never makes five alerts.

Checks:
  * controller: heartbeat ~/.hermes/state/epic_gate_status.json missing / last_success older than 600 s; journal
    (partial creation pending > 10 min, fewer cards than groups)
  * rows/cards: stuck 進行中 row without a live card, blocked card, duplicate rows, more than one live card, live card while
    the row is 未着手, a person must decide (row レビュー中)
  * order: `ALERT order: <later phase> live while <earlier phase> not 完了` (a later phase has a live card although an earlier
    phase row is not finished)
  * intake: a non-phase タスク row left 未着手 for more than 10 minutes
  * orphan: a non-phase タスク row 進行中 with no edit for more than 15 minutes, no live kanban card (any board) whose
    text holds the row's page id and no agent session message (state.db of every profile) mentioning the page id in the last 15
    minutes: the worker's session ended midway and nothing will pick the row up again
  * closeable / nearly-done: such a row (nobody working on it) with a filled-in 完了レポート and a fully ticked checklist (closeable,
    idle more than 5 minutes) or exactly one unticked item (nearly-done, idle more than 10 minutes): the work is over but the status
    was never set 完了. Several unticked items, a placeholder report (なし / 未実施) or no checklist never raise either alert
  * review-unreflected: a non-phase row レビュー中 for more than 5 minutes that no row of the review DB points at and no live card
    covers: the status was set but ask_human.py was never run
  * context: a live card told to keep CONTEXT.md in its scratch workspace whose latest executor run (over 5 minutes) ended more
    than 2 minutes ago without leaving one, or left one older than that run's start (the next run would rebuild its context)
  * every row alert carries `[stage N]` that grows the longer the condition lasts (stage_of): an unfixed alert changes its text,
    so the monitor cron wakes its agent again (it wakes only when stdout changes)
"""
import argparse
import os
import sys
import time
import traceback

import epic_common as C

STALL_SECONDS = 900  # a ready card unclaimed this long is parked (e.g. blocker_auth respawn guard)
PHASES = C.ORDER
LIVE = C.LIVE_CARD_STATUSES
INTAKE_STALE_SECONDS = 600
ORPHAN_SECONDS = 900  # a 進行中 row nobody has touched this long, no live card and no recent session message mention: the work stopped
CLOSEABLE_SECONDS = 300  # a 進行中 row with every checklist item ticked and a filled 完了レポート, idle this long: closeable
NEARLY_DONE_SECONDS = 600  # same with exactly one unticked item left: nearly-done
REVIEW_UNREFLECTED_SECONDS = 300  # a レビュー中 row without a row in the review DB, idle this long
ACTIVITY_WINDOW_SECONDS = 900  # an agent session message mentioning the row's page id this recently means somebody is working on it
CONTEXT_MIN_RUN_SECONDS = 300  # an executor run shorter than this (crash at start-up) is not expected to have written CONTEXT.md
CONTEXT_GRACE_SECONDS = 120  # seconds after the run ended before a missing / stale CONTEXT.md raises the context alert
STAGE_OFFSETS = (1800, 5400, 12600)  # seconds past an alert's base age at which its text changes ([stage 2], [stage 3], ...)
STAGE_REPEAT_SECONDS = 14400  # after the last offset the stage grows once per 4 hours


def controller_alerts(now):
    """ALERT lines about the controller itself (stable text: no ages / timestamps)."""
    out = []
    st = C.read_status()
    if not st:
        deployed = C.read_json(C.state_path(C.DEPLOY_FILE), None)
        recent = isinstance(deployed, (int, float)) and 0 <= now - deployed <= C.HEARTBEAT_STALE_SECONDS
        if not recent:  # right after a deploy the first controller run has not happened yet
            out.append("ALERT controller heartbeat missing (epic_gate_status.json): the gate controller has not run")
    else:
        last = st.get("last_success")
        if last is None or now - last > C.HEARTBEAT_STALE_SECONDS:
            err = (st.get("error") or "")[:60]
            out.append(f"ALERT controller heartbeat stale: no successful run for more than {C.HEARTBEAT_STALE_SECONDS}s"
                       + (f" (last error: {err})" if err else ""))
    for key, e in sorted(C.load_journal().items()):
        tag = f"{e.get('epic', key.split(':')[0])[-8:]} [{e.get('phase', '?')}]"
        groups = e.get("groups") or {}
        n_cards = len(e.get("cards") or {})
        if e.get("row") == "pending" and now - e.get("started", now) > C.JOURNAL_STALE_SECONDS:
            out.append(f"ALERT journal {tag} partial creation pending (cards {n_cards}/{len(groups)})")
        elif e.get("row") == "done" and n_cards < len(groups):
            out.append(f"ALERT journal {tag} row started but only {n_cards}/{len(groups)} cards recorded")
    return out


def phase_cards(cards, tenant, eid, ph):
    return [x for x in cards if x["tenant"] == tenant and C.is_phase_card_key(x["key"], eid, ph)]


def row_cards(cards, tenant, eid, title):
    """The cards of the row titled `title`: those of any phase the row works (C.row_phases), each once (a merged card or the card of a range
    covers several phases)."""
    seen, out = set(), []
    for ph in C.row_phases(title) or ():
        for x in phase_cards(cards, tenant, eid, ph):
            if x["id"] not in seen:
                seen.add(x["id"])
                out.append(x)
    return out


def snapshot(now, cards, hubs, rows_of):
    """-> (alerts, lines). `lines` is the full snapshot (alerts included, each self-contained with the epic id)."""
    lines = controller_alerts(now)
    for h in sorted(hubs, key=lambda x: x["id"]):
        eid = h["id"].replace("-", "")  # the epic id is the hub row id
        e8 = eid[-8:]
        tenant = C.tenant_of(eid)
        stop = bool(h["properties"]["新規作成の停止"]["checkbox"])
        by_title = {}  # row title -> [row statuses]; a title twice is a duplicate
        for r in rows_of(h):
            nm = C.title_of(r, "タスク名").strip()
            if C.row_phases(nm) is not None:
                by_title.setdefault(nm, []).append(C.status_of(r))
        rows = {}  # phase -> statuses of the rows that work it (a row of a range stands for each of its phases)
        for nm, sts in by_title.items():
            for ph in C.row_phases(nm):
                rows.setdefault(ph, []).extend(sts)
        mine = [x for x in cards if x["tenant"] == tenant]
        active = [x for x in mine if x["status"] in LIVE]
        for x in mine:  # a ready card nobody claims (e.g. the dispatcher's respawn guard parks it): stalled
            if x["status"] == "ready" and x.get("idle_since") and now - x["idle_since"] > STALL_SECONDS:
                lines.append(f"ALERT {e8} card {x['id']} ready but not started (stalled)")
        live_of = C.live_phase_cards(cards, eid)
        lines.append(f"EPIC {e8} {C.title_of(h, '名前')[:24]} stop={'yes' if stop else 'no'}")
        listing, shown = [], set()  # (name, statuses | None): every row once, in phase order; a phase without a row is MISSING
        for ph in PHASES:
            owners = [nm for nm in by_title if ph in C.row_phases(nm)]
            if not owners:
                listing.append((ph, None))
            for nm in owners:
                if nm not in shown:
                    shown.add(nm)
                    listing.append((nm, by_title[nm]))
        for ph, sts in listing:
            ph_cards = row_cards(cards, tenant, eid, ph) if sts else phase_cards(cards, tenant, eid, ph)
            cs = ",".join(f"{x['status']}:{x['assignee']}" for x in ph_cards) or "-"
            lines.append(f"  {ph}: row={','.join(sts) if sts else 'MISSING'} cards={cs}")
            if sts and len(sts) > 1:
                lines.append(f"  ALERT {e8} {ph}: duplicate rows")
            if sts and "進行中" in sts and not any(x["status"] in LIVE for x in ph_cards):
                lines.append(f"  ALERT {e8} {ph}: row 進行中 but no live card (stuck)")
            if sts and "進行中" in sts and any(x["status"] == "blocked" for x in ph_cards):
                lines.append(f"  ALERT {e8} {ph}: card blocked")
            if sts and "レビュー中" in sts:
                lines.append(f"  ALERT {e8} {ph}: row レビュー中 (a person must decide; later phases wait)")
            if len([x for x in ph_cards if x["status"] in ("running", "ready", "todo")]) > 1:
                lines.append(f"  ALERT {e8} {ph}: more than one live card")
            if sts and "未着手" in sts and any(x["status"] in ("running", "ready", "todo") for x in ph_cards):
                lines.append(f"  ALERT {e8} {ph}: live card while row 未着手")
        # order: a later phase must not be live while an earlier phase row is not finished
        for i, ph in enumerate(PHASES):
            if not live_of.get(ph):
                continue
            shared = {x["id"] for x in live_of[ph]}  # a merged card covers several phases: its own earlier phases are not an order violation
            earlier = next((p for p in PHASES[:i] if rows.get(p) and not all(s in C.DONE_ROW for s in rows[p])
                            and not shared & {x["id"] for x in live_of.get(p, [])}), None)
            if earlier:
                lines.append(f"  ALERT order: {ph} live while {earlier} not 完了 (epic {e8})")
        if len(active) > 3:
            lines.append(f"  ALERT {e8} {len(active)} live cards for one epic")
    alerts = [l.strip() for l in lines if l.strip().startswith("ALERT")]
    return alerts, lines


# ---------------------------------------------------------------- staged ALERT text

def stage_of(age, base):
    """1, 2, 3 ... the longer a condition (older than `base` seconds) lasts: the ALERT text then changes, so the monitor cron
    (which wakes its agent only when stdout changes) wakes the agent again for an alert nobody fixed. Stable inside a stage."""
    n = 1 + sum(1 for o in STAGE_OFFSETS if age >= base + o)
    extra = age - base - STAGE_OFFSETS[-1]
    return n + (int(extra // STAGE_REPEAT_SECONDS) if extra >= STAGE_REPEAT_SECONDS else 0)


def _stage(age, base):
    return f" [stage {stage_of(age, base)}]"


def _minutes(seconds):
    return f"{int(seconds // 60)} minutes"


def _live_texts(card_texts):
    return [c["text"].replace("-", "").lower() for c in card_texts if c.get("status") in LIVE]


def _pid(p):
    return p["id"].replace("-", "").lower()


# ---------------------------------------------------------------- intake: tasks left 未着手

def intake_alerts(pages, now):
    """ALERT for every non-phase タスク row that is 未着手 and was last edited more than 10 minutes ago (staged)."""
    out = []
    for p in sorted(pages, key=lambda x: x["id"]):
        if C.is_phase_row(p):
            continue
        age = C.age_seconds(p.get("last_edited_time"), now)
        if age is not None and age > INTAKE_STALE_SECONDS:
            title = C.title_of(p, "タスク名").strip()[:30]
            out.append(f"ALERT intake: task {p['id'].replace('-', '')[-8:]} 未着手 for more than 10 minutes{_stage(age, INTAKE_STALE_SECONDS)}: {title}")
    return out


# ---------------------------------------------------------------- orphan: 進行中 rows nobody is working on

def _unattended(p, live, now, active_ids, min_age):
    """Age of a non-phase row when it is idle for more than `min_age`, has no live card mentioning it and no recent session
    message mentions it; else None."""
    if C.is_phase_row(p):
        return None
    age = C.age_seconds(p.get("last_edited_time"), now)
    pid = _pid(p)
    if age is None or age <= min_age or pid in active_ids or any(pid in t for t in live):
        return None
    return age


def orphan_alerts(pages, card_texts, now, active_ids=frozenset(), skip_ids=frozenset()):
    """ALERT for every non-phase 進行中 row idle for more than ORPHAN_SECONDS without a live card that mentions its page id and
    without a recent agent session message mentioning it (a card-less direct Slack row is 'working' while its session writes).
    `skip_ids`: rows already covered by a more specific alert (closeable / nearly-done). Phase rows have their own 'stuck' check.
    Text changes by stage, never by age."""
    out = []
    live = _live_texts(card_texts)
    for p in sorted(pages, key=lambda x: x["id"]):
        if C.status_of(p) != "進行中" or _pid(p) in skip_ids:
            continue
        age = _unattended(p, live, now, active_ids, ORPHAN_SECONDS)
        if age is not None:
            title = C.title_of(p, "タスク名").strip()[:30]
            out.append(f"ALERT orphan: task {_pid(p)[-8:]} 進行中 but no live card and no update for more than {_minutes(ORPHAN_SECONDS)}"
                       f"{_stage(age, ORPHAN_SECONDS)}: {title}")
    return out


# ---------------------------------------------------------------- done-but-open / nearly-done: 進行中 rows whose work is finished

_PLACEHOLDERS = {"", "未実施", "なし", "-", "ー", "未記入", "n/a"}


def _plain(rt):
    return "".join(x.get("plain_text", "") for x in rt or [])


def checklist_state(blks):
    """(total, ticked, report_filled) of a task page's blocks. total/ticked count the to_do items under the heading that holds
    やることチェックリスト (all to_do items when the page has no such heading); report_filled is True when the 完了レポート chapter
    holds text other than placeholders (未実施 / なし); callouts (template guidance) never count."""
    chapter, total, ticked, filled = "", 0, 0, False
    has_list = any(b.get("type") == "heading_1" and "チェックリス" in _plain(b["heading_1"].get("rich_text")) for b in blks)
    for b in blks:
        t = b.get("type")
        body = b.get(t) or {}
        if t == "heading_1":
            chapter = _plain(body.get("rich_text"))
        elif t == "to_do" and (not has_list or "チェックリス" in chapter):
            total += 1
            ticked += 1 if body.get("checked") else 0
        elif "完了レポート" in chapter and t in ("paragraph", "bulleted_list_item", "numbered_list_item", "table_row", "quote"):
            text = "".join(_plain(c) for c in body.get("cells", [])) if t == "table_row" else _plain(body.get("rich_text"))
            if text.strip().lower() not in _PLACEHOLDERS:
                filled = True
    return total, ticked, filled


def row_states(pages, now, live, active_ids, cache=None):
    """{page id: (total, ticked, report_filled)} for the 進行中 rows that could raise a closeable / nearly-done alert. Block reads
    are cached per (page, last_edited_time) in epic_close_cache.json, so an idle row costs one Notion read once."""
    cache = cache if cache is not None else C.read_json(C.state_path(C.CLOSE_CACHE_FILE), None)
    if not isinstance(cache, dict):
        cache = {}
    out, seen = {}, set()
    for p in pages:
        if C.status_of(p) != "進行中" or _unattended(p, live, now, active_ids, CLOSEABLE_SECONDS) is None:
            continue
        pid = _pid(p)
        seen.add(pid)
        ent = cache.get(pid)
        if not ent or ent.get("edited") != p.get("last_edited_time"):
            ent = dict(zip(("total", "ticked", "report"), checklist_state(C.iter_blocks(p["id"]))), edited=p.get("last_edited_time"))
            cache[pid] = ent
        out[pid] = (ent["total"], ent["ticked"], ent["report"])
    C.atomic_write_json(C.state_path(C.CLOSE_CACHE_FILE), {k: v for k, v in cache.items() if k in seen})
    return out


def close_alerts(pages, card_texts, now, states, active_ids=frozenset()):
    """-> (alerts, covered ids). A non-phase 進行中 row with no live card, no recent session activity and no edit for more than
    CLOSEABLE_SECONDS whose work is over but whose status is still 進行中:
      closeable: every checklist item is ticked and the 完了レポート is filled (the agent forgot to set 完了)
      nearly-done: the 完了レポート is filled and exactly one checklist item is left (idle more than NEARLY_DONE_SECONDS)
    Several unticked items, a placeholder report or an empty checklist never alert here (work still open, or the generic orphan alert).
    `states` comes from row_states(). These replace the generic orphan alert for the same row (more specific instruction)."""
    out, covered = [], set()
    live = _live_texts(card_texts)
    for p in sorted(pages, key=lambda x: x["id"]):
        pid = _pid(p)
        if C.status_of(p) != "進行中" or pid not in states:
            continue
        age = _unattended(p, live, now, active_ids, CLOSEABLE_SECONDS)
        if age is None:
            continue
        total, ticked, report = states[pid]
        title = C.title_of(p, "タスク名").strip()[:30]
        if report and total > 0 and ticked == total:
            out.append(f"ALERT closeable: task {pid[-8:]} 進行中 but every checklist item is ticked and the 完了レポート is filled"
                       f"{_stage(age, CLOSEABLE_SECONDS)}: {title}")
        elif report and total > 0 and ticked == total - 1 and age > NEARLY_DONE_SECONDS:
            out.append(f"ALERT nearly-done: task {pid[-8:]} 進行中 but the 完了レポート is filled and only one checklist item is left"
                       f"{_stage(age, NEARLY_DONE_SECONDS)}: {title}")
        else:
            continue
        covered.add(pid)
    return out, covered


# ---------------------------------------------------------------- review-unreflected: レビュー中 rows nobody asked a person about

def review_unreflected_alerts(pages, card_texts, review_rows, now):
    """ALERT for every non-phase row in ステータス レビュー中 (a person must decide) that no row of the review DB asks about and that no
    live card (e.g. a card in review) covers: the status was set but ask_human.py was never run, so nobody will ever see the request.
    A review row asks about a task when its 対象エピック is the task's エピック, or its キー starts with the task's id8 (<task id8>-<slug>).
    Rows from before the properties were dropped still count through 対象タスク / レビュー先 (read with .get; absent = nothing).
    Phase rows have their own レビュー中 alert."""
    asked, asked_epics, key_prefixes = set(), set(), set()
    for r in review_rows:
        pr = r.get("properties", {})
        asked_epics.update(x["id"].replace("-", "").lower() for x in (pr.get("対象エピック") or {}).get("relation", []))
        key = "".join(x.get("plain_text", "") for x in (pr.get("キー") or {}).get("rich_text", []))
        if "-" in key:
            key_prefixes.add(key.split("-", 1)[0].lower())
        asked.update(x["id"].replace("-", "").lower() for x in (pr.get("対象タスク") or {}).get("relation", []))
        url = (pr.get("レビュー先") or {}).get("url") or ""
        asked.update(m.replace("-", "").lower() for m in C._UUID_RE.findall(url))
    out, live = [], _live_texts(card_texts)
    for p in sorted(pages, key=lambda x: x["id"]):
        if C.is_phase_row(p) or C.status_of(p) != "レビュー中":
            continue
        age, pid = C.age_seconds(p.get("last_edited_time"), now), _pid(p)
        epics = {x["id"].replace("-", "").lower() for x in (p.get("properties", {}).get(C.TASK_EPIC_PROP) or {}).get("relation", [])}
        if age is None or age <= REVIEW_UNREFLECTED_SECONDS or pid in asked or pid[-8:] in key_prefixes or epics & asked_epics \
                or any(pid in t for t in live):
            continue
        title = C.title_of(p, "タスク名").strip()[:30]
        out.append(f"ALERT review-unreflected: task {pid[-8:]} レビュー中 but no row in the review DB asks for it"
                   f"{_stage(age, REVIEW_UNREFLECTED_SECONDS)}: {title}")
    return out


# ---------------------------------------------------------------- context: an executor run ended without keeping CONTEXT.md

def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def context_alerts(cards, now, mtime=_mtime):
    """ALERT for every live card that was told to keep CONTEXT.md (C.kanban_context_cards) in its scratch workspace when the latest
    finished executor run (longer than CONTEXT_MIN_RUN_SECONDS, ended more than CONTEXT_GRACE_SECONDS ago) left no CONTEXT.md, or
    one older than that run's start: the next run of the card starts without the context and rebuilds it. `mtime(path)` returns
    the file's modification time or None. Text: card id and run id only, staged like the row alerts."""
    out = []
    for c in sorted(cards, key=lambda x: x["id"]):
        if c["status"] not in LIVE or c.get("kind") != "scratch" or not c.get("workspace") or not c.get("run_ended"):
            continue
        started, ended = c.get("run_started") or 0, c["run_ended"]
        age = now - ended
        if ended - started < CONTEXT_MIN_RUN_SECONDS or age <= CONTEXT_GRACE_SECONDS:
            continue
        m = mtime(os.path.join(c["workspace"], C.CONTEXT_FILE))
        if m is not None and m >= started:
            continue
        what = "missing after" if m is None else "not updated during"
        out.append(f"ALERT context: card {c['id']} {C.CONTEXT_FILE} {what} executor run {c['run']}{_stage(age, CONTEXT_GRACE_SECONDS)}")
    return out


# ---------------------------------------------------------------- main

def collect(now):
    """All alerts and the full snapshot lines of one run (reads kanban, Notion). Raises on the monitor's own failures."""
    cards = C.kanban_cards()
    hubs = C.registry_hubs()
    alerts, lines = snapshot(now, cards, hubs,
                             lambda h: C.query(C.TASK_DS, {"property": C.TASK_EPIC_PROP, "relation": {"contains": h["id"]}}))
    pending = C.query(C.TASK_DS, {"property": "ステータス", "status": {"equals": "未着手"}})
    running = C.query(C.TASK_DS, {"property": "ステータス", "status": {"equals": "進行中"}})
    in_review = C.query(C.TASK_DS, {"property": "ステータス", "status": {"equals": "レビュー中"}})
    card_texts = C.kanban_card_texts()
    active = C.recent_message_ids(now, ACTIVITY_WINDOW_SECONDS)
    states = row_states(running, now, _live_texts(card_texts), active)
    closing, covered = close_alerts(running, card_texts, now, states, active)
    review_rows = C.query(C.REVIEW_DS) if any(C.status_of(p) == "レビュー中" and not C.is_phase_row(p) for p in in_review) else []
    extra = (intake_alerts(pending, now) + closing + orphan_alerts(running, card_texts, now, active, covered)
             + review_unreflected_alerts(in_review, card_texts, review_rows, now) + context_alerts(C.kanban_context_cards(), now))
    lines += extra
    return alerts + extra, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Epic pipeline health monitor (read-only)", allow_abbrev=False)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        import epic_selftest
        return epic_selftest.main(["health"])
    try:
        alerts, lines = collect(int(time.time()))
        C.write_text(C.state_path(C.SNAPSHOT_FILE), "\n".join(lines) + "\n\n" + (f"ALERTS: {len(alerts)}" if alerts else "ok") + "\n")
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        return 1
    print("\n".join(alerts) if alerts else "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
