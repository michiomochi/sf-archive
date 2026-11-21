#!/usr/bin/env python3
"""リポジトリと本番(~/.hermes)の差を検出し、反映する。

対象(どちらも `${...}` を環境の設定で置換した結果を本番へ反映する。置換は tools/factory.py):
  - skills/<name>/ -> <paths.skills>/<name>/
  - prompts/*.txt  -> <hermes.cron_profile> プロファイルの cron ジョブの prompt(ジョブ名の前方一致で対応づけ)

使い方:
  sync_live.py --check [--config FILE]   差があれば内容を表示して終了コード 1(何も変更しない。`factory.py check` が全体の確認)
  sync_live.py --apply [--config FILE]   リポジトリを正として本番へ反映(スキルは上書き、cron は hermes -p <cron_profile> cron edit)

設定は --config > $SF_CONFIG > envs/ の唯一のファイル。本番側にだけある内容はリポジトリに取り込んでから反映する。--apply は本番側の編集を上書きする。
スクリプトとカードのテンプレート(<paths.live>)は safe_deploy.sh が扱うので対象外。
"""
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import factory

REPO = factory.REPO
PROMPT_JOBS = {}  # 指示文の名前 -> ジョブ名。環境で有効なプラグインのぶんだけ(use_env が埋める)
# 本番の場所は設定(paths.skills、hermes.home / cron_profile)から決める。HERMES_HOME の既定には依存しない(既定だと default の jobs.json を見てしまう)。
# 環境変数での上書きはテスト用(一時ディレクトリで --check / --apply を試すため)。main() が設定を読んで埋める。
SKILLS_LIVE = JOBS_JSON = HERMES_CMD = None
RENDERED = {}  # repo-relative path -> 置換済みの本文


def skill_files(root):
    return {
        str(p.relative_to(root)): p
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"
    }


def deployed():
    """{スキル名: {スキル内のパス: 置換済みの本文}}(プラグインのスキルと SKILL.addendum.md の合成を含む)。"""
    return factory.sfplugins.deployed_skills(RENDERED)


def skill_names():
    return sorted(deployed())


def repo_skill_texts(name):
    """{path inside the skill: 置換済みの本文} (every file of the skill is a rendered template)."""
    return deployed().get(name, {})


def skill_drift():
    out = []
    for name in skill_names():
        rf, live = repo_skill_texts(name), SKILLS_LIVE / name
        lf = skill_files(live) if live.exists() else {}
        for rel in sorted(set(rf) | set(lf)):
            if rel not in lf:
                out.append(("skill", f"{name}/{rel}", "本番に無い"))
            elif rel not in rf:
                out.append(("skill", f"{name}/{rel}", "リポジトリに無い(本番だけにある)"))
            elif rf[rel].encode("utf-8") != lf[rel].read_bytes():
                out.append(("skill", f"{name}/{rel}", "内容が違う"))
    return out


STATE = Path(os.environ.get("SYNC_LIVE_STATE", os.path.expanduser("~/.hermes/state/sf_deployed_skills.json")))
PRUNE_DIR = Path(os.environ.get("SYNC_LIVE_PRUNE_DIR", os.path.expanduser("~/.hermes/state/pruned_skills")))


def stale_skills():
    """前回配ったスキルのうち、リポジトリから消えたもの。本番にだけ残っている。"""
    if not STATE.exists():
        return []
    prev = set(json.loads(STATE.read_text(encoding="utf-8")))
    return [("stale_skill", n, "リポジトリから消えた(本番に残っています)")
            for n in sorted(prev - set(skill_names()))]


def save_manifest():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(sorted(skill_names()), ensure_ascii=False, indent=0), encoding="utf-8")


def use_env(raw):
    """環境の設定で、置換済みの本文と cron の対応表を作り直す。有効でないプラグインのジョブは対応表に入らない。"""
    RENDERED.clear()
    RENDERED.update(factory.render_all(raw)[0])
    PROMPT_JOBS.clear()
    PROMPT_JOBS.update(factory.prompt_jobs(raw))


def want_prompt(fname):
    """本体の prompts/ と、有効なプラグインの plugins/<名前>/prompts/ のどちらの指示文も引く。"""
    return factory.sfplugins.deployed_prompts(RENDERED)[fname].strip()


def load_jobs():
    data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    return data["jobs"] if isinstance(data, dict) else data


def prompt_drift():
    out, jobs = [], load_jobs()
    for fname, prefix in PROMPT_JOBS.items():
        want = want_prompt(fname)
        job = next((j for j in jobs if j.get("name", "").startswith(prefix)), None)
        if job is None:
            out.append(("cron", fname, f"ジョブ「{prefix}」が見つからない"))
        elif (job.get("prompt") or "").strip() != want:
            out.append(("cron", fname, f"指示文が違う(job {job['id']})"))
    return out


def show_prompt_diff(fname, job):
    want = want_prompt(fname).splitlines()
    have = (job.get("prompt") or "").strip().splitlines()
    for line in list(difflib.unified_diff(have, want, "本番cron", "リポジトリ", lineterm="", n=0))[:12]:
        print("    " + line[:160])


def apply(drift):
    jobs = load_jobs()
    for kind, key, _ in drift:
        if kind == "skill":
            name, rel = key.split("/", 1)
            text, dst = repo_skill_texts(name).get(rel), SKILLS_LIVE / name / rel
            if text is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text, encoding="utf-8")
                print(f"反映: skill {key}")
            else:
                # repo から消えたファイル（スキル内の1ファイル）。live に残すと check が永久に落ちるので退避する
                if dst.exists():
                    PRUNE_DIR.mkdir(parents=True, exist_ok=True)
                    target = PRUNE_DIR / key
                    if target.exists():
                        target = PRUNE_DIR / f"{key}.{int(os.path.getmtime(dst))}"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(target))
                    print(f"退避: file {key} -> {target}")
        elif kind == "stale_skill":
            src = SKILLS_LIVE / key
            if src.exists():
                PRUNE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(PRUNE_DIR / key))
                print(f"退避: skill {key} -> {PRUNE_DIR / key}")
        else:
            job = next((j for j in jobs if j.get("name", "").startswith(PROMPT_JOBS[key])), None)
            if job is None:
                continue
            prompt = want_prompt(key)
            subprocess.run([*HERMES_CMD, "cron", "edit", job["id"], "--prompt", prompt], check=True)
            print(f"反映: cron {key} (job {job['id']})")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--config", help="環境の設定ファイル(既定: $SF_CONFIG か envs/ の唯一のファイル)")
    args = ap.parse_args()
    global SKILLS_LIVE, JOBS_JSON, HERMES_CMD
    raw, cfg = factory.load_cfg(factory.pick_config(args))
    try:
        use_env(raw)
    except factory.RenderError as e:
        print(f"置換できません(反映しません):\n{e}", file=sys.stderr)
        return 1
    loc = factory.live_locations(cfg)
    SKILLS_LIVE = Path(os.environ.get("SYNC_LIVE_SKILLS_DIR", loc["skills"]))
    JOBS_JSON = Path(os.environ.get("SYNC_LIVE_JOBS_JSON", loc["jobs"]))
    HERMES_CMD = os.environ.get("SYNC_LIVE_HERMES_CMD", f"hermes -p {cfg['hermes']['cron_profile']}").split()
    drift = skill_drift() + prompt_drift() + stale_skills()
    for kind, key, why in drift:
        print(f"差分 [{kind}] {key}: {why}")
    if not drift:
        print("差分なし: リポジトリと本番(スキル、cron の指示文)が一致")
        return 0
    if args.check:
        for kind, key, _ in drift:
            if kind == "stale_skill":
                print(f"  直し方: `--apply` で {PRUNE_DIR}/{key} へ退避します（消しません）")
        jobs = load_jobs()
        for kind, key, _ in drift:
            if kind == "cron":
                job = next((j for j in jobs if j.get("name", "").startswith(PROMPT_JOBS[key])), None)
                if job:
                    show_prompt_diff(key, job)
        return 1
    apply(drift)
    left = skill_drift() + prompt_drift()
    left = [d for d in left if not d[2].startswith("リポジトリに無い")]
    if left:
        print("反映後も差分が残っています:", left)
        return 1
    save_manifest()
    print("反映完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
