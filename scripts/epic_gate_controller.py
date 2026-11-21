#!/usr/bin/env python3
"""Gate controller (light version): create kanban cards for the phases of every managed epic, in work order.

Policy: an epic goes through the 5 phases 要件定義 -> 設計 -> 実装 -> テスト -> リリース判定 (ORDER in epic_common) strictly in order.
Progress is decided by the phase rows only (task rows of タスク titled with the phase name, linked to the epic).
What stays human-only: the one approval at completion (リリース承認 when リリース判定 is 完了; phases before it are AI-reviewed only) and any request to
another person. The approval carries the release's own pull request: ask_human.py --release opens the draft PR first (ship_approved.py prepare), and after
the person's 承認 the cron 'SF 出荷' (ship_approved.py run, no AI) publishes, squash-merges and deploys it. Agents never create, publish or merge a PR.

Registry: エピック rows of kind プロダクト開発 (not 完了/中止, 新規作成の停止 unchecked). There is no company epic and no company
task DB: the epic id is the hub row id (32 hex), the kanban tenant is epic-<last 8 hex>.
Default is dry-run. Use --apply to create cards and write the phase row / epic statuses.
Deterministic: no LLM. Prints a stable summary (empty line set => nothing to do).

Robustness (see the skill autonomous-task-execution, 'Changing pipeline scripts'):
  * pure decision functions (parse_rows, phase_procs, split_groups, order_group, next_action, render_body)
  * card bodies come from templates/*.md (strict ${var} substitution)
  * a journal (<state dir>/epic_gate_journal.json) makes partial creation resumable: the phase row is set to 進行中 only after ALL
    cards of the phase exist
  * per-epic error boundary: one broken epic never blocks the others; any error => ERROR/FAIL lines + exit 1
  * heartbeat <state dir>/epic_gate_status.json (written by --apply runs; the health monitor alerts when it is stale)
  * --selftest runs the offline test-suite (epic_selftest.py) with warnings as errors

Flow rules:
  * a phase row counts as finished only when 完了 or キャンセル (レビュー中 = a person must decide: NOT finished); a phase starts only
    when all earlier rows are finished AND no live kanban card of an earlier phase exists (same tenant, same phase idempotency key /
    "[phase]" title prefix)
  * G4: a 未着手 row whose body chapter やることチェックリスト holds no item is set キャンセル with a note (never silently skipped)
  * each checklist item of the row becomes one section of the card body
  * after `kanban create` the returned card is verified: an idempotency-key hit on an already done card is a FAIL
  * the hub ステータス = how far the AI has got: the earliest unfinished phase row; all five rows finished but the review row <epic8>-承認 (or the
    old -リリース承認, or its latest -rN round) not 承認 -> 承認待ち (a person's review is pending); 承認 and no live phase card -> 完了, but when the
    approved key has a ship record (ship_approved.json) only after that ship is done (merged, and deployed where the repository is registered).
    Never overwrites 完了/中止
  * gates due according to the rows (リリース承認 only) are asked via ask_human.py, once per key; the ask opens the draft PR first. When it cannot
    (ask_human exit 3: HOLD <key> <code>: <reason>) no row is made: a 管理ログ line + a card comment + `HELD` once per reason, one 候補 per key (not for
    gh-permission / draft-unsupported: a person's), and the ask is tried again when the judgement page changed or after SHIP_RECHECK_SECONDS.
    A ship that ended in void (the branch moved on) is asked again as <key>-r(N+1) once no phase card of the epic is live
  * the controller also decides when a row is DONE (finish_rows): a 進行中 row whose card is done is set 完了 only when the reviewer passed the card (a task_runs
    run of the reviewer with outcome completed after the last kanban_request_review), every checklist item is ticked (a row of a range: and each item still has its
    `<phase>：` head), the 完了レポート passes scripts/remaining_work.py, and a row that holds リリース判定 has no PROBLEMS in `ask_human.py --release --dry-run`.
    Anything else: the row stays 進行中 and ONE `ALERT row-close ...` line says why (the row is read again only when it was edited). An L1 epic (no phase rows)
    is set 完了 in the run that finds all its work rows finished (close_l1_epic)
  * enforce_review_limit: the request_review after levels.toml `max_reviews` of the epic's level (L1 1, L2 and L3 3) is blocked (`hermes kanban block`) with one `ALERT review-limit ...`
  * level_down_alert: an epic whose rows are those of a higher level than its レベル (L3 rows, level lowered to L2) says so once (`ALERT level ...`); no row is changed
  * the card model follows the phase table of epic_common (every phase Sonnet, 設計 = [models].design); a card gets --model only when
    that model differs from the executor's default, so the reviewer reviews it on its own model
"""
import argparse
import json
import re
import sys
import time
import traceback

import create_card
import epic_common as C
import remaining_work as RW
import ship_approved as S

# Names the health monitor / other tools historically imported from this module.
from epic_common import (BOARD, DONE_ROW, FINISHED, MERGE_PHASES, ORDER, PARALLEL, PHASE_MODEL, PHASE_ROLE,  # noqa: F401
                         REGISTRY_DS, TASK_DS, PipelineError, query, title_of)

RUN_BUDGET_SECONDS = 180  # stop starting new work after this (the cron wrapper kills at 240 s)
HARD_LIMIT_SECONDS = 240  # the cron wrapper (epic_gate_apply.TIMEOUT) kills the run here
ASK_TIMEOUT = 150  # ask_human.py --release: reads the epic, the phase rows and the judgement, opens the draft PR (git fetch + 3-5 gh calls), copies the images (measured 2026-11-09: 5 images in 30 s); an ask is not started when less time than this is left, so a cut-off run is completed by the next one
SHIP_RECHECK_SECONDS = 600  # a held ask (no draft PR could be had) is tried again at most this often unless the judgement page changed
ROW_SETTLE_SECONDS = 600  # a 未着手 row without checklist items is cancelled only when it has not been edited for this long


# ---------------------------------------------------------------- Notion / kanban I/O

def ntn(*args, data=None):
    return C.ntn(*args, data=data)


def hermes(*args, stdin=None):
    """Run `hermes kanban ...`. A whole argv that starts with `kanban` (create_card.build_args) is run as it is; else `kanban --board <board>` is put before `args`."""
    cmd = [C.which_tool("hermes"), *(args if args[:1] == ("kanban",) else ("kanban", "--board", BOARD, *args))]
    return C._sh(cmd, input=stdin, timeout=90)


def make_row(epic_row_id, title, items, reason):
    """Make one row of a level through create_row.py (the only way rows are made) -> page id. Raises PipelineError; the message carries
    `page=<id>` when a half-filled row was already created (that row stays in 進行中: it is never made again)."""
    spec = {"kind": "task", "title": title, "status": "未着手", "epic": epic_row_id,
            "overview": {"目的": f"{reason}。行の題が受け持つ工程の成果物を作る", "完了条件": "チェックリストの項目が全部済み、完了レポートに根拠がある",
                         "対象外": "PR の作成・公開・マージ、デプロイ（ゲート制御と出荷のスクリプトだけが行う）。他の人への依頼",
                         "対象": "ハブの関連ドキュメントにあるリポジトリとブランチ"},
            "checklist": items, "memo": [f"ゲート制御が作った行（{reason}）"]}
    r = C._sh([sys.executable, str(C.SCRIPTS_DIR / "create_row.py"), "--spec-json", json.dumps(spec, ensure_ascii=False)], timeout=120)
    out = (r.stdout or "").strip()
    m = re.search(r"CREATED task (\S+)", out)
    if r.returncode != 0 or not m:
        page = re.search(r"page=(\S+)", out + " " + (r.stderr or ""))
        raise PipelineError(f"create_row.py for {title!r} failed (rc {r.returncode}): {((r.stderr or '') + out)[-200:]}"
                            + (f" page={page.group(1)}" if page else ""))
    return m.group(1)


def set_status(page_id, name):
    ntn(f"v1/pages/{page_id}", "-X", "PATCH", data={"properties": {"ステータス": {"status": {"name": name}}}})


# ---------------------------------------------------------------- pure functions

def parse_rows(rows):
    """Rows of a hub -> ({phase: row}, [names that more than one row claim]). A row stands for every phase its title works (C.row_phases)."""
    return C.rows_by_phase(rows)


def phase_procs(ph, texts):
    """{label: dict(full=label, text=item text)} of a row's checklist items in document order; label = '[phase] item'.

    A repeated item text gets ' (2)', ' (3)' ... so every label is unique."""
    out = {}
    for t in texts:
        label, n = f"[{ph}] {t}", 1
        while label in out:
            n += 1
            label = f"[{ph}] {t} ({n})"
        out[label] = dict(full=label, text=t)
    return out


def split_groups(ph, procs):
    groups = {"A": set(procs)}
    for g, names in PARALLEL.get(ph, {}).items():
        sel = {k for k, v in procs.items() if v["text"] in names}
        if sel and sel != set(procs):
            groups[g] = sel
            groups["A"] = set(procs) - sel
    return groups


def order_group(gset, procs):
    """Checklist order (document order of the row)."""
    pos = {k: i for i, k in enumerate(procs)}
    return sorted(gset, key=lambda k: pos[k])


def next_action(items_of, prows, hub_name, live=None, order=None, now=None):
    """Decide what to do for one epic -> (message lines, action | None, rows to cancel).

    items_of(row title, row) -> checklist item texts of a 未着手 row (called lazily, only for rows that matter).
    A row is one card (行＝カード): the decision runs over the rows (C.row_units); a row of a range (<first>〜<last>) is ONE unit that works several
    phases. action = dict(ph (first phase), row_title, phases (all phases of the card), procs (of the first phase), prow[, extra]). Rows decide progress:
      * a row is finished only when 完了/キャンセル; 進行中 and レビュー中 keep the later rows waiting
      * G4: a 未着手 row without checklist items is cancelled (list of (row title, row)), never silently skipped
      * a row starts only if all earlier rows are finished and `live` ({phase: [cards]}) has no live card of an earlier row
      * a phase without a row waits for the rows to be made (create_epic_rows makes them when the epic has none)
      * a range row's items are split by their head `<phase>：` (C.split_items); the later phases of the row become `extra` of the same card
      * old rows (one per phase, C.is_legacy): every ready row after the first joins its card (MERGE_PHASES); a range row never merges
    """
    order = list(order or ORDER)
    live = live or {}
    msgs, cancels, cancelled = [], [], set()
    units = C.row_units(prows, order)
    legacy = C.is_legacy(prows)
    for i, (title, phases, prow) in enumerate(units):
        if prow is None:
            return msgs + [f"NOROW [{title}] {hub_name}: waiting for the row to be made"], None, cancels
        st = C.status_of(prow)
        if st in DONE_ROW:
            continue
        if st != "未着手":
            return msgs, None, cancels  # 進行中 / レビュー中: this row is open; later rows wait
        texts = items_of(title, prow)
        if not texts:
            age = C.age_seconds(prow.get("last_edited_time"), now) if now is not None else None
            if age is not None and age < ROW_SETTLE_SECONDS:  # the row may still be being written
                return msgs + [f"WAIT [{title}] {hub_name}: row 未着手 without checklist items (waiting for the row to be filled)"], None, cancels
            cancels.append((title, prow))
            cancelled.add(title)
            continue
        earlier = [u[0] for u in units[:i] if u[2] is not None and u[0] not in cancelled and C.status_of(u[2]) not in DONE_ROW]
        if earlier:
            return msgs + [f"ORDER [{title}] {hub_name} (earlier phase not finished: {', '.join(earlier)})"], None, cancels
        earlier_live = [p for u in units[:i] for p in u[1] if live.get(p)]
        if earlier_live:
            return msgs + [f"ORDER [{title}] {hub_name} (earlier phase has live cards: {', '.join(earlier_live)})"], None, cancels
        if len(phases) > 1:  # a row of a range: its card works the row's phases; nothing merges into it
            by_phase, warns = C.split_items(phases, texts)
            msgs += [f"WARN [{title}] {hub_name}: {w}" for w in warns]
            first = next((p for p in phases if by_phase.get(p)), phases[0])
            extra = [dict(ph=p, procs=phase_procs(p, by_phase[p]), prow=prow, row_title=title) for p in phases if p != first and by_phase.get(p)]
            act = dict(ph=first, row_title=title, phases=list(phases), procs=phase_procs(first, by_phase.get(first) or texts), prow=prow)
            if extra:
                act["extra"] = extra
            return msgs, act, cancels
        act = dict(ph=title, row_title=title, phases=[title], procs=phase_procs(title, texts), prow=prow)
        extra = merge_followers([u[0] for u in units[i + 1:]], items_of, prows) if (MERGE_PHASES and legacy) else []
        if extra:
            act["extra"] = extra
            act["phases"] = [title] + [x["ph"] for x in extra]
        return msgs, act, cancels
    return msgs, None, cancels


def merge_followers(later, items_of, prows):
    """Later phases that join the card of the first startable phase: row 未着手 with checklist items, contiguous in work order.

    Only for old rows (one row per phase, C.is_legacy). A finished row, or a 未着手 row without items (the controller cancels it when it is
    reached), is skipped; a row that is 進行中 / レビュー中, or a phase whose row does not exist yet, ends the list."""
    out = []
    for ph in later:
        prow = prows.get(ph)
        if prow is None:
            break
        st = C.status_of(prow)
        if st in DONE_ROW:
            continue
        if st != "未着手":
            break
        texts = items_of(ph, prow)
        if texts:
            out.append(dict(ph=ph, procs=phase_procs(ph, texts), prow=prow))
    return out


def render_body(ph, g, n_groups, group_names, epic_url, hub_name, prow_url, procs, ordered, text_of, tail=""):
    """Card body from templates. text_of(process_info) -> extra text of one checklist item (may be empty). `tail` (the card of a range row:
    range_tail) ends ${parts}; without it the body is the one every card has always had (tests/golden_bodies.json)."""
    parts = [C.render("phase_card_process", full=procs[k]["full"], text=text_of(procs[k])) + "\n" for k in ordered]
    parallel = (C.render("phase_card_parallel_note", n_groups=n_groups, group_names=", ".join(sorted(group_names)), g=g) + " "
                if n_groups > 1 else "")
    return C.render("phase_card", epic_url=epic_url, hub_name=hub_name, ph=ph,
                    prow_url=prow_url, parts="\n".join(parts) + (f"\n{tail}\n" if tail else ""), parallel_note=parallel)


def deliverable_titles(level, row_title):
    """The deliverables (levels.toml `deliverables` of the level) the gate controller makes for the card of the row `row_title`: only a row
    that spans every phase of the level (L2's one row). An old row (one phase), a row of a part of the phases (L3's two rows: levels.toml
    does not say which deliverable belongs to which row) and an epic without a level make none."""
    node = C.LEVEL_DEFS[level or "L3"]
    phases = C.row_phases(row_title) or ()
    return list(node["deliverables"]) if len(phases) > 1 and list(phases) == list(node["phases"]) else []


def deliverable_pages(row_id, titles, apply):
    """([(title, url)] of the child pages of the row, [titles that were (or, without `apply`, would be) made]). Each title once: a page the row
    already has is never made again (a run that stopped after some of them makes the rest next time). Raises PipelineError."""
    have = {}
    for b in C.blocks(row_id):
        if b.get("type") == "child_page":
            have.setdefault(C.evidence_text(b).strip(), b["id"])
    made = [t for t in titles if t not in have]
    if apply:
        for t in made:
            page = ntn("v1/pages", "-X", "POST", data={"parent": {"page_id": row_id}, "properties": {"title": {"title": [{"text": {"content": t}}]}}})
            have[t] = page["id"]
    return [(t, C.page_url(have[t])) for t in titles if t in have], made


def range_tail(links):
    """The end of ${parts} of a range row's card: the links of its deliverable pages and the verify command (templates/phase_card_range_note.md)."""
    return C.render("phase_card_range_note", links="\n".join(f"- [{t}]({u})" for t, u in links))


CARD_SKILLS = ("autonomous-task-execution", "product-development-process")


def card_args(title, role, tenant, key, phases):
    """The `hermes kanban create` argv of a phase card (the body goes through stdin): create_card.build_args, the ONE builder.
    `key` is the idempotency key; `phases` are the phases the card works (--model/--provider only when their model is not the executor's default)."""
    return create_card.build_args(BOARD, title, role, None, tenant, "-", skills=CARD_SKILLS, idempotency_key=key,
                                  model_args=C.card_model_args(phases))


def parse_created(stdout):
    """(card id, created_at | None, status | None) from `kanban create --json` stdout; falls back to the 'Created t_… (status, …)' text."""
    try:
        d, _ = json.JSONDecoder().raw_decode((stdout or "")[(stdout or "").index("{"):])  # tolerate notices before the JSON
        return d["id"], d.get("created_at"), d.get("status")
    except (ValueError, KeyError, TypeError):
        m = re.search(r"Created (t_[0-9a-f]+)\s+\((\w+)", stdout or "")
        if m:
            return m.group(1), None, m.group(2)
        m = re.search(r"Created (t_[0-9a-f]+)", stdout or "")
        if m:
            return m.group(1), None, None
        raise PipelineError(f"cannot read card id from kanban output: {(stdout or '')[:100]!r}")


def card_status(card_id):
    """Current status of a kanban card from the board (read-only sqlite); None when it is not listed."""
    return next((c["status"] for c in C.kanban_cards() if c["id"] == card_id), None)


def ask_release(key, epic_row_id):
    """Create the release approval row through `ask_human.py --release` (once per key; it opens the draft PR, reads the judgement, builds the nine
    chapters, copies the images and mentions the owner). -> (ok, 'CREATED'|'DUP'|error text). Exit 3 (no draft PR could be had, no row made) is
    (False, 'HOLD <key> <code>: <reason>'). Never raises."""
    cmd = [sys.executable, str(C.ASK_HUMAN), "--release", "--key", key, "--epic", epic_row_id]
    try:
        r = C._sh(cmd, timeout=ASK_TIMEOUT)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:150]
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out.split(None, 1)[:1] in (["CREATED"], ["DUP"]):
        return True, out.split(None, 1)[0]
    if r.returncode == 3 and out.startswith("HOLD "):
        return False, out.splitlines()[0][:400]
    return False, ((r.stderr or "").strip() or out)[:150]


# ---------------------------------------------------------------- journal helpers

def log_trial(title, g, role, ts, phases):
    m = C.model_for(phases)
    with open(C.state_path(C.MODEL_TRIAL_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps({"title": title, "group": g, "role": role, "model": m["model"], "reasoning": m["reasoning"],
                            "phases": list(phases), "ts": int(ts)}, ensure_ascii=False) + "\n")


def prune_journal(J, now, keep_seconds=14 * 86400):
    for key in [k for k, e in J.items() if e.get("row") == "done" and now - e.get("updated", now) > keep_seconds]:
        del J[key]


# ---------------------------------------------------------------- one epic

def start_phase(act, ctx, apply, J, epic, hub_name, out):
    """Create the cards of the next phase (journal-based; the phase row is set 進行中 only after ALL group cards exist).

    epic = dict(epic_id, url, tenant, level). Appends lines to `out`; raises on unexpected failures."""
    epic_id, url, tenant = epic["epic_id"], epic["url"], epic["tenant"]
    ph, procs, prow = act["ph"], act["procs"], act["prow"]
    extra = act.get("extra") or []
    row_title = act.get("row_title") or ph  # old rows: the title is the phase name, so keys, titles and journal entries stay as they were
    phases = list(act.get("phases") or ([ph] + [x["ph"] for x in extra]))
    merged = len(phases) > 1  # one card works all these phases in order; every row moves to 進行中, the reviewer closes them together
    if merged:
        procs = dict(procs)
        for x in extra:
            procs.update(x["procs"])
    role = PHASE_ROLE.get(ph, "executor")
    jkey = C.journal_key(epic_id, row_title)
    plan = J.get(jkey)
    resuming = bool(apply and plan and plan.get("row") == "pending")
    if resuming:
        groups = {g: {k for k in names if k in procs} for g, names in plan["groups"].items()}
        groups = {g: s for g, s in groups.items() if s}
    else:
        groups = {"A": set(procs)} if merged else split_groups(ph, procs)
    if len(groups) > 1:
        out.append(f"SPLIT [{ph}] {hub_name}: " + " | ".join(f"{g}={sorted(v)}" for g, v in groups.items()))
    ordered_of = {g: order_group(gset, procs) for g, gset in groups.items()}
    if merged:  # work order: phase by phase, each phase's items in checklist order
        by_phase = {x["ph"]: x["procs"] for x in [dict(ph=ph, procs=act["procs"])] + extra}
        ordered_of = {"A": [k for p in phases for k in order_group(set(by_phase.get(p) or {}), by_phase.get(p) or {})]}
    card_key = C.merged_phase_key(epic_id, phases) if merged else None
    card_rows, seen_rows = [], set()  # the rows this card works, each once (a row of a range stands for several phases)
    for t, r in [(row_title, prow)] + [(x.get("row_title") or x["ph"], x["prow"]) for x in extra]:
        if r["id"] not in seen_rows:
            seen_rows.add(r["id"])
            card_rows.append((t, r))
    title = f"[{row_title}] {hub_name}"
    titles = deliverable_titles(epic.get("level"), row_title)  # the deliverables of a range row are made BEFORE its card (the card links them)
    if not apply:
        if titles:
            out.append(f"WOULD CREATE DELIVERABLES {title}: {', '.join(deliverable_pages(prow['id'], titles, False)[1]) or 'none missing'}")
        for g in groups:
            pin = "pinned" if C.card_model_args(phases) else "profile default"
            out.append(f"WOULD CREATE {title} [{g}] -> {role} ({len(ordered_of[g])} items, model {C.model_for(phases)['model']} {pin})"
                       + (f" merged phases: {', '.join(phases)}" if merged else ""))
        return
    if ctx["deadline"] and time.time() > ctx["deadline"]:
        out.append(f"WARN run time budget used up; [{ph}] {hub_name} continues next run")
        return
    tail = ""
    if titles:
        links, made = deliverable_pages(prow["id"], titles, True)  # raises: no card until every page exists; the next run makes the missing ones only
        tail = range_tail(links)
        if made:
            out.append(f"MADE DELIVERABLES {title}: {', '.join(made)}")
    if not resuming:
        plan = J[jkey] = dict(row="pending", started=ctx["now"], updated=ctx["now"], epic=epic_id, phase=ph,
                              groups={g: ordered_of[g] for g in groups}, cards={})
        C.save_journal(J)
    plan["groups"] = {g: ordered_of[g] for g in groups}
    plan["cards"] = {g: c for g, c in plan["cards"].items() if g in groups}
    for g in groups:
        if g in plan["cards"]:
            continue
        body = render_body(ph, g, len(groups), groups, url, hub_name, prow["url"], procs, ordered_of[g], lambda v: "", tail)
        if merged:
            body += "\n" + C.render("phase_card_merged_note", phases=" -> ".join(phases),
                                    rows="\n".join(f"- {t}: {r['url']}" for t, r in card_rows))
        r = hermes(*card_args(title, role, tenant, card_key or C.phase_key(epic_id, ph, g), phases), stdin=body)
        if r.returncode != 0:
            plan["updated"] = int(time.time())
            C.save_journal(J)
            out.append(f"FAIL {title}: {(r.stderr or r.stdout).strip()[:150]}")
            return
        card_id, created_at, cstatus = parse_created(r.stdout)
        if cstatus is None:  # output without a status: read the board (read-only)
            cstatus = card_status(card_id)
        if cstatus == "done":  # idempotency-key hit on an OLD finished card: nothing new was created, the phase would never run
            plan["updated"] = int(time.time())
            C.save_journal(J)
            out.append(f"FAIL {title}: idempotency key {card_key or C.phase_key(epic_id, ph, g)} matched card {card_id} which is already done; "
                       f"archive the old card (hermes kanban archive {card_id}), then the next run creates the new card")
            return
        plan["cards"][g] = card_id
        plan["updated"] = int(time.time())
        C.save_journal(J)
        if created_at is not None and created_at >= ctx["run_start"]:  # log only really new cards (not idempotent re-hits)
            try:
                log_trial(title, g, role, created_at, phases)
            except OSError as e:
                out.append(f"WARN model_trial.jsonl not written: {e}")
    # Only after ALL group cards exist does the phase row move to 進行中 (otherwise a half-created phase would be started).
    for _, r in card_rows:
        set_status(r["id"], "進行中")
    plan["row"] = "done"
    plan["updated"] = int(time.time())
    C.save_journal(J)
    for g in groups:
        out.append(f"CREATED {title} [{g}] -> {role} ({len(ordered_of[g])} items" + (f"; merged phases: {', '.join(phases)})" if merged else ")"))


ROW_JOURNAL_OPEN = ("done", "half")  # journal states of a row that must never be made again (`creating` only while the run is alive)


def create_epic_rows(row, epic_id, rows, hub_name, J, ctx, apply, out):
    """Make the rows of an epic that has none, from levels.toml (`rows` of its level; `items` give each row's checklist), ONCE. -> True when rows
    were made (or would be, in a dry run).

    Rows are made by this script and no one else (the planner makes none): the row titles and the order of the phases are levels.toml's.
    An epic that already has a row (the old one-row-per-phase kind, or any row of the pipeline) is never touched. What makes it once: the rows
    listed by Notion, and the journal (<epic>:row:<title>) for a row made a moment ago that the query does not list yet. A run that stopped
    after the first row of L3 makes the missing one next time (the journal tells it started). A failure that left a half-filled page is
    never retried (the page is named in the error); a failure that left nothing is retried by the next run.
    rows = the raw task rows of the hub (C.query of the エピック relation)."""
    level = C.level_of(row)
    titles = C.level_rows(level)
    have = {title_of(r, "タスク名").strip() for r in rows}
    started = any(k.startswith(f"{epic_id}:row:") for k in J)
    if not titles or (not started and any(C.row_phases(t) is not None for t in have)):
        return False
    made = False
    for t in titles:
        e = J.get(f"{epic_id}:row:{t}") or {}
        in_doubt = e.get("row") == "creating" and ctx["now"] - e.get("updated", 0) < ROW_SETTLE_SECONDS
        if t in have or e.get("row") in ROW_JOURNAL_OPEN or in_doubt:
            continue
        items = C.row_items(level, t)
        if not items:
            raise PipelineError(f"levels.toml gives the row {t!r} of {level or 'L3'} no checklist item")
        why = f"エピックができたため、レベル {level or 'L3'} の行を levels.toml から作る"
        if not apply:
            out.append(f"WOULD CREATE ROW [{t}] {hub_name}: {why} ({len(items)} items)")
            made = True
            continue
        if ctx.get("deadline") and time.time() > ctx["deadline"]:
            out.append(f"WARN run time budget used up; the row [{t}] of {hub_name} is made by the next run")
            break
        jkey = f"{epic_id}:row:{t}"
        J[jkey] = dict(row="creating", epic=epic_id, phase=t, started=ctx["now"], updated=ctx["now"], cards={}, groups={})
        C.save_journal(J)
        try:
            page = make_row(row["id"], t, items, why)
        except PipelineError as e:
            m = re.search(r"page=(\S+)", str(e))
            if m:  # a half-filled page exists: never a second row
                J[jkey].update(row="half", page=m.group(1), updated=int(time.time()))
            else:
                del J[jkey]
            C.save_journal(J)
            raise
        J[jkey].update(row="done", page=page, updated=int(time.time()))
        C.save_journal(J)
        out.append(f"CREATED ROW [{t}] {hub_name}: {why}")
        made = True
    return made


def cancel_rows(cancels, hub_name, apply, out):
    """G4: rows without checklist items: note + キャンセル (never silently skipped)."""
    for ph, prow in cancels:
        if apply:
            C.append_note(prow["id"], C.render("phase_row_cancel_note", reason="未着手のまま、やることチェックリストに項目がありません"))
            set_status(prow["id"], "キャンセル")
            out.append(f"CANCELLED [{ph}] {hub_name}: row set キャンセル (no checklist items)")
        else:
            out.append(f"WOULD CANCEL [{ph}] {hub_name}: row has no checklist items")
        prow["properties"]["ステータス"] = {"status": {"name": "キャンセル"}}  # later decisions of this run see it as finished


def release_complete(epic_id, statuses, live):
    """Hub should become 完了: all five phase rows 完了/キャンセル, the latest round of the release review row (<epic8>-承認, the old key, or -rN) is 承認,
    no live phase card, and - when that key has a ship record (ship_approved.json) - the ship is done (merged, deployed where registered).
    A key without a record is an approval from before the ship existed: it completes the epic as it always did. `live` None = unknown (never complete).
    The review DB is read only when the cheap conditions hold."""
    if live is None or any(live.values()) or not C.phases_all_finished(statuses):
        return False
    keys = sorted(C.release_review_keys(epic_id))
    pages = C.query(C.REVIEW_DS, {"or": [{"property": "キー", "rich_text": {"starts_with": k}} for k in keys]})
    key = C.release_approved_key(pages, epic_id)
    if key is None:
        return False
    rec = C.ship_record(key)
    return rec is None or rec.get("stage") == C.SHIP_DONE_STAGE


def sync_hub_status(row, hub_status, prows, hub_name, apply, out, epic_id=None, live=None):
    """Hub ステータス = how far the AI has got (never 完了/中止 overwritten; never approval values):
      1. some phase row is not 完了/キャンセル -> the earliest such phase (mapped);
      2. all five finished, release approval not 承認 (or a phase card is still live) -> 承認待ち (a person's review is pending);
      3. all five finished + release review row (<epic8>-承認 / old -リリース承認) 承認 + no live phase card -> 完了 (the person's approval is the decision)."""
    if hub_status in C.HUB_KEEP_STATUSES:
        return
    statuses = {ph: C.status_of(r) for ph, r in prows.items()}
    approved = False
    if C.phases_all_finished(statuses):
        approved = bool(epic_id) and release_complete(epic_id, statuses, live)
        target = C.HUB_DONE_STATUS if approved else C.HUB_WAITING_STATUS
    else:
        target = C.hub_target_status(statuses)
    if target is None or hub_status == target:
        return
    if not approved and (target in C.HUB_APPROVAL_STATUSES or target in C.HUB_KEEP_STATUSES or target not in C.HUB_STATUSES):
        raise PipelineError(f"refusing to write hub status {target!r}")
    if not apply:
        out.append(f"WOULD SET hub ステータス {hub_status} -> {target}: {hub_name}")
        return
    set_status(row["id"], target)
    row["properties"]["ステータス"] = {"status": {"name": target}}
    out.append(f"HUBSTATUS {hub_name}: {hub_status} -> {target}")


def ask_due_gates(row, epic_id, prows, hub_name, ctx, apply, out):
    """No-agent gate asks: every gate that is due according to the rows and has no key yet (allowed out of order)."""
    statuses = {ph: C.status_of(r) for ph, r in prows.items()}
    for gate in C.gates_due(statuses):
        key = C.gate_key(epic_id, gate)
        if key in ctx["asked"] or C.legacy_gate_key(epic_id, gate) in ctx["asked"]:  # the old key format counts: a gate already asked is never asked again
            key = reask_key(epic_id, key, ctx)  # ... except a ship that ended in void because the branch moved on: the next round <key>-r(N+1)
            if key is None:
                continue
        ev = release_evidence(key, prows, ctx, out)
        if ev is None:  # held and the decision page has not changed since: nothing to do
            continue
        if not ev["verdict"]["ok"]:
            hold_release_ask(row, epic_id, key, ev, ctx, apply, out)
            continue
        if not apply:
            out.append(f"WOULD ASK {key}")
            continue
        if ctx.get("deadline") and time.time() + ASK_TIMEOUT > ctx["deadline"] - RUN_BUDGET_SECONDS + HARD_LIMIT_SECONDS:
            out.append(f"WAIT {key}: less than {ASK_TIMEOUT} s are left of this run; the ask is started by the next one")
            continue
        ok, msg = ask_release(key, row["id"])
        if ok:
            ctx["asked"].add(key)
            out.append(f"ASKED {key} {msg}")
            _drop_hold(key)
        elif msg.startswith("HOLD "):
            hold_ship_ask(row, epic_id, key, msg, ev, ctx, apply, out)
        else:
            out.append(f"ERROR gate ask {key}: {msg}")


def reask_key(epic_id, key, ctx):
    """<key>-r(N+1) when the latest round of the release ask ended in a stopped ship (void) whose branch has moved on since, and no phase card of the
    epic is live (the fix is done); else None. The decision is ship_approved.reask_key."""
    if any(C.live_phase_cards(ctx["cards"], epic_id).values()):
        return None
    return S.reask_key(key, ctx["asked"])


def _holds():
    d = C.read_json(C.state_path(C.UI_HOLD_FILE), {})
    return d if isinstance(d, dict) else {}


def _drop_hold(key):
    holds = _holds()
    if holds.pop(key, None) is not None:
        C.atomic_write_json(C.state_path(C.UI_HOLD_FILE), holds)


def release_evidence(key, prows, ctx, out):
    """UI evidence of the release ask (C.collect_ui_evidence). The ask itself (ask_human.py --release) reads the decision page again for its copy.

    A key that is held (ui_evidence_hold.json) is re-checked only when its decision page changed (one block read + one page read per run);
    otherwise 'HOLD <key> (unchanged)' and None."""
    hold = _holds().get(key)
    ref = C.decision_page_ref(prows)
    recheck = bool(hold) and hold.get("kind") == "ship" and ctx["now"] - hold.get("checked", 0) >= SHIP_RECHECK_SECONDS  # no draft PR could be had: the branch, the PR body page or GitHub may have changed
    if hold and (ref or {}).get("edited") == hold.get("edited") and not recheck:
        out.append(f"HOLD {key} (unchanged): {hold.get('reason', '')}")
        return None
    return C.collect_ui_evidence(prows, ref=ref or False)  # False = no decision page (not read again)


def release_card(cards, epic_id):
    """The newest card of the epic that worked the リリース判定 phase (any status but archived), or None."""
    hits = [c for c in cards if c["tenant"] == C.tenant_of(epic_id)
            and (C.is_phase_card_key(c["key"], epic_id, "リリース判定") or c["title"].startswith("[リリース判定]"))]
    return hits[-1] if hits else None


HOLD_CANDIDATE = ("打ち手：リリース判定書の草稿に変更前・変更後の画像を入れる（ゲート制御がリリース承認の依頼を止めた理由：{reason}）"
                  " ／ 判断の宣言：画像を足してリリース承認の依頼を出すかが決まる ／ 感度の見積もり：画像欠けの依頼 1件→0件（止めたままの依頼が出せる）"
                  " ／ 開始できるフェーズ：リリース判定")


def hold_release_ask(row, epic_id, key, ev, ctx, apply, out):
    """UI change without the before/after images on the decision page: no ask. Once per reason: a 管理ログ line on the hub, a comment on the
    epic's リリース判定 card, the line 'HELD <key>: <reason>' (the cron wrapper posts it); once per key: an unchecked 候補 item (the planner then
    creates the task that adds the images). Next runs re-check only when the decision page changed."""
    reason = ev["verdict"]["reason"]
    if not apply:
        out.append(f"WOULD HOLD {key}: {reason}")
        return
    holds = _holds()
    prev = holds.get(key) or {}
    edited = (ev.get("page") or {}).get("edited")
    if prev.get("reason") == reason:
        holds[key] = dict(prev, edited=edited)
        C.atomic_write_json(C.state_path(C.UI_HOLD_FILE), holds)
        out.append(f"HOLD {key} (unchanged): {reason}")
        return
    day = time.strftime("%Y-%m-%d", time.localtime(ctx["now"]))
    note = (f"{day} ゲート制御：リリース承認の依頼を止めました（{reason}）。判定書の草稿の本文に変更前・変更後の画像を入れると"
            f"（キャプションに「変更前」「変更後」）、次の実行で依頼を作ります。")
    C.append_to_chapter(row["id"], "管理ログ", [C.text_block("paragraph", note)])
    if not prev:
        C.append_to_chapter(row["id"], "候補", [C.text_block("to_do", HOLD_CANDIDATE.format(reason=reason), checked=False)])
    card = release_card(ctx["cards"], epic_id)
    if card:
        r = hermes("comment", card["id"], note)
        if r.returncode != 0:
            out.append(f"WARN comment on {card['id']} failed: {((r.stderr or r.stdout) or '').strip()[:120]}")
    else:
        out.append(f"WARN no リリース判定 card of epic {epic_id[-8:]} to comment on")
    holds[key] = dict(reason=reason, edited=edited, since=ctx["now"])
    C.atomic_write_json(C.state_path(C.UI_HOLD_FILE), holds)
    out.append(f"HELD {key}: {reason}")


SHIP_HUMAN_CODES = ("gh-permission", "draft-unsupported")  # an agent cannot fix these: no 候補, one ERROR line for the person
SHIP_FIX = {
    "no-branch": "リリース判定書の「何を出すか」に、リポジトリ（owner/repo）とブランチ名（michiomochi/…）を書く",
    "branch-not-pushed": "ブランチを origin に push する",
    "no-pr-body": "判定書の「PR 本文」の章を書く（旧形は、リリース判定の行の子ページ「PR 本文の下書き」を作る）（1行目『タイトル：…』、4節 概要／背景／変更内容／検証、Notion のリンクを残さない）",
    "pr-body-shape": "判定書の「PR 本文」の章（旧形は子ページ「PR 本文の下書き」）を直す（1行目『タイトル：…』、4節 概要／背景／変更内容／検証がすべて中身あり、ひな形の〈…〉・Notion のリンクと DB 名を残さない）",
    "conflict": "origin/main をブランチにマージしてコンフリクトを直し、push する",
    "no-diff": "対象のブランチに変更が入っているかを確かめ、リリース判定書のブランチ名を直す",
}
SHIP_CANDIDATE = ("打ち手：{fix}（ゲート制御がリリース承認の依頼を止めた理由：{code}：{reason}）"
                  " ／ 判断の宣言：直して、下書き PR 付きのリリース承認の依頼を出すかが決まる ／ 感度の見積もり：止まった依頼 1件→0件（下書き PR 付きの依頼が出せる）"
                  " ／ 開始できるフェーズ：リリース判定")


def hold_ship_ask(row, epic_id, key, msg, ev, ctx, apply, out):
    """ask_human.py --release exited 3: no draft PR could be had, so no review row was made. `msg` = 'HOLD <key> <code>: <reason>'.
    Like hold_release_ask: once per reason a 管理ログ line + a card comment + 'HELD <key>: <code>: <reason>'; once per key a 候補 item (not for
    SHIP_HUMAN_CODES: those are one ERROR line the first time). The hold is re-checked after SHIP_RECHECK_SECONDS or when the judgement page changed."""
    m = re.match(r"HOLD \S+ ([\w-]+): (.*)", msg, re.S)
    code, why = (m.group(1), m.group(2).strip()) if m else ("unknown", msg)
    reason = f"{code}：{why}"
    if not apply:
        out.append(f"WOULD HOLD {key}: {reason}")
        return
    holds = _holds()
    prev = holds.get(key) or {}
    edited = (ev.get("page") or {}).get("edited")
    if prev.get("reason") == reason:
        holds[key] = dict(prev, edited=edited, checked=ctx["now"])
        C.atomic_write_json(C.state_path(C.UI_HOLD_FILE), holds)
        out.append(f"HOLD {key} (unchanged): {reason}")
        return
    day = time.strftime("%Y-%m-%d", time.localtime(ctx["now"]))
    human = code in SHIP_HUMAN_CODES
    fix = SHIP_FIX.get(code)
    note = (f"{day} ゲート制御：リリース承認の依頼を止めました（下書き PR を作れません：{reason}）。"
            + (f"{fix}と、次の実行で依頼を作ります。" if fix else "人が GitHub の設定を確かめる必要があります。"))
    C.append_to_chapter(row["id"], "管理ログ", [C.text_block("paragraph", note)])
    if not prev and not human and fix:
        C.append_to_chapter(row["id"], "候補", [C.text_block("to_do", SHIP_CANDIDATE.format(fix=fix, code=code, reason=why), checked=False)])
    card = release_card(ctx["cards"], epic_id)
    if card:
        r = hermes("comment", card["id"], note)
        if r.returncode != 0:
            out.append(f"WARN comment on {card['id']} failed: {((r.stderr or r.stdout) or '').strip()[:120]}")
    else:
        out.append(f"WARN no リリース判定 card of epic {epic_id[-8:]} to comment on")
    holds[key] = dict(kind="ship", reason=reason, edited=edited, since=ctx["now"], checked=ctx["now"])
    C.atomic_write_json(C.state_path(C.UI_HOLD_FILE), holds)
    out.append(f"ERROR gate ask {key}: {reason}" if human else f"HELD {key}: {reason}")


# ---------------------------------------------------------------- done: rows completed by the controller, the review limit, the L1 epic, a lowered level

REVIEW_BLOCKABLE = ("running", "ready")  # kanban can block a card from these; a card in `review` is blocked once the reviewer has claimed it (next run)


def alert(ctx, key, value, text, apply, out):
    """Say `text` as ONE `ALERT ...` line per (key, value): what was said (ctx['alerts'], written at the end of an --apply run) is never said again. A dry run prints
    `WOULD ALERT ...` and remembers nothing. -> True when the line was (or would be) printed."""
    if ctx["alerts"].get(key) == value:
        return False
    if apply:
        ctx["alerts"][key] = value
        ctx["alerts_dirty"] = True
        out.append(f"ALERT {text}")
    else:
        out.append(f"WOULD ALERT {text}")
    return True


def enforce_review_limit(epic_id, level, hub_name, ctx, apply, out):
    """A card of the epic whose worker called kanban_request_review more often than levels.toml `max_reviews` of its level allows (L1 1, L2 and L3 3): block it
    (`hermes kanban block`, kind needs_input: a person decides) and say so with ONE ALERT. No ask_human. A card in `review` waits for the reviewer to claim it
    (kanban cannot block from there); a card that is done (the reviewer passed it) or already blocked is left alone. After a person unblocks it, only a request
    beyond the count it was blocked at blocks it again (no loop). A block kanban refuses is one ALERT of its own and is tried again next run."""
    limit, tenant, e8 = C.max_reviews(level), C.tenant_of(epic_id), epic_id[-8:]
    for card in ctx["reviews"]:
        key = f"reviews:{card['id']}"
        if card["tenant"] != tenant or card["requests"] <= limit or card["status"] not in REVIEW_BLOCKABLE or ctx["alerts"].get(f"{key}:blocked") == card["requests"]:
            continue
        lv = level or "L3"
        why = (f"{card['requests']}回目の kanban_request_review です（{lv} の上限は {limit} 回。levels.toml の max_reviews）。人が見て、続けるなら unblock してください")
        if not apply:
            out.append(f"WOULD BLOCK card {card['id']} ({hub_name}): {why}")
            continue
        r = hermes("block", "--kind", "needs_input", card["id"], f"review limit: {why}")  # options before the positionals: argparse leaves the reason unrecognized otherwise
        if r.returncode != 0:
            alert(ctx, f"{key}:failed", card["requests"], f"review-limit {e8} {hub_name}: card {card['id']} は {card['requests']}回目の request_review で上限（{lv} は {limit} 回）を超えたが、"
                  f"block を止められませんでした（{((r.stderr or r.stdout) or '').strip()[:100]}）。次の実行でもう一度試します", apply, out)
            continue
        ctx["alerts"][f"{key}:blocked"] = card["requests"]
        ctx["alerts_dirty"] = True
        out.append(f"BLOCKED card {card['id']} ({hub_name}): review limit")
        out.append(f"ALERT review-limit {e8} {hub_name}: card {card['id']} を block しました。{why}")


def release_judgement_problems(epic_row_id, key):
    """The PROBLEMS of `ask_human.py --release --dry-run` for the epic (the 未確認：… / 注意：… lines of the release ask built from its 判定書) -> (problems, conclusive).
    A dry run that did not run properly is not conclusive: the row is looked at again next run instead of being remembered as checked."""
    cmd = [sys.executable, str(C.ASK_HUMAN), "--release", "--key", key, "--epic", epic_row_id, "--dry-run"]
    try:
        r = C._sh(cmd, timeout=ASK_TIMEOUT)
    except Exception as e:
        return [f"判定書の検査（ask_human --release --dry-run）が動きませんでした: {type(e).__name__}: {e}"[:200]], False
    out = r.stdout or ""
    if r.returncode != 0:
        return [f"判定書の検査（ask_human --release --dry-run）が rc {r.returncode} で終わりました: {((r.stderr or '').strip() or out.strip())[-150:]}"], False
    heads = list(re.finditer(r"^PROBLEMS \((\d+)\):[ \t]*$", out, re.M))
    if not heads:
        return ["判定書の検査（ask_human --release --dry-run）の出力に PROBLEMS の行がありません"], False
    tail = out[heads[-1].end():].lstrip("\n").splitlines()
    problems = []
    for line in tail:
        if not line.startswith("- "):
            break
        problems.append(line[2:].strip())
    return problems, True


def close_problems(titles_phases, states, passed, card_id):
    """The reasons a card's rows are not finished (pure). titles_phases = [(row title, its phases)], states = {row title: C.close_state(...)}, passed = the reviewer's pass
    (task_runs id, 0 = none) -> [problem text]. No pass is the only problem reported when it is missing (the rows are not read then)."""
    if not passed:
        return [f"reviewer の合格がありません（card {card_id} は done ですが、reviewer の合格の run がありません。executor が自分で完了にしたか、人が done にした）"]
    out = []
    for title, phases in titles_phases:
        st = states[title]
        unticked = [t for t, ticked in st["items"] if not ticked]
        if not st["items"]:
            out.append(f"[{title}] チェックリストに項目がありません")
        elif unticked:
            out.append(f"[{title}] チェックリストが済んでいない項目が {len(unticked)} 件あります: " + " ／ ".join(t[:40] for t in unticked[:3]))
        if len(phases) > 1:  # a row of a range: every item keeps its `<phase>：` head (levels.toml writes them); an item that lost it is not counted for any phase
            headless = [t for t, _ in st["items"] if C.item_head(t) not in phases]
            if headless:
                out.append(f"[{title}] 項目の頭 `<工程名>：` が無い（またはこの行の工程ではない）項目が {len(headless)} 件あります: " + " ／ ".join(t[:40] for t in headless[:3]))
        if not st["report_filled"]:
            out.append(f"[{title}] 完了レポートが空です")
        else:
            out += [f"[{title}] 完了レポート: {p}" for p in RW.check(st["report"])]
    return out


def finish_rows(row, epic_id, rows, hub_name, ctx, apply, out):
    """Complete the 進行中 rows of the epic whose card the reviewer passed (the rule is in the module docstring; one card = all the rows its body names, completed together
    or not at all). `rows` = the raw task rows of the hub; a row set 完了 is updated in place, so the decisions after it in this run see it. A card that is still live
    (running / ready / review / blocked ...) is waited for. What is not met is ONE `ALERT row-close ...` per distinct reason (a row that was not edited and a card that did not
    change are not looked at again). -> True when a row was (or, in a dry run, would be) completed."""
    cards = [c for c in ctx["reviews"] if c["tenant"] == C.tenant_of(epic_id)]
    groups = {}  # card id -> (card, [rows])
    for r in rows:
        if C.status_of(r) != "進行中":
            continue
        pid = r["id"].replace("-", "")
        hits = [c for c in cards if pid in c["body"].replace("-", "")]
        if not hits or any(c["status"] in C.LIVE_CARD_STATUSES for c in hits):
            continue
        done = [c for c in hits if c["status"] == "done"]
        if not done:
            continue
        card = max(done, key=lambda c: c["passed"])
        groups.setdefault(card["id"], (card, []))[1].append(r)
    completed = False
    for card, rs in groups.values():
        key = f"close:{card['id']}"
        sig = "|".join([str(card["passed"])] + sorted(f"{r['id']}@{r.get('last_edited_time')}" for r in rs))
        rec = ctx["alerts"].get(key) or {}
        if rec.get("final") and rec.get("sig") == sig:
            continue  # looked at, said, and nothing changed since: no second try
        titles = [(title_of(r, "タスク名").strip(), r) for r in rs]
        problems, final = [], True
        if not card["passed"]:
            problems = close_problems([], {}, 0, card["id"])
        else:
            states = {t: C.close_state(C.iter_blocks(r["id"])) for t, r in titles}
            problems = close_problems([(t, C.row_phases(t) or ()) for t, _ in titles], states, card["passed"], card["id"])
            if not problems and any("リリース判定" in (C.row_phases(t) or ()) for t, _ in titles):
                found, final = release_judgement_problems(row["id"], C.gate_key(epic_id, C.GATE_ROWS[-1][1]))
                problems += [f"判定書: {p}" for p in found]
        if not problems:
            for t, r in titles:
                if apply:
                    set_status(r["id"], "完了")
                    r["properties"]["ステータス"] = {"status": {"name": "完了"}}
                    out.append(f"COMPLETED [{t}] {hub_name}: the reviewer passed card {card['id']}; row set 完了")
                else:
                    out.append(f"WOULD COMPLETE [{t}] {hub_name}: the reviewer passed card {card['id']}")
            if apply and key in ctx["alerts"]:
                del ctx["alerts"][key]
                ctx["alerts_dirty"] = True
            completed = True
            continue
        text = (f"row-close {epic_id[-8:]} [{' / '.join(t for t, _ in titles)}] {hub_name}（card {card['id']}）: 行を完了にしませんでした。" + " ／ ".join(problems))
        if rec.get("problems") == problems:
            if apply:  # the same reasons as said before: remember the new state silently
                ctx["alerts"][key] = dict(sig=sig, final=final, problems=problems)
                ctx["alerts_dirty"] = True
        elif apply:
            ctx["alerts"][key] = dict(sig=sig, final=final, problems=problems)
            ctx["alerts_dirty"] = True
            out.append(f"ALERT {text}")
        else:
            out.append(f"WOULD ALERT {text}")
    return completed


def close_l1_epic(row, rows, hub_name, apply, out):
    """An L1 epic is only the vessel of its work (levels.toml gives L1 no phase rows): once every work row of it is finished (完了 / キャンセル, at least one 完了),
    the epic is set 完了 in the same run. An epic without a work row yet (create_row --level L1 makes the task after the epic) is left."""
    work = [r for r in rows if C.row_phases(title_of(r, "タスク名").strip()) is None]
    sts = [C.status_of(r) for r in work]
    if not work or any(st not in DONE_ROW for st in sts) or "完了" not in sts or C.status_of(row) in C.HUB_KEEP_STATUSES:
        return False
    if not apply:
        out.append(f"WOULD CLOSE {hub_name}: every work row of the L1 epic is finished")
        return True
    set_status(row["id"], C.HUB_DONE_STATUS)
    row["properties"]["ステータス"] = {"status": {"name": C.HUB_DONE_STATUS}}
    out.append(f"CLOSED L1 epic {C.epic_id_from_relation(row['id'])[-8:]} {hub_name}: every work row is finished; epic set {C.HUB_DONE_STATUS}")
    return True


def level_down_alert(epic_id, level, prows, hub_name, ctx, apply, out):
    """The epic's rows are the rows of a HIGHER level than its レベル (L3's two rows while the person set L2): rows are not removed or changed, so the epic would run on
    quietly with rows the level no longer has. Say so once (`ALERT level ...`). An old-form epic (a row per phase) and an epic whose rows belong to no level are not a lowering."""
    rows = C.distinct_rows(prows, C.ORDER)
    titles = {title_of(r, "タスク名").strip() for r in rows}
    had = C.rows_level(titles) if titles and not C.is_legacy(prows) else None
    if had is None or C.level_rank(had) <= C.level_rank(level):
        return
    alert(ctx, f"level:{epic_id}:{had}>{level}", True,
          f"level {epic_id[-8:]} {hub_name}: レベルを {had}→{level} に下げました。行は {had} のまま（{'・'.join(sorted(titles))}）で、ゲート制御は行を変えません。"
          f"{level} の行（{'・'.join(C.level_rows(level))}）にしたいときは、人が行を直してください", apply, out)


def guard_reviews(row, epic_id, level, hub_name, ctx, apply, out):
    """enforce_review_limit inside its own error boundary (a failure of it never stops the work of the epic)."""
    try:
        enforce_review_limit(epic_id, level, hub_name, ctx, apply, out)
    except Exception as e:
        out.append(f"ERROR epic {epic_id[-8:]} review limit {type(e).__name__}: {e}")


def handle_l1_epic(row, epic_id, level, apply, ctx, out):
    """An L1 epic (levels.toml gives it no phase rows): no row, no card is made. Its work rows are completed the way every row is (finish_rows), its cards keep the
    review limit, and the epic itself is set 完了 in the run that finds all its work rows finished (close_l1_epic)."""
    hub_name = title_of(row, "名前")
    out.append(f"SKIP {level} epic {epic_id[-8:]} {hub_name}: levels.toml gives {level} no phase rows")
    guard_reviews(row, epic_id, level, hub_name, ctx, apply, out)
    rows = C.query(TASK_DS, {"property": C.TASK_EPIC_PROP, "relation": {"contains": row["id"]}})
    finish_rows(row, epic_id, [r for r in rows if C.row_phases(title_of(r, "タスク名").strip()) is None], hub_name, ctx, apply, out)
    close_l1_epic(row, rows, hub_name, apply, out)


def handle_epic(row, apply, J, ctx, out):
    """Appends the lines for one registry row to `out`. Raises on unexpected failures (the caller isolates it per epic).

    Work (cancel / start a phase) first, then the independent side steps (hub ステータス, gate asks) even when the work failed."""
    epic_id = C.epic_id_from_relation(row["id"])
    state = {}  # filled as soon as the rows are known (side steps need them)
    guard_reviews(row, epic_id, C.level_of(row), title_of(row, "名前"), ctx, apply, out)
    try:
        _work(row, epic_id, apply, J, ctx, out, state)
    finally:
        if "prows" in state:
            for step in (lambda: sync_hub_status(row, state["hub_status"], state["prows"], state["hub_name"], apply, out, epic_id, state.get("live")),
                         lambda: ask_due_gates(row, epic_id, state["prows"], state["hub_name"], ctx, apply, out)):
                try:
                    step()
                except Exception as e:
                    out.append(f"ERROR epic {epic_id[-8:]} {type(e).__name__}: {e}")


def _work(row, epic_id, apply, J, ctx, out, state):
    url = "https://app.notion.com/p/" + epic_id
    hub_status = C.status_of(row)
    order = list(C.ORDER)
    tenant = C.tenant_of(epic_id)
    hub_name = title_of(row, "名前")
    out.append(f"EPIC {epic_id[-8:]} status={hub_status} open_phases={order}")
    # Progress follows the rows of this hub (task rows titled with a phase or a range of phases; a row is one card). A phase is finished only when
    # its row is 完了 or キャンセル (レビュー中 = a person must decide: not finished). An epic without any row gets them first (create_epic_rows).
    query_rows = lambda: C.query(TASK_DS, {"property": C.TASK_EPIC_PROP, "relation": {"contains": row["id"]}})
    rows = query_rows()
    if create_epic_rows(row, epic_id, rows, hub_name, J, ctx, apply, out) and apply:
        rows = query_rows()  # the rows made just now: the same run starts their card
    prows, dups = parse_rows(rows)
    if dups:
        out.append(f"DUP [{', '.join(dups)}] {hub_name}: more than one phase row; skipping this epic")
        return
    state.update(prows=prows, hub_status=hub_status, hub_name=hub_name)
    reconcile(J, epic_id, prows, apply, ctx["now"])
    level = C.level_of(row)
    level_down_alert(epic_id, level, prows, hub_name, ctx, apply, out)
    finish_rows(row, epic_id, rows, hub_name, ctx, apply, out)  # the rows whose card the reviewer passed are 完了 BEFORE the next row is decided
    live = C.live_phase_cards(ctx["cards"], epic_id)
    state["live"] = live
    cache = {}

    def items_of(title, prow):  # checklist items of a 未着手 row: read once per run, only for rows the decision reaches
        if prow["id"] not in cache:
            cache[prow["id"]] = C.checklist_items(C.blocks(prow["id"]))
        return cache[prow["id"]]

    msgs, act, cancels = next_action(items_of, prows, hub_name, live, order, ctx["now"])
    out.extend(msgs)
    cancel_rows(cancels, hub_name, apply, out)
    if act:
        start_phase(act, ctx, apply, J, dict(epic_id=epic_id, url=url, tenant=tenant, level=C.level_of(row)), hub_name, out)


def reconcile(J, epic_id, prows, apply, now):
    """A pending journal entry whose phase row has meanwhile left 未着手 (crash between row patch and journal save) is closed."""
    if not apply:
        return
    seen = set()
    for ph, row in prows.items():
        if row["id"] in seen:  # a row of a range stands for several phases
            continue
        seen.add(row["id"])
        e = J.get(C.journal_key(epic_id, title_of(row, "タスク名").strip() or ph))  # the journal entry is named by the row title
        if e and e.get("row") == "pending" and C.status_of(row) != "未着手":
            e["row"] = "done"
            e["updated"] = now
            C.save_journal(J)


# ---------------------------------------------------------------- whole run

def sanity():
    """Cheap config/template check that runs before every --apply (full suite: --selftest)."""
    procs = phase_procs("要件定義", ["a"])
    render_body("要件定義", "A", 2, {"A", "B"}, "e", "h", "p", procs, list(procs), lambda v: "t")
    built = C.build_release_ask(dict(key="k", hub_name="h", hub_url="u", repo=None, branch=None, page=None, md=None, cond_cell="", ui=None))
    if not C.has_review_chapters(built["body"]):
        raise PipelineError("build_release_ask does not make the nine chapters of templates/gate_ask.md")
    C.render("phase_row_cancel_note", reason="r")
    phases = list(C.ORDER)
    missing = [p for p in phases if p not in PHASE_ROLE or p not in PHASE_MODEL]
    if missing:
        raise PipelineError(f"PHASE_ROLE / PHASE_MODEL lack {missing}")
    for level in ("L2", "L3"):  # every row levels.toml makes is readable and has its checklist (create_epic_rows would make an empty row otherwise)
        for t in C.level_rows(level):
            if C.row_phases(t) is None or not C.row_items(level, t):
                raise PipelineError(f"levels.toml {level}: the row {t!r} is not a phase or a range of phases, or has no items")


def run(apply, now=None, deadline=None):
    """One controller pass -> (lines, errors). Per-epic error boundary; registry/query failures raise."""
    now = int(now if now is not None else time.time())
    ctx = dict(now=now, run_start=now, deadline=deadline, cards=[], asked=set(), reviews=[], alerts={}, alerts_dirty=False)
    lines = []
    J = C.load_journal() if apply else {}
    ctx["cards"] = C.kanban_cards()  # read-only board snapshot: live cards of earlier phases block later phases
    ctx["asked"] = C.asked_keys()
    ctx["reviews"] = C.kanban_reviews()  # the same board with each card's body and review facts (requests, reviewer pass): the rows' completion and the review limit
    ctx["alerts"] = C.read_alerts()  # what was already said with an ALERT line
    reg = C.registry_hubs([{"property": "新規作成の停止", "checkbox": {"equals": False}}])  # the manual stop of a person (any level)
    for row in reg:
        out = []
        try:
            level = C.level_of(row)
            if not C.makes_phase_rows(level):  # scripts/levels.toml: this level (L1) has no phase rows. The level decides, not the 新規作成の停止 flag
                handle_l1_epic(row, C.epic_id_from_relation(row["id"]), level, apply, ctx, out)
            else:
                handle_epic(row, apply, J, ctx, out)
        except Exception as e:
            out.append(f"ERROR epic {row['id'].replace('-', '')[-8:]}: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stderr)
        lines += out
    if apply:
        prune_journal(J, now)
        C.save_journal(J)
        if ctx["alerts_dirty"]:
            C.save_alerts(ctx["alerts"])
    errors = [l for l in lines if l.startswith(("FAIL", "ERROR"))]
    return lines, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gate controller (default: dry run)", allow_abbrev=False)
    ap.add_argument("--apply", action="store_true", help="create kanban cards and write back statuses")
    ap.add_argument("--selftest", action="store_true", help="run the offline test-suite and exit")
    a = ap.parse_args(argv)  # an unknown/mistyped flag exits 2 instead of silently running a dry run
    if a.selftest:
        import epic_selftest
        return epic_selftest.main(["controller"])
    if a.apply:
        try:
            sanity()
        except Exception as e:
            print(f"ERROR sanity check failed, not running: {type(e).__name__}: {e}")
            C.write_status(False, f"sanity: {e}")
            return 3
        lock = C.RunLock()
        with lock:
            if not lock.acquired:
                print("SKIP another controller run is active")
                return 0
            return _run_and_report(True)
    return _run_and_report(False)


def _run_and_report(apply):
    t0 = time.time()
    try:
        lines, errors = run(apply, deadline=t0 + RUN_BUDGET_SECONDS)
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        if apply:
            C.write_status(False, f"{type(e).__name__}: {e}", seconds=round(time.time() - t0, 1))
        return 1
    print("\n".join(lines) if lines else "no managed epics")
    if apply:
        created = sum(1 for l in lines if l.startswith("CREATED"))
        C.write_status(not errors, errors[0] if errors else None, seconds=round(time.time() - t0, 1),
                       epics=sum(1 for l in lines if l.startswith("EPIC")), created=created,
                       asked=sum(1 for l in lines if l.startswith("ASKED")))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
