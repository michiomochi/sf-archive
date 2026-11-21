#!/usr/bin/env python3
"""Compare the template renderer with tests/golden_bodies.json (bodies built by the original code). Exit 1 on mismatch."""
import difflib
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import epic_gate_controller as G  # noqa: E402

with open(os.path.join(HERE, "tests", "golden_bodies.json"), encoding="utf-8") as f:
    cases = json.load(f)
bad = 0
for c in cases:
    body = G.render_body(c["ph"], c["g"], c["n_groups"], c["group_names"], c["epic_url"], c["hub_name"], c["prow_url"],
                         c["procs"], c["ordered"], lambda v: c["texts"][v["id"]])
    if body != c["body"]:
        bad += 1
        for l in list(difflib.unified_diff(c["body"].splitlines(), body.splitlines(), lineterm="", n=0))[:6]:
            print(l[:300])
print(f"{len(cases)} cases, mismatches {bad}")
sys.exit(1 if bad else 0)
