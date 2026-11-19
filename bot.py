#!/usr/bin/env python3
"""
News -> Telegram channel bot.

Reads sources from sources.json (RSS feeds, plain websites, Telegram channels),
skips anything already posted (state.json) and publishes new items to your
channel.

Changes in this version:
  * Dollar price from @DO_L4 posted and pinned at 9, 12, 17, 21 Tehran time
  * The same story from several sources is posted only once
  * Posts go out one at a time (default: 1 post every minute, newest first)
  * Source channel names (@username, t.me links) are replaced with YOUR channel
  * Website / RSS source links are kept
  * Ad posts are skipped (keyword filter)

Env vars (GitHub Secrets):
  CHANNEL_ID  - e.g. @my_channel  (or -100123456789)
  MY_CHANNEL  - optional, e.g. @my_channel (REQUIRED if CHANNEL_ID is numeric)
  BOT_TOKEN   - simple mode only
  API_ID, API_HASH, TG_SESSION - full mode (see gen_session.py)
"""
import calendar
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
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
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "").strip()   # شناسه عددی چت خودت برای دریافت هشدار
TEST_ALERT = os.environ.get("TEST_ALERT", "").strip().lower() == "true"
SKIP_BACKLOG = os.environ.get("SKIP_BACKLOG", "").strip().lower() == "true"   # صف فعلی بدون ارسال رد شود
ALERT_COOLDOWN = 6 * 3600   # یک هشدار تکراری حداکثر هر ۶ ساعت ارسال می‌شود

# اسم کانال خودت (اگر CHANNEL_ID عددی است، MY_CHANNEL را در Secrets/Variables بگذار
# یا همین‌جا دستی بنویس، مثلا "@my_channel")
MY_CHANNEL = "@rooozane24"

# کلمات تبلیغاتی؛ هر پستی که شامل یکی از این‌ها باشد ارسال نمی‌شود
# (کلمه‌های عمومی مثل «پشتیبانی»، «ثبت‌نام»، «سفارش» و «فیلترشکن» عمدا حذف شده‌اند،
#  چون در خبرهای عادی زیاد می‌آیند و پست خبری را به اشتباه حذف می‌کردند)
AD_KEYWORDS = [
    "#تبلیغ", "#تبلیغات", "تبلیغات ویژه", "پست تبلیغاتی", "رزرو تبلیغ",
    "کد تخفیف", "تخفیف ویژه", "فروش ویژه", "خرید اینترنتی", "شارژ ایرانسل",
    "vpn", "وی پی ان", "وی‌پی‌ان", "ویپی ان", "ویپی‌ان", "ویپیان",   # هر پستی با کلمه VPN
    "خرید فیلترشکن", "فروش فیلترشکن", "فیلترشکن رایگان", "سرور v2ray", "خرید کانفیگ",
    "شرط بندی", "شرط‌بندی", "کازینو", "سایت شرط", "بت فوروارد",
    "کانال vip", "کپی تریدینگ", "سیگنال رایگان",
    "جهت سفارش", "جهت خرید", "برای سفارش", "با ما تماس", "لینک عضویت",
    "جوین شو", "جوین شوید", "عضو شوید", "کلیک کنید",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
TIMEOUT = 25
UPLOAD_TIMEOUT = 180
MAX_AGE_HOURS = 6           # پست‌های قدیمی‌تر از این (ساعت) ارسال نمی‌شوند؛ 0 = بدون محدودیت
MAX_STATE_ITEMS = 5000

# پخش تدریجی: هر چند دقیقه فقط یک پست (از تنظیمات Variables هم قابل تغییر است)
POST_INTERVAL_MIN = float(os.environ.get("POST_INTERVAL_MIN") or 1)
RUN_MINUTES = float(os.environ.get("RUN_MINUTES") or 50)   # هر اجرا چند دقیقه کار کند
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


def prepare_queue(items):
    """
    پست‌های خیلی قدیمی را حذف و بقیه را مرتب می‌کند: جدیدترین اول.
    (چون در هر اجرا فقط یک پست در هر چند دقیقه می‌رود، اگر صف شلوغ باشد
    خبر تازه زودتر منتشر می‌شود و خبرهای کهنه بعد از MAX_AGE_HOURS کنار می‌روند.)
    """
    now = time.time()
    cutoff = now - MAX_AGE_HOURS * 3600 if MAX_AGE_HOURS > 0 else 0
    ok = [
        (it, src) for it, src in items
        if src.get("scan_back") or not it.get("ts") or it["ts"] >= cutoff
    ]
    ok.sort(key=lambda x: x[0].get("ts") or now, reverse=True)
    return ok


class Seen(dict):
    """لیست پست‌های ارسال‌شده با حفظ ترتیب (تا هنگام کوتاه کردن، قدیمی‌ترین‌ها حذف شوند)."""
    def add(self, key):
        self.pop(key, None)
        self[key] = None


def entry_ts(e):
    try:
        t = e.get("published_parsed") or e.get("updated_parsed")
        return calendar.timegm(t) if t else None
    except Exception:
        return None


# ---------------------------------------------------------------- alerts
ERRORS = {}


def report_error(key: str, msg: str):
    ERRORS[key] = msg[:300]
    print(f"ALERT: {msg[:300]}")


def send_alerts(state):
    """هشدار خطاها را از طریق Bot API به چت خودت می‌فرستد (با جلوگیری از تکرار)."""
    if not ERRORS:
        return
    if not (BOT_TOKEN and ALERT_CHAT_ID):
        print("Alerts skipped: set BOT_TOKEN and ALERT_CHAT_ID to receive them.")
        return
    now = time.time()
    last = state.setdefault("alerts", {})
    for k in [k for k, t in last.items() if now - t > 86400]:
        del last[k]
    lines = []
    for key, msg in ERRORS.items():
        if now - last.get(key, 0) < ALERT_COOLDOWN:
            continue
        lines.append(f"• {msg}")
        last[key] = now
    if not lines:
        return
    text = ("⚠️ هشدار ربات خبری\n\n" + "\n".join(lines))[:3500]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ALERT_CHAT_ID, "text": text},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"alert send failed: {r.status_code} {r.text[:150]}")
    except Exception as ex:
        print(f"alert send failed: {type(ex).__name__}")


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
                "ts": entry_ts(e),
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
    # یک فایل گاهی دو بار در صفحه آمده؛ تکراری‌ها حذف می‌شوند
    uniq, keys = [], set()
    for m in media:
        k = m["url"].split("?")[0]
        if k not in keys:
            keys.add(k)
            uniq.append(m)
    return uniq


def fetch_telegram_channel(src):
    """Simple mode: public channels only, via the t.me/s/<name> web preview."""
    name = src["channel"].lstrip("@")
    r = requests.get(f"https://t.me/s/{name}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    wraps = soup.select(".tgme_widget_message_wrap")
    deep = bool(src.get("scan_back", False))
    wraps = list(reversed(wraps)) if deep else wraps[-src.get("limit", 10):]
    for msg in wraps:
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
        ts = None
        try:
            t = msg.select_one(".tgme_widget_message_date time")
            ts = datetime.fromisoformat(t["datetime"]).timestamp()
        except Exception:
            pass
        if not media and len(text) < 20:
            continue
        items.append(
            {
                "id": item_id(post),
                "title": "",
                "summary": text,
                "link": f"https://t.me/{post}",
                "ts": ts,
                "media": media,
                "from_telegram": True,
            }
        )
        if len(items) >= src.get("limit", 10):
            break
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
            report_error(f"src:{src.get('name')}",
                         f"منبع {src.get('name')} خطا داد: {type(ex).__name__}: {str(ex)[:150]}")
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
def tg_call(method: str, payload: dict, files=None):
    """درخواست به Bot API؛ در صورت موفقیت مقدار result و در غیر این صورت None برمی‌گرداند."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(3):
        if files:
            data = {k: (v if isinstance(v, (str, int)) else json.dumps(v)) for k, v in payload.items()}
            r = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
        else:
            r = requests.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json().get("result", True)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            print(f"rate limited, sleeping {wait}s")
            time.sleep(wait + 1)
            continue
        print(f"telegram error ({method}) {r.status_code}: {r.text[:200]}")
        return None
    return None


def tg(method: str, payload: dict, files=None) -> bool:
    return tg_call(method, payload, files) is not None


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


# تلگرام معمولا نمی‌تواند فایل را مستقیم از لینک t.me بگیرد (خطای failed to get HTTP URL content)،
# پس اول خودمان دانلود و آپلود می‌کنیم و فقط اگر دانلود نشد، ارسال با لینک را امتحان می‌کنیم.
def send_single(m, caption):
    method, field, fname = METHODS[m["type"]]
    payload = {"chat_id": CHANNEL_ID}
    if caption:
        payload.update(caption=caption, parse_mode="HTML")
    if m["type"] == "video":
        payload["supports_streaming"] = True
    blob = download(m["url"])
    if blob:
        return tg(method, payload, files={field: (fname, blob)})
    return tg(method, {**payload, field: m["url"]})


def send_group(visual, caption):
    def build(attach: bool):
        arr = []
        for i, m in enumerate(visual):
            entry = {"type": m["type"], "media": f"attach://f{i}" if attach else m["url"]}
            if i == 0 and caption:
                entry.update(caption=caption, parse_mode="HTML")
            arr.append(entry)
        return arr

    blobs, hashes, kept = [], set(), []
    for m in visual:
        blob = download(m["url"])
        if not blob:
            blobs = None
            break
        h = hashlib.md5(blob).hexdigest()
        if h in hashes:          # همان فایل دوباره آمده؛ فقط یک بار ارسال شود
            print("duplicate media in post skipped")
            continue
        hashes.add(h)
        kept.append(m)
        blobs.append(blob)
    if blobs is not None:
        visual = kept
        if len(visual) == 1:
            method, field, fname = METHODS[visual[0]["type"]]
            payload = {"chat_id": CHANNEL_ID}
            if caption:
                payload.update(caption=caption, parse_mode="HTML")
            if visual[0]["type"] == "video":
                payload["supports_streaming"] = True
            return tg(method, payload, files={field: (fname, blobs[0])})
        files = {f"f{i}": (f"f{i}", b) for i, b in enumerate(blobs)}
        return tg("sendMediaGroup", {"chat_id": CHANNEL_ID, "media": build(True)}, files=files)
    return tg("sendMediaGroup", {"chat_id": CHANNEL_ID, "media": build(False)})


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
    # scan_back=true در sources.json: تبلیغ‌ها رد می‌شوند و تا رسیدن به limit پست سالم به عقب می‌رویم
    deep = bool(src.get("scan_back", False))
    msgs = await client.get_messages(peer, limit=limit * 4 + (10 if deep else 0))  # newest first
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
    for key in (order if deep else order[:limit]):
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
                "ts": grp[0].date.timestamp() if grp[0].date else None,
                "msgs": media_msgs,
                "from_telegram": True,
            }
        )
        if len(items) >= limit:
            break
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


# ================================================================ DUPLICATE NEWS
# اگر چند منبع یک خبر را منتشر کنند، فقط اولی ارسال می‌شود.
DUP_WINDOW_HOURS = 12    # متن‌های ارسال‌شده در این بازه با پست جدید مقایسه می‌شوند
DUP_THRESHOLD = 0.6      # چه سهمی از کلمات پست کوتاه‌تر در پست دیگر باشد تا تکراری حساب شود
DUP_MIN_WORDS = 8        # پست‌های کوتاه‌تر از این (مثلا عکس بدون متن) بررسی نمی‌شوند
MAX_RECENT = 1500

STOPWORDS = set("""
و در به از که این آن را با است برای یک تا هم شد شده می ها های بر بود باشد کرد کرده
خود اما یا نیز اند ای ما من تو او شما آنها ایشان دیگر پس اگر چه چون هر همه وی
""".split())


def fingerprint(text: str):
    t = (text or "").lower()
    t = re.sub(r"https?://\S+|t\.me/\S+|@\w+|#\w+", " ", t)
    t = t.translate(str.maketrans("يكۀة‌", "یکهه "))      # یکسان‌سازی حروف عربی/فارسی و نیم‌فاصله
    t = t.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    words = re.findall(r"\w+", t)
    words = {w for w in words if len(w) > 1 and w not in STOPWORDS}
    return sorted({hashlib.md5(w.encode()).hexdigest()[:6] for w in words})


def find_duplicate(fp, state):
    if len(fp) < DUP_MIN_WORDS:
        return None
    a = set(fp)
    cutoff = time.time() - DUP_WINDOW_HOURS * 3600
    for r in state.get("recent", []):
        if r["t"] < cutoff or len(r["w"]) < DUP_MIN_WORDS:
            continue
        b = set(r["w"])
        if len(a & b) / min(len(a), len(b)) >= DUP_THRESHOLD:
            return r
    return None


def remember(fp, link, state):
    cutoff = time.time() - DUP_WINDOW_HOURS * 3600
    recent = [r for r in state.get("recent", []) if r["t"] >= cutoff]
    recent.append({"t": time.time(), "w": fp, "l": link})
    state["recent"] = recent[-MAX_RECENT:]


# ================================================================ PRICE POSTS
# روزی چند بار آخرین قیمت دلار از کانال PRICE_CHANNEL منتشر و بالای کانال پین می‌شود.
PRICE_CHANNEL = "DO_L4"
PRICE_TIMES = ["09:00", "12:00", "17:00", "21:00"]   # به وقت تهران
PRICE_LATE_MIN = 60        # اگر ربات دیر رسید، تا این چند دقیقه بعد از موعد هنوز ارسال کند
PRICE_MAX_AGE_MIN = 180    # قیمتی قدیمی‌تر از این منتشر نمی‌شود
PRICE_WORDS = ("دلار",)

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:
    from datetime import timezone, timedelta
    TEHRAN = timezone(timedelta(hours=3, minutes=30))

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def due_price_slot(state):
    """اگر وقت یکی از پست‌های قیمت رسیده و هنوز ارسال نشده، کلید آن را برمی‌گرداند."""
    now = datetime.now(TEHRAN)
    done = state.get("price_done", [])
    for hhmm in PRICE_TIMES:
        h, m = map(int, hhmm.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        key = slot.strftime("%Y-%m-%d ") + hhmm
        if key in done:
            continue
        late = (now - slot).total_seconds() / 60
        if 0 <= late <= PRICE_LATE_MIN:
            return key
    return None


def mark_price_done(state, key):
    state["price_done"] = (state.get("price_done", []) + [key])[-20:]


def pick_price(items):
    """جدیدترین پست قیمت دلار (بدون تبلیغ و نه خیلی قدیمی) را انتخاب می‌کند."""
    cutoff = time.time() - PRICE_MAX_AGE_MIN * 60
    good = [
        it for it in items
        if any(w in it.get("summary", "") for w in PRICE_WORDS)
        and re.search(r"[0-9۰-۹]{2,}", it.get("summary", ""))
        and not is_ad(it.get("summary", ""))
        and (it.get("ts") or 0) >= cutoff
    ]
    good.sort(key=lambda it: it.get("ts") or 0, reverse=True)
    return good[0] if good else None


def price_text(body_html: str, key: str) -> str:
    hhmm = key.split(" ")[1]
    parts = [f"💵 <b>قیمت دلار | ساعت {hhmm.translate(FA_DIGITS)}</b>", body_html]
    if MY_CHANNEL:
        parts.append(esc(MY_CHANNEL))
    return "\n\n".join(p for p in parts if p)


def simple_price_post(state, key) -> bool:
    items = fetch_telegram_channel({"channel": PRICE_CHANNEL, "limit": 20})
    it = pick_price(items)
    if not it:
        report_error("price", f"پست قیمت ساعت {key.split(' ')[1]} ارسال نشد: قیمت تازه‌ای در @{PRICE_CHANNEL} پیدا نشد.")
        return False
    msg = tg_call("sendMessage", {"chat_id": CHANNEL_ID, "text": price_text(esc(it["summary"]), key),
                                  "parse_mode": "HTML", "disable_web_page_preview": True})
    if not msg:
        report_error("price", "ارسال پست قیمت دلار ناموفق بود.")
        return False
    pin_new(state, msg["message_id"], pin=simple_pin,
            unpin=lambda mid: tg("unpinChatMessage", {"chat_id": CHANNEL_ID, "message_id": mid}))
    return True


def simple_pin(mid) -> bool:
    if not tg("pinChatMessage", {"chat_id": CHANNEL_ID, "message_id": mid, "disable_notification": True}):
        return False
    # تلگرام بعد از پین یک پیام خودکار «… پین شد» می‌گذارد که شماره‌اش درست بعد از پست است؛ پاکش می‌کنیم.
    # (ربات پست‌ها را یکی‌یکی می‌فرستد، پس پیام بعدی همان پیام خودکار است.)
    tg("deleteMessage", {"chat_id": CHANNEL_ID, "message_id": mid + 1})
    return True


def pin_new(state, new_id, pin, unpin):
    old = state.get("price_pinned")
    if not pin(new_id):
        report_error("pin", "پین کردن پست قیمت ناموفق بود؛ دسترسی Pin Messages ربات را در کانال چک کن.")
        return
    if old and old != new_id:
        unpin(old)
    state["price_pinned"] = new_id


async def run_price_if_due(price_post, state, seen):
    key = due_price_slot(state)
    if not key:
        return
    print(f"price slot {key}")
    try:
        ok = await price_post(state, key)
    except Exception as ex:
        ok = False
        report_error("price", f"پست قیمت خطا داد: {type(ex).__name__}: {str(ex)[:150]}")
    mark_price_done(state, key)   # موفق یا ناموفق، این نوبت دیگر تکرار نمی‌شود
    save_state(state, seen)
    print(f"price slot {key}: {'posted' if ok else 'FAILED'}")


# ================================================================ PACED LOOP
IDLE_CHECK_SEC = 60    # وقتی پست تازه‌ای نیست، هر چند ثانیه دوباره منابع چک شوند


async def paced_loop(collect, publish_one, state, seen, price_post=None):
    """
    تا RUN_MINUTES دقیقه کار می‌کند و هر POST_INTERVAL_MIN دقیقه فقط یک پست می‌فرستد.
    زمان آخرین ارسال در state ذخیره می‌شود تا فاصله بین اجراها هم رعایت شود.
    """
    import asyncio

    deadline = time.time() + RUN_MINUTES * 60
    interval = POST_INTERVAL_MIN * 60
    failed_ids = set()
    posted = failed = pending = 0

    while True:
        wait = state.get("last_post", 0) + interval - time.time()
        if wait > 0:
            if time.time() + wait > deadline:
                break
            print(f"waiting {int(wait)}s until next post")
            await asyncio.sleep(wait)

        if price_post:
            await run_price_if_due(price_post, state, seen)
        queue = [(it, s) for it, s in prepare_queue(await collect()) if it["id"] not in failed_ids]
        sent = False
        cycle_failed = 0
        for it, src in queue:
            fp = fingerprint(f"{it.get('title', '')} {it.get('summary', '')}")
            dup = find_duplicate(fp, state)
            if dup:
                print(f"duplicate skipped: {it['link']} (same as {dup.get('l')})")
                seen.add(it["id"])
                save_state(state, seen)
                continue
            if await publish_one(it, src):
                seen.add(it["id"])
                remember(fp, it["link"], state)
                state["last_post"] = time.time()
                save_state(state, seen)
                posted += 1
                sent = True
                print(f"posted {it['link']}")
                break
            failed_ids.add(it["id"])
            failed += 1
            cycle_failed += 1
        pending = max(0, len(queue) - (1 if sent else 0) - cycle_failed)
        print(f"queue: {len(queue)} waiting")

        if not sent:
            if time.time() + IDLE_CHECK_SEC > deadline:
                break
            await asyncio.sleep(IDLE_CHECK_SEC)

    if failed:
        report_error("publish", f"ارسال {failed} پست به کانال ناموفق بود (لاگ Actions را ببین).")
    return posted, pending


def skip_backlog(items, seen, state):
    """همه پست‌های در صف را بدون ارسال «دیده‌شده» می‌کند؛ از این به بعد فقط پست‌های تازه می‌روند."""
    for it, _ in items:
        seen.add(it["id"])
    save_state(state, seen)
    print(f"Backlog cleared: {len(items)} queued posts skipped without posting.")


def mark_first_run(items, seen):
    print("First run: marking existing items as seen without posting.")
    for it, _ in items:
        seen.add(it["id"])
    return 0, 0


async def mt_main(sources, seen, first_run, state):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(TG_SESSION), int(API_ID), API_HASH)
    client.parse_mode = "html"
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("TG_SESSION is invalid or expired. Generate a new one with gen_session.py")

        # یک بار لیست چت‌ها را می‌گیریم تا کانال‌هایی که حساب عضوشان است از حافظه شناسایی شوند
        # (جلوگیری از درخواست‌های مکرر جستجوی یوزرنیم که تلگرام برایشان محدودیت FloodWait می‌گذارد)
        await client.get_dialogs()
        target = await client.get_entity(parse_peer(CHANNEL_ID))

        async def collect():
            new_items = collect_basic(sources, seen, skip_telegram=True)
            for src in sources:
                if src.get("type") != "telegram":
                    continue
                try:
                    items = await mt_fetch(client, src)
                    print(f"[{src.get('name')}] fetched {len(items)}")
                except Exception as ex:
                    print(f"[{src.get('name')}] ERROR: {ex!r}")
                    report_error(f"src:{src.get('name')}",
                                 f"منبع {src.get('name')} خطا داد: {type(ex).__name__}: {str(ex)[:150]}")
                    continue
                new_items += [(it, src) for it in items if it["id"] not in seen]
            return new_items

        async def publish_one(it, src):
            return await mt_publish(client, target, it, src)

        async def price_post(state, key):
            it = pick_price(await mt_fetch(client, {"channel": PRICE_CHANNEL, "limit": 20}))
            if not it:
                report_error("price", f"پست قیمت ساعت {key.split(' ')[1]} ارسال نشد: قیمت تازه‌ای در @{PRICE_CHANNEL} پیدا نشد.")
                return False
            msg = await client.send_message(target, price_text(it.get("html") or esc(it["summary"]), key),
                                            parse_mode="html", link_preview=False)

            async def pin(mid):
                try:
                    service = await client.pin_message(target, mid, notify=False)
                    if service is not None and getattr(service, "action", None):
                        try:  # پاک کردن پیام خودکار «… پین شد»
                            await client.delete_messages(target, service.id)
                        except Exception as ex:
                            print(f"could not delete pin notice: {ex!r}")
                    return True
                except Exception as ex:
                    print(f"pin failed: {ex!r}")
                    return False

            async def unpin(mid):
                try:
                    await client.unpin_message(target, mid)
                except Exception as ex:
                    print(f"unpin failed: {ex!r}")

            old = state.get("price_pinned")
            if await pin(msg.id):
                if old and old != msg.id:
                    await unpin(old)
                state["price_pinned"] = msg.id
            else:
                report_error("pin", "پین کردن پست قیمت ناموفق بود؛ دسترسی Pin Messages را چک کن.")
            return True

        if first_run:
            return mark_first_run(await collect(), seen)
        if SKIP_BACKLOG:
            skip_backlog(await collect(), seen, state)
        return await paced_loop(collect, publish_one, state, seen, price_post)
    finally:
        await client.disconnect()


async def simple_main(sources, seen, first_run, state):
    async def collect():
        return collect_basic(sources, seen)

    async def publish_one(it, src):
        return publish(it, src)

    async def price_post(state, key):
        return simple_price_post(state, key)

    if first_run:
        return mark_first_run(await collect(), seen)
    if SKIP_BACKLOG:
        skip_backlog(await collect(), seen, state)
    return await paced_loop(collect, publish_one, state, seen, price_post)


# ---------------------------------------------------------------- main
def save_state(state, seen):
    ids = list(seen)  # ترتیب حفظ می‌شود: قدیمی‌ترین اول
    state["seen"] = ids[-MAX_STATE_ITEMS:] if len(ids) > MAX_STATE_ITEMS else ids
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    import asyncio

    if not CHANNEL_ID:
        sys.exit("CHANNEL_ID must be set.")
    if not mtproto_enabled() and not BOT_TOKEN:
        sys.exit("Set BOT_TOKEN (simple mode) or API_ID + API_HASH + TG_SESSION (full mode).")
    if not MY_CHANNEL:
        print("WARNING: MY_CHANNEL is empty (CHANNEL_ID is numeric). "
              "Set MY_CHANNEL=@your_channel so source channel names can be replaced.")

    sources = load_json(SOURCES_FILE, [])
    state = load_json(STATE_FILE, {"seen": []})
    seen = Seen.fromkeys(state.get("seen", []))
    first_run = not seen

    posted = pending = 0
    crash = None
    if TEST_ALERT:
        report_error(f"test:{int(time.time())}", "تست: سیستم هشدار کار می‌کند ✅")
    print(f"Pacing: 1 post every {POST_INTERVAL_MIN:g} min, running {RUN_MINUTES:g} min")
    try:
        if mtproto_enabled():
            print("Mode: full (MTProto)")
            posted, pending = asyncio.run(mt_main(sources, seen, first_run, state))
        else:
            print("Mode: simple (Bot API)")
            posted, pending = asyncio.run(simple_main(sources, seen, first_run, state))
    except BaseException as ex:  # شامل SystemExit (مثلا نشست نامعتبر)
        crash = ex
        report_error("crash", f"ربات متوقف شد: {type(ex).__name__}: {str(ex)[:200]}")
    finally:
        send_alerts(state)
        save_state(state, seen)
    print(f"Done. Posted {posted}, pending {pending}.")
    if crash is not None:
        raise crash


if __name__ == "__main__":
    main()
