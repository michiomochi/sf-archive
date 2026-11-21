#!/usr/bin/env python3
"""Ask michiomochi for a human decision: creates one row in the レビュー DB (once per key), then mentions him in Discord.

usage: ask_human.py --release --epic <エピック row id> --key <epic8-承認> [--title T] [--dry-run]     (the release approval)
       ask_human.py --key <epic8-done|send-…> --text "<free-form ask>" [--title T] [--epic <エピック row id>] [--dry-run]
The row is created with the chapters scripts/levels.toml gives the epic's レベル (`review_chapters`: L3 all nine of scripts/templates/gate_ask.md,
notion/templates/review.md; H1 headings, yellow background: 判断すること / 推奨 / 人が確かめること / 完了条件の達成状況 / 何を出すか /
残っている危険と未確認 / 根拠（開くページ） / 判断の結果 / 反映の記録; a level that lists fewer gets those only: L2 the six 判断すること / 推奨 /
人が確かめること / 根拠（開くページ） / 判断の結果 / 反映の記録. The 未確認 / 注意 lines of a chapter a level drops are put at the end of 人が確かめること, so PROBLEMS still lists them), generated here in the template's shape (a template_id POST is not used for this DB: it was measured only on the タスク and エピック DBs, see create_row.py).
An epic without a レベル (made before the levels), and L1 (which has no chapters of its own), get the L3 shape.
--release reads the epic, its phase rows and the release judgement page (判定書, shape: scripts/templates/<judgement_template of the level>) and copies the chapters
mechanically (epic_common.collect_release_facts / build_release_ask): no AI, nothing summarized; what cannot be read is written as 未確認：… and the row is
still created. The gate controller and the re-ask after a 差し戻し or a stopped ship (-r2) both go through this one path. The title carries the
recommendation and the number of completion conditions met. A UI change whose judgement lacks the before/after images is refused (ERROR, exit 1).
Before the row exists, `ship_approved.py prepare` opens (or updates) the DRAFT pull request of the epic's branch and reads it back (draft, base main,
head = origin, mergeable, files changed); its URL goes into 何を出すか and chapter 1. When no such draft PR can be had, no row is made: `HOLD <key>
<code>: <reason>` and exit 3 (the gate controller turns it into a HELD line and a 候補); a transient failure is ERROR, exit 1. Approving the row then
publishes, squash-merges and deploys that PR (cron 'SF 出荷', ship_approved.py run); nobody else sends anything.
A free-form --text (-done, -send-*, レビュー中) must carry the four headings 判断すること / 推奨 / 人が確かめること / 根拠, and the 根拠 a Markdown link; the other
chapters of the level read 該当なし (the level is read from --epic). Else ERROR (exit 1) and no row is created. --dry-run prints the title, the Discord message and the body (and lists the 未確認 lines)
without writing anything to Notion or Discord (Notion and git are only read).
The row has no 種別 / 優先度 / レビュー先 / 対象タスク properties; --epic fills 対象エピック. (--kind / --url / --priority / --task are accepted and ignored, so older instructions keep working.)
Only for: the release approval at completion (its approval carries the PR's publication, merge and deploy) and what the instructions name as a person's
decision (requests to another person, any send that is not the release's own PR).
The person decides by setting the row's ステータス (承認 / 差し戻し / 取り下げ); review_inbox_status.py and the review-apply cron carry the
result into the flow. Prints CREATED <page id> or DUP <page id> (exit 0); ERROR <reason> (exit 1). Markdown links only; no bare URLs.

Notification (only for a newly CREATED row): a 4-line message (notify_lines) `<@mention_user> リリース承認をお願いします: <epic name>` / `推奨: 承認（条件 達成3・一部1・未実施1）` /
`確かめること: <1st item>（ほか n 件）` / the row URL (a free-form ask: `承認をお願いします: <row name>`)
goes to the thread of the epic, chosen from --epic's `チャット` URL in one of three ways:
  1. https://discord.com/channels/<guild>/<id> (a thread, or a channel: the URL cannot tell them apart): posted in <id>; `チャット` is not changed.
  2. https://discord.com/channels/<guild>/<channel>/<message> (the message the request came from): a thread is started on that message
     (POST /channels/<channel>/messages/<message>/threads, name `承認待ち: <epic name, first 40 chars>`) and the message is posted in it. When the
     message already has a thread (Discord 400, code 160004) it is posted in that thread (a thread started on a message has the message's id);
     when the message is itself inside a thread (code 50024) it is posted in that thread (<channel>).
  3. no --epic, an empty `チャット` or a non-Discord URL: a new thread (the same name) is created under [chat].fallback_parent and posted in.
In 2 and 3 the thread is written back to the epic's `チャット` (https://discord.com/channels/<guild>/<thread id>), so later asks of the epic take way 1
and gather in the same thread; a failed write-back is only a WARN. The bot token is DISCORD_BOT_TOKEN in the cron profile's .env (hermes.home /
hermes.cron_profile); it is never printed. A failed post (any other Discord error included) only writes a WARN to stderr: the row stays (Notion is the
SSOT) and the exit code is unchanged. Standard library only.
"""
import argparse
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import epic_common as C
import ship_approved as S

DISCORD_API = "https://discord.com/api/v10"
DISCORD_CHAT_URL = re.compile(r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(\d+|@me)/(\d+)(?:/(\d+))?/?$")
THREAD_NAME_PREFIX = "承認待ち: "
THREAD_ARCHIVE_MINUTES = 10080
POST_TIMEOUT = 15
THREAD_ALREADY_CREATED = 160004  # Discord JSON error code: "A thread has already been created for this message"
WRONG_CHANNEL_TYPE = 50024       # "Cannot execute action on this channel type" (e.g. the message is inside a thread)


class DiscordError(RuntimeError):
    """An HTTP error answer of Discord: `status` (HTTP) and `code` (Discord's JSON error code, None when absent). The text never has the token."""

    def __init__(self, text, status=None, code=None):
        super().__init__(text)
        self.status, self.code = status, code


def token_env_path():
    """<hermes.home>/profiles/<hermes.cron_profile>/.env: where the cron profile keeps DISCORD_BOT_TOKEN."""
    h = C.CFG["hermes"]
    return Path(h["home"]) / "profiles" / h["cron_profile"] / ".env"


def read_bot_token():
    path = token_env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e.strerror or e}")
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, _, v = line.partition("=")
        if k.strip() == "DISCORD_BOT_TOKEN" and v.strip().strip("\"'"):
            return v.strip().strip("\"'")
    raise RuntimeError(f"DISCORD_BOT_TOKEN is not set in {path}")


def discord_api(token, method, path, payload):
    """One Discord REST call -> parsed JSON. Raises RuntimeError (never containing the token). One retry on 429."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in (1, 2):
        req = urllib.request.Request(DISCORD_API + path, data=data, method=method, headers={
            "Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": "DiscordBot (sf, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as r:
                body = r.read()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:150]
            except Exception:
                pass
            finally:
                e.close()
            if e.code == 429 and attempt == 1:
                try:
                    wait = float(json.loads(detail).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                C._sleep(min(wait, 5.0))
                continue
            try:
                code = json.loads(detail).get("code")
            except Exception:  # not JSON, or cut at 150 characters
                m = re.search(r'"code"\s*:\s*(\d+)', detail)
                code = int(m.group(1)) if m else None
            raise DiscordError(f"Discord {method} {path} -> HTTP {e.code} {detail}".replace(token, "***"), status=e.code, code=code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"Discord {method} {path} failed: {type(e).__name__}: {e}".replace(token, "***"))


def parse_chat_url(url):
    """(guild, channel, message or None) of a Discord channel / thread / message URL; None for an empty or non-Discord value.
    guild is digits or '@me'; a thread URL has the thread id as channel (a thread URL and a channel URL look the same)."""
    m = DISCORD_CHAT_URL.match((url or "").strip())
    return (m.group(1), m.group(2), m.group(3)) if m else None


def chat_channel_id(url):
    """Channel (or thread) id of a Discord message / channel URL; None for an empty or non-Discord value."""
    p = parse_chat_url(url)
    return p[1] if p else None


def chat_target(token, chat_url, thread_name, fallback_parent):
    """(channel id to post in, guild for the write-back) for the three ways of the module doc. The guild is None when the epic's チャット
    stays as it is (way 1, or a message URL outside a server), '' for a new thread whose server Discord did not name (the write-back is then
    only a WARN), else the server id: `チャット` becomes https://discord.com/channels/<guild>/<channel id>.
    Raises (DiscordError / RuntimeError) when no target can be had; main() turns it into a WARN."""
    parsed = parse_chat_url(chat_url)
    if parsed and not parsed[2]:  # 1: a thread (or channel) URL, unchanged
        return parsed[1], None
    if parsed:  # 2: a message URL -> the thread on that message
        guild, channel, message = parsed
        try:
            target = discord_api(token, "POST", f"/channels/{channel}/messages/{message}/threads",
                                 {"name": thread_name, "auto_archive_duration": THREAD_ARCHIVE_MINUTES})["id"]
        except DiscordError as e:
            if e.status == 400 and e.code == THREAD_ALREADY_CREATED:
                target = message  # the existing thread of a message has the message's id
            elif e.status == 400 and e.code == WRONG_CHANNEL_TYPE:
                target = channel  # the message is inside a thread: that thread is the epic's thread
            else:
                raise
        return target, (guild if guild.isdigit() else None)
    parent = parent_channel_id(fallback_parent)  # 3: no usable チャット -> a new thread under the fallback parent
    if not parent:
        raise RuntimeError("the epic has no Discord チャット URL and [chat].fallback_parent is not set")
    thread = discord_api(token, "POST", f"/channels/{parent}/threads",
                         {"name": thread_name, "auto_archive_duration": THREAD_ARCHIVE_MINUTES, "type": 11})
    return thread["id"], str(thread.get("guild_id") or "")


def parent_channel_id(target):
    """`discord:<channel>[:<thread>]` ([chat].fallback_parent) -> the channel id (the parent a thread is created under)."""
    parts = (target or "").strip().split(":")
    return parts[1] if len(parts) >= 2 and parts[0] == "discord" and parts[1].isdigit() else None


def recommendation_line(text):
    """One line of the ask's recommendation: the line after a 推奨 heading, or the rest of a `推奨: ...` line.

    A nine-chapter body (the current form): the first line of the H1 chapter 推奨 without its '推奨：', followed by the number of completion
    conditions met ('承認（条件 達成3・一部1・未実施1）'). An old seven-chapter body: the H1 chapter 推奨の選択肢 wins over any earlier 推奨 line
    (the 根拠 chapter may hold a copied decision page with its own 推奨 lines); the value is the same as before the nine chapters."""
    lines = [l.strip() for l in (text or "").splitlines()]
    h1 = [i for i, l in enumerate(lines) if re.match(r"^# 推奨", l)]
    for i, l in [(i, lines[i]) for i in h1] + list(enumerate(lines)):
        clean = re.sub(r"\s*\{[^}]*\}\s*$", "", l.lstrip("#").strip())
        if not clean.startswith("推奨"):
            continue
        rest = re.sub(r"^推奨(の選択肢)?\s*[:：]?\s*", "", clean)
        if not rest:
            rest = next((x for x in lines[i + 1:] if x and not x.startswith(("#", "<"))), "")
        rest = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", rest).replace("**", "").lstrip("-* ").strip()
        if C.has_review_chapters(text):
            rest = re.sub(r"^推奨\s*[:：]\s*", "", rest)
            brief = C.counts_brief(text)
            rest = rest + (f"（条件 {brief}）" if brief and not rest.startswith("未確認") else "")
        if rest:
            return rest[:200]
    return "本文の「推奨」を見てください" if C.has_review_chapters(text) else "本文の「推奨の選択肢」を見てください"


def check_items(body):
    """(first item, number of items) of the 人が確かめること chapter of a nine-chapter body: its numbered lines plus the 'ほか n 件' note."""
    chap = C.chapter_text(body, "人が確かめること")
    items = C.list_items(chap)
    extra = re.search(r"（ほか (\d+) 件", chap)
    first = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", items[0]) if items else None  # Discord: the link text only
    return first, len(items) + (int(extra.group(1)) if extra else 0)


def notify_lines(name, url, body, mention, release=False):
    """The Discord message of a new review row, four lines (each cut to 200 characters, the URL excepted): mention and what is asked / the
    recommendation with the number of conditions met / the first thing to check (and how many more) / the row URL."""
    first, n = check_items(body) if C.has_review_chapters(body) else (None, 0)
    ask = f"リリース承認をお願いします: {name[:100]}" if release else f"承認をお願いします: {name[:100]}"
    check = "確かめること: " + (C.cut_text(first, 120) + (f"（ほか {n - 1} 件）" if n > 1 else "") if first else "なし")
    return [C.cut_text((f"<@{mention}> " if mention else "") + ask, 200), C.cut_text(f"推奨: {recommendation_line(body)}", 200),
            C.cut_text(check, 200), url]


def epic_level(epic_id):
    """The レベル (L1 / L2 / L3) of the epic row, which decides the chapters of a free-form ask; None without --epic, for an epic without a レベル,
    or when the epic cannot be read (a WARN: the L3 shape is used, as before the levels)."""
    if not epic_id:
        return None
    try:
        return C.level_of(C.ntn(f"v1/pages/{epic_id}"))
    except Exception as e:
        print(f"WARN cannot read the レベル of the epic {epic_id}; using the L3 chapters: {e}"[:300], file=sys.stderr)
        return None


def epic_chat_and_name(epic_id):
    """(チャット URL, epic name) of the epic row; ('', '') when it cannot be read (the thread fallback then applies)."""
    if not epic_id:
        return "", ""
    try:
        props = C.ntn(f"v1/pages/{epic_id}").get("properties") or {}
    except Exception as e:
        print(f"WARN cannot read the epic {epic_id} for its チャット: {e}"[:300], file=sys.stderr)
        return "", ""
    name = next(("".join(x.get("plain_text", "") for x in p.get("title", [])) for p in props.values() if p.get("type") == "title"), "")
    return ((props.get("チャット") or {}).get("url") or ""), name


def notify(key, text, page, title=None, epic_id=None, release=False):
    """Mention the owner about the new review row (see the module doc). Raises on any failure; main() turns it into a WARN."""
    chat = C.CFG.get("chat") or {}
    mention = str(chat.get("mention_user") or "").strip()
    token = read_bot_token()
    name = C.review_title(key, text, title)
    chat_url, epic_name = epic_chat_and_name(epic_id)
    channel, guild = chat_target(token, chat_url, THREAD_NAME_PREFIX + (epic_name or name)[:40], chat.get("fallback_parent"))
    lines = notify_lines(epic_name or name if release else name, page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}", text,
                         mention, release=release)
    payload = {"content": "\n".join(lines), "allowed_mentions": {"users": [mention]} if mention else {"parse": []}}
    discord_api(token, "POST", f"/channels/{channel}/messages", payload)
    if guild is not None and epic_id:  # ways 2 and 3: later asks of this epic go to the same thread
        try:
            if not guild:
                raise RuntimeError("Discord's thread response has no guild_id")
            C.ntn(f"v1/pages/{epic_id}", "-X", "PATCH", data={"properties": {"チャット": {"url": f"https://discord.com/channels/{guild}/{channel}"}}})
        except Exception as e:
            print(f"WARN cannot write the new thread back to the epic's チャット: {e}"[:300], file=sys.stderr)
    return channel


# ---------------------------------------------------------------- images of the decision page (--images-from)
# The release ask copies the decision page's images into the review row as Notion-hosted image blocks: each image is downloaded from its
# signed file URL (valid about an hour), uploaded with `ntn files create` and put after the '［画像 n］' paragraph of the 根拠 copy.
# (Measured 2026-11-09: `ntn files create --external-url <signed URL>` is refused with 400, and a markdown image with the signed URL becomes
# an external image that breaks when the URL expires.)

def source_images(page_id):
    """[dict(url, caption, name)] of the image blocks of a page, nested ones included, in document order."""
    out = []
    for b in C.iter_blocks(page_id):
        if b.get("type") != "image":
            continue
        im = b["image"]
        url = (im.get(im.get("type")) or {}).get("url", "")
        name = Path(urllib.parse.urlparse(url).path).name or f"image{len(out) + 1}.png"
        out.append(dict(url=url, caption=C.evidence_text(b), name=name))
    return out


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "software-factory"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def files_create(data, filename):
    """Upload bytes with `ntn files create` -> file upload id (status uploaded). Raises RuntimeError."""
    ctype = mimetypes.guess_type(filename)[0] or "image/png"
    r = C._sh_bytes([C.which_tool("ntn"), "files", "create", "--filename", filename, "--content-type", ctype, "--json"], data, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ntn files create failed: {(r.stderr or b'').decode('utf-8', 'replace').strip()[:150]}")
    d = json.loads((r.stdout or b"{}").decode("utf-8"))
    if d.get("status") != "uploaded" or not d.get("id"):
        raise RuntimeError(f"ntn files create: unexpected status {d.get('status')!r}")
    return d["id"]


def upload_images(page_id, numbers):
    """{n: upload id} for the images n (1-based) of the page. Raises when the page has fewer images."""
    imgs = source_images(page_id)
    out = {}
    for n in sorted(set(numbers)):
        if n > len(imgs):
            raise RuntimeError(f"the decision page has {len(imgs)} images; the copy asks for image {n}")
        out[n] = files_create(download(imgs[n - 1]["url"]), imgs[n - 1]["name"])
    return out, imgs


def image_marks(text):
    """Numbers of the '［画像 n］' paragraphs of an ask text."""
    return [int(m.group(1)) for m in re.finditer(r"^\s*［画像 (\d+)］", text or "", re.M)]


def _missing_marks(blks):
    """[(n, mark block)] of the '［画像 n］' paragraphs not followed by an image block."""
    out = []
    for i, b in enumerate(blks):
        m = C.IMAGE_MARK_RE.match(C.evidence_text(b)) if b.get("type") == "paragraph" else None
        if m and not (i + 1 < len(blks) and blks[i + 1].get("type") == "image"):
            out.append((int(m.group(1)), b))
    return out


def image_block(upload_id, caption):
    cap = [{"type": "text", "text": {"content": caption[:1900]}}] if caption else []
    return {"object": "block", "type": "image", "image": {"type": "file_upload", "file_upload": {"id": upload_id}, "caption": cap}}


def ensure_images(row_id, src_page, uploads=None, require_ui=False):
    """Put the n-th image of src_page after every '［画像 n］' paragraph of the row that has none yet (idempotent: a re-run after a partial
    failure only adds what is missing), then read the row back: every mark has its image and, for a UI change (require_ui), the row has
    >= 1 変更前 and >= 1 変更後 image (C.ui_evidence_verdict, the same check as for the decision page). -> number of images added. Raises."""
    missing = _missing_marks(C.iter_blocks(row_id))
    uploads = dict(uploads or {})
    imgs = None
    if missing:
        todo = [n for n, _ in missing if n not in uploads]
        if todo:
            more, imgs = upload_images(src_page, todo)
            uploads.update(more)
        imgs = imgs or source_images(src_page)
        for n, mark in missing:
            parent = mark.get("parent") or {}
            pid = parent.get("block_id") or parent.get("page_id") or row_id
            C.ntn(f"v1/blocks/{pid}/children", "-X", "PATCH", data={
                "children": [image_block(uploads[n], imgs[n - 1]["caption"] if n <= len(imgs) else "")],
                "position": {"type": "after_block", "after_block": {"id": mark["id"]}}})
    blks = C.iter_blocks(row_id)
    left = _missing_marks(blks)
    if left:
        raise RuntimeError(f"{len(left)} image(s) still missing in the review row after reading it back")
    if require_ui:
        v = C.ui_evidence_verdict(None, blks, assume_ui=True)
        if not v["ok"]:
            raise RuntimeError("review row: " + v["reason"])
    return len(missing)


def remember(key):
    """Cache the key in ask_human.json (the controller skips cached keys without asking Notion on every run)."""
    path = C.state_path(C.ASK_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        d = {}
    if not isinstance(d, dict):
        raise C.PipelineError(f"{path} is not a JSON object")
    d.setdefault(key, "review-db")
    C.atomic_write_json(path, d)


def ask(key, text, before_create=None, **kw):
    """-> ('CREATED'|'DUP', page id); the created page is kept in ask.last_page for notify(). The DB's キー column is the single truth for 'already asked'.

    before_create(): called only when the row will be created, right before (the image uploads: a failure there creates nothing).
    While the live レビュー DB still lacks the 対象エピック property (before its schema is changed) Notion rejects the create; the row is then
    created without the relation (a warning goes to stderr) so that the ask is never lost."""
    hit = C.query(C.REVIEW_DS, {"property": "キー", "rich_text": {"equals": key}})
    if hit:
        ask.last_page = hit[0]  # a DUP whose images were repaired is announced once (notify needs the page)
        return "DUP", hit[0]["id"]
    if before_create:
        before_create()
    body = C.review_page(key, text, **kw)
    try:
        page = C.ntn("v1/pages", "-X", "POST", data=body)
    except C.PipelineError as e:
        if "対象エピック" not in str(e) or "対象エピック" not in body["properties"]:
            raise
        print(f"WARN the レビュー DB has no 対象エピック property yet; creating the row without it: {e}"[:300], file=sys.stderr)
        del body["properties"]["対象エピック"]
        page = C.ntn("v1/pages", "-X", "POST", data=body)
    ask.last_page = page
    return "CREATED", page["id"]


def dry_run_report(title, body, lines):
    """What --dry-run prints: the row title, the Discord message, the body, and the lines of the body that say 未確認 / 注意."""
    problems = C.release_problems(body)
    out = [f"TITLE: {title}", f"NOTIFY ({len(lines)} lines, {sum(len(l) for l in lines)} chars):", *lines,
           f"BODY ({C.visible_len(body)} chars, {len(re.findall(r'^# ', body, re.M))} chapters, limit {C.REVIEW_BODY_MAX}):", body.rstrip(),
           f"PROBLEMS ({len(problems)}):", *[f"- {p}" for p in problems]]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--text", help="free-form ask: the four headings 判断すること / 推奨 / 人が確かめること / 根拠 (not with --release)")
    ap.add_argument("--release", action="store_true", help="the release approval: read the epic's release judgement and build the nine chapters (needs --epic)")
    ap.add_argument("--dry-run", action="store_true", help="print the title, the Discord message and the body; write nothing to Notion or Discord")
    ap.add_argument("--title")
    ap.add_argument("--epic", help="エピック row id (fills 対象エピック)")
    ap.add_argument("--images-from", help="page id whose images go after the '［画像 n］' paragraphs of the text (the release decision page)")
    ap.add_argument("--require-ui-images", action="store_true", help="UI change: the row must end with >= 1 変更前 and >= 1 変更後 image")
    for old in ("--kind", "--url", "--priority", "--task"):  # dropped properties: still accepted, never written
        ap.add_argument(old, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        if a.release == bool(a.text):
            raise C.ReviewAskError("--release (the release approval) or --text (a free-form ask): exactly one of them")
        if a.release and not a.epic:
            raise C.ReviewAskError("--release needs --epic <エピック row id>")
        verdict, epic_name, images_from, require_ui = None, "", a.images_from, a.require_ui_images
        if a.release:
            facts = C.collect_release_facts(a.epic, a.key)
            if not a.dry_run and not facts["verdict"]["ok"]:  # no draft PR is opened for an ask that would not be made
                print(f"ERROR the release ask is not created: {facts['verdict']['reason']}")
                return 1
            if not a.dry_run and C.query(C.REVIEW_DS, {"property": "キー", "rich_text": {"equals": a.key}}):  # a row that exists is never prepared again (its PR is in the record)
                rec = C.ship_record(a.key) or {}
                facts.update(pr=rec.get("pr"), pr_url=rec.get("pr_url"))
            else:
                prep = S.prepare(a.epic, a.key, dry_run=a.dry_run, repo=facts["repo"], branch=facts["branch"])
                if prep["status"] != "ok":  # no draft PR that was read back = no row (exit 3 = held for a reason, 1 = try again next run)
                    print(f"{'HOLD' if prep['status'] == 'hold' else 'ERROR'} {a.key} {prep['code']}: {prep['reason']}")
                    return 3 if prep["status"] == "hold" else 1
                facts.update(pr=prep["pr"], pr_url=prep["pr_url"])
            built = C.build_release_ask(facts)
            text, title, verdict, epic_name = built["body"], a.title or built["title"], facts["verdict"], facts["hub_name"]
            images_from = facts["page"]["id"] if facts.get("page") and built["marks"] else None
            require_ui = verdict["ui"] is True
        else:
            text, title = C.review_markdown(a.key, a.text, level=epic_level(a.epic)), a.title  # refuses a free-form ask without the four headings before anything is written
        if a.dry_run:
            mention = str((C.CFG.get("chat") or {}).get("mention_user") or "").strip()
            name = C.review_title(a.key, text, title)
            print(dry_run_report(name, text, notify_lines(epic_name if a.release and epic_name else name, "(the row URL goes here)", text, mention,
                                                          release=a.release)))
            if verdict is not None and not verdict["ok"]:
                print(f"HOLD: the ask would not be created: {verdict['reason']}")
            return 0
        if verdict is not None and not verdict["ok"]:
            print(f"ERROR the release ask is not created: {verdict['reason']}")
            return 1
        marks = image_marks(text) if images_from else []
        uploads = {}

        def pre_upload():  # before the row exists: a failed download/upload creates nothing (the controller retries next run)
            if marks:
                uploads.update(upload_images(images_from, marks)[0])

        state, pid = ask(a.key, text, before_create=pre_upload, title=title, epic_id=a.epic)
        added = 0
        if marks:
            try:
                added = ensure_images(pid, images_from, uploads, require_ui=require_ui)
            except Exception as e:  # the row exists but is incomplete: no cache, no mention; the next run repairs it (DUP path)
                print(f"ERROR images of the review row {pid}: {type(e).__name__}: {e}"[:300])
                return 1
        try:
            remember(a.key)
        except Exception as e:  # the cache is an optimisation only
            print(f"WARN cannot cache key: {e}", file=sys.stderr)
        if state == "CREATED" or (state == "DUP" and added):  # a repaired row was never announced (the first run stopped before notify)
            try:
                notify(a.key, text, ask.last_page, title=title, epic_id=a.epic, release=a.release)
            except Exception as e:  # the row is the ask (SSOT); a failed mention must not fail the command
                print(f"WARN cannot notify Discord (the review row {pid} exists): {type(e).__name__}: {e}"[:300], file=sys.stderr)
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}"[:600])
        return 1
    print(state, pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
