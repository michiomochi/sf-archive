#!/usr/bin/env python3
"""Notify on タスク events (created / status changed / renamed / removed).
No LLM. Compares the DB with a stored snapshot; prints one line per event (empty = silent).
The first run only records a baseline and prints nothing.
The タスク DB has no 優先度 property any more (a card's priority is left to the kanban default): it is never read or reported.
"""
import json, os, shutil, subprocess, sys

import sfconfig

CFG = sfconfig.get()
DS = CFG["notion"]["task_ds"]
EPIC_DS = CFG["notion"]["epic_ds"]
EPIC_PROP = CFG["notion"].get("task_epic_prop", "エピック")
STATE = os.path.join(CFG["hermes"]["home"], "cache", "michiomochi_task_snapshot.json")


def ntn(*args, data=None):
    exe = shutil.which("ntn") or shutil.which("ntn", path=sfconfig.tool_search_path(CFG)) or "ntn"
    cmd = [exe, "api", *args]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return json.loads(r.stdout)


def fetch_epics():
    out, cur = {}, None
    while True:
        body = {"page_size": 100}
        if cur:
            body["start_cursor"] = cur
        d = ntn(f"v1/data_sources/{EPIC_DS}/query", "-X", "POST", data=body)
        for p in d["results"]:
            for prop in p["properties"].values():
                if prop["type"] == "title":
                    out[p["id"]] = "".join(x["plain_text"] for x in prop["title"])
        if not d.get("has_more"):
            return out
        cur = d["next_cursor"]


def fetch():
    epics = fetch_epics()
    out, cur = {}, None
    while True:
        body = {"page_size": 100}
        if cur:
            body["start_cursor"] = cur
        d = ntn(f"v1/data_sources/{DS}/query", "-X", "POST", data=body)
        for p in d["results"]:
            if p.get("in_trash") or p.get("archived"):
                continue
            pr = p["properties"]
            out[p["id"]] = {
                "title": "".join(x["plain_text"] for x in pr["タスク名"]["title"]),
                "status": pr["ステータス"]["status"]["name"],
                "url": p["url"],
                "epic": "、".join(epics.get(r["id"], "不明") for r in pr[EPIC_PROP]["relation"]) or "エピックなし",
            }
        if not d.get("has_more"):
            return out
        cur = d["next_cursor"]


def link(t):
    return f"<{t['url']}|{t['title']}>（エピック：{t.get('epic', '-')}）"


def main():
    now = fetch()
    first = not os.path.exists(STATE)
    old = {}
    if not first:
        with open(STATE, encoding="utf-8") as f:
            old = json.load(f)
    lines = []
    if not first:
        for pid, t in now.items():
            o = old.get(pid)
            if o is None:
                lines.append(f"🆕 タスク作成：{link(t)}（{t['status']}）")
                continue
            if o["status"] != t["status"]:
                lines.append(f"🔄 ステータス変更：{link(t)}　{o['status']} → {t['status']}")
            if o["title"] != t["title"]:
                lines.append(f"✏️ タスク名変更：{o['title']} → {link(t)}")
        for pid, o in old.items():
            if pid not in now:
                lines.append(f"🗑️ タスク削除（ゴミ箱）：{o['title']}")
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(now, f, ensure_ascii=False)
    os.replace(tmp, STATE)
    if lines:
        print("タスクの更新\n" + "\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"タスク通知スクリプトのエラー：{e}")
