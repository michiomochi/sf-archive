#!/usr/bin/env python3
"""Inventory of all Hermes profiles for the morning standup (read-only, no secrets).
Prints a compact text report: per profile skills/plugins/MCP/config differences vs the default profile."""
import hashlib, json, os, re, sys, time
from pathlib import Path
import subprocess

def load_yaml(path):
    # PyYAML is not available in the cron interpreter; macOS ruby can read YAML.
    r = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts YAML.load_file(ARGV[0]).to_json", str(path)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0: raise RuntimeError(r.stderr.strip()[:120])
    return json.loads(r.stdout)

HOME = Path(os.environ.get("INVENTORY_HOME") or os.path.expanduser("~/.hermes"))
PROFILES = {"default": HOME}
pdir = HOME / "profiles"
if pdir.is_dir():
    for d in sorted(pdir.iterdir()):
        if (d / "config.yaml").exists():
            PROFILES[d.name] = d
SHARED = HOME / "shared" / "skills"
# config keys compared (never secrets): prefix match on flattened paths
WATCH = ("skills/", "kanban/", "delegation/", "mcp_servers/", "toolsets", "platform_toolsets", "plugins/", "display/language", "model/", "agent/max_turns")
DEFAULT_ONLY = ("/gateway/", "/platforms/", "/slack/")  # only the default profile runs the Slack gateway; not drift
SECRET_HINT = ("token", "secret", "key", "password", "authorization", "env", "headers")

def sha(p):
    try: return hashlib.sha1(Path(p).read_bytes()).hexdigest()[:8]
    except OSError: return "-"

def flat(x, p=""):
    out = {}
    if isinstance(x, dict):
        for k, v in x.items(): out.update(flat(v, p + "/" + str(k)))
    elif isinstance(x, list):
        out[p] = json.dumps(x, ensure_ascii=False, sort_keys=True)
    else:
        out[p] = x
    return out

def skills_of(root, external):
    res = {}
    for f in root.rglob("SKILL.md"):
        if any(part.startswith(".") for part in f.relative_to(root).parts): continue
        res[f.parent.name] = (sha(f), "symlink" if f.parent.is_symlink() or any(q.is_symlink() for q in f.parents if root in q.parents) else "dir")
    return res

def usage(root):
    try:
        u = json.loads((root / "skills" / ".usage.json").read_text())
    except Exception:
        return {}
    return {k: (v.get("use_count", 0), (v.get("last_used_at") or "")[:10]) for k, v in u.items()}

BUNDLED = set()
try:
    BUNDLED = {l.split(":")[0].strip() for l in (HOME / "skills" / ".bundled_manifest").read_text().splitlines() if l.strip()}
except OSError:
    pass
RAWUSE = {}
for _n, _r in PROFILES.items():
    try: RAWUSE[_n] = json.loads((_r / "skills" / ".usage.json").read_text())
    except Exception: RAWUSE[_n] = {}
info = {}
for name, root in PROFILES.items():
    try: cfg = load_yaml(root / "config.yaml") or {}
    except Exception as e: cfg = {}; print(f"! {name}: config unreadable: {e}")
    local = skills_of(root / "skills", False)
    ext = skills_of(SHARED, True) if SHARED.is_dir() else {}
    ext_cfg = ((cfg.get("skills") or {}).get("external_dirs")) or []
    uses_shared = any(os.path.expanduser(str(e)).rstrip("/") == str(SHARED) for e in ext_cfg)
    mcp = cfg.get("mcp_servers") or {}
    tok = root / "mcp-tokens"
    info[name] = dict(
        local=local, uses_shared=uses_shared, usage=usage(root),
        plugins=sorted(p.name for p in (root / "plugins").iterdir()) if (root / "plugins").is_dir() else [],
        mcp=sorted(mcp.keys()), tokens="symlink" if tok.is_symlink() else ("dir" if tok.is_dir() else "none"),
        soul=sha(root / "SOUL.md"), cfg=flat(cfg),
        cron=len(json.loads((root / "cron" / "jobs.json").read_text()).get("jobs", [])) if (root / "cron" / "jobs.json").exists() else 0,
    )

print("# PROFILE INVENTORY (read-only)")
print("profiles:", ", ".join(info))
print("shared skills dir:", SHARED, "->", sorted(skills_of(SHARED, True)) if SHARED.is_dir() else "MISSING")
for n, i in info.items():
    print(f"\n## {n}")
    print(f"- uses shared external_dir: {i['uses_shared']}; mcp servers: {i['mcp']}; mcp-tokens: {i['tokens']}; SOUL.md: {i['soul']}; cron jobs: {i['cron']}")
    print(f"- plugins: {i['plugins']}")
    print(f"- local skills: {len(i['local'])}")
# 1. shadows of shared skills
shared = skills_of(SHARED, True) if SHARED.is_dir() else {}
print("\n## shadowing / drift")
found = False
for n, i in info.items():
    for s in shared:
        if s in i["local"]:
            found = True; print(f"- {n}: local copy of shared skill '{s}' (local wins; drift risk)")
# 2. skills missing from some profiles (local sets compared, shared excluded)
allnames = set().union(*[set(i["local"]) for i in info.values()]) - set(shared)
for s in sorted(allnames):
    have = [n for n, i in info.items() if s in i["local"]]
    hashes = {i["local"][s][0] for n, i in info.items() if s in i["local"]}
    if len(have) != len(info):
        found = True; print(f"- skill '{s}' only in {have} (missing in {[n for n in info if n not in have]})")
    elif len(hashes) > 1:
        found = True
        bundled = s in BUNDLED
        rows = []
        for n, i in info.items():
            u = RAWUSE[n].get(s, {})
            rows.append((u.get("last_patched_at") or "", n, i["local"][s][0], u.get("patch_count", 0)))
        rows.sort(reverse=True)
        newest = rows[0][1]
        print(f"- skill '{s}' differs across profiles ({'bundled' if bundled else 'custom'}); most recently patched: {newest}")
        for lp, n, h, pc in rows:
            dl = ""
            if n != newest:
                a = PROFILES[newest] / "skills"; b = PROFILES[n] / "skills"
                fa = next(a.rglob(f"{s}/SKILL.md"), None); fb = next(b.rglob(f"{s}/SKILL.md"), None)
                if fa and fb:
                    r = subprocess.run(["diff", "-U0", str(fb), str(fa)], capture_output=True, text=True)
                    dl = f" diff_vs_{newest}={sum(1 for l in r.stdout.splitlines() if l[:1] in '+-' and l[:3] not in ('+++','---'))} lines"
            print(f"   {n}: hash={h} last_patched={lp[:16] or '-'} patch_count={pc}{dl}")
        print(f"   path of newest: {next((PROFILES[newest]/'skills').rglob(f'{s}/SKILL.md'), '')}")
# 3. plugin / mcp differences
for key in ("plugins", "mcp"):
    sets = {n: tuple(i[key]) for n, i in info.items()}
    if len(set(sets.values())) > 1:
        found = True; print(f"- {key} differ: {sets}")
if not all(i["uses_shared"] for i in info.values()):
    found = True; print("- profiles not using the shared external_dir:", [n for n, i in info.items() if not i["uses_shared"]])
bad = [n for n, i in info.items() if n != "default" and i["tokens"] != "symlink"]
if bad: found = True; print("- profiles whose mcp-tokens is not shared with default:", bad)
# 4. config differences vs default (watched keys, no secrets)
base = info["default"]["cfg"]
for n, i in info.items():
    if n == "default": continue
    diffs = []
    for k in sorted(set(base) | set(i["cfg"])):
        if k.startswith(DEFAULT_ONLY): continue
        if any(h in k.lower() for h in SECRET_HINT): continue
        if base.get(k) != i["cfg"].get(k):
            diffs.append(f"{k}: default={str(base.get(k))[:50]!r} {n}={str(i['cfg'].get(k))[:50]!r}")
    if diffs:
        found = True; print(f"- config differences {n} vs default ({len(diffs)}):"); [print("   ", d) for d in diffs[:15]]
if not found: print("- none")
# 4b. memories (user facts), .env variable NAMES, hooks, SOUL presence
def names_env(root):
    try: return sorted(l.split("=",1)[0] for l in (root/".env").read_text().splitlines() if l and not l.startswith("#") and "=" in l)
    except OSError: return []
ENV_DEFAULT_ONLY = {"SLACK_ALLOWED_USERS","SLACK_APP_TOKEN","SLACK_BOT_TOKEN","SLACK_HOME_CHANNEL","SLACK_HOME_CHANNEL_THREAD_ID"}
dn = set(names_env(PROFILES["default"])) - ENV_DEFAULT_ONLY
for n, root in PROFILES.items():
    if n == "default": continue
    miss = sorted(dn - set(names_env(root))); extra_ = sorted(set(names_env(root)) - dn)
    if miss or extra_:
        found = True; print(f"- .env variable names differ in {n}: missing={miss} extra={extra_} (names only)")
for fname in ("USER.md", "MEMORY.md"):
    hs = {n: sha(r/"memories"/fname) for n, r in PROFILES.items()}
    if len(set(hs.values())) > 1:
        found = True
        print(f"- memories/{fname} differs: {hs}  (USER.md = facts about the user: default is the source; MEMORY.md = environment notes: propose only)")
hk = {n: sorted(p.name for p in (r/"hooks").iterdir()) if (r/"hooks").is_dir() else [] for n, r in PROFILES.items()}
if len({tuple(v) for v in hk.values()}) > 1:
    found = True; print(f"- hooks differ: {hk}")

# 5. usage
print("\n## skill usage (use_count, last_used) top per profile")
for n, i in info.items():
    top = sorted(i["usage"].items(), key=lambda kv: -kv[1][0])[:5]
    print(f"- {n}: {[(k, v[0], v[1]) for k, v in top]}")

# 6. a レビュー中 task without its review row: one line, silent when none (the deployed scripts hold the Notion config)
_chk = Path(os.environ.get("SF_LIVE") or os.path.expanduser("~/.hermes/scripts/sf")) / "check_review_pairs.py"
try:
    _r = subprocess.run([sys.executable, str(_chk)], capture_output=True, text=True, timeout=120)
    _n = ", ".join(f"{a} {b}" for a, b in re.findall(r"^review pairs: (\d+) (missing|stale)$", _r.stdout, re.M) if a != "0")
    if _r.returncode == 1 and _n: print(f"- review pairs: {_n} (python3 check_review_pairs.py lists them)")
    elif _r.returncode: print(f"- review pairs: check failed ({((_r.stdout or _r.stderr).strip().splitlines() or ['rc ' + str(_r.returncode)])[0][:120]})")
except (OSError, subprocess.TimeoutExpired) as _e: print(f"- review pairs: check failed ({type(_e).__name__})")
