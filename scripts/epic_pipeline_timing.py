#!/usr/bin/env python3
"""Read-only: per phase and model, how long phase cards take (work, review, rework, gaps) and how many review rounds.
Model comes from <state>/model_trial.jsonl (cards not in the log = profile default). Run: python3 epic_pipeline_timing.py"""
import sqlite3, re, json, os, collections, statistics as S
import sfconfig
CFG = sfconfig.get()
c = sqlite3.connect(os.path.join(CFG["hermes"]["home"], "kanban", "boards", CFG["hermes"]["board"], "kanban.db")); c.row_factory = sqlite3.Row
trial = {}
p = os.path.join(CFG["paths"]["state"], "model_trial.jsonl")
if os.path.exists(p):
    for l in open(p):
        d = json.loads(l); trial[(d["title"], d["group"])] = d["model"]
agg = collections.defaultdict(lambda: collections.defaultdict(list))
for t in c.execute("select id,title,created_at,completed_at from tasks where title like '[%] %' and completed_at is not null"):
    ph = re.match(r"\[(.+?)\]", t["title"]).group(1)
    runs = c.execute("select profile,started_at,ended_at from task_runs where task_id=? and started_at is not null and ended_at is not null order by started_at", (t["id"],)).fetchall()
    work = [r for r in runs if r["profile"] != "reviewer"]; rev = [r for r in runs if r["profile"] == "reviewer"]
    if not work: continue
    m = lambda r: (r["ended_at"] - r["started_at"]) / 60
    model = trial.get((t["title"], "A")) or trial.get((t["title"], "B")) or "profile-default"
    k = (ph, work[0]["profile"], model)
    for n, v in dict(total=(t["completed_at"] - t["created_at"]) / 60, work1=m(work[0]), review=sum(map(m, rev)), rework=sum(map(m, work[1:])), rounds=len(rev)).items():
        agg[k][n].append(v)
print("phase | worker | model | n | total | work1 | review | rework | review rounds (means, minutes)")
for k, d in sorted(agg.items()):
    print(" | ".join(k), "|", len(d["total"]), "|", " | ".join(f"{S.mean(d[x]):.0f}" if x != "rounds" else f"{S.mean(d[x]):.1f}" for x in ("total", "work1", "review", "rework", "rounds")))
