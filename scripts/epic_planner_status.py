#!/usr/bin/env python3
"""Deterministic guard metrics for the epic planner (no LLM, read-only).

For every エピック row whose ステータス is not 完了/中止, count the tasks linked through
the エピック relation and print one stable line per epic. The planner cron runs this as a monitor
script: the agent wakes only when the output changes.

Settled: settled=yes when the epic has at least one non-cancelled task and every one of them is 完了 or レビュー中
(event trigger for the planner's 打ち手の見直し; no weekly sweep).
Guards (defaults; override in the hub page's 調査の予算 section, which the planner reads):
  新規作成の停止 checked    -> allow=none     (manual stop)
  レベル whose levels.toml rows are empty (L1) -> allow=none reasons=level_L1  (the level decides; the flag is not needed)
Exit code is non-zero (with an ERROR line) when a Notion call fails.
Output is stable: no timestamps. Two hashes wake the planner only on real changes:
  tasks_hash = sorted (task id, status) pairs of the non-phase tasks
  watch_hash = unchecked 候補 lines of the hub page + the phase-row statuses
               (replaces the old hub last_edited_time, which changed on every edit)
gates_due = gates that are due according to the rows (the row that works リリース判定 is 完了 -> リリース承認).
The gate controller asks them (no agent involved).
The planner makes NO row: the rows of an epic (titled with a phase or a range of phases <first>〜<last>; a row is one card) are made by the gate
controller from levels.toml (epic_gate_controller.create_epic_rows). A row is never a 打ち手 task here and stands for every phase it works.
"""
import hashlib
import sys
import traceback

import epic_common as C

HUB_DS = C.REGISTRY_DS
TASK_DS = C.TASK_DS
query = C.query


def sel(p, k):
    return ((p.get(k) or {}).get("select") or {}).get("name", "-")


def status(p, k):
    return ((p.get(k) or {}).get("status") or {}).get("name", "-")


def title(p, k):
    return "".join(x["plain_text"] for x in (p.get(k) or {}).get("title", []))


PHASES = tuple(C.ORDER)
WATCH_SECTIONS = ("候補",)


def hub_watch_lines(page_id):
    """Unchecked 候補 items (top-level to_do blocks of the hub page), as stable lines."""
    section, out = None, []
    for b in C.blocks(page_id):
        t = b.get("type")
        if t == "heading_1":
            txt = C.block_text(b).strip()
            section = txt if txt in WATCH_SECTIONS else None
        elif t == "to_do" and section and not b["to_do"].get("checked"):
            out.append(f"{section}:{C.block_text(b).strip()}")
    return out


def main():
    """Every minute at most once: two overlapping planner runs could both create the same task."""
    lock = C.RunLock("planner.lock")
    with lock:
        if not lock.acquired:
            print("SKIP another planner run is active")
            return 0
        return _run()


def _run():
    hubs = query(HUB_DS, {"and": [
        {"property": "ステータス", "status": {"does_not_equal": "完了"}},
        {"property": "ステータス", "status": {"does_not_equal": "中止"}}]})
    lines = []
    for h in sorted(hubs, key=lambda x: x["id"]):
        hp = h["properties"]
        kind = ((hp.get("種別") or {}).get("select") or {}).get("name", "未設定")
        tasks = query(TASK_DS, {"property": C.TASK_EPIC_PROP, "relation": {"contains": h["id"]}})
        eff = open_n = 0
        sig = []
        prows, dups = C.rows_by_phase(tasks)  # {phase: row}: a row of a range stands for every phase it works
        row_titles = {title(t["properties"], "タスク名").strip() for t in tasks if C.row_phases(title(t["properties"], "タスク名")) is not None}
        row_status = {ph: status(r["properties"], "ステータス") for ph, r in prows.items()}
        for name in dups:
            for ph in (C.row_phases(name) or (name,)):
                row_status[ph] = "DUP"
        for t in tasks:
            tp = t["properties"]
            ttl = title(tp, "タスク名")
            if ttl.strip() in row_titles:  # rows (made and started by the gate controller) are not 打ち手 (候補) work
                continue
            st = status(tp, "ステータス")
            sig.append(f"{t['id']}:{st}:")
            if st == "キャンセル":
                continue
            eff += 1
            if st not in ("完了", "レビュー中"):
                open_n += 1
        settled = "yes" if eff and not open_n else "no"
        manual = bool((hp.get("新規作成の停止") or {}).get("checkbox"))
        allow = "all"
        reasons = []
        level = C.level_of(h)
        if manual:
            allow = "none"; reasons.append("manual_stop")
        if not C.makes_phase_rows(level):  # levels.toml: no phase rows at this level (L1), and no task made from a 候補 either (the one task of an L1 epic is already there)
            allow = "none"; reasons.append(f"level_{level}")
        h8 = hashlib.sha1("|".join(sorted(sig)).encode()).hexdigest()[:8]
        row_lines = sorted({f"row:{ttl}={st}" for ttl, _, r in C.row_units(prows, C.ORDER) if r is not None
                            for st in [("DUP" if ttl in dups else status(r["properties"], "ステータス"))]})
        watch = hub_watch_lines(h["id"]) + row_lines
        w8 = hashlib.sha1("\n".join(sorted(watch)).encode()).hexdigest()[:8]
        product = kind == "プロダクト開発"
        gates = ",".join(C.gates_due(row_status)) if product else ""
        lines.append(
            f"EPIC {h['id'].replace('-', '')[-8:]} {title(hp, '名前')[:40]} status={status(hp, 'ステータス')} "
            f"kind={kind} tasks={eff} settled={settled} "
            f"allow={allow} reasons={','.join(reasons) or '-'} "
            f"tasks_hash={h8} watch_hash={w8} gates_due={gates or '-'}")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
