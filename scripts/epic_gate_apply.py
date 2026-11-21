#!/usr/bin/env python3
"""Cron wrapper: run the gate controller with --apply; print only what changed (empty = silent).

Exit code: non-zero whenever the controller failed (non-zero exit, FAIL/ERROR lines or a timeout) so the scheduler's
failure alert fires. Success stays silent (CREATED/COMPLETED/SKIP/WARN lines are not printed); HELD and ALERT lines are posted without failing the run.
"""
import os
import subprocess
import sys

import sfconfig

# cron スクリプトはゲートウェイの PATH を引き継ぐため、設定の hermes.path_prepend(mise の shims など)を明示的に足す
os.environ["PATH"] = os.pathsep.join([sfconfig.tool_search_path(), os.environ.get("PATH", "")])

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROLLER = os.path.join(HERE, "epic_gate_controller.py")
REVIEW_DECISIONS = os.path.join(HERE, "apply_review_decisions.py")  # 承認 -> task 完了; rides this cron, no job of its own
TIMEOUT = 240


def _mark_failed(error):
    """Record the failure in the heartbeat file when the controller could not (e.g. it was killed)."""
    try:
        sys.path.insert(0, HERE)
        import epic_common
        epic_common.write_status(False, error)
    except Exception:
        pass


def main(run=subprocess.run):
    try:
        r = run([sys.executable, CONTROLLER, "--apply"], capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"ゲート制御（michiomochi）\nERROR controller timed out after {TIMEOUT}s")
        _mark_failed(f"controller timed out after {TIMEOUT}s")
        return 1
    except Exception as e:  # cannot even start it
        print(f"ゲート制御（michiomochi）\nERROR cannot run controller: {type(e).__name__}: {e}")
        return 1
    out = [l for l in r.stdout.splitlines() if l.startswith(("FAIL", "ERROR"))]  # CREATED/SKIP は Notion 側通知があるため無言
    failed = bool(out) or r.returncode != 0
    # HELD = UI 変更なのに判定書に変更前・変更後の画像が無く、リリース承認の依頼を止めた（初回だけ出る）。失敗ではないが人に知らせる
    out += [l for l in r.stdout.splitlines() if l.startswith("HELD ")]
    # ALERT = a row was not completed (no reviewer pass / unfinished checklist / 完了レポート / 判定書の PROBLEMS), a card hit the review limit and was blocked, or an epic's level was lowered.
    # Said once by the controller (it remembers); a person has to look. Not a failure of the controller. COMPLETED / CLOSED / BLOCKED are silent: Notion and the board show them
    out += [l for l in r.stdout.splitlines() if l.startswith("ALERT ")]
    if r.returncode != 0:
        tail = (r.stderr.strip().splitlines() or [""])[-1]
        out.append(f"ERROR controller exit {r.returncode}: {tail[:150]}")
    try:  # APPLIED lines stay silent (Notion shows the task 完了); only a failure is posted
        d = run([sys.executable, REVIEW_DECISIONS, "--apply"], capture_output=True, text=True, timeout=TIMEOUT)
        bad = [l for l in d.stdout.splitlines() if l.startswith("ERROR")] or ([f"ERROR review decisions exit {d.returncode}"] if d.returncode else [])
    except Exception as e:
        bad = [f"ERROR review decisions: {type(e).__name__}: {e}"[:200]]
    out += bad
    failed = failed or bool(bad)
    if out:
        print("ゲート制御（michiomochi）\n" + "\n".join(out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
