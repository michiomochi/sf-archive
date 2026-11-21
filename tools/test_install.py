#!/usr/bin/env python3
"""インストーラ(tools/factory.py の cron / wrappers / profiles / board / notion-* / doctor)のオフラインのテスト。

本物の hermes / ntn / Notion / 本番(~/.hermes)には触れない: 一時ディレクトリに偽の ~/.hermes と偽の hermes / ntn(SF_HERMES / SF_NTN)を作り、
呼ばれたコマンドと書かれたファイルを確かめる。`make test` から実行する。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import factory  # noqa: E402
import sync_live  # noqa: E402

REPO = factory.REPO

FAKE_HERMES = '''#!{py}
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
log = os.environ["STUB_LOG"]
state_path = Path(os.environ["STUB_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {{"config": {{}}, "boards": ["default"]}}
profile = "default"
if argv[:1] == ["-p"]:
    profile, argv = argv[1], argv[2:]
open(log, "a").write(json.dumps([profile] + argv, ensure_ascii=False) + "\\n")
home = Path(os.environ["STUB_HOME"])
jobs_path = home / "profiles" / "dev" / "cron" / "jobs.json"
def save():
    state_path.write_text(json.dumps(state))
if argv == ["--version"]:
    print("Hermes Agent v0.22.6 (2026.9.24) · stable"); sys.exit(0)
if argv[:2] == ["cron", "create"]:
    name = argv[argv.index("--name") + 1]
    data = json.loads(jobs_path.read_text())
    data["jobs"].append({{"id": "new%09d" % len(data["jobs"]), "name": name, "state": "paused", "enabled": False}})
    jobs_path.write_text(json.dumps(data)); sys.exit(0)
if argv[:2] in (["cron", "pause"], ["cron", "resume"], ["cron", "edit"]):
    sys.exit(0)
if argv[:2] == ["profile", "create"]:
    d = home / "profiles" / argv[2]; d.mkdir(parents=True, exist_ok=True); (d / "config.yaml").write_text("x: 1\\n"); sys.exit(0)
if argv[:2] == ["config", "set"]:
    v = argv[3]
    try:
        v = json.loads(v) if v.startswith("[") else v
    except ValueError:
        pass
    state["config"].setdefault(profile, {{}})[argv[2]] = v; save(); sys.exit(0)
if argv[:2] == ["config", "get"]:
    v = state["config"].get(profile, {{}}).get(argv[2])
    if v is None:
        sys.exit(1)
    print("\\n".join("  - " + x for x in v) if isinstance(v, list) else v); sys.exit(0)
if argv[:3] == ["kanban", "boards", "list"]:
    print("    SLUG                      NAME                          COUNTS")
    for b in state["boards"]:
        print("●   %-24s  %-28s  done=1" % (b, b))
    sys.exit(0)
if argv[:3] == ["kanban", "boards", "create"]:
    state["boards"].append(argv[3]); save(); sys.exit(0)
if argv[:2] == ["mcp", "list"]:
    print("  notion           https://mcp.notion.com/mcp     all          ✓ enabled"); sys.exit(0)
if argv[:2] == ["gateway", "status"]:
    print("✓ Gateway is running via the default-profile multiplexer"); sys.exit(0)
sys.stderr.write("fake hermes: 未対応 %r\\n" % (argv,)); sys.exit(2)
'''

# Notion の最小の偽物: POST v1/databases で作った DB を覚え、GET v1/data_sources/<id> でその応答の形に戻す。
# dual_property の関係は、本物と同じく相手の DB に対の関係を自動で作る。
FAKE_NTN = '''#!{py}
import json, os, sys, uuid
from pathlib import Path
argv = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(json.dumps(["ntn"] + argv, ensure_ascii=False) + "\\n")
sp = Path(os.environ["STUB_NTN_STATE"])
st = json.loads(sp.read_text()) if sp.exists() else {{"ds": {{}}}}
if argv[:1] == ["whoami"]:
    print("id\\texample-dev\\tbot"); sys.exit(0)
if argv[:1] != ["api"]:
    sys.exit(2)
path = argv[1]
def to_response(ds):
    props = {{}}
    for n, p in ds["properties"].items():
        t = p["type"]; e = {{"type": t, "name": n, t: p.get(t, {{}})}}
        if t == "status":
            opts, groups = [], {{}}
            for o in p["status"]["options"]:
                oid = str(uuid.uuid5(uuid.NAMESPACE_DNS, n + o["name"])); opts.append({{"id": oid, "name": o["name"], "color": o.get("color")}})
                groups.setdefault(o["group"], []).append(oid)
            e["status"] = {{"options": opts, "groups": [{{"name": g, "option_ids": ids}} for g, ids in groups.items()]}}
        props[n] = e
    return {{"object": "data_source", "id": ds["id"], "properties": props}}
if "-X" in argv and argv[argv.index("-X") + 1] == "POST" and path == "v1/databases":
    body = json.loads(sys.stdin.read())
    ds_id = str(uuid.uuid4())
    props = body["initial_data_source"]["properties"]
    st["ds"][ds_id] = {{"id": ds_id, "properties": props, "title": body["title"][0]["text"]["content"], "parent": body["parent"]}}
    for n, p in props.items():
        r = p.get("relation")
        if r and r["type"] == "dual_property":  # 相手に対の関係を作る
            other = st["ds"][r["data_source_id"]]
            other["properties"][r["dual_property"]["synced_property_name"]] = {{"type": "relation", "relation": {{
                "data_source_id": ds_id, "type": "dual_property", "dual_property": {{"synced_property_name": n}}}}}}
    sp.write_text(json.dumps(st))
    print(json.dumps({{"object": "database", "id": "db-" + ds_id[:8], "data_sources": [{{"id": ds_id, "name": body["title"][0]["text"]["content"]}}]}}))
    sys.exit(0)
if path.startswith("v1/data_sources/"):
    ds = st["ds"].get(path.rsplit("/", 1)[1])
    if ds is None:
        print(json.dumps({{"object": "error", "code": "object_not_found", "message": "not found"}})); sys.exit(0)
    print(json.dumps(to_response(ds))); sys.exit(0)
sys.exit(2)
'''


def run(argv, **env):
    """factory.main(argv) を実行して (終了コード, 標準出力, 標準エラー) を返す。"""
    out, err = io.StringIO(), io.StringIO()
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = factory.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (print(e.code, file=sys.stderr) or 1)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})
    return rc, out.getvalue(), err.getvalue()


class Sandbox:
    """偽の ~/.hermes、偽の hermes / ntn、テスト用の設定ファイル。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-install-test-"))
        t = self.tmp
        self.home, self.live, self.skills, self.bin = t / "hermes", t / "live", t / "skills", t / "bin"
        for d in (self.home / "profiles" / "dev" / "cron", self.home / "profiles" / "dev" / "scripts", self.live, self.skills, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        text = (REPO / "tests" / "config.test.toml").read_text(encoding="utf-8")
        for key, val in (("home", self.home), ("live", self.live), ("skills", self.skills), ("state", t / "state")):
            text = "\n".join(f'{key} = "{val}"' if l.startswith(f"{key} = ") else l for l in text.split("\n"))
        self.config = t / "env.toml"
        self.config.write_text(text, encoding="utf-8")
        self.log, self.state, self.ntn_state = t / "calls.log", t / "hermes_state.json", t / "ntn_state.json"
        for name, src in (("hermes", FAKE_HERMES), ("ntn", FAKE_NTN)):
            (self.bin / name).write_text(src.format(py=sys.executable), encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.env = dict(SF_HERMES=self.bin / "hermes", SF_NTN=self.bin / "ntn", STUB_LOG=self.log, STUB_STATE=self.state,
                        STUB_NTN_STATE=self.ntn_state, STUB_HOME=self.home, SF_CRON_IDS=self.live / "cron_ids.json")
        self.ctx = factory.Ctx(type("A", (), {"config": str(self.config), "env": None})())
        self.desired = factory.desired_jobs(factory.load_cron_toml(), self.ctx.cfg, self.ctx.rendered())
        self.write_jobs({k: dict(w, id=f"{i:012x}") for i, (k, w) in enumerate(self.desired.items(), 1)})

    def write_jobs(self, by_key):
        jobs = []
        for key, w in by_key.items():
            jobs.append(dict(id=w["id"], name=w["name"], schedule_display=w["schedule"], catch_up=w["catch_up"] == "on", deliver=w["deliver"],
                             failure_deliver=w["failure_deliver"], no_agent=w["no_agent"], script=w["script"], monitor_script=w["monitor_script"],
                             prompt=w.get("prompt", ""), skills=w.get("skills", []), model=w.get("model"), provider=w.get("provider"),
                             reasoning_effort=w.get("reasoning"), state="scheduled", enabled=True))
        (self.home / "profiles" / "dev" / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        self.ids = {k: w["id"] for k, w in by_key.items()}
        self.by_key = by_key

    def jobs(self):
        return json.loads((self.home / "profiles" / "dev" / "cron" / "jobs.json").read_text())["jobs"]

    def adopt_all(self):
        factory.write_ids(self.ctx, self.ids)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def cli(self, *argv, **extra):
        return run([*argv, "--config", str(self.config)], **{**self.env, **extra})

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    # ---- cron.toml
    def test_cron_toml_resolves_for_every_env(self):
        for cfg_path in [*factory.env_files(), REPO / "tests" / "config.test.toml"]:
            raw, cfg = factory.load_cfg(cfg_path)
            try:
                rendered = factory.render_all(raw)[0]
            except factory.SourceMissingError:
                continue  # 外のリポジトリのプラグインがこの機械に無い環境は飛ばす
            jobs = factory.desired_jobs(factory.load_cron_toml(raw), cfg, rendered)
            core = {"intake", "planner", "health", "review_apply", "gate", "ship", "task_events", "drift", "decision_digest", "profile_standup"}
            ext = {"example": {"example." + k for k in ("slack_join", "approval_review", "slack_digest", "procedure_maintenance")}}
            want = core.union(*(ext.get(n, set()) for n in raw.get("plugins", [])))
            self.assertEqual(set(jobs), want, cfg_path)
            self.assertTrue(all(j["deliver"] == cfg["chat"]["deliver"] and j["failure_deliver"] == cfg["chat"]["failure_deliver"] for j in jobs.values()))
            self.assertEqual(jobs["gate"]["no_agent"], True)
            self.assertEqual(jobs["intake"]["model"], cfg["models"]["worker"])
            self.assertNotIn("${", "".join(j.get("prompt", "") for j in jobs.values()))

    def test_plugin_can_be_turned_off(self):
        """plugins から外せば、同梱の example プラグインの内容は何も入らない。"""
        raw = dict(self.sb.ctx.raw, plugins=[])
        files, _ = factory.render_all(raw)
        self.assertFalse([f for f in files if f.startswith("plugins/")])
        self.assertFalse([k for k in factory.load_cron_toml(raw)["jobs"] if k.startswith("example.")])
        self.assertNotIn("example-dev-env-ops", factory.sfplugins.deployed_skills(files))
        on, _ = factory.render_all(dict(raw, plugins=["example"], plugin={"example": {}}))
        self.assertIn("example-dev-env-ops", factory.sfplugins.deployed_skills(on))

    # ---- 環境で有効なプラグインだけを、本番との照合の対象にする(sync_live.py / factory.py check)
    EXAMPLE_PROMPTS = {"approval_review", "slack_digest", "procedure_maintenance"}

    def raw_with_plugins(self, names):
        return dict(self.sb.ctx.raw, plugins=names, plugin={"example": {}} if names else {})

    def test_prompt_jobs_follow_enabled_plugins(self):
        """cron の対応表(PROMPT_JOBS)は、環境で有効なプラグインのジョブだけ。plugins 無し・[] では example の指示文は入らず、["example"] では入る。"""
        raw_none = {k: v for k, v in self.sb.ctx.raw.items() if k not in ("plugins", "plugin")}
        for raw in (raw_none, self.raw_with_plugins([])):
            self.assertFalse(self.EXAMPLE_PROMPTS & set(factory.prompt_jobs(raw)), raw.get("plugins"))
        on = factory.prompt_jobs(self.raw_with_plugins(["example"]))
        self.assertTrue(self.EXAMPLE_PROMPTS <= set(on))
        self.assertTrue(on["approval_review"].startswith("approval"))

    def test_sync_live_drift_does_not_crash_with_plugin_prompts(self):
        """無効なプラグインの指示文が置換済みに無くても prompt_drift() は落ちない(以前は KeyError: 'prompts/approval_review.txt')。
        有効なら plugins/<名前>/prompts/ の指示文を引き、本番にジョブが無ければ差分として出す。"""
        saved = (sync_live.SKILLS_LIVE, sync_live.JOBS_JSON, dict(sync_live.RENDERED), dict(sync_live.PROMPT_JOBS))
        def restore():
            sync_live.SKILLS_LIVE, sync_live.JOBS_JSON = saved[:2]
            sync_live.RENDERED.clear(); sync_live.RENDERED.update(saved[2])
            sync_live.PROMPT_JOBS.clear(); sync_live.PROMPT_JOBS.update(saved[3])
        self.addCleanup(restore)
        sync_live.SKILLS_LIVE = self.sb.skills
        sync_live.JOBS_JSON = self.sb.home / "profiles" / "dev" / "cron" / "jobs.json"
        sync_live.use_env(self.raw_with_plugins([]))
        self.assertEqual(sync_live.prompt_drift(), [])
        sync_live.use_env(self.raw_with_plugins(["example"]))
        missing = {key for kind, key, why in sync_live.prompt_drift() if kind == "cron" and "見つからない" in why}
        self.assertEqual(missing, self.EXAMPLE_PROMPTS)
        self.assertIn("approval", sync_live.want_prompt("approval_review").lower())

    def test_disabled_plugin_puts_nothing_into_production(self):
        """無効なプラグインの cron ジョブ・スキル・指示文・ラッパーは、本番に置くものに一切入らない(有効なら入る)。"""
        ctx, wrapper = self.sb.ctx, "sf-slack-join.sh"
        def built(names):
            raw = self.raw_with_plugins(names)
            files = factory.render_all(raw)[0]
            cron = factory.load_cron_toml(raw)
            old, ctx.raw = ctx.raw, raw
            try:
                wrappers = factory.render_wrappers(ctx, cron)
            finally:
                ctx.raw = old
            jobs = factory.desired_jobs(cron, ctx.cfg, files)
            return files, jobs, wrappers
        files, jobs, wrappers = built([])
        self.assertFalse([f for f in files if f.startswith("plugins/")])
        self.assertFalse([k for k in jobs if k.startswith("example.")])
        self.assertNotIn(wrapper, wrappers)
        self.assertFalse(self.EXAMPLE_PROMPTS & set(factory.sfplugins.deployed_prompts(files)))
        self.assertNotIn("example-dev-env-ops", factory.sfplugins.deployed_skills(files))
        files, jobs, wrappers = built(["example"])
        self.assertTrue({"example.slack_join", "example.approval_review"} <= set(jobs))
        self.assertIn(wrapper, wrappers)
        self.assertTrue(self.EXAMPLE_PROMPTS <= set(factory.sfplugins.deployed_prompts(files)))
        self.assertIn("example-dev-env-ops", factory.sfplugins.deployed_skills(files))

    def test_external_plugin_source(self):
        """別リポジトリのプラグイン([plugin_sources])の指示文・スキル・cron・ラッパーが読まれ、無いパスは SourceMissingError。"""
        d = Path(tempfile.mkdtemp(prefix="sf-plugin-test-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "prompts").mkdir()
        (d / "skills" / "ext-skill").mkdir(parents=True)
        (d / "scripts").mkdir()
        (d / "plugin.toml").write_text('name = "ext"\ndescription = "t"\n[template_names]\nextv = "extv"\n', encoding="utf-8")
        (d / "prompts" / "ext_job.txt").write_text("hello ${extv}\n", encoding="utf-8")
        (d / "skills" / "ext-skill" / "SKILL.md").write_text("---\nname: ext-skill\ndescription: t\n---\nvalue ${extv}\n", encoding="utf-8")
        (d / "scripts" / "x.py").write_text("print(1)\n", encoding="utf-8")
        (d / "cron.toml").write_text('[jobs.j]\nname = "ext job"\nschedule = "0 9 * * *"\nprompt = "ext_job"\nmodel = "worker"\nreasoning = "low"\n'
                                     '[wrappers."ext.sh"]\nrun = "python3"\ntarget = "plugin/ext/scripts/x.py"\nabout = "t"\n', encoding="utf-8")
        raw = dict(self.sb.ctx.raw, plugins=["ext"], plugin_sources={"ext": str(d)}, plugin={"ext": {"extv": "VAL"}})
        files, _ = factory.render_all(raw)
        self.assertEqual(files["plugins/ext/prompts/ext_job.txt"], "hello VAL\n")
        self.assertIn("value VAL", factory.sfplugins.deployed_skills(files)["ext-skill"]["SKILL.md"])
        cron = factory.load_cron_toml(raw)
        self.assertIn("ext.j", cron["jobs"])
        ctx = self.sb.ctx
        old = ctx.raw
        ctx.raw = raw
        try:
            w = factory.render_wrappers(ctx, cron)
        finally:
            ctx.raw = old
        self.assertIn(f'TARGET="{d}/scripts/x.py"', w["ext.sh"])
        raw["plugin_sources"] = {"ext": str(d / "nowhere")}
        with self.assertRaises(factory.SourceMissingError):
            factory.render_all(raw)

    def test_unknown_reference_in_cron_toml_stops(self):
        cron = factory.load_cron_toml()
        cron["jobs"]["intake"]["model"] = "no_such_model"
        with self.assertRaises(SystemExit) as cm:
            factory.desired_jobs(cron, self.sb.ctx.cfg, self.sb.ctx.rendered())
        self.assertIn("no_such_model", str(cm.exception))

    def test_no_hardcoded_discord_ids_or_repo_url_left_in_templates(self):
        for _, rel in factory.source_files():
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertNotIn("1473768887694246154", text, rel)
            self.assertNotIn("1421420969335515535", text, rel)
        self.assertNotIn("sample-repo", (REPO / "notion" / "templates" / "task.md").read_text(encoding="utf-8"))

    # ---- cron adopt
    def test_adopt_stops_on_unknown_id_and_writes_nothing(self):
        rc, out, err = self.sb.cli("cron", "adopt", f"intake={self.sb.ids['intake']}", "gate=deadbeefdead")
        self.assertEqual(rc, 1)
        self.assertIn("deadbeefdead", err)
        self.assertFalse(self.sb.ctx.ids_path.exists())

    def test_adopt_rejects_unknown_key_and_duplicate_id(self):
        self.assertEqual(self.sb.cli("cron", "adopt", f"nokey={self.sb.ids['intake']}")[0], 1)
        rc, _, err = self.sb.cli("cron", "adopt", f"intake={self.sb.ids['intake']}", f"gate={self.sb.ids['intake']}")
        self.assertEqual(rc, 1)
        self.assertIn("同じジョブ ID", err)

    def test_adopt_saves_pairs_and_merges(self):
        self.assertEqual(self.sb.cli("cron", "adopt", f"intake={self.sb.ids['intake']}")[0], 0)
        self.assertEqual(self.sb.cli("cron", "adopt", f"gate={self.sb.ids['gate']}")[0], 0)
        self.assertEqual(json.loads(self.sb.ctx.ids_path.read_text()), {"gate": self.sb.ids["gate"], "intake": self.sb.ids["intake"]})

    # ---- cron sync
    def test_sync_dry_run_with_matching_production_changes_nothing(self):
        self.sb.adopt_all()
        rc, out, _ = self.sb.cli("cron", "sync", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("一致 10 / 変更 0 / 作成 0", out)
        self.assertEqual([c for c in self.sb.calls() if c[1:3] == ["cron", "edit"] or c[1:3] == ["cron", "create"]], [])

    def test_sync_refuses_to_duplicate_a_job_that_exists_but_is_not_adopted(self):
        rc, _, err = self.sb.cli("cron", "sync", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("cron adopt intake=", err)
        self.assertEqual(self.sb.calls(), [])

    def test_sync_edits_only_the_differences(self):
        self.sb.adopt_all()
        by = {k: dict(w) for k, w in self.sb.by_key.items()}
        by["planner"].update(prompt="old prompt", reasoning="low", skills=["autonomous-task-execution"], model="other-model")
        gate_minutes = factory.sfconfig.schedule_minutes(self.sb.desired["gate"]["schedule"])  # 周期は hermes/cron.toml から読む(手で書かない)
        by["gate"].update(schedule=f"every {gate_minutes + 7}m", deliver="discord:1")  # cron.toml と違う周期
        self.sb.write_jobs(by)
        rc, out, _ = self.sb.cli("cron", "sync", "--dry-run")
        self.assertEqual((rc, self.sb.calls()), (0, []))
        for f in ("prompt", "reasoning", "skills", "model", "schedule", "deliver"):
            self.assertIn(f"{f}:", out)
        rc, out, _ = self.sb.cli("cron", "sync")
        self.assertEqual(rc, 0, out)
        edits = {c[3]: c[4:] for c in self.sb.calls() if c[1:3] == ["cron", "edit"]}
        self.assertEqual(set(edits), {self.sb.ids["planner"], self.sb.ids["gate"]})
        p = edits[self.sb.ids["planner"]]
        self.assertEqual(p[p.index("--reasoning-effort") + 1], "high")
        self.assertEqual(p[p.index("--model") + 1], self.sb.ctx.cfg["models"]["worker"])
        self.assertEqual([p[i + 1] for i, a in enumerate(p) if a == "--skill"], ["autonomous-task-execution", "product-development-process"])
        self.assertEqual(p[p.index("--prompt") + 1], self.sb.desired["planner"]["prompt"])
        g = edits[self.sb.ids["gate"]]
        self.assertEqual((g[g.index("--schedule") + 1], g[g.index("--deliver") + 1]), (self.sb.desired["gate"]["schedule"], self.sb.ctx.cfg["chat"]["deliver"]))
        self.assertNotIn("--prompt", g)

    def test_sync_creates_missing_job_paused_and_adopts_it(self):
        by = {k: dict(w) for k, w in self.sb.by_key.items() if k != "drift"}
        self.sb.write_jobs(by)
        self.sb.adopt_all()
        rc, out, _ = self.sb.cli("cron", "sync", "--dry-run")
        self.assertIn("+ drift", out)
        self.assertEqual(self.sb.calls(), [])
        rc, out, _ = self.sb.cli("cron", "sync")
        self.assertEqual(rc, 0, out)
        creates = [c for c in self.sb.calls() if c[1:3] == ["cron", "create"]]
        self.assertEqual(len(creates), 1)
        c = creates[0]
        self.assertIn("--paused", c)
        self.assertEqual(c[3], "37 * * * *")
        self.assertIn("--no-agent", c)
        self.assertEqual(c[c.index("--script") + 1], "sf-repo-drift.sh")
        new_id = next(j["id"] for j in self.sb.jobs() if j["name"] == self.sb.desired["drift"]["name"])
        self.assertEqual(json.loads(self.sb.ctx.ids_path.read_text())["drift"], new_id)

    def test_sync_stops_when_adopted_id_vanished(self):
        factory.write_ids(self.sb.ctx, {**self.sb.ids, "gate": "ffffffffffff"})
        rc, _, err = self.sb.cli("cron", "sync", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("ffffffffffff", err)

    # ---- pause / resume
    def test_pause_and_resume_touch_only_adopted_jobs(self):
        self.sb.write_jobs(self.sb.by_key)
        jobs = self.sb.jobs()
        jobs.append(dict(id="999999999999", name="他のジョブ", state="scheduled", enabled=True))
        (self.sb.home / "profiles" / "dev" / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
        factory.write_ids(self.sb.ctx, {k: v for k, v in self.sb.ids.items() if k != "drift"})
        rc, out, _ = self.sb.cli("cron", "pause", "--dry-run")
        self.assertEqual((rc, self.sb.calls()), (0, []))
        rc, out, _ = self.sb.cli("cron", "pause")
        self.assertEqual(rc, 0)
        paused = {c[3] for c in self.sb.calls() if c[1:3] == ["cron", "pause"]}
        self.assertEqual(paused, {v for k, v in self.sb.ids.items() if k != "drift"})
        self.assertNotIn("999999999999", paused)

    def test_pause_without_cron_ids_stops(self):
        rc, _, err = self.sb.cli("cron", "pause")
        self.assertEqual(rc, 1)
        self.assertIn("cron adopt", err)

    # ---- wrappers
    def test_wrappers_embed_paths_and_are_valid_shell(self):
        out = self.sb.tmp / "wr"
        rc, text, _ = self.sb.cli("wrappers", "--out", str(out))
        self.assertEqual(rc, 0)
        files = sorted(out.glob("*.sh"))
        self.assertEqual(len(files), 9)
        gate = (out / "sf-gate.sh").read_text(encoding="utf-8")
        self.assertIn(f'LIVE="{self.sb.live}"', gate)
        self.assertIn(f'REPO="{self.sb.ctx.repo}"', gate)
        self.assertIn(".local/share/mise/shims:", gate)
        self.assertIn('exec python3 "$TARGET"', gate)
        self.assertIn('TARGET="$REPO/tools/repo_drift_check.sh"', (out / "sf-repo-drift.sh").read_text(encoding="utf-8"))
        for f in files:
            self.assertTrue(os.access(f, os.X_OK), f)
            self.assertEqual(subprocess.run(["sh", "-n", str(f)]).returncode, 0, f)
            self.assertNotIn("config.toml", f.read_text(encoding="utf-8"))  # 実行時に設定を読まない
        # 2回目は一致
        self.assertIn("一致 9 / 書く 0", self.sb.cli("wrappers", "--out", str(out))[1])

    # ---- profiles / board
    def test_profiles_creates_missing_then_is_idempotent(self):
        rc, out, _ = self.sb.cli("profiles", "--dry-run")
        self.assertEqual((rc, self.sb.calls()), (0, []))
        self.assertIn("profile create executor --clone-from dev --no-alias --description", out)
        rc, out, _ = self.sb.cli("profiles")
        self.assertEqual(rc, 0, out)
        creates = [c[3] for c in self.sb.calls() if c[1:3] == ["profile", "create"]]
        self.assertEqual(creates, ["executor", "reviewer"])
        cfg = json.loads(self.sb.state.read_text())["config"]
        self.assertEqual(cfg["executor"]["model.default"], "claude-sonnet-5-5[1m]")
        self.assertEqual(cfg["reviewer"]["model.default"], "claude-opus-5-5[1m]")
        self.assertEqual(cfg["executor"]["agent.max_turns"], "150")
        self.assertEqual(cfg["executor"]["skills.external_dirs"], [str(self.sb.skills)])
        self.assertIn("DENY", cfg["executor"]["approvals.smart_policy"])
        soul = (self.sb.home / "profiles" / "executor" / "SOUL.md").read_text(encoding="utf-8")
        self.assertNotIn("${", soul)
        self.assertIn(str(self.sb.ctx.repo), soul)
        n = len(self.sb.calls())
        rc, out, _ = self.sb.cli("profiles")  # 2回目: 作らない
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in self.sb.calls()[n:] if c[1:3] == ["profile", "create"] or c[1:3] == ["config", "set"]], [])
        self.assertIn("あります(設定・SOUL とも一致", out)

    def test_profiles_existing_drift_is_reported_and_fixed_only_with_update(self):
        self.sb.cli("profiles")
        state = json.loads(self.sb.state.read_text())
        state["config"]["reviewer"]["agent.max_turns"] = "50"
        self.sb.state.write_text(json.dumps(state))
        n = len(self.sb.calls())
        rc, out, _ = self.sb.cli("profiles")
        self.assertIn("agent.max_turns", out)
        self.assertEqual([c for c in self.sb.calls()[n:] if c[1:3] == ["config", "set"]], [])
        self.sb.cli("profiles", "--update")
        self.assertEqual(json.loads(self.sb.state.read_text())["config"]["reviewer"]["agent.max_turns"], "150")

    def test_board_creates_once_and_sets_review_dispatch(self):
        rc, out, _ = self.sb.cli("board", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in self.sb.calls() if c[1:3] == ["kanban", "boards"] and c[3] == "create"], [])
        self.sb.cli("board")
        self.sb.cli("board")
        creates = [c for c in self.sb.calls() if c[1:4] == ["kanban", "boards", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][4], "dev")
        self.assertEqual(json.loads(self.sb.state.read_text())["config"]["dev"]["kanban.review_dispatch"], "true")

    # ---- Notion
    def test_committed_schema_is_consistent(self):
        dbs = factory.load_schema()
        self.assertEqual(factory.check_dual_pairs(dbs), [])
        self.assertEqual(set(dbs), {"epic", "task", "review"})
        for db in dbs.values():
            self.assertNotIn("優先度", db["properties"])  # 優先度はどの DB にも無い
        self.assertEqual(list(dbs["review"]["properties"]), ["名前", "キー", "ステータス", "対象エピック", "反映済み", "作成日時"])
        for key, groups in (("task", {"未着手": "To-do", "完了": "Complete", "キャンセル": "Complete"}),
                            ("review", {"レビュー待ち": "To-do", "承認": "Complete"})):
            got = {o["name"]: o["group"] for o in dbs[key]["properties"]["ステータス"]["options"]}
            for name, group in groups.items():
                self.assertEqual(got[name], group)

    def test_schema_mismatch_detection(self):
        dbs = factory.load_schema()
        broken = json.loads(json.dumps(dbs["task"]))
        del broken["properties"]["作成日時"]
        broken["properties"]["ステータス"]["options"][0]["group"] = "Complete"
        broken["properties"]["エピック"]["relation"]["target"] = "review"
        m = factory.schema_mismatches(dbs["task"], broken)
        self.assertTrue(any("作成日時" in x for x in m))
        self.assertTrue(any("ステータス" in x for x in m))
        self.assertTrue(any("エピック" in x for x in m))
        self.assertEqual(factory.schema_mismatches(dbs["task"], dbs["task"]), [])

    def test_create_body_order_and_relations(self):
        dbs = factory.load_schema()
        epic = factory.build_create_body("epic", dbs["epic"], "P", {})
        self.assertNotIn("タスク", epic["initial_data_source"]["properties"])  # 後の task が dual で作る
        task = factory.build_create_body("task", dbs["task"], "P", {"epic": "EPIC-DS"})
        rel = task["initial_data_source"]["properties"]["エピック"]["relation"]
        self.assertEqual((rel["data_source_id"], rel["type"], rel["dual_property"]), ("EPIC-DS", "dual_property", {"synced_property_name": "タスク"}))
        review = factory.build_create_body("review", dbs["review"], "P", {"epic": "EPIC-DS", "task": "TASK-DS"})
        rel = review["initial_data_source"]["properties"]["対象エピック"]["relation"]
        self.assertEqual((rel["data_source_id"], rel["type"], rel["single_property"]), ("EPIC-DS", "single_property", {}))
        st = task["initial_data_source"]["properties"]["ステータス"]["status"]["options"]
        self.assertEqual({o["name"]: o["group"] for o in st}["レビュー中"], "In progress")
        with self.assertRaises(factory.NtnError):
            factory.build_create_body("task", dbs["task"], "P", {})

    def test_set_notion_ids_keeps_everything_else(self):
        text = (REPO / "envs" / "michiomochi-wsl.toml").read_text(encoding="utf-8")
        new = factory.set_notion_ids(text, {"task": "T-NEW", "epic": "E-NEW"})
        self.assertIn('task_ds = "T-NEW"', new)
        self.assertIn('epic_ds = "E-NEW"', new)
        self.assertIn('review_ds = "31cd1b97-b525-0c17-3e66-508b9f72b88a"', new)
        self.assertEqual(len(new.splitlines()), len(text.splitlines()))
        self.assertEqual([l for l in new.splitlines() if "_ds" not in l], [l for l in text.splitlines() if "_ds" not in l])

    def _unset_ids(self):
        text = "\n".join(f'{l.split("=")[0]}= "UNSET"' if l.split("=")[0].strip() in ("epic_ds", "task_ds", "review_ds") else l
                         for l in self.sb.config.read_text(encoding="utf-8").split("\n"))
        self.sb.config.write_text(text, encoding="utf-8")

    def _git_init(self):
        """一時ディレクトリを git リポジトリにする(bootstrap のコミットが別のリポジトリに入らないように)。"""
        git = lambda *a: subprocess.run(["git", "-C", str(self.sb.tmp), *a], capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", "env.toml")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        return git

    def test_bootstrap_dry_run_posts_nothing(self):
        self._unset_ids()
        rc, out, _ = self.sb.cli("notion-bootstrap", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("POST v1/databases", out)
        self.assertEqual(self.sb.calls(), [])
        self.assertIn('"UNSET"', self.sb.config.read_text(encoding="utf-8"))

    def test_bootstrap_creates_in_order_writes_ids_back_and_commits(self):
        self._unset_ids()
        git = self._git_init()
        env = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        rc, out, err = self.sb.cli("notion-bootstrap", **env)
        self.assertEqual(rc, 0, out + err)
        posts = [c for c in self.sb.calls() if c[:2] == ["ntn", "api"] and "POST" in c]
        self.assertEqual(len(posts), 3)
        written = factory.load_cfg(self.sb.config)[1]["notion"]
        ids = {k: written[factory.DB_ENV_KEY[k]] for k in factory.DB_ORDER}
        self.assertTrue(all(factory.UUID_RE.match(v) for v in ids.values()), ids)
        self.assertEqual(len(set(ids.values())), 3)
        self.assertIn("作成後の照合: 3つの DB とも schema.json と一致", out)
        self.assertIn("chore(env)", git("log", "-1", "--format=%s"))
        self.assertNotIn("env.toml", git("status", "--porcelain"))  # env.toml はコミット済み
        # 2回目は既にあるので止まる(二重に作らない)
        rc2, _, err2 = self.sb.cli("notion-bootstrap", **env)
        self.assertEqual(rc2, 1)
        self.assertIn("既に存在", err2)
        self.assertEqual(len([c for c in self.sb.calls() if "POST" in c]), 3)

    def test_export_schema_roundtrips_through_bootstrap_output(self):
        # bootstrap が作った(偽の)DB を export しても、同じ schema になる(色は偽の ntn が持たないので除く)
        self._unset_ids()
        self._git_init()
        self.assertEqual(self.sb.cli("notion-bootstrap", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")[0], 0)
        out = self.sb.tmp / "schema.json"
        rc, text, err = self.sb.cli("notion-export-schema", "--out", str(out))
        self.assertEqual(rc, 0, text + err)
        got, want = json.loads(out.read_text())["databases"], factory.load_schema()
        for k in factory.DB_ORDER:
            self.assertEqual(factory.schema_mismatches(want[k], got[k]), [], k)

    # ---- doctor
    def test_doctor_lists_everything_missing_and_exits_nonzero(self):
        rc, out, _ = self.sb.cli("doctor", SF_HERMES="/nonexistent/hermes", SF_NTN="/nonexistent/ntn")
        self.assertEqual(rc, 1)
        self.assertIn("足りない:", out)
        self.assertIn("Hermes のバージョン", out)
        self.assertIn("ntn whoami", out)

    def test_doctor_passes_pieces_that_are_present(self):
        self.sb.cli("profiles")
        self.sb.cli("board")
        self.sb.cli("wrappers")
        self.sb.adopt_all()
        self.sb.cli("cron", "sync")
        rc, out, _ = self.sb.cli("doctor")
        self.assertIn("OK  Hermes のバージョン", out)
        self.assertIn("OK  ntn whoami", out)
        self.assertIn("OK  プロファイル executor の設定", out)
        self.assertIn("OK  kanban ボード dev", out)
        self.assertIn("OK  cron_ids.json に全ジョブ登録済み", out)
        self.assertIn("OK  cron の薄い .sh が置換後と一致", out)
        self.assertIn("OK  ゲートウェイ(dev)が稼働中", out)
        # 偽の ntn は本番の DB を持たないので Notion の照合だけが足りない。cron_mode は偽の hermes が持たない
        self.assertEqual(rc, 1)

    # ---- [chat]: 節名と配信先の検査 / doctor の Slack 検査
    def _write_cfg(self, **repl):
        text = self.sb.config.read_text(encoding="utf-8")
        for a, b in repl.items():
            self.assertIn(a, text)
            text = text.replace(a, b)
        self.sb.config.write_text(text, encoding="utf-8")

    def _load_err(self):
        with self.assertRaises(factory.sfconfig.ConfigError) as cm:
            factory.sfconfig.load(self.sb.config)
        return str(cm.exception)

    def test_old_discord_section_is_refused_with_rename_hint(self):
        self._write_cfg(**{"[chat]\n": "[discord]\n"})
        self.assertIn("[chat] に改名してください", self._load_err())
        rc, _, err = self.sb.cli("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("[chat] に改名してください", err)

    def test_chat_targets_accept_comma_separated_discord_and_slack(self):
        for good in ("discord:1473768887694246154", "discord:111:222", "slack:C0123ABCD", "discord:1,slack:C0123ABCD", "slack:C0123ABCD:1700000000.000100"):
            self._write_cfg(**{'deliver = "discord:1473768887694246154"': f'deliver = "{good}"'})
            self.assertEqual(factory.sfconfig.load(self.sb.config)["chat"]["deliver"], good)
            self._write_cfg(**{f'deliver = "{good}"': 'deliver = "discord:1473768887694246154"'})

    def test_chat_targets_reject_malformed_elements(self):
        for bad in ("telegram:1", "discord:", "discord:1,", "discord:1, slack:C1", "slack C1", "discord", "discord:a b"):
            self._write_cfg(**{'failure_deliver = "discord:1421420969335515535"': f'failure_deliver = "{bad}"'})
            self.assertIn("[chat].failure_deliver", self._load_err(), bad)
            self._write_cfg(**{f'failure_deliver = "{bad}"': 'failure_deliver = "discord:1421420969335515535"'})

    def test_mention_user_and_fallback_parent_are_optional_and_checked(self):
        sf = factory.sfconfig
        lines = self.sb.config.read_text(encoding="utf-8").splitlines(keepends=True)
        self.sb.config.write_text("".join(l for l in lines if not l.startswith(("mention_user", "fallback_parent"))), encoding="utf-8")
        cfg = sf.load(self.sb.config)  # a config without them still loads
        self.assertNotIn("mention_user", cfg["chat"])
        self.assertEqual(sf.template_vars(cfg)["mention_user"], "")
        self.sb.config.write_text("".join(lines), encoding="utf-8")
        self.assertEqual(sf.template_vars(sf.load(self.sb.config))["mention_user"], "199335018222053302")
        self._write_cfg(**{'mention_user = "199335018222053302"': 'mention_user = "michan"'})
        self.assertIn("[chat].mention_user", self._load_err())
        self._write_cfg(**{'mention_user = "michan"': 'mention_user = "199335018222053302"', 'fallback_parent = "discord:1421420969335515535"': 'fallback_parent = "telegram:1"'})
        self.assertIn("[chat].fallback_parent", self._load_err())

    def test_doctor_checks_slack_bot_token_only_when_slack_is_a_target(self):
        env = self.sb.home / "profiles" / "dev" / ".env"
        _, out, _ = self.sb.cli("doctor")
        self.assertNotIn("SLACK_BOT_TOKEN", out)  # discord だけなら検査しない
        self._write_cfg(**{'deliver = "discord:1473768887694246154"': 'deliver = "discord:1473768887694246154,slack:C0123ABCD"'})
        _, out, _ = self.sb.cli("doctor")
        self.assertIn("NG  dev: .env に SLACK_BOT_TOKEN([chat].deliver に slack:)", out)
        env.write_text("OTHER=1\nSLACK_BOT_TOKEN=\n", encoding="utf-8")
        _, out, _ = self.sb.cli("doctor")
        self.assertIn("NG  dev: .env に SLACK_BOT_TOKEN", out)
        self.assertIn("空", out)
        env.write_text("OTHER=1\nexport SLACK_BOT_TOKEN=xoxb-secret-value-123\n", encoding="utf-8")
        _, out, _ = self.sb.cli("doctor")
        self.assertIn("OK  dev: .env に SLACK_BOT_TOKEN([chat].deliver に slack:)", out)
        self.assertNotIn("xoxb-secret-value-123", out)  # 値は出さない

    def test_parse_hermes_version(self):
        self.assertEqual(factory.parse_hermes_version("Hermes Agent v0.22.6 (2026.9.24) · stable"), (0, 22, 6))
        self.assertIsNone(factory.parse_hermes_version("nothing"))


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=0, argv=[sys.argv[0]])
    sys.exit(0 if r.result.wasSuccessful() else 1)
