#!/usr/bin/env python3
"""The rule "remaining work is filed as a row before a task is closed" (one owner: skills/autonomous-task-execution/references/row-lifecycle.md).

Work you leave behind must be a Notion row, so the 完了レポート never ends with untracked follow-ups. check(text) reads a report (Markdown):

  * requires the section 未確認・残件,
  * accepts 'なし' / '未実施' as 'no remaining work',
  * otherwise requires every bullet in that section to carry a Notion URL
    (https://app.notion.com/p/... or https://www.notion.so/...).

Two callers, one rule: tools/remaining_work_check.py (the command an agent runs on its report) and the gate controller
(epic_gate_controller.finish_rows, which completes a row only when its 完了レポート passes). Standard library only; deployed next to epic_common.py.
"""
import re

SECTION = "未確認・残件"
NONE_WORDS = ("なし", "無し", "未実施")
ROW_URL = re.compile(r"https://(?:app\.notion\.com|www\.notion\.so)/\S+")


def section_lines(text):
    """Return the body lines of the 未確認・残件 section, or None when absent."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and SECTION in line:
            start = i + 1
            break
    if start is None:
        return None
    out = []
    for line in lines[start:]:
        if line.lstrip().startswith("#"):
            break
        out.append(line)
    return out


def check(text):
    """-> the problems of a report (an empty list = it passes)."""
    body = section_lines(text)
    if body is None:
        return [f"「{SECTION}」の章がありません（残作業が無い場合も「なし」と書いてください）"]
    items = [l.strip() for l in body if l.strip().startswith(("-", "*"))]
    if not items:
        flat = " ".join(l.strip() for l in body if l.strip())
        if not flat:
            return [f"「{SECTION}」が空です（残作業が無い場合は「なし」）"]
        if any(w in flat for w in NONE_WORDS):
            return []
        return [f"「{SECTION}」に箇条書きがありません（残作業を項目ごとに書いてください）"]
    problems = []
    for item in items:
        if any(w in item for w in NONE_WORDS):
            continue
        if not ROW_URL.search(item):
            problems.append(f"起票されていない残作業があります（行の URL を添えてください）: {item[:80]}")
    return problems
