#!/usr/bin/env python3
"""雛形の `$` のエスケープ漏れを file:line で出す(標準ライブラリのみ。`make test` から実行)。

prompts / skills / SOUL / カードの雛形 / プラグインの雛形 / cron のラッパーの雛形は、`string.Template` で `${名前}` を置換する。
そのまま書きたい `$`(`$(cat file)`、`$1`、`$$` の PID など)は `$$` と書く。書き漏らすと、render が環境ごとに
「置換できません」と落ちる。この検査は環境に依らず全雛形を読み、原因の行を render の総合エラーより先に出す。

    python3 tools/lint_dollar.py            # リポジトリの全雛形(plugins/ と、環境の [plugin_sources] にある作業コピー)
    python3 tools/lint_dollar.py FILE...    # 指定したファイルだけ(テスト・単発用)

見つけたら `file:line: ...` を1行ずつ出し、終了コード 1。
"""
import string
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import factory  # noqa: E402
import sfplugins  # noqa: E402

WRAPPER_TMPL = "hermes/wrapper.sh.tmpl"  # factory.render_wrappers も string.Template で置換する


def invalid_dollars(text, label):
    """text にある、Template が受け付けない `$` の [(label:行: 説明)]。"""
    out = []
    for m in string.Template.pattern.finditer(text):
        if m.group("invalid") is not None:
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"{label}:{line}: 不正な `$`(そのまま書くなら `$$`): {text[m.start():m.start() + 24]!r}")
    return out


def template_sources(repo=REPO):
    """[(表示するパス, 本文)]。render が読む雛形すべて。外のリポジトリのプラグインは、作業コピーがある環境の分だけ。"""
    seen, out = set(), []

    def add(rel):
        if rel not in seen:
            seen.add(rel)
            out.append((rel, sfplugins.read_source(repo, rel)))

    for _, rel in factory.source_files(repo):  # plugins=None: repo の plugins/ すべて
        add(rel)
    add(WRAPPER_TMPL)
    for cfg_path in factory.env_files(repo):  # [plugin_sources] の外のプラグイン(作業コピーが無ければ飛ばす)
        try:
            raw = factory.sfconfig.load(cfg_path, expand=False)
            sfplugins.register_sources(raw)
            names = [n for n in sfplugins._SOURCES if (sfplugins._SOURCES[n] / sfplugins.MANIFEST).is_file()]
        except (factory.sfconfig.ConfigError, sfplugins.PluginError):
            continue
        for _, rel in sfplugins.source_files(repo, names):
            add(rel)
    sfplugins._SOURCES.clear()
    return out


def problems_in(files):
    out = []
    for label, text in files:
        out += invalid_dollars(text, label)
    return out


def main(argv):
    if argv:
        files = [(a, Path(a).read_text(encoding="utf-8")) for a in argv]
    else:
        files = template_sources()
    problems = problems_in(files)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"lint_dollar: {len(problems)} 件の `$` のエスケープ漏れ", file=sys.stderr)
        return 1
    print(f"ok lint_dollar: {len(files)} ファイルに `$` のエスケープ漏れなし")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
