#!/usr/bin/env python3
"""Check that remaining work is filed as a row before a task is closed.

Rule (one owner: skills/autonomous-task-execution/references/row-lifecycle.md; the function is scripts/remaining_work.py, which the gate
controller calls too when it completes a row): work you leave behind must be a Notion row, so the 完了レポート never ends with
untracked follow-ups. This command reads a report (Markdown) and:

  * requires the section 未確認・残件,
  * accepts 'なし' / '未実施' as 'no remaining work',
  * otherwise requires every bullet in that section to carry a Notion URL
    (https://app.notion.com/p/... or https://www.notion.so/...).

Exit 0 when the report passes, 1 with one line per problem when it does not.
Standard library only.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from remaining_work import NONE_WORDS, ROW_URL, SECTION, check, section_lines  # noqa: E402,F401  (re-exported: the tests and the callers use these names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="完了レポートの Markdown ファイル（- で標準入力）")
    args = ap.parse_args()
    text = sys.stdin.read() if args.report == "-" else open(args.report, encoding="utf-8").read()
    problems = check(text)
    for p in problems:
        print(f"REMAINING {p}")
    if problems:
        print(f"remaining_work: {len(problems)} 件の起票漏れ")
        return 1
    print("remaining_work: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
