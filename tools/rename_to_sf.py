#!/usr/bin/env python3
"""リポジトリと本番の置き場所を software-factory から sf に移す、1回だけの切り替え(標準ライブラリのみ)。

使い方(リポジトリの外の cwd から、リポジトリの外に写したこのファイルを実行する。移設中に自分のファイルが動くため):
  git -C <旧リポジトリ> show origin/main:tools/rename_to_sf.py > "$TMPDIR/rename_to_sf.py"
  python3 "$TMPDIR/rename_to_sf.py"              # dry-run(既定): ガードの判定と実行予定だけを出す。何も変えない
  python3 "$TMPDIR/rename_to_sf.py" --apply      # 実行
  python3 "$TMPDIR/rename_to_sf.py" --rollback   # 戻す(状態ファイルに残った手順から逆順に)

前提: 改名の変更(envs の paths.live。paths.repo / git.repo_url は先に main に入っている)が PR で origin/main に入っていて、
ローカルの main はまだその前(pull していない)。GitHub のリポジトリ名は変更済み(旧 URL はリダイレクト)。
マージから --apply までの間は、改名後の main で make(stage / deploy-check / deploy / sync / wrappers)を流さない。流すと新しい
置き場所に書く(staging/ だけならガード 3 は待たずに --apply が消す。それ以外はガード 3 で待つ)。

ガード(1つでも満たさなければ何も変えずに終了コード 3 = 待つ):
  1. kanban ボードの running / ready / review が0枚
  2. 旧・新のリポジトリ(.worktrees/ を含む)と本番の下を cwd にしたプロセスが無い
  3. 新しいリポジトリの置き場所がまだ無い。新しい本番の置き場所が無いか staging/ だけ。旧い置き場所が両方ある
  4. 旧リポジトリが main でクリーン、origin/main に無いコミットが無い、ローカルの main の paths.live が旧い本番(pull していない)
  5. origin/main の envs の paths.repo と paths.live が新しい置き場所(PR がマージ済み)
  6. 旧リポジトリ(マージ前の main)で `make check` に内容の差が無い(本番だけの編集を deploy で消さないため)。
     main に先に入った新しいリポジトリ名そのものは差に数えない(paths.repo / git.repo_url を旧い名前にした設定の写しで比べる)。
     本番にまだ無いスキル・ジョブ(足すだけで消える編集ではない)は NOTE にして待たない
cron を止めたあとにガード 1・2 をもう一度判定し、満たさなければ cron を再開して終了コード 3。

手順: 1 cron-pause(旧設定) → 2 再判定 → 3 staging/ だけの残りを消す、mv リポジトリ、git worktree repair、mv 本番 → 4 main を ff →
5 cron の前半と配布: (a) cron-sync --dry-run が「同じ名前のジョブが既にある」と止めたキーだけを cron-adopt で取り込む
(本番にだけあって cron_ids.json に無いジョブ。名前を推測して取り込まない) (b) wrappers(新しいジョブの薄い .sh が無いと
cron-sync が作れない) (c) cron-sync(main にあって本番に無いジョブを止めた状態で作る。`make deploy` の sync_live はジョブが
足りないと失敗にするため) (d) deploy (e) render + SOUL の写し → 6 check(差分なし) / smoke / cron-sync --dry-run(変更 0 / 作成 0) →
7 cron-resume(手順5で作ったジョブと取り込んだジョブも動き出す。取り込む前に止めてあったジョブは NOTE を見て止め直す)。
戻すときもマージ前のコミットで手順5を同じ順に流し、そのあと cron_ids.json を「切り替え前 + 取り込んだキー」に戻す
(手順5やロールバックの cron-sync が作ったジョブは cron_ids.json から外し、止めたまま残す)。
手順 3〜6 の失敗は自動で戻す(cron は戻し終わってから再開)。手順 1 の失敗は止まった分を再開して終わる(何も動かさない)。
cron の再開(手順 2・7・戻し)に失敗したときは戻さず、やり直しのコマンドを ERROR 行に出して終了コード 1。
進んだ手順は状態ファイルに書く。終了コード: 0 成功、1 失敗(戻した、または cron の再開をやり直す)、2 使い方の誤り、3 ガードで待つ。
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

HOME = Path.home()
DEFAULTS = dict(
    old_repo=HOME / "ghq/github.com/example-org/software-factory",
    new_repo=HOME / "ghq/github.com/example-org/sf",
    old_live=HOME / ".hermes/scripts/software-factory",
    new_live=HOME / ".hermes/scripts/sf",
    board_db=HOME / ".hermes/kanban/boards/dev/kanban.db",
    state_file=HOME / ".hermes/state/rename_to_sf.json",
)
ENV_FILE = "envs/michiomochi-wsl.toml"
BUSY = ("running", "ready", "review")
SOUL_PROFILES = ("executor", "reviewer")
# 子の make に渡さない環境変数(テスト用の上書きが残っていると dry run が飛ばされたり scratch に書いたりする)
STRIP_ENV_PREFIX = ("SAFE_DEPLOY_", "SYNC_LIVE_", "SF_")
WAIT, FAIL, USAGE = 3, 1, 2
# cron-sync が同じ名前のジョブで止めたときの案内(tools/factory.py の plan_sync)。tools/test_install.py が同じ文字列を固定している
ADOPT_HINT = re.compile(r"`factory\.py cron adopt ([^=`\s]+)=([^`\s]+)`")


class Abort(Exception):
    pass


def log(tag, msg):
    print(f"{tag} {msg}", flush=True)


KEEP_ENV = set()  # --keep-env: 通す環境変数(リハーサルで偽の hermes を SF_HERMES / SYNC_LIVE_HERMES_CMD で渡すときだけ)


def child_env():
    return {k: v for k, v in os.environ.items() if k in KEEP_ENV or not k.startswith(STRIP_ENV_PREFIX)}


def run(cmd, cwd=None, check=True, quiet=False):
    """Run a command (list); print it; return CompletedProcess. check=True raises Abort on rc != 0."""
    log("RUN", " ".join(shlex.quote(str(c)) for c in cmd) + (f"   (cwd {cwd})" if cwd else ""))
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True, env=child_env())
    out = (r.stdout + r.stderr).strip()
    if out and not quiet:
        for line in out.splitlines()[-25:]:
            print("    " + line)
    if check and r.returncode != 0:
        raise Abort(f"rc={r.returncode}: {' '.join(map(str, cmd))}")
    return r


class Mig:
    def __init__(self, a):
        self.a = a
        self.old_repo, self.new_repo = Path(a.old_repo).expanduser(), Path(a.new_repo).expanduser()
        self.old_live, self.new_live = Path(a.old_live).expanduser(), Path(a.new_live).expanduser()
        self.board_db, self.state_file = Path(a.board_db).expanduser(), Path(a.state_file).expanduser()
        self.make = shlex.split(a.make)
        self.git = shlex.split(a.git)

    # ---------------------------------------------------------------- state
    def load_state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.is_file() else {}

    def save_state(self, **kw):
        st = self.load_state()
        st.update(kw, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st, ensure_ascii=False, indent=2))

    # ---------------------------------------------------------------- guards
    def busy_cards(self):
        if not self.board_db.is_file():
            return [f"board DB が無い: {self.board_db}"]
        c = sqlite3.connect(f"file:{self.board_db}?mode=ro", uri=True)
        try:
            q = "select id, status, title from tasks where status in (%s) order by id" % ",".join("?" * len(BUSY))
            return [f"{i} {s} {t}" for i, s, t in c.execute(q, BUSY)]
        finally:
            c.close()

    def cwd_procs(self):
        roots = [str(p) for p in (self.old_repo, self.new_repo, self.old_live, self.new_live)]
        me = {os.getpid(), os.getppid()}
        hits = []
        for p in Path(self.a.proc).iterdir():
            if not p.name.isdigit() or int(p.name) in me:
                continue
            try:
                cwd = os.readlink(p / "cwd")
                cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                continue
            if any(cwd == r or cwd.startswith(r + "/") for r in roots):
                hits.append(f"pid {p.name} cwd {cwd} cmd {cmd[:100]}")
        return hits

    def env_paths(self, repo, ref):
        """(paths.repo, paths.live) of ENV_FILE at ref, expanded; (None, None) when it cannot be read."""
        r = run(self.git + ["-C", repo, "show", f"{ref}:{ENV_FILE}"], check=False, quiet=True)
        if r.returncode != 0:
            return None, None
        p = tomllib.loads(r.stdout)["paths"]
        return str(Path(p["repo"]).expanduser()), str(Path(p["live"]).expanduser())

    def staging_only(self, p):
        """True when p is a directory whose only entry is staging/ (left by `make stage` / deploy-check run on the
        renamed main before --apply). It holds only copies of repository files, so --apply may delete it."""
        return p.is_dir() and not p.is_symlink() and [e.name for e in p.iterdir()] == ["staging"] and (p / "staging").is_dir()

    def guard_busy(self):
        ng = []
        cards = self.busy_cards()
        if cards:
            ng.append("1 board に動いている/待っているカードがある: " + " / ".join(cards))
        procs = self.cwd_procs()
        if procs:
            ng.append("2 リポジトリか本番の下を cwd にしたプロセスがある: " + " / ".join(procs))
        return ng

    def guards(self):
        ng = self.guard_busy()
        if self.new_repo.exists():
            ng.append(f"3 新しい置き場所が既にある: {self.new_repo}")
        if self.staging_only(self.new_live):
            log("NOTE", f"3 新しい本番の置き場所に staging/ だけがある(改名後の main で make stage / deploy-check が走った残り)。"
                        f"--apply が cron を止めたあとに消す: {self.new_live}")
        elif self.new_live.exists():
            ng.append(f"3 新しい置き場所が既にある: {self.new_live}(staging/ 以外もある = 改名後の main で make deploy が走った。"
                      "手順書『マージ後に main で make が走ったとき』で片付けてから)")
        for p in (self.old_repo, self.old_live):
            if not p.is_dir():
                ng.append(f"3 旧い置き場所が無い: {p}")
        if self.old_repo.is_dir():
            run(self.git + ["-C", self.old_repo, "fetch", "-q", "origin"], check=False, quiet=True)
            br = run(self.git + ["-C", self.old_repo, "rev-parse", "--abbrev-ref", "HEAD"], check=False, quiet=True).stdout.strip()
            dirty = run(self.git + ["-C", self.old_repo, "status", "--porcelain"], check=False, quiet=True).stdout.strip()
            ahead = run(self.git + ["-C", self.old_repo, "rev-list", "--count", "origin/main..main"], check=False, quiet=True).stdout.strip()
            if br != "main" or dirty or ahead != "0":
                ng.append(f"4 旧リポジトリが main・クリーン・未 push 0 でない(branch={br} dirty={bool(dirty)} ahead={ahead})")
            _, local_live = self.env_paths(self.old_repo, "main")
            if local_live != str(self.old_live):
                ng.append(f"4 ローカルの main の paths.live が旧い本番でない(改名の PR まで pull 済み): {local_live}。"
                          "このままの make は新しい置き場所に書く。手順書『マージ後に main で make が走ったとき』を見る")
            want_repo, want_live = self.env_paths(self.old_repo, "origin/main")
            if (want_repo, want_live) != (str(self.new_repo), str(self.new_live)):
                ng.append(f"5 origin/main の paths.repo / paths.live が新しい置き場所でない(PR が未マージ): {want_repo} / {want_live}")
            if not ng:  # make check は重いので他が通ってから
                blocking, additions = self.check_diffs(self.old_repo, neutral=True)
                for a in additions:
                    log("NOTE", f"6 本番にまだ無いだけ(消える編集ではない。--apply の手順5で足し、ジョブは手順7から動く): {a}")
                if blocking:
                    ng.append("6 旧リポジトリで make check に内容の差がある(本番だけの編集を先にリポジトリへ取り込む): " + " / ".join(blocking))
        return ng

    def neutral_env(self, repo, tmpdir):
        """A copy of repo's ENV_FILE whose paths.repo / git.repo_url name the OLD repository, so that `make check` of a
        main that already carries the new repo name (it came in ahead of the move) does not count the name itself as a
        difference. Only the last path component is swapped, so the value keeps its written form (`~/...` stays `~/...`:
        the render writes it as written in some places and expanded in others). The file keeps the env's stem so the
        render goes to the same build/<env>/ directory."""
        src = (Path(repo) / ENV_FILE).read_text()
        new_name, old_name = re.escape(self.new_repo.name), self.old_repo.name
        t = re.sub(rf'^(repo = "[^"]*/){new_name}"$', rf'\g<1>{old_name}"', src, count=1, flags=re.M)
        t = re.sub(rf'^(repo_url = "[^"]*/){new_name}"$', rf'\g<1>{old_name}"', t, count=1, flags=re.M)
        dst = Path(tmpdir) / Path(ENV_FILE).name
        dst.write_text(t)
        return dst

    def check_diffs(self, repo, neutral=False):
        """Run `make check` and split its 差分 lines into (blocking, additions). Additions only add to production
        (a skill or a cron job the repository has and production does not yet have): deploying loses nothing.
        Anything else (content differs) is blocking. A failure without 差分 lines is blocking too."""
        with tempfile.TemporaryDirectory(prefix="rename_to_sf_env_") as td:
            extra = [f"ENV_FILE={self.neutral_env(repo, td)}"] if neutral else []
            r = run(self.make + ["-C", repo, "check", *extra], check=False, quiet=True)
        lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip().startswith("差分 ")]
        additions = [l for l in lines if "本番に無い" in l or "が見つからない" in l]
        blocking = [l for l in lines if l not in additions]
        if r.returncode != 0 and not lines:
            blocking = [f"make check が rc={r.returncode} で失敗(差分の行なし)"]
        return blocking, additions

    # ---------------------------------------------------------------- steps
    def worktrees(self, repo):
        """Registered linked worktree paths (not the main one) as strings."""
        r = run(self.git + ["-C", repo, "worktree", "list", "--porcelain"], quiet=True)
        paths = [l.split(" ", 1)[1] for l in r.stdout.splitlines() if l.startswith("worktree ")]
        return paths[1:]

    def check_worktrees(self, repo):
        r = run(self.git + ["-C", repo, "worktree", "list", "--porcelain"], quiet=True)
        if "prunable" in r.stdout:
            raise Abort(f"worktree に prunable がある:\n{r.stdout}")
        for w in self.worktrees(repo):
            if not Path(w).is_dir():
                raise Abort(f"worktree のパスが無い: {w}")
            run(self.git + ["-C", w, "status", "--short"], quiet=True)
        log("OK", f"worktree {len(self.worktrees(repo))} 本が健全({repo})")

    def move_repo(self, src, dst):
        wts = self.worktrees(src)
        moved = [str(dst / Path(w).relative_to(src)) if Path(w).is_relative_to(src) else w for w in wts]
        log("RUN", f"mv {src} {dst}")
        os.rename(src, dst)
        if moved:
            run(self.git + ["-C", dst, "worktree", "repair", *moved])
        self.check_worktrees(dst)

    def pause(self, repo):
        run(self.make + ["-C", repo, "cron-pause"])
        self.save_state(paused=True)

    def resume(self, repo):
        run(self.make + ["-C", repo, "cron-resume"])
        self.save_state(paused=False)

    def resume_or_report(self, repo, redo=None):
        """Resume cron; on failure print the command to redo it as an ERROR line (the monitors are paused too, so
        nobody else notices) and return False."""
        try:
            self.resume(repo)
            return True
        except Abort as e:
            redo = redo or f"make -C {repo} cron-resume"
            log("ERROR", f"cron の再開に失敗しました({e})。監視の cron も止まったままです。"
                         f"すぐに `{redo}` をやり直し、`hermes -p dev cron list` で SF の cron が動いていることを確かめてください")
            self.save_state(resume_failed=True, resume_redo=redo)
            return False

    def cron_ids(self, live):
        p = Path(live) / "cron_ids.json"
        return json.loads(p.read_text()) if p.is_file() else {}

    def adopt_prod_only(self, repo):
        """本番にだけあるジョブ(cron_ids.json に無いが cron.toml と同じ名前)を取り込む。対象は cron-sync --dry-run が
        「同じ名前のジョブ … が既にあります」と止めたキーだけで、キーと ID はその行の案内(`factory.py cron adopt K=ID`)から取る。
        dry-run がほかの理由で止めたときは何も取り込まない(続く cron-sync の失敗として戻す)。"""
        r = run(self.make + ["-C", repo, "cron-sync", "FLAGS=--dry-run"], check=False)
        pairs = dict(ADOPT_HINT.findall(r.stdout + r.stderr))
        if not pairs:
            return {}
        run(self.make + ["-C", repo, "cron-adopt", "IDS=" + " ".join(f"{k}={v}" for k, v in sorted(pairs.items()))])
        self.save_state(adopted={**self.load_state().get("adopted", {}), **pairs})
        prof = tomllib.loads((Path(repo) / ENV_FILE).read_text()).get("hermes", {}).get("cron_profile", "<cron プロファイル>")
        log("NOTE", "本番にだけあったジョブを取り込んだ(cron_ids.json に足した。最後の cron-resume で動き出す。"
                    f"取り込む前に止めてあったジョブなら `hermes -p {prof} cron pause <ID>` で止め直す): "
                    + ", ".join(f"{k}={v}" for k, v in sorted(pairs.items())))
        return pairs

    def deploy(self, repo):
        # cron の前半: 本番にだけあるジョブを取り込み、薄い .sh を置いてから cron-sync で main が定義していて本番にまだ無い
        # ジョブ(改名とは別の変更)を作る。`make deploy` の sync_live は反映後にジョブが足りないと失敗にし、`hermes cron create
        # --script` は .sh が無いと作れないため。cron-sync は無いジョブを --paused で作り、最後の cron-resume で動く
        self.adopt_prod_only(repo)
        run(self.make + ["-C", repo, "wrappers"])
        run(self.make + ["-C", repo, "cron-sync"])
        run(self.make + ["-C", repo, "deploy"])
        # SOUL だけを置換後の写しで入れる。`make profiles FLAGS=--update` はプロファイルの設定(model など)の差も書き換えるので使わない
        run(self.make + ["-C", repo, "render"])
        cfg = tomllib.loads((Path(repo) / ENV_FILE).read_text())
        home = Path(cfg["hermes"]["home"]).expanduser()
        for prof in SOUL_PROFILES:
            src = Path(repo) / "build" / Path(ENV_FILE).stem / "hermes/profiles" / prof / "SOUL.md"
            dst = home / "profiles" / prof / "SOUL.md"
            log("RUN", f"cp {src} {dst}")
            shutil.copyfile(src, dst)

    def verify(self, repo):
        blocking, additions = self.check_diffs(repo)
        if blocking or additions:
            raise Abort("切り替え後の make check に差がある: " + " / ".join(blocking + additions))
        run(self.make + ["-C", repo, "smoke"])
        r = run(self.make + ["-C", repo, "cron-sync", "FLAGS=--dry-run"])
        if "変更 0 / 作成 0" not in r.stdout:
            raise Abort("cron-sync --dry-run に差がある")

    def plan(self):
        o, n = self.old_repo, self.new_repo
        steps = [
            f"1 make -C {o} cron-pause",
            "2 ガード 1・2 を再判定(満たさなければ cron-resume して終了コード 3)",
            f"3 ({self.new_live} が staging/ だけなら消す); mv {o} {n}; git -C {n} worktree repair <.worktrees/* の新しいパス>; "
            f"mv {self.old_live} {self.new_live}",
            f"4 git -C {n} merge --ff-only origin/main(paths.repo が {n} になることを確かめる)",
            f"5 make -C {n} cron-sync FLAGS=--dry-run(同じ名前のジョブが既にあると止めたキーだけ make -C {n} cron-adopt で取り込む); "
            f"make -C {n} wrappers; make -C {n} cron-sync(本番に無いジョブを止めた状態で作る); make -C {n} deploy; "
            f"make -C {n} render と SOUL 2つ(executor / reviewer)の写し",
            f"6 make -C {n} check(差分なし); make -C {n} smoke; make -C {n} cron-sync FLAGS=--dry-run(変更 0 / 作成 0)",
            f"7 make -C {n} cron-resume",
        ]
        for s in steps:
            log("PLAN", s)
        if self.old_repo.is_dir():
            log("PLAN", f"worktree {len(self.worktrees(self.old_repo))} 本を移す: " + ", ".join(Path(w).name for w in self.worktrees(self.old_repo)))

    # ---------------------------------------------------------------- modes
    def dry_run(self):
        ng = self.guards()
        for g in ng:
            log("WAIT", g)
        self.plan()
        log("DRY-RUN", "何も変更していません" + (f"(ガード未達 {len(ng)} 件: --apply は待つ)" if ng else "(ガードはすべて満たしています)"))
        return WAIT if ng else 0

    def apply(self):
        if self.load_state().get("step"):
            log("ERROR", f"前回の状態が残っています({self.state_file})。--rollback で戻すか、確かめてから消してください")
            return USAGE
        ng = self.guards()
        if ng:
            for g in ng:
                log("WAIT", g)
            return WAIT
        pre = run(self.git + ["-C", self.old_repo, "rev-parse", "main"], quiet=True).stdout.strip()
        # 前の実行(戻し済み)の ids_before / adopted を持ち越さない
        self.save_state(step=0, pre_sha=pre, started=time.strftime("%Y-%m-%d %H:%M:%S"), ids_before=None, adopted={})
        t0 = time.monotonic()
        try:
            self.pause(self.old_repo)
        except Abort as e:  # 途中まで止まった cron が残らないように、全部を再開してから終わる(何も動かしていない)
            log("ERROR", f"cron の停止に失敗しました({e})。止まった分を再開して終わります")
            run(self.make + ["-C", self.old_repo, "cron-resume"], check=False)
            self.state_file.unlink(missing_ok=True)
            return FAIL
        self.save_state(step=1, paused_at=time.monotonic())
        ng = self.guard_busy()
        if ng:
            for g in ng:
                log("WAIT", g)
            if not self.resume_or_report(self.old_repo):
                return FAIL
            self.state_file.unlink()
            return WAIT
        try:
            if self.staging_only(self.new_live):
                log("RUN", f"rm -r {self.new_live}(staging/ だけの残り。中身はリポジトリの写し)")
                shutil.rmtree(self.new_live)
                self.save_state(removed_staging_only=True)
            self.move_repo(self.old_repo, self.new_repo)
            self.save_state(step=3.1)
            log("RUN", f"mv {self.old_live} {self.new_live}")
            os.rename(self.old_live, self.new_live)
            self.save_state(step=3)
            run(self.git + ["-C", self.new_repo, "merge", "--ff-only", "origin/main"])
            self.save_state(step=4)
            got = str(Path(tomllib.loads((self.new_repo / ENV_FILE).read_text())["paths"]["repo"]).expanduser())
            if got != str(self.new_repo):
                raise Abort(f"main の paths.repo が {got}")
            # 手順5の前の cron_ids.json(戻すときに、取り込んだキー以外の増えたキーを外す基準)。ロールバックの deploy は書き直さない
            self.save_state(ids_before=self.cron_ids(self.new_live))
            self.deploy(self.new_repo)
            self.save_state(step=5)
            self.verify(self.new_repo)
            self.save_state(step=6)
        except (Abort, OSError) as e:
            log("ERROR", f"{e} -> 戻します")
            try:
                self.rollback(already_paused=True)
            except (Abort, OSError) as e2:
                log("ERROR", f"戻すのにも失敗しました({e2})。cron は止まったままです。状態ファイル {self.state_file} を見て、"
                             f"直してから --rollback をやり直してください")
            return FAIL
        if not self.resume_or_report(self.new_repo):  # 切り替え自体は済んでいる。戻さずに再開だけをやり直してもらう
            return FAIL
        secs = time.monotonic() - t0
        self.save_state(step=7, paused_seconds=round(secs, 1))
        log("OK", f"切り替え完了。cron の停止 {secs:.1f} 秒。続けて旧名の数え直しと epic_gate_status.json の last_success を確かめる")
        return 0

    def restore_ids(self):
        """旧本番の cron_ids.json を「手順5の前(ids_before) + 取り込んだキー(adopted)」に戻す。それ以外のキー(手順5や
        ロールバックの cron-sync が作ったジョブ)は外し、戻した設定の cron-resume で動かさない(止めたまま残す)。"""
        st = self.load_state()
        if st.get("ids_before") is None:  # 手順5まで進んでいない(cron_ids.json は変わっていない)
            return
        adopted = st.get("adopted", {})
        now = self.cron_ids(self.old_live)
        keep = {**st["ids_before"], **{k: v for k, v in now.items() if k in adopted}}
        extra = {k: v for k, v in now.items() if k not in keep}
        if keep != now:
            (self.old_live / "cron_ids.json").write_text(json.dumps(keep, ensure_ascii=False, indent=2) + "\n")
        if extra:
            log("NOTE", "切り替えの cron-sync が作ったジョブは止めたまま残した(cron_ids.json から外した。消すかは人が決める): "
                        + ", ".join(f"{k}={v}" for k, v in sorted(extra.items())))
        if adopted:
            log("NOTE", "本番にだけあったジョブは取り込んだまま残した(切り替えの前から本番にあるジョブ): "
                        + ", ".join(f"{k}={v}" for k, v in sorted(adopted.items())))

    def rollback(self, already_paused=False):
        st = self.load_state()
        step = st.get("step")
        if step is None:
            log("ERROR", f"状態ファイルがありません({self.state_file})。戻すものが分からないので何もしません")
            return USAGE
        at_new_repo = self.new_repo.is_dir() and not self.old_repo.exists()
        at_new_live = self.new_live.is_dir() and not self.old_live.exists()
        if not already_paused:
            if at_new_repo and at_new_live and step >= 4:
                run(self.make + ["-C", self.new_repo, "cron-pause"], check=False)
            elif not at_new_repo:
                run(self.make + ["-C", self.old_repo, "cron-pause"], check=False)
        if at_new_live:
            log("RUN", f"mv {self.new_live} {self.old_live}")
            os.rename(self.new_live, self.old_live)
        if at_new_repo:
            self.move_repo(self.new_repo, self.old_repo)
        else:
            self.check_worktrees(self.old_repo)
        if step >= 4:  # main が進み、新しい設定で配った可能性がある: マージ前のコミットで配り直す
            tmp = Path(tempfile.mkdtemp(prefix="rename_to_sf_rollback_"))
            wt = tmp / "pre"
            run(self.git + ["-C", self.old_repo, "worktree", "add", "--detach", wt, st["pre_sha"]])
            try:
                # deploy の前に cron の前半(取り込み → wrappers → cron-sync)も流す。マージ前の main が定義していて本番に無い
                # ジョブがあると sync_live が失敗にするため。cron_ids.json はこのあとで戻す(先に戻すと、切り替えが作ったジョブを
                # 同じ名前のジョブとして取り込み直して再開してしまう)
                self.deploy(wt)
                self.restore_ids()
                run(self.make + ["-C", wt, "check"], check=False)
                resumed = self.resume_or_report(wt, redo=(
                    f"git -C {self.old_repo} worktree add --detach \"$TMPDIR/pre\" {st['pre_sha']} && "
                    f"make -C \"$TMPDIR/pre\" cron-resume && git -C {self.old_repo} worktree remove --force \"$TMPDIR/pre\""))
            finally:
                run(self.git + ["-C", self.old_repo, "worktree", "remove", "--force", wt], check=False)
                shutil.rmtree(tmp, ignore_errors=True)
            log("NOTE", "ローカルの main は origin/main(改名後)のまま。main を戻すには revert の PR が要る(人の指示のあと)。"
                        f"もう一度 --apply するときは先に `git -C {self.old_repo} reset --keep {st['pre_sha']}` でマージ前に戻す(ガード 4)")
        else:
            self.restore_ids()
            resumed = self.resume_or_report(self.old_repo)
        self.save_state(step=None, rolled_back=time.strftime("%Y-%m-%d %H:%M:%S"))
        if not resumed:
            return FAIL
        log("OK", "戻しました")
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="リポジトリと本番の置き場所を software-factory から sf に移す(既定は dry-run)")
    m = ap.add_mutually_exclusive_group()
    m.add_argument("--apply", action="store_true")
    m.add_argument("--rollback", action="store_true")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), default=str(v))
    ap.add_argument("--make", default="make", help="make コマンド(テストでは偽物)")
    ap.add_argument("--git", default="git")
    ap.add_argument("--proc", default="/proc")
    ap.add_argument("--allow-script-in-repo", action="store_true", help="テスト用")
    ap.add_argument("--keep-env", action="append", default=[], help="子の make に通す環境変数名(既定では SAFE_DEPLOY_* / SYNC_LIVE_* / SF_* を落とす)")
    a = ap.parse_args(argv)
    KEEP_ENV.update(a.keep_env)
    mig = Mig(a)
    me = Path(__file__).resolve()
    if (a.apply or a.rollback) and not a.allow_script_in_repo and any(
            me.is_relative_to(p) for p in (mig.old_repo, mig.new_repo)):
        log("ERROR", f"このファイル({me})はリポジトリの中にあります。外に写してから実行してください(使い方は先頭の説明)")
        return USAGE
    if a.apply:
        return mig.apply()
    if a.rollback:
        return mig.rollback()
    return mig.dry_run()


if __name__ == "__main__":
    sys.exit(main())
