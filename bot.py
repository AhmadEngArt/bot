#!/usr/bin/env python3
"""
News -> Telegram channel bot.

Reads sources from sources.json (RSS feeds, plain websites, Telegram channels),
skips anything already posted (state.json) and publishes new items to your
channel.

Changes in this version:
  * Source channel names (@username, t.me links) are replaced with YOUR channel
  * Website / RSS source links are kept
  * Ad posts are skipped (keyword filter)

Env vars (GitHub Secrets):
  CHANNEL_ID  - e.g. @my_channel  (or -100123456789)
  MY_CHANNEL  - optional, e.g. @my_channel (REQUIRED if CHANNEL_ID is numeric)
  BOT_TOKEN   - simple mode only
  API_ID, API_HASH, TG_SESSION - full mode (see gen_session.py)
"""
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
SOURCES_FILE = BASE / "sources.json"
STATE_FILE = BASE / "state.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
API_ID = os.environ.get("API_ID", "")
API_HASH = os.environ.get("API_HASH", "")
TG_SESSION = os.environ.get("TG_SESSION", "")

# اسم کانال خودت (اگر CHANNEL_ID عددی است، MY_CHANNEL را در Secrets/Variables بگذار
# یا همین‌جا دستی بنویس، مثلا "@my_channel")
MY_CHANNEL = "@rooozane24"

# کلمات تبلیغاتی؛ هر پستی که شامل یکی از این‌ها باشد ارسال نمی‌شود
AD_KEYWORDS = [
    "تبلیغ", "تبلیغات", "پشتیبانی", "سفارش", "تخفیف", "فروش ویژه",
    "ثبت نام", "ثبت‌نام", "کد تخفیف", "خرید اینترنتی", "خرید از", "شارژ ایرانسل",
    "شارژ همراه", "vpn", "وی پی ان", "فیلترشکن", "شرط بندی", "شرط‌بندی",
    "کازینو", "سایت شرط", "کانال vip", "کپی تریدینگ", "سیگنال رایگان",
    "جهت سفارش", "با ما تماس", "لینک عضویت", "جوین شو", "جوین شوید",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
TIMEOUT = 25
UPLOAD_TIMEOUT = 180
MAX_POSTS_PER_RUN = 30
MAX_STATE_ITEMS = 5000
SEND_DELAY = 3
MAX_FILE_BYTES = 49 * 1024 * 1024
CAPTION_LIMIT = 1024


def mtproto_enabled() -> bool:
    return bool(API_ID and API_HASH and TG_SESSION)


# ---------------------------------------------------------------- helpers
def item_id(url_or_text: str) -> str:
    return hashlib.sha1(url_or_text.encode("utf-8")).hexdigest()[:16]


def clean(text: str, limit: int = 600) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def replace_channels(text: str) -> str:
    """@username و لینک t.me را با کانال خودت عوض می‌کند (لینک سایت‌ها دست‌نخورده)."""
    if not text or not MY_CHANNEL:
        return text
    my_link = f"https://t.me/{MY_CHANNEL.lstrip('@')}"
    text = re.sub(r"(?:https?://)?t\.me/[\w/+\-]+", my_link, text)
    text = re.sub(r"(?<![\w./])@\w{4,}", MY_CHANNEL, text)
    return text


def is_ad(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in AD_KEYWORDS)


# ---------------------------------------------------------------- fetchers
def fetch_rss(src):
    feed = feedparser.parse(src["url"], request_headers=UA)
    items = []
    for e in feed.entries[: src.get("limit", 10)]:
        link = e.get("link")
        if not link:
            continue
        items.append(
            {
                "id": item_id(link),
                "title": clean(e.get("title", ""), 250),
                "summary": clean(e.get("summary", ""), 400),
                "link": link,
            }
        )
    return items


def extract_media(msg):
    """Simple mode: photos, videos and voice exposed by the t.me/s web preview."""
    media = []
    for a in msg.select("a.tgme_widget_message_photo_wrap"):
        m = re.search(r"url\(['\"]?(.*?)['\"]?\)", a.get("style", ""))
        if m:
            media.append({"type": "photo", "url": m.group(1)})
    for v in msg.select("video.tgme_widget_message_video, video.tgme_widget_message_roundvideo"):
        if v.get("src"):
            media.append({"type": "video", "url": v["src"]})
    for au in msg.select("audio.tgme_widget_message_voice"):
        if au.get("src"):
            media.append({"type": "voice", "url": au["src"]})
    return media


def fetch_telegram_channel(src):
    """Simple mode: public channels only, via the t.me/s/<name> web preview."""
    name = src["channel"].lstrip("@")
    r = requests.get(f"https://t.me/s/{name}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message_wrap")[-src.get("limit", 10):]:
        bubble = msg.select_one(".tgme_widget_message")
        if not bubble:
            continue
        post = bubble.get("data-post")  # channel/123
        if not post:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = clean(text_el.get_text(" ", strip=True), 700) if text_el else ""
        if is_ad(text):
            continue
        text = replace_channels(text)
        media = extract_media(msg)
        if not media and len(text) < 20:
            continue
        items.append(
            {
                "id": item_id(post),
                "title": "",
                "summary": text,
                "link": f"https://t.me/{post}",
                "media": media,
                "from_telegram": True,
            }
        )
    return items


def fetch_website(src):
    """
    Generic HTML scraper. In sources.json set:
      "link_selector": CSS selector matching <a> tags of article links
    """
    r = requests.get(src["url"], headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    soup = BeautifulSoup(r.text, "html.parser")
    items, seen = [], set()
    for a in soup.select(src.get("link_selector", "article a")):
        href = a.get("href")
        title = clean(a.get_text(" ", strip=True), 250)
        if not href or len(title) < 15:
            continue
        link = urljoin(src["url"], href)
        if link in seen:
            continue
        seen.add(link)
        items.append({"id": item_id(link), "title": title, "summary": "", "link": link})
        if len(items) >= src.get("limit", 10):
            break
    return items


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram_channel, "website": fetch_website}


def collect_basic(sources, seen, skip_telegram=False):
    new = []
    for src in sources:
        kind = src.get("type")
        if kind == "telegram" and skip_telegram:
            continue
        fetcher = FETCHERS.get(kind)
        if not fetcher:
            print(f"unknown source type: {src}")
            continue
        try:
            items = fetcher(src)
            print(f"[{src.get('name')}] fetched {len(items)}")
        except Exception as ex:
            print(f"[{src.get('name')}] ERROR: {ex}")
            continue
        for it in items:
            if is_ad(f"{it['title']} {it['summary']}"):
                continue
            if it["id"] not in seen:
                new.append((it, src))
    return new


def format_message(item, src) -> str:
    parts = []
    if item["title"]:
        parts.append(f"<b>{esc(replace_channels(item['title']))}</b>")
    if item["summary"]:
        parts.append(esc(replace_channels(item["summary"])))
    if item.get("from_telegram"):
        # پست تلگرامی: فقط اسم کانال خودت
        if MY_CHANNEL:
            parts.append(esc(MY_CHANNEL))
    else:
        # RSS و سایت: لینک منبع خبر حفظ می‌شود
        source_name = esc(src.get("name", "منبع"))
        parts.append(f'🔗 <a href="{html.escape(item["link"], quote=True)}">{source_name}</a>')
        if MY_CHANNEL:
            parts.append(esc(MY_CHANNEL))
    footer = os.environ.get("CHANNEL_FOOTER", "").strip()
    if footer:
        parts.append(esc(footer))
    return "\n\n".join(parts)[:4000]


# ================================================================ SIMPLE MODE (Bot API)
def tg(method: str, payload: dict, files=None) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(3):
        if files:
            data = {k: (v if isinstance(v, (str, int)) else json.dumps(v)) for k, v in payload.items()}
            r = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
        else:
            r = requests.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            print(f"rate limited, sleeping {wait}s")
            time.sleep(wait + 1)
            continue
        print(f"telegram error ({method}) {r.status_code}: {r.text[:200]}")
        return False
    return False


def send_text(text: str, preview: bool = True) -> bool:
    return tg(
        "sendMessage",
        {
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        },
    )


def download(url: str):
    try:
        r = requests.get(url, headers=UA, timeout=UPLOAD_TIMEOUT, stream=True)
        r.raise_for_status()
        buf, size = bytearray(), 0
        for chunk in r.iter_content(1 << 16):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                print(f"file too large, skipping: {url[:80]}")
                return None
            buf.extend(chunk)
        return bytes(buf)
    except Exception as ex:
        print(f"download failed: {ex}")
        return None


METHODS = {"photo": ("sendPhoto", "photo", "photo.jpg"),
           "video": ("sendVideo", "video", "video.mp4"),
           "voice": ("sendVoice", "voice", "voice.ogg")}


def send_single(m, caption):
    method, field, fname = METHODS[m["type"]]
    payload = {"chat_id": CHANNEL_ID, field: m["url"]}
    if caption:
        payload.update(caption=caption, parse_mode="HTML")
    if m["type"] == "video":
        payload["supports_streaming"] = True
    if tg(method, payload):
        return True
    blob = download(m["url"])
    if not blob:
        return False
    payload.pop(field)
    return tg(method, payload, files={field: (fname, blob)})


def send_group(visual, caption):
    def build(attach: bool):
        arr = []
        for i, m in enumerate(visual):
            entry = {"type": m["type"], "media": f"attach://f{i}" if attach else m["url"]}
            if i == 0 and caption:
                entry.update(caption=caption, parse_mode="HTML")
            arr.append(entry)
        return arr

    if tg("sendMediaGroup", {"chat_id": CHANNEL_ID, "media": build(False)}):
        return True
    files = {}
    for i, m in enumerate(visual):
        blob = download(m["url"])
        if not blob:
            return False
        files[f"f{i}"] = (f"f{i}", blob)
    return tg("sendMediaGroup", {"chat_id": CHANNEL_ID, "media": build(True)}, files=files)


def publish(item, src) -> bool:
    text = format_message(item, src)
    media = item.get("media") or []
    preview = not item.get("from_telegram")
    if not media:
        return send_text(text, preview)

    visual = [m for m in media if m["type"] in ("photo", "video")][:10]
    voices = [m for m in media if m["type"] == "voice"]
    short = len(text) <= CAPTION_LIMIT
    cap = text if short else None
    any_ok = captioned = False

    if len(visual) == 1:
        ok = send_single(visual[0], cap)
    elif len(visual) > 1:
        ok = send_group(visual, cap)
    else:
        ok = False
    if ok:
        any_ok = True
        captioned = short

    for i, v in enumerate(voices):
        c = text if (short and not captioned and i == 0) else None
        if send_single(v, c):
            any_ok = True
            captioned = captioned or bool(c)

    if captioned:
        return True
    return send_text(text, preview) or any_ok


# ================================================================ FULL MODE (MTProto / Telethon)
def parse_peer(value):
    v = str(value).strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    v = re.sub(r"^https?://t\.me/", "", v)
    return v.lstrip("@")


def footer_html(link: str, src) -> str:
    """فوتر پست‌های تلگرامی: فقط اسم کانال خودت (بدون اسم/لینک کانال مبدا)."""
    parts = []
    if MY_CHANNEL:
        parts.append(esc(MY_CHANNEL))
    f = os.environ.get("CHANNEL_FOOTER", "").strip()
    if f:
        parts.append(esc(f))
    return "\n\n".join(parts)


def post_link(peer, msg_id: int) -> str:
    if isinstance(peer, int):
        return f"https://t.me/c/{str(peer).replace('-100', '', 1)}/{msg_id}"
    return f"https://t.me/{peer}/{msg_id}"


def is_visual(m) -> bool:
    """Photos and (non-round, non-gif) videos can share one album."""
    return bool(m.photo or (m.video and not m.video_note and not m.gif))


async def mt_fetch(client, src):
    peer = parse_peer(src["channel"])
    limit = src.get("limit", 10)
    msgs = await client.get_messages(peer, limit=limit * 4)  # newest first
    groups, order = {}, []
    for m in msgs:
        if m.action:
            continue
        key = ("g", m.grouped_id) if m.grouped_id else ("m", m.id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    items = []
    for key in order[:limit]:
        grp = sorted(groups[key], key=lambda x: x.id)
        raw = next((m.raw_text for m in grp if m.raw_text), "") or ""
        body = next((m.text for m in grp if m.raw_text), "") or ""
        if is_ad(raw):
            continue
        raw = replace_channels(raw)
        body = replace_channels(body)
        media_msgs = [m for m in grp if (m.photo or m.document)]
        if not media_msgs and len(raw) < 20:
            continue
        first_id = grp[0].id
        items.append(
            {
                "id": item_id(f"{peer}/{first_id}"),
                "title": "",
                "summary": raw,
                "html": body,
                "link": post_link(peer, first_id),
                "msgs": media_msgs,
                "from_telegram": True,
            }
        )
    return items


async def mt_send_text(client, target, text, preview) -> bool:
    if not text.strip():
        return False
    try:
        await client.send_message(target, text, parse_mode="html", link_preview=preview)
        return True
    except Exception as ex:
        print(f"send_message failed: {ex!r}")
        return False


async def mt_send_media(client, target, msgs, caption) -> bool:
    kw = {"caption": caption, "parse_mode": "html"} if caption else {}
    objs = [m.media for m in msgs]
    try:  # ارسال مستقیم با ارجاع به فایل؛ بدون دانلود و بدون محدودیت حجم
        await client.send_file(target, objs if len(objs) > 1 else objs[0], **kw)
        return True
    except Exception as ex:
        print(f"direct media send failed: {ex!r}; retrying with download")
    tmp = tempfile.mkdtemp()
    try:  # کانال‌های محافظت‌شده و موارد خاص: دانلود و آپلود دوباره
        paths = []
        for m in msgs:
            p = await client.download_media(m, file=tmp + os.sep)
            if p:
                paths.append(p)
        if not paths:
            return False
        await client.send_file(
            target, paths if len(paths) > 1 else paths[0], supports_streaming=True, **kw
        )
        return True
    except Exception as ex:
        print(f"media upload failed: {ex!r}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def mt_publish(client, target, it, src) -> bool:
    if not it.get("from_telegram"):
        return await mt_send_text(client, target, format_message(it, src), True)

    raw, body = it["summary"], it.get("html", "")
    foot = footer_html(it["link"], src)
    full = "\n\n".join(p for p in (body, foot) if p)
    plain_len = len(raw) + len(MY_CHANNEL) + len(os.environ.get("CHANNEL_FOOTER", "")) + 8
    if plain_len > 3900:
        full = "\n\n".join(p for p in (f"{esc(raw[:3800])}…", foot) if p)
        plain_len = 3900

    msgs = it.get("msgs") or []
    if not msgs:
        return await mt_send_text(client, target, full, False)

    cap = full if (full and plain_len <= CAPTION_LIMIT - 24) else None
    visual = [m for m in msgs if is_visual(m)][:10]
    others = [m for m in msgs if not is_visual(m)]
    any_ok = captioned = False

    if visual and await mt_send_media(client, target, visual, cap):
        any_ok = True
        captioned = bool(cap)
    for m in others:
        c = cap if (cap and not captioned) else None
        if await mt_send_media(client, target, [m], c):
            any_ok = True
            captioned = captioned or bool(c)

    if captioned:
        return True
    if any_ok and not full:
        return True
    return (await mt_send_text(client, target, full, False)) or any_ok


async def mt_main(sources, seen, first_run):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(TG_SESSION), int(API_ID), API_HASH)
    client.parse_mode = "html"
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("TG_SESSION is invalid or expired. Generate a new one with gen_session.py")

        peers = [parse_peer(CHANNEL_ID)] + [
            parse_peer(s["channel"]) for s in sources if s.get("type") == "telegram"
        ]
        if any(isinstance(p, int) for p in peers):
            await client.get_dialogs()  # برای شناسایی کانال‌هایی که با شناسه‌ی عددی داده شده‌اند
        target = await client.get_entity(parse_peer(CHANNEL_ID))

        new_items = collect_basic(sources, seen, skip_telegram=True)
        for src in sources:
            if src.get("type") != "telegram":
                continue
            try:
                items = await mt_fetch(client, src)
                print(f"[{src.get('name')}] fetched {len(items)}")
            except Exception as ex:
                print(f"[{src.get('name')}] ERROR: {ex!r}")
                continue
            new_items += [(it, src) for it in items if it["id"] not in seen]

        if first_run:
            print("First run: marking existing items as seen without posting.")
            for it, _ in new_items:
                seen.add(it["id"])
            return 0, 0

        posted = 0
        for it, src in reversed(new_items):
            if posted >= MAX_POSTS_PER_RUN:
                break
            if await mt_publish(client, target, it, src):
                seen.add(it["id"])
                posted += 1
                time.sleep(SEND_DELAY)
        return posted, max(0, len(new_items) - posted)
    finally:
        await client.disconnect()


# ---------------------------------------------------------------- main
def save_state(state, seen):
    ids = list(seen)
    state["seen"] = ids[-MAX_STATE_ITEMS:] if len(ids) > MAX_STATE_ITEMS else ids
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    if not CHANNEL_ID:
        sys.exit("CHANNEL_ID must be set.")
    if not mtproto_enabled() and not BOT_TOKEN:
        sys.exit("Set BOT_TOKEN (simple mode) or API_ID + API_HASH + TG_SESSION (full mode).")
    if not MY_CHANNEL:
        print("WARNING: MY_CHANNEL is empty (CHANNEL_ID is numeric). "
              "Set MY_CHANNEL=@your_channel so source channel names can be replaced.")

    sources = load_json(SOURCES_FILE, [])
    state = load_json(STATE_FILE, {"seen": []})
    seen = set(state["seen"])
        first_run = not state["seen"]

    posted = pending = 0
    try:
        if mtproto_enabled():
            import asyncio

            print("Mode: full (MTProto)")
            posted, pending = asyncio.run(mt_main(sources, seen, first_run))
        else:
            print("Mode: simple (Bot API)")
            new_items = collect_basic(sources, seen)
            if first_run:
                print("First run: marking existing items as seen without posting.")
                for it, _ in new_items:
                    seen.add(it["id"])
                new_items = []
            for it, src in reversed(new_items):  # قدیمی‌تر اول
                if posted >= MAX_POSTS_PER_RUN:
                    break
                if publish(it, src):
                    seen.add(it["id"])
                    posted += 1
                    time.sleep(SEND_DELAY)
            pending = max(0, len(new_items) - posted)
    finally:
        save_state(state, seen)
    print(f"Done. Posted {posted}, pending {pending}.")


if __name__ == "__main__":
    main()
