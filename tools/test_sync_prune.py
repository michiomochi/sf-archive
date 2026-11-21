"""sync_live が「リポジトリから消えたスキル」を本番に残さないこと。

同じ失敗（repo からスキルを消したのに、本番には古いコピーが残り続ける）を
仕組みで止める。消すのではなく、退避用のディレクトリへ移す。

悪い例（台帳にあるが repo に無いスキル＝古い）を拒み、
良い例（台帳と repo が同じ）を通すことまで確かめる。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def load_module():
    spec = importlib.util.spec_from_file_location("sync_live_under_test", ROOT / "tools" / "sync_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(tmp: Path, repo_skills, deployed_skills):
    m = load_module()
    m.SKILLS_LIVE = tmp / "skills"
    m.STATE = tmp / "state" / "deployed.json"
    m.PRUNE_DIR = tmp / "pruned"
    m.skill_names = lambda: set(repo_skills)
    m.STATE.parent.mkdir(parents=True, exist_ok=True)
    m.STATE.write_text(json.dumps(sorted(deployed_skills)), encoding="utf-8")
    m.JOBS_JSON = tmp / "jobs.json"
    m.JOBS_JSON.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    for name in deployed_skills:
        d = m.SKILLS_LIVE / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return m


def main():
    import tempfile

    checks = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 悪い例: 台帳に gh-lessons があるが、repo からは消えている
        m = run(tmp, repo_skills={"github", "notion"}, deployed_skills={"github", "notion", "gh-lessons"})
        drift = m.stale_skills()
        assert drift == [("stale_skill", "gh-lessons", "リポジトリから消えた(本番に残っています)")], drift
        checks += 1
        m.apply(drift)
        assert not (m.SKILLS_LIVE / "gh-lessons").exists(), "古いスキルが本番に残っている"
        assert (m.PRUNE_DIR / "gh-lessons" / "SKILL.md").exists(), "退避されていない（消してしまった可能性）"
        checks += 1
        # 良い例: 台帳と repo が同じなら何もしない
        m2 = run(tmp / "ok", repo_skills={"github"}, deployed_skills={"github"})
        assert m2.stale_skills() == [], "一致しているのに差分と言った"
        checks += 1
        # 台帳が無い初回は、勝手に何も消さない
        m3 = load_module()
        m3.STATE = tmp / "none.json"
        m3.SKILLS_LIVE = tmp / "none"
        m3.skill_names = lambda: set()
        assert m3.stale_skills() == [], "台帳が無いのに差分と言った"
        checks += 1

        # repo から消えた「スキル内の1ファイル」も退避する（live に残すと check が永久に落ちる）
        live_dir = tmp / "live" / "alpha"
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / "extra.md").write_text("live only\n", encoding="utf-8")
        m4 = load_module()
        m4.SKILLS_LIVE = tmp / "live"
        m4.PRUNE_DIR = tmp / "pruned2"
        m4.JOBS_JSON = tmp / "jobs.json"
        m4.NAMED = {}
        m4.repo_skill_texts = lambda name: {}
        m4.apply([("skill", "alpha/extra.md", "リポジトリに無い(本番だけにある)")])
        assert not (live_dir / "extra.md").exists(), "repo から消えたファイルが live に残っている"
        assert (tmp / "pruned2" / "alpha" / "extra.md").exists(), "退避されていない（消した可能性）"
        checks += 1

    print(f"sync_prune: {checks} checks, 0 failed")


if __name__ == "__main__":
    main()
