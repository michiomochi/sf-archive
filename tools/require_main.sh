#!/bin/bash
# require_main.sh - `make deploy` を、main の作業ツリー1か所からだけ通すための検査。満たさなければ理由を1行出して終了コード 1。
#
#   deploy 拒否: branch=<名前> rev=<短縮ハッシュ> (<理由>)
#
# 検査(上から順。最初に落ちたもので止まる):
#   1. ブランチが main
#   2. main の作業ツリーである(worktree でない): git-dir と git-common-dir が同じ
#   3. `git fetch origin main` のあと HEAD が origin/main と同じ(古い木・未 push の木・fetch できないときは拒否)
#   4. 追跡ファイルに未コミットの変更が無い(deploy は作業ツリーのファイルをそのまま写すため)
#   5. 本番へ写すパス(scripts skills prompts tools plugins hermes envs)に、追跡外で無視もされていないファイルが無い
#      (stage は scripts/*.py などを、sync_live は skills/ や prompts/ を、追跡されているかを見ずに写すため)
# make deploy と make sync が通す。deploy-check と stage はここを通さない(マージ前のブランチから確認するため)。排他は tools/with_lock.py が受け持つ。
# 環境変数 SF_REQUIRE_MAIN_LABEL で先頭の語を変えられる(既定 deploy)。
set -u
LABEL="${SF_REQUIRE_MAIN_LABEL:-deploy}"

branch="$(git symbolic-ref --short -q HEAD 2>/dev/null || true)"
rev="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
deny() { echo "$LABEL 拒否: branch=${branch:-(detached)} rev=$rev ($1)" >&2; exit 1; }

git rev-parse --git-dir > /dev/null 2>&1 || deny "git のリポジトリの中ではない"

# 1. ブランチが main
[ "$branch" = "main" ] || deny "ブランチが main ではない"

# 2. main の作業ツリー(worktree でない)
gitdir="$(cd "$(git rev-parse --git-dir)" && pwd -P)"
common="$(cd "$(git rev-parse --git-common-dir)" && pwd -P)"
if [ "$gitdir" != "$common" ]; then
  main_tree="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
  deny "worktree からは ${LABEL} できない。main の作業ツリーは $main_tree"
fi

# 3. fetch のあと HEAD が origin/main と同じ
git fetch --quiet origin main 2> /dev/null || deny "git fetch origin main に失敗"
head_full="$(git rev-parse HEAD)"
origin_full="$(git rev-parse --verify --quiet origin/main)" || deny "origin/main が無い"
if [ "$head_full" != "$origin_full" ]; then
  if git merge-base --is-ancestor HEAD origin/main; then
    n="$(git rev-list --count HEAD..origin/main)"
    deny "木が古い。origin/main が $n コミット先($(git rev-parse --short origin/main))。git pull してから打つ"
  fi
  n="$(git rev-list --count origin/main..HEAD)"
  deny "HEAD が origin/main に含まれない。未 push の差分が $n コミットある"
fi

# 4. 追跡ファイルに未コミットの変更が無い
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  deny "追跡ファイルに未コミットの変更がある"
fi

# 5. 本番へ写すパスに追跡外のファイルが無い(.gitignore で無視されているものは数えない)
untracked="$(git ls-files --others --exclude-standard -- scripts skills prompts tools plugins hermes envs 2> /dev/null)"
if [ -n "$untracked" ]; then
  n="$(printf '%s\n' "$untracked" | wc -l | tr -d ' ')"
  first="$(printf '%s\n' "$untracked" | head -n 3 | tr '\n' ' ')"
  deny "本番へ写すパスに追跡外のファイルが ${n} 件ある: ${first}。git add してコミットするか、消す"
fi
exit 0
