#!/usr/bin/env python3
"""Generate tests/golden_bodies.json from the ORIGINAL (pre-redesign) controller's own body-building code.

Usage: python3 make_golden.py <original epic_gate_controller.py> <out json> [<original epic_common.py>]

The grouping / card-body code (the inline block that starts at `groups = {"A": set(procs)}`) is extracted from the original file and executed as-is, so the
golden file records exactly what the old code built. epic_selftest.py compares the template-based renderer with it.
The legacy code needs the ORIGINAL AGENT / PARALLEL tables (company-process table of the old pipeline). They are read from the original
epic_common.py (3rd argument; default: epic_common.py next to the original controller), never from the current one: the light version
dropped AGENT. Not deployed; run only when the original is available (backup/ or `git show <rev>:scripts/...`).
"""
import importlib.util
import json
import os
import sys
import textwrap

if len(sys.argv) not in (3, 4):
    sys.exit(__doc__)
src_path, out_path = sys.argv[1], sys.argv[2]
common_path = sys.argv[3] if len(sys.argv) == 4 else os.path.join(os.path.dirname(os.path.abspath(src_path)), "epic_common.py")
spec = importlib.util.spec_from_file_location("original_epic_common", common_path)
C = importlib.util.module_from_spec(spec)  # the ORIGINAL constants (import has no side effects)
spec.loader.exec_module(C)
for _name in ("AGENT", "PARALLEL"):
    if not hasattr(C, _name):
        sys.exit(f"{common_path} has no {_name}: pass the ORIGINAL (pre-light-version) epic_common.py as 3rd argument")
with open(src_path, encoding="utf-8") as _f:
    lines = _f.read().splitlines()
# Locate the block by its markers instead of fixed line numbers (174..202 broke as soon as the file changed).
# Start: a line `groups = {"A": set(procs)}`; end: the first later line ending in  '.")  (last line of the body text).
_START = 'groups = {"A": set(procs)}'
_ENDS = [i for i, l in enumerate(lines) if l.rstrip().endswith("""'.")""")]
_PAIRS = [(i, next((e for e in _ENDS if i < e < i + 60), None)) for i, l in enumerate(lines) if l.lstrip().startswith(_START)]
_PAIRS = [(a, b) for a, b in _PAIRS if b is not None]
if not _PAIRS:
    sys.exit(f"{src_path}: no inline body-building block found (start `{_START}`, end: a line ending in '.\")). "
             "Pass the ORIGINAL controller from before the bodies moved into templates/render_body, not the current one.")
chunk = textwrap.dedent("\n".join(lines[_PAIRS[0][0]:_PAIRS[0][1] + 1]))
legacy_src = ("def legacy(ph, procs, url, hub_name, prow, AGENT, PARALLEL, page_text):\n    bodies = {}\n    lines = []\n"
              + textwrap.indent(chunk, "    ") + "\n        bodies[g] = (body, ordered)\n    return groups, bodies\n")
ns = {}
exec(compile(legacy_src, "<legacy>", "exec"), ns)  # our own extracted source

PROCS = {  # scenario -> process names present (unfinished)
    "案件作成": ["案件概要の作成"],
    "プロダクト要件定義": ["PRDの作成", "ユースケース仕様書の作成", "CSタスクの洗い出し", "マーケタスクの洗い出し", "PRタスクの洗い出し", "リーガルチェック", "商品・業務リスク評価の実施"],
    "プロダクト要件定義/onlyB": ["CSタスクの洗い出し", "PRタスクの洗い出し"],
    "プロダクト要件定義/onlyA": ["PRDの作成", "リーガルチェック"],
    "開発要件定義": ["Design Docの作成", "実装・リリースタスクの洗い出し", "実装見積もりの作成"],
    "開発": ["実装・リリースタスクの実行"],
    "テスト": ["開発テスト計画の作成", "開発テストの実施", "QAケースの作成"],
    "リリース判定": ["リリース判定書の作成"],
}
TEXTS = ["", "line one\nline two", "has ${placeholder} and $dollar and {braces} and %s"]
cases = []
for key, names in PROCS.items():
    ph = key.split("/")[0]
    for text in TEXTS:
        procs = {n: dict(id=f"id-{i}", url=f"https://app.notion.com/p/{i:032d}", phase=ph, full=f"[{ph}] {n}", status="未着手")
                 for i, n in enumerate(names)}
        texts = {v["id"]: text for v in procs.values()}
        groups, bodies = ns["legacy"](ph, procs, "https://app.notion.com/p/EPIC", "ハブ名 テスト",
                                      {"url": "https://www.notion.so/PROW"}, C.AGENT, C.PARALLEL, lambda pid: texts[pid])
        for g, (body, ordered) in bodies.items():
            cases.append(dict(ph=ph, g=g, group_names=sorted(groups), n_groups=len(groups), epic_url="https://app.notion.com/p/EPIC",
                              hub_name="ハブ名 テスト", prow_url="https://www.notion.so/PROW", procs=procs, ordered=ordered,
                              texts=texts, body=body))
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(cases, f, ensure_ascii=False, indent=1)
print(f"wrote {len(cases)} cases to {out_path}")
