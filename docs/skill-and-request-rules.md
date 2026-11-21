# スキルと依頼の書き方（この repo の規約）

2026-11-11 制定。根拠は Anthropic の公式資料・Lauren Tan のルール集・Hermes の公式ドキュメント。
**規約は文章だけでは守られない**ので、機械で判定できるものは `tools/skill_lint.py` と `make test` で落とす
（Claude Code 公式の「deterministic / non-deterministic」の区別: 文章はモデルが守るか分からないが、hook・deny・検査は必ず効く）。

## 0. なぜ規約が要るか（実測）

- カード1枚が**作業前に読む量は 87,868 字（約2.2万トークン）**だった。うち 76,600 字がスキル2本
- 1セッションの `cache_read` は 42.7M トークン（毎ターン同じ前置きを送り直す）
- 所要の9割は「ターン数 × 1ターン中央値6秒」。読む量が多いほど、探し直し・読み直しでターンが増える

Anthropic「Effective context engineering for AI agents」: モデルには注意予算があり、
トークンが増えるほど召回精度が落ちる（context rot）。目指すのは **"smallest possible set of high-signal tokens"**。

## 1. スキルの書き方

### 1.1 形（Hermes 公式の SKILL.md の型に合わせる）

```markdown
---
name: <ディレクトリ名と同じ>
description: "Use when <起動する条件>. <何をするか>."   # 120字以内。"Use when …" で始める
---

# <タイトル>

## いつ使うか（When to Use）
## 索引（どの参照を、いつ読むか）   ← references/*.md をここから全部指す
## 手順（Procedure）                ← 短く。詳細は references へ
## 落とし穴（Pitfalls）
## 検証（Verification）             ← 何をもって終わりとするか
```

- 根拠: Anthropic「Agent Skills」の 3 段階（Level 1 = frontmatter の description だけが常時ロード、
  Level 2 = SKILL.md 本体は起動時、Level 3 = references は参照時）。
  Hermes「Skills System」も **progressive disclosure** を明記し、`description` を起動の手掛かりに使う。
- Hermes の `hermes-agent` スキル自身が実例: 本文は約5,000字で「ルーティング表 → 参照を読め」だけを書く。

### 1.2 予算（機械で落とす）

| 対象 | 上限 | 理由 |
|---|---|---|
| `SKILL.md` | **6,000 字** | 起動時に必ず読まれる。超えたら references へ |
| `references/*.md` | 12,000 字 | 参照時でも1ファイルは小さく |
| `description` | 120 字 | 常時ロードされる（全スキル分が積み上がる） |
| カード本文（描画後） | 3,000 字 | 毎ターン再送される前置き |

### 1.3 規則の持ち主は1か所

- 同じ規則を SKILL.md・references・カード雛形・SOUL・プロンプトに**書かない**。持ち主を決めて他は参照する。
- 根拠: Lauren Tan のルール集「Extend existing instructions rather than accumulating duplicates」。
- 検査: `skill_lint` が 80 字以上の同一行の重複を検出する。
- 手順・禁止・チェックリストをスキルに書かず script / lint に移す規則（規則9）は skills repo の `auditing-skills` が持つ。検査は `tools/lint_procedures.py`（`make lint-procedures`）。

### 1.4 参照は必ず辿れる

- `references/x.md` は SKILL.md の索引から「いつ読むか」付きで指す（孤立禁止・参照切れ禁止）。
- references は**1トピック1ファイル**。増やす前に既存を延長する（Lauren: extend before adding）。

### 1.5 スキルは「観測した失敗」からだけ育てる

- 失敗の証拠（ログ・実測・再現手順）が無い一般論は書かない。
- 1つの教訓 = 1つのルール。事故の固有名詞や日付は本文に残さない（何度も読むものなので腐る）。
- 根拠: Lauren Tan「Skills should grow from evidence about how agents work」「Record the general principle, omit the incident's names」。

### 1.6 使わないスキルは出さない

- Hermes の条件付き発動（`metadata.hermes.requires_tools` / `requires_toolsets` / `platforms`）で、
  その場で使わないスキルを**そもそも見せない**。
- カードは「そのレベルの作業に必要なスキル」だけを `skills` に列挙する（L1 は1本）。

## 2. 依頼（エピック・タスク）の書き方

### 2.1 エピックの目的表（4行、これ以外を増やさない）

| 行 | 書くこと |
|---|---|
| 目的 | 誰のどんな困りごとを、どう変えるか。現状の数字を1つ入れる |
| 目標値 | 数値と期限。**測り方**も書く（どのコマンド・どの行を見るか） |
| 完了条件 | 検証できる形で箇条書き。①…⑧まで。測れない動詞（改善する・最適化する）は使わない |
| 対象外 | この依頼でやらないこと（先に切り出しておく。走行中に足さない） |

加えて `レベル`（L1/L2/L3）を**依頼を立てる人が決める**。タスク（単発）は L1 相当。

### 2.2 依頼の単位

- **1エピックは半日以内**に終わる大きさに割る。設計が2枚になったら、その時点で「最初に出す半分」に切る。
- 走行中のカードの対象を変えない。変わったら別エピックに切り出し、旧エピックの管理ログに1行残す。

### 2.3 設計は先、カードは後

- 設計を外に回す案件は、行を作ったら `新規作成の停止` を立てる（`create_row.py` の `"stop_new": true`）。
  設計が確定して本文に入れてから外す。走行中のカードに設計を追記しない（block → 本文 → 再開）。

## 3. 守らせる仕組み（deterministic）

| 仕組み | 何を守るか | どこ |
|---|---|---|
| `python3 tools/skill_lint.py` | 1.2〜1.4 の予算・参照・重複 | `make test`（`make skill-lint` 単体でも動く） |
| カード本文の描画検査 | 3,000 字上限・HARD RULE の存在 | `scripts/epic_selftest.py` |
| `card_timing.py` | ターン数・モデル待ち・固まり | 週次で見る。1枚 100 ターンを超えたら原因を見る |
| provider のタイムアウト | 返らない呼び出しで30分待たない | `providers.<id>.request_timeout_seconds` |
| STALL 検知 | heartbeat だけで「生きている」と誤判定しない | エピック `748b9234` |
| `max_runtime_seconds` | 1枚の暴走を止める（Claude Code の `--max-turns` 相当） | レベル別に設定（エピック `89cafadc`） |

## 4. 根拠（出典）

- Anthropic「Effective context engineering for AI agents」 — context rot・注意予算・smallest set of high-signal tokens・just-in-time retrieval
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic「Agent Skills」 — 3 段階の progressive disclosure、description が起動条件
  https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- Claude Code「Best practices」 — deterministic（hook・deny・CI・`--max-turns`）と non-deterministic（prompt・CLAUDE.md・skill）の区別、
  「Enforce the rules that can't have exceptions」
  https://code.claude.com/docs/en/best-practices
- Hermes「Skills System」「Creating Skills」 — on-demand / progressive disclosure、SKILL.md の型、条件付き発動、skill bundles
  https://hermes-agent.nousresearch.com/docs/user-guide/features/skills ・ https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills
- Lauren Tan（@poteto）のルール集 — 同じ失敗は仕組みで直す（設計 > 機械検査 > 短文ルール > スキル）、延長して重複を増やさない、
  検査は「悪い例を拒否し良い例を通す」ことを確かめる、スキルは観測した失敗から育てる
  https://github.com/unicodef1wn/lauren-poteto-rules（本人の講演の要約）
