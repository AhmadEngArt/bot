#!/usr/bin/env python3
"""
News -> Telegram channel bot.

Reads sources from sources.json (RSS feeds, plain websites, Telegram channels),
skips anything already posted (state.json) and publishes new items to your
channel.

Changes in this version:
  * Dollar price from @DO_L4 posted and pinned at 9, 12, 17, 21 Tehran time
  * The same story from several sources is posted only once
  * Posts go out one at a time (default: 1 post every 4 minutes, newest first), with a topic label
  * Nightly digest of the day's most important news at 22:00 Tehran
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

# امضای پایین هر پست: با لینک عضویت، تا هر کس پست فورواردشده را دید با یک لمس عضو شود
SIGN_TEXT = "روزانه ۲۴"
SIGN_SLOGAN = "خبر فوری، هر روز، هر ساعت"


def signature() -> str:
    if not MY_CHANNEL:
        return ""
    link = f"https://t.me/{MY_CHANNEL.lstrip('@')}"
    return f'📡 <a href="{link}"><b>{SIGN_TEXT}</b></a> | {SIGN_SLOGAN}'


def signature_len() -> int:
    """طول متن دیده‌شده‌ی امضا (برای محدودیت ۱۰۲۴ کاراکتری کپشن)."""
    return len(f"📡 {SIGN_TEXT} | {SIGN_SLOGAN}") if MY_CHANNEL else 0

# کلمات تبلیغاتی؛ هر پستی که شامل یکی از این‌ها باشد ارسال نمی‌شود
# (کلمه‌های عمومی مثل «پشتیبانی»، «ثبت‌نام»، «سفارش» و «فیلترشکن» عمدا حذف شده‌اند،
#  چون در خبرهای عادی زیاد می‌آیند و پست خبری را به اشتباه حذف می‌کردند)
AD_KEYWORDS = [
    "#تبلیغ", "#تبلیغات", "تبلیغات ویژه", "پست تبلیغاتی", "رزرو تبلیغ",
    "کد تخفیف", "تخفیف ویژه", "فروش ویژه", "خرید اینترنتی", "شارژ ایرانسل",
    "vpn", "وی پی ان", "وی‌پی‌ان", "ویپی ان", "ویپی‌ان", "ویپیان",   # هر پستی با کلمه VPN
    "خرید فیلترشکن", "فروش فیلترشکن", "فیلترشکن رایگان", "سرور v2ray", "خرید کانفیگ",
    "شرط بندی", "شرط‌بندی", "کازینو", "سایت شرط", "بت فوروارد", "zomabet", "زومابت", "بونوس", "شارژ حساب",
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
POST_INTERVAL_MIN = float(os.environ.get("POST_INTERVAL_MIN") or 4)
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


_DECOR = r"[^\w.!?،؛…»)\]\n<>@/]*"     # ایموجی، فاصله و علامت‌های تزئینی کنار آیدی (نه حرف و نه پایان جمله)
_MENTION = r"(?:(?:https?://)?(?:t\.me|telegram\.me)/[\w/+\-?=]+|(?<![\w.])@\s?[A-Za-z]\w{3,})"


def replace_channels(text: str) -> str:
    """
    آیدی و لینک t.me کانال‌های مبدا را کامل حذف می‌کند (همراه ایموجی‌های کنارش، مثل «📡 @VahidOnline»
    یا «@ VahidOOnLine»)؛ امضای کانال خودت جداگانه پایین پست می‌آید. لینک سایت‌ها دست‌نخورده می‌ماند.
    """
    if not text:
        return text
    # لینک‌های HTML به تلگرام (در حالت کامل) با متنشان حذف شوند
    text = re.sub(r'<a [^>]*href="(?:https?://)?(?:t\.me|telegram\.me)/[^"]*"[^>]*>.*?</a>', " ", text, flags=re.S)
    for _ in range(5):   # چند آیدی پشت سر هم
        new = re.sub(_DECOR + _MENTION + _DECOR, " ", text)
        if new == text:
            break
        text = new
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([،.!?؛:])", r"\1", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# نشانه‌های تبلیغ‌های شبکه‌های تبلیغاتی تلگرام
AD_PATTERNS = [
    re.compile(r"#(?!(?:13|14|19|20)\d\d(?!\d))\d{4,7}(?!\d)"),   # کد تبلیغ مثل #28083 (سال‌ها مثل #1401 نه)
    re.compile(r"(?:ثبت[ \u200c]?نام|دانلود|نصب|خرید) کن(?:ه)?(?![\w\u200c])"),
    re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)"),          # لینک دعوت کانال/گروه = تبلیغ کانال   # «ثبت‌نام کن/کنه»، نه «کنند»
    re.compile(r"(?:https?://)?(?:www\.)?(?:bo0\.ir|yun\.ir|bit\.ly|b2n\.ir|l1nk\.ir|zaya\.io|"
               r"tinyurl\.com|cutt\.ly|rebrand\.ly|shorturl\.at|t\.co|goo\.gl|is\.gd|s\.id|ln\.run|"
               r"yek\.link|ylnk\.ir|tapsell|yektanet)", re.I),   # لینک کوتاه و شبکه‌های تبلیغاتی
]
AD_PHRASES = [
    "اطلاعات بیشتر در لینک", "لینک زیر", "روی لینک بزن", "همین حالا ثبت", "شانست رو", "شانس خود را",
    "جایزه‌دار", "جایزه دار", "قرعه‌کشی بزرگ", "قرعه کشی بزرگ", "برنده‌ی جایزه", "برنده جایزه",
    "همین الان بخر", "سفارش بده",
    "رایگان دریافت", "هدیه بگیر", "کد معرف",
]


# امتیاز تجاری: هر نشانه امتیاز دارد؛ خبر معمولا ۱-۲ امتیاز دارد، تبلیغ چندتا با هم
AD_SCORE_WORDS = {
    "کلیک کن": 2, "همین حالا": 2, "همین الان": 1, "پیشنهاد ویژه": 2, "شرایط ویژه": 2, "فرصت محدود": 2,
    "فقط امروز": 2, "هدیه": 1, "هدیه جذاب": 2, "جایزه": 1, "رایگان": 1, "مشاوره": 1, "اشتراک": 1,
    "پکیج": 1, "خرید": 1, "با خرید": 2, "برای خرید": 1, "ثبت نام": 1, "ثبت‌نام": 1, "دعوت از دوستان": 2,
    "معرفی دوستان": 2, "پاداش": 1, "تومان پاداش": 2, "سود تضمینی": 2, "کسب‌وکار شما": 2, "کسب و کار شما": 2,
    "انتخاب با شما": 2, "خودت انتخاب کن": 2, "دریافت جزئیات": 2, "اطلاعات بیشتر": 1, "تماس بگیرید": 1,
    "سفارش": 1, "تخفیف": 1, "٪ تخفیف": 2, "% تخفیف": 2, "اپلیکیشن": 1, "نصب": 1, "دانلود": 1,
    "سایت": 1, "عضو شو": 2, "لینک": 1, "👇": 1, "🎁": 2, "🔥": 1,
    # تبلیغ‌های داستانی کانال‌ها («…آموزشا اینجاست»، «عاشقش میشی»)
    "اینجاست": 3, "آموزشا": 2, "آموزش‌ها اینجا": 3, "عاشقش میشی": 2, "امتحانش کن": 2, "رسپی": 1,
    "یاد گرفتم": 1, "از کجا یاد گرفتی": 2, "پیجش": 2, "پیجشون": 2,
}
AD_SCORE_LIMIT = 5
REFERRAL_URL = re.compile(r"https?://\S+[?&](?:r|ref|referral|invite|inviter|utm_[a-z]+|aff|code)=", re.I)


def ad_score(t: str) -> int:
    score = sum(v for k, v in AD_SCORE_WORDS.items() if k in t)
    if REFERRAL_URL.search(t):
        score += 4
    # آگهی کسب‌وکار: شماره موبایل، آدرس، لوکیشن، لینک پیام‌رسان‌های دیگر
    if re.search(r"(?<!\d)(?:\+98|0098|0)9\d{9}(?!\d)", t.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(" ", "").replace("-", "")):
        score += 3
    score += 2 if re.search(r"شماره تماس|تلفن تماس|جهت تماس|تماس:|آدرس:|نشانی:|لوکیشن", t) else 0
    score += 2 if re.search(r"nshn\.ir|neshan\.org|balad\.ir|maps\.google|goo\.gl/maps", t) else 0
    score += 3 if re.search(r"eitaa\.com|ble\.ir|rubika\.ir|splus\.ir|igap\.net", t) else 0
    score += 1 if re.search(r"اجاره|رزرو|ارسال رایگان|پرداخت در محل|واتساپ|دایرکت", t) else 0
    if re.search(r"https?://(?!t\.me)\S+", t):   # لینک به سایت بیرونی
        score += 1
    # تبلیغ کانال دیگر: «این کانال/کاناله/کانالش» + آیدی یا لینک کانال
    score += 3 if re.search(r"این کانال|کاناله(?![\w])", t) else 0
    score += 2 if re.search(r"کانالش|کانالا(?![\w])|کانال‌ها رو|عضو کانال", t) else 0
    if re.search(r"(?:t\.me|telegram\.me)/[A-Za-z]\w{3,}(?!/\d)", t):   # لینک به یک کانال (نه یک پست)
        score += 2
    if re.search(r"(?<![\w.])@\s?[A-Za-z]\w{3,}", t):
        score += 1
    return score


def is_ad(text: str) -> bool:
    t = (text or "").lower()
    if any(k in t for k in AD_KEYWORDS) or any(k in t for k in AD_PHRASES):
        return True
    if any(p.search(t) for p in AD_PATTERNS):
        return True
    return ad_score(t) >= AD_SCORE_LIMIT


PRICE_TICKER_WORDS = ("دلار", "یورو", "طلا", "سکه", "تتر", "usdt", "آبشده", "مثقال", "درهم", "بیت کوین")


def is_price_ticker(text: str) -> bool:
    """
    پست‌های جدول قیمت (دلار/طلا/تتر با چند عدد بزرگ و خرید/فروش) از منبع‌های خبری رد می‌شوند؛
    قیمت فقط سر ساعت‌های مشخص از PRICE_CHANNEL می‌آید. خبر عادی درباره دلار رد نمی‌شود.
    """
    t = (text or "").lower().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    words = sum(1 for w in PRICE_TICKER_WORDS if w in t)
    big_numbers = len(re.findall(r"\d{1,3}(?:[,،٬]\d{3})+|\d{5,}", t))
    buy_sell = ("خرید" in t and "فروش" in t) or "معامله" in t
    return words >= 1 and big_numbers >= 2 and (buy_sell or words >= 2)


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


FAIL_COUNTS = {}
SOURCE_FAIL_ALERT = 10   # منبع فقط وقتی هشدار می‌دهد که چند بار پشت سر هم خطا بدهد (خطای لحظه‌ای نه)


def report_error(key: str, msg: str):
    ERRORS[key] = msg[:300]
    print(f"ALERT: {msg[:300]}")


def source_failed(key: str, msg: str):
    FAIL_COUNTS[key] = FAIL_COUNTS.get(key, 0) + 1
    print(f"{key} failed ({FAIL_COUNTS[key]} in a row): {msg[:150]}")
    if FAIL_COUNTS[key] >= SOURCE_FAIL_ALERT:
        report_error(key, f"{msg} ({FAIL_COUNTS[key]} بار پشت سر هم)")


def source_ok(key: str):
    FAIL_COUNTS.pop(key, None)


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


def has_missing_media(msg, media) -> bool:
    """
    آیا پست ویدیویی دارد که صفحه‌ی وب فایلش را نمی‌دهد (معمولا ویدیوی حجیم)؟
    فقط وقتی true است که یک پخش‌کننده‌ی ویدیو هیچ <video src> داخلش نداشته باشد.
    (بلوک «media not supported» در همه‌ی ویدیوها به‌صورت پنهان هست و نشانه‌ی مشکل نیست.)
    """
    for player in msg.select("a.tgme_widget_message_video_player"):
        if not player.select_one("video[src]"):
            return True
    return False


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
        if has_missing_media(msg, media):
            # ویدیو/فایلی که صفحه‌ی وب تلگرام لینک دانلودش را نمی‌دهد (معمولا ویدیوی حجیم):
            # پست بدون ویدیو ناقص به نظر می‌رسد، پس منتشر نمی‌شود (در حالت کامل منتقل می‌شود)
            print(f"skipped (media not downloadable): https://t.me/{post}")
            continue
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
            source_ok(f"src:{src.get('name')}")
        except Exception as ex:
            print(f"[{src.get('name')}] ERROR: {ex}")
            source_failed(f"src:{src.get('name')}",
                         f"منبع {src.get('name')} خطا داد: {type(ex).__name__}: {str(ex)[:150]}")
            continue
        for it in items:
            if is_ad(f"{it['title']} {it['summary']}"):
                continue
            if is_price_ticker(f"{it['title']} {it['summary']}"):
                continue
            if it["id"] not in seen:
                new.append((it, src))
    return new


# ---------------------------------------------------------------- topic labels
# برچسب موضوعی اول هر پست؛ برچسب پیش‌فرض از "tag" در sources.json، و برای خبرها
# اگر متن درباره‌ی ورزش یا اقتصاد بود، برچسب همان موضوع
SPORT_WORDS = ("فوتبال", "پرسپولیس", "استقلال", "سپاهان", "تراکتور", "لیگ", "جام جهانی", "تیم ملی",
               "سرمربی", "بازیکن", "دربی", "والیبال", "کشتی", "المپیک", "فیفا", "ای‌اف‌سی", "گل ")
ECON_WORDS = ("دلار", "بورس", "تورم", "طلا", "سکه", "بانک مرکزی", "یارانه", "قیمت", "ارز", "بنزین",
              "اقتصاد", "حقوق", "مالیات", "تومان")
NEWSY_TAGS = ("🔴 خبر", "🌐 خارجی", "🔥 داغ")


def tag_for(item, src) -> str:
    tag = (src or {}).get("tag", "")
    if not tag:
        return ""
    if tag in NEWSY_TAGS:
        t = f"{item.get('title', '')} {item.get('summary', '')}"
        if sum(1 for w in SPORT_WORDS if w in t) >= 2:
            return "⚽ ورزش"
        if sum(1 for w in ECON_WORDS if w in t) >= 2:
            return "💰 اقتصاد"
    return tag


def format_message(item, src) -> str:
    parts = []
    tag = tag_for(item, src)
    if tag:
        parts.append(f"<b>{esc(tag)}</b>")
    if item["title"]:
        parts.append(f"<b>{esc(replace_channels(item['title']))}</b>")
    if item["summary"]:
        parts.append(esc(replace_channels(item["summary"])))
    if item.get("from_telegram"):
        # پست تلگرامی: فقط اسم کانال خودت
        if MY_CHANNEL:
            parts.append(signature())
    else:
        # RSS و سایت: لینک منبع خبر حفظ می‌شود
        source_name = esc(src.get("name", "منبع"))
        parts.append(f'🔗 <a href="{html.escape(item["link"], quote=True)}">{source_name}</a>')
        if MY_CHANNEL:
            parts.append(signature())
    footer = os.environ.get("CHANNEL_FOOTER", "").strip()
    if footer:
        parts.append(esc(footer))
    return "\n\n".join(parts)[:4000]


# ================================================================ SIMPLE MODE (Bot API)
LAST_SENT_ID = None   # شماره‌ی اولین پیامِ آخرین پست (برای لینک در خلاصه‌ی شبانه)
LAST_WAS_GROUP = False


# ---------------------------------------------------------------- share button
# دکمه‌ی «ارسال برای دوستان» زیر هر پست: با یک لمس، پست برای دوست یا گروه فرستاده می‌شود
SHARE_TEXT = "📢 ارسال برای دوستان"
SHARE_BUTTON = False   # کاربر خواست دکمه زیر پست‌ها نباشد


def share_markup(post_id=None) -> dict:
    from urllib.parse import quote
    handle = MY_CHANNEL.lstrip("@")
    url = f"https://t.me/{handle}/{post_id}" if post_id else f"https://t.me/{handle}"
    share = f"https://t.me/share/url?url={quote(url, safe='')}&text={quote(SIGN_TEXT + ' | ' + SIGN_SLOGAN)}"
    return {"inline_keyboard": [[{"text": SHARE_TEXT, "url": share}]]}


def remove_old_share_buttons(state):
    """یک بار: دکمه‌ی «ارسال برای دوستان» را از پست‌هایی که قبلا گرفته بودند برمی‌دارد."""
    if SHARE_BUTTON or state.get("share_buttons_removed"):
        return
    ids = [p["m"] for p in state.get("site_posts", [])]
    if ids:
        ids = list(range(545, max(ids) + 1))   # دکمه از پست ۵۴۵ اضافه شده بود
    if state.get("price_pinned"):
        ids.append(state["price_pinned"])
    removed = 0
    for mid in sorted(set(ids)):
        if tg("editMessageReplyMarkup", {"chat_id": CHANNEL_ID, "message_id": mid,
                                         "reply_markup": {"inline_keyboard": []}}):
            removed += 1
        time.sleep(1)
    state["share_buttons_removed"] = True
    print(f"share buttons removed from {removed} posts")


def add_share_button(message_id):
    """دکمه بعد از ارسال اضافه می‌شود تا لینک همان پست را داشته باشد (آلبوم‌ها دکمه نمی‌گیرند)."""
    if not SHARE_BUTTON or not MY_CHANNEL or not message_id:
        return
    tg("editMessageReplyMarkup", {"chat_id": CHANNEL_ID, "message_id": message_id,
                                   "reply_markup": share_markup(message_id)})


def tg_call(method: str, payload: dict, files=None):
    """درخواست به Bot API؛ در صورت موفقیت مقدار result و در غیر این صورت None برمی‌گرداند."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(4):
        try:
            if files:
                data = {k: (v if isinstance(v, (str, int)) else json.dumps(v)) for k, v in payload.items()}
                r = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
            else:
                r = requests.post(url, json=payload, timeout=TIMEOUT)
        except requests.exceptions.ReadTimeout:
            # ممکن است تلگرام پیام را گرفته باشد؛ دوباره نمی‌فرستیم تا پست تکراری نشود
            # تلگرام معمولا پیام را گرفته؛ «ارسال‌شده» حساب می‌کنیم تا در اجرای بعد تکرار نشود
            print(f"telegram timeout ({method}); assuming it was delivered")
            return {"timeout": True}
        except requests.exceptions.RequestException as ex:
            # قطعی لحظه‌ای اتصال (مثل Connection reset): کمی صبر و تلاش دوباره
            wait = 5 * (attempt + 1)
            print(f"telegram connection error ({method}): {type(ex).__name__}; retry in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 200:
            res = r.json().get("result", True)
            global LAST_SENT_ID
            global LAST_WAS_GROUP
            if isinstance(res, dict) and res.get("message_id"):
                if not LAST_SENT_ID:
                    LAST_SENT_ID, LAST_WAS_GROUP = res["message_id"], False
            elif isinstance(res, list) and res and isinstance(res[0], dict):
                if not LAST_SENT_ID:
                    LAST_SENT_ID, LAST_WAS_GROUP = res[0].get("message_id"), True
            return res
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
        parts.append(signature())
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
        msg = await client.send_message(target, text, parse_mode="html", link_preview=preview)
        global LAST_SENT_ID
        LAST_SENT_ID = LAST_SENT_ID or getattr(msg, "id", None)
        return True
    except Exception as ex:
        print(f"send_message failed: {ex!r}")
        return False


async def mt_send_media(client, target, msgs, caption) -> bool:
    kw = {"caption": caption, "parse_mode": "html"} if caption else {}
    objs = [m.media for m in msgs]
    try:  # ارسال مستقیم با ارجاع به فایل؛ بدون دانلود و بدون محدودیت حجم
        sent = await client.send_file(target, objs if len(objs) > 1 else objs[0], **kw)
        global LAST_SENT_ID
        first = sent[0] if isinstance(sent, list) and sent else sent
        LAST_SENT_ID = LAST_SENT_ID or getattr(first, "id", None)
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
    tag = tag_for(it, src)
    if tag:
        body = f"<b>{esc(tag)}</b>\n\n{body}"
        raw = f"{tag}\n\n{raw}"
    full = "\n\n".join(p for p in (body, foot) if p)
    plain_len = len(raw) + signature_len() + len(os.environ.get("CHANNEL_FOOTER", "")) + 8
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
DUP_MIN_SHARED = 8       # و دست‌کم این تعداد کلمه‌ی مشترک (تا دو خبر متفاوت هم‌موضوع تکراری حساب نشوند)
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
        shared = len(a & b)
        if shared >= DUP_MIN_SHARED and shared / min(len(a), len(b)) >= DUP_THRESHOLD:
            return r
    return None


def remember(fp, link, state, key=""):
    cutoff = time.time() - DUP_WINDOW_HOURS * 3600
    recent = [r for r in state.get("recent", []) if r["t"] >= cutoff]
    recent.append({"t": time.time(), "w": fp, "l": link, "k": key, "h": 1})
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
        parts.append(signature())
    return "\n\n".join(p for p in parts if p)


def simple_price_post(state, key) -> bool:
    items = fetch_telegram_channel({"channel": PRICE_CHANNEL, "limit": 20})
    it = pick_price(items)
    if not it:
        report_error("price", f"پست قیمت ساعت {key.split(' ')[1]} ارسال نشد: قیمت تازه‌ای در @{PRICE_CHANNEL} پیدا نشد.")
        return False
    msg = tg_call("sendMessage", {"chat_id": CHANNEL_ID, "text": price_text(esc(it["summary"]), key),
                                  "parse_mode": "HTML", "disable_web_page_preview": True,
                                  **({"reply_markup": share_markup()} if SHARE_BUTTON else {})})
    if not msg:
        report_error("price", "ارسال پست قیمت دلار ناموفق بود.")
        return False
    if not msg.get("message_id"):      # پاسخ تلگرام نرسید؛ پست رفته ولی شماره‌اش معلوم نیست
        return True
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


# ================================================================ NIGHTLY DIGEST
# هر شب ساعت DIGEST_TIME (تهران) «مهم‌ترین خبرهای امروز»: خبرهایی که منابع بیشتری منتشرشان کرده‌اند
DIGEST_TIME = "22:00"
DIGEST_COUNT = 10
DIGEST_MIN_ITEMS = 8          # اگر خبر کافی نبود، خلاصه منتشر نمی‌شود
DIGEST_TAGS = ("🔴 خبر", "🌐 خارجی", "💰 اقتصاد", "⚽ ورزش", "🔥 داغ", "🚗 خودرو", "👀 حاشیه")


def snippet(text: str, words: int = 14) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    w = t.split(" ")
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


def log_news(state, it, src):
    """هر خبرِ منتشرشده با شماره‌ی پیامش در کانال ثبت می‌شود (۲۴ ساعت نگه داشته می‌شود)."""
    tag = tag_for(it, src)
    if tag not in DIGEST_TAGS or not LAST_SENT_ID:
        return
    log = state.setdefault("news_log", {})
    cutoff = time.time() - 26 * 3600
    for k in [k for k, v in log.items() if v["t"] < cutoff]:
        del log[k]
    log[it["id"]] = {"t": time.time(), "m": LAST_SENT_ID, "s": snippet(it.get("title") or it.get("summary")),
                     "g": tag, "h": 1}


def jalali(d):
    """تاریخ میلادی به شمسی (الگوریتم استاندارد)."""
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm = 1 + days // 31 if days < 186 else 7 + (days - 186) // 30
    jd = 1 + (days % 31 if days < 186 else (days - 186) % 30)
    return jy, jm, jd


FA_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def digest_due(state):
    now = datetime.now(TEHRAN)
    h, m = map(int, DIGEST_TIME.split(":"))
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    key = slot.strftime("%Y-%m-%d")
    late = (now - slot).total_seconds() / 60
    if state.get("digest_done") == key or not (0 <= late <= 90):
        return None
    return key


def build_digest(state):
    now = datetime.now(TEHRAN)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    items = [v for v in state.get("news_log", {}).values() if v["t"] >= start]
    if len(items) < DIGEST_MIN_ITEMS:
        return None
    items.sort(key=lambda v: (v.get("h", 1), v["t"]), reverse=True)   # پرتکرارترین در منابع، بعد جدیدترین
    top = sorted(items[:DIGEST_COUNT], key=lambda v: v["t"])            # به ترتیب زمان
    link = f"https://t.me/{MY_CHANNEL.lstrip('@')}"
    jy, jm, jd = jalali(now.date())
    date_fa = f"{jd} {FA_MONTHS[jm - 1]}".translate(FA_DIGITS)
    lines = [f"📋 <b>مهم‌ترین خبرهای امروز</b> | {date_fa}", ""]
    for i, v in enumerate(top, 1):
        lines.append(f"{str(i).translate(FA_DIGITS)}. {v['g'].split(' ')[0]} "
                     f'<a href="{link}/{v["m"]}">{esc(v["s"])}</a>')
    lines += ["", signature()]
    return "\n".join(lines)


async def run_digest_if_due(send_text_fn, state, seen):
    key = digest_due(state)
    if not key:
        return
    text = build_digest(state)
    state["digest_done"] = key
    if not text:
        print("digest skipped: not enough news today")
    else:
        try:
            ok = await send_text_fn(text)
        except Exception as ex:
            ok = False
            print(f"digest failed: {ex!r}")
        if not ok:
            report_error("digest", "ارسال خلاصه‌ی شبانه ناموفق بود.")
        print(f"digest {key}: {'posted' if ok else 'FAILED'}")
    save_state(state, seen)


# ================================================================ WEBSITE (GitHub Pages)
# هر اجرا صفحه‌ی docs/index.html را با آخرین پست‌ها می‌سازد؛ گیتهاب آن را رایگان منتشر می‌کند
# (Settings → Pages → Branch: main، پوشه /docs) و گوگل ایندکسش می‌کند.
SITE_DIR = BASE / "docs"
SITE_POSTS = 60


def site_add(state, it, src):
    if not LAST_SENT_ID:
        return
    posts = state.setdefault("site_posts", [])
    text = re.sub(r"\s+", " ", f"{it.get('title', '')} {it.get('summary', '')}").strip()
    posts.append({"t": time.time(), "m": LAST_SENT_ID, "g": tag_for(it, src),
                  "s": text[:420] + ("…" if len(text) > 420 else ""),
                  "v": bool(it.get("media"))})
    state["site_posts"] = posts[-SITE_POSTS:]


def write_site(state):
    try:
        handle = MY_CHANNEL.lstrip("@")
        link = f"https://t.me/{handle}"
        cards = []
        for p in reversed(state.get("site_posts", [])):
            when = datetime.fromtimestamp(p["t"], TEHRAN).strftime("%H:%M").translate(FA_DIGITS)
            media = '<span class="m">🎬 تصویر/ویدیو در کانال</span>' if p.get("v") else ""
            cards.append(
                f'<article><div class="h"><span class="g">{esc(p.get("g") or "📰")}</span>'
                f'<time>{when}</time></div><p>{esc(p["s"])}</p>{media}'
                f'<a class="r" href="{link}/{p["m"]}" rel="noopener">ادامه در تلگرام ←</a></article>')
        jy, jm, jd = jalali(datetime.now(TEHRAN).date())
        date_fa = f"{jd} {FA_MONTHS[jm - 1]} {jy}".translate(FA_DIGITS)
        page = SITE_TEMPLATE.format(link=link, handle=handle, date=date_fa, cards="\n".join(cards),
                                    title=f"{SIGN_TEXT} | {SIGN_SLOGAN}")
        SITE_DIR.mkdir(exist_ok=True)
        (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
        (SITE_DIR / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    except Exception as ex:
        print(f"site write failed: {ex!r}")


SITE_TEMPLATE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — آخرین خبرهای امروز</title>
<meta name="description" content="آخرین خبرهای فوری ایران و جهان، ورزش، اقتصاد و قیمت دلار؛ هر روز، هر ساعت در کانال تلگرام روزانه ۲۴.">
<meta property="og:title" content="{title}">
<meta property="og:description" content="خبر فوری ایران و جهان + قیمت دلار ۴ بار در روز. عضو کانال تلگرام شو.">
<meta property="og:type" content="website">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;900&display=swap">
<style>
:root{{--bg:#F4F6F9;--card:#fff;--ink:#111827;--sub:#4B5563;--acc:#E3141B;--nav:#0A1A3F;--y:#FFD400}}
*{{box-sizing:border-box}}body{{margin:0;font-family:Vazirmatn,Tahoma,sans-serif;background:var(--bg);color:var(--ink);line-height:1.9}}
header{{background:var(--nav);color:#fff;padding:28px 16px 24px;text-align:center}}
.logo{{display:inline-flex;width:72px;height:72px;border-radius:50%;background:var(--y);color:#111;font-weight:900;font-size:34px;align-items:center;justify-content:center}}
h1{{margin:10px 0 4px;font-size:26px;font-weight:900}}header p{{margin:0;color:#CBD5E1}}
.join{{display:inline-block;margin-top:16px;background:var(--acc);color:#fff;text-decoration:none;font-weight:900;font-size:18px;padding:12px 28px;border-radius:999px}}
.join:hover{{background:#B70F15}}
main{{max-width:720px;margin:0 auto;padding:16px}}
.d{{color:var(--sub);font-size:14px;margin:4px 4px 12px}}
article{{background:var(--card);border-radius:14px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.h{{display:flex;justify-content:space-between;color:var(--sub);font-size:13px}}.g{{font-weight:700;color:var(--ink)}}
article p{{margin:6px 0 8px;font-size:16px}}.m{{display:block;color:var(--sub);font-size:13px;margin-bottom:4px}}
.r{{color:#1D4ED8;text-decoration:none;font-weight:700;font-size:14px}}.r:hover{{text-decoration:underline}}
.cta{{text-align:center;background:var(--card);border-radius:14px;padding:20px;margin:20px 0}}
footer{{text-align:center;color:var(--sub);font-size:13px;padding:24px}}
</style>
</head>
<body>
<header>
<div class="logo" aria-hidden="true">۲۴</div>
<h1>روزانه ۲۴</h1>
<p>خبر فوری ایران و جهان · ورزش · اقتصاد · قیمت دلار ۴ بار در روز</p>
<a class="join" href="{link}">📡 عضویت در کانال تلگرام</a>
</header>
<main>
<div class="d">آخرین خبرها · به‌روزرسانی: {date}</div>
{cards}
<div class="cta"><b>همه‌ی خبرها، زودتر از همه، در تلگرام</b><br><a class="join" href="{link}">عضویت در <bdi dir="ltr">@{handle}</bdi></a></div>
</main>
<footer>روزانه ۲۴ · <a href="{link}" dir="ltr">t.me/{handle}</a></footer>
</body>
</html>
"""


# ================================================================ PACED LOOP
IDLE_CHECK_SEC = 60    # وقتی پست تازه‌ای نیست، هر چند ثانیه دوباره منابع چک شوند


async def paced_loop(collect, publish_one, state, seen, price_post=None, digest_send=None):
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
        if digest_send:
            await run_digest_if_due(digest_send, state, seen)
        try:
            collected = await collect()
            source_ok("collect")
        except Exception as ex:   # خطای لحظه‌ای شبکه نباید کل اجرا را متوقف کند
            print(f"collect failed: {ex!r}")
            source_failed("collect", f"خواندن منابع خطا داد: {type(ex).__name__}")
            collected = []
        queue = [(it, s) for it, s in prepare_queue(collected) if it["id"] not in failed_ids]
        sent = False
        cycle_failed = 0
        for it, src in queue:
            fp = fingerprint(f"{it.get('title', '')} {it.get('summary', '')}")
            dup = find_duplicate(fp, state)
            if dup:
                dup["h"] = dup.get("h", 1) + 1          # چند منبع همین خبر را داشتند (برای خلاصه‌ی شبانه)
                log = state.get("news_log", {}).get(dup.get("k", ""))
                if log:
                    log["h"] = dup["h"]
                print(f"duplicate skipped: {it['link']} (same as {dup.get('l')})")
                seen.add(it["id"])
                save_state(state, seen)
                continue
            try:
                ok = await publish_one(it, src)
            except Exception as ex:
                print(f"publish failed: {ex!r}")
                ok = False
            if ok:
                seen.add(it["id"])
                remember(fp, it["link"], state, key=it["id"])
                log_news(state, it, src)
                site_add(state, it, src)
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
                    source_ok(f"src:{src.get('name')}")
                except Exception as ex:
                    print(f"[{src.get('name')}] ERROR: {ex!r}")
                    source_failed(f"src:{src.get('name')}",
                                 f"منبع {src.get('name')} خطا داد: {type(ex).__name__}: {str(ex)[:150]}")
                    continue
                new_items += [(it, src) for it in items
                              if it["id"] not in seen and not is_price_ticker(it.get("summary", ""))]
            return new_items

        async def publish_one(it, src):
            global LAST_SENT_ID
            LAST_SENT_ID = None
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

        async def digest_send(text):
            await client.send_message(target, text, parse_mode="html", link_preview=False)
            return True

        if first_run:
            return mark_first_run(await collect(), seen)
        if SKIP_BACKLOG:
            skip_backlog(await collect(), seen, state)
        return await paced_loop(collect, publish_one, state, seen, price_post, digest_send)
    finally:
        await client.disconnect()


async def simple_main(sources, seen, first_run, state):
    async def collect():
        return collect_basic(sources, seen)

    async def publish_one(it, src):
        global LAST_SENT_ID
        LAST_SENT_ID = None
        ok = publish(it, src)
        if ok and LAST_SENT_ID and not LAST_WAS_GROUP:
            add_share_button(LAST_SENT_ID)
        return ok

    async def price_post(state, key):
        return simple_price_post(state, key)

    async def digest_send(text):
        return tg("sendMessage", {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
                                  "disable_web_page_preview": True,
                                  **({"reply_markup": share_markup()} if SHARE_BUTTON else {})})

    if first_run:
        return mark_first_run(await collect(), seen)
    remove_old_share_buttons(state)
    if SKIP_BACKLOG:
        skip_backlog(await collect(), seen, state)
    return await paced_loop(collect, publish_one, state, seen, price_post, digest_send)


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
        write_site(state)
    print(f"Done. Posted {posted}, pending {pending}.")
    if crash is not None:
        raise crash


if __name__ == "__main__":
    main()
