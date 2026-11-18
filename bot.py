#!/usr/bin/env python3
"""
News -> Telegram channel bot.

Reads sources from sources.json (RSS feeds, plain websites, public Telegram
channels), skips anything already posted (state.json) and publishes new items
to your channel.

Env vars (set as GitHub Secrets):
  BOT_TOKEN   - token from @BotFather
  CHANNEL_ID  - e.g. @my_channel  (or -100123456789)
"""
import hashlib
import html
import json
import os
import re
import sys
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

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
TIMEOUT = 25
MAX_POSTS_PER_RUN = 15      # سقف ارسال در هر اجرا (جلوگیری از اسپم و محدودیت تلگرام)
MAX_STATE_ITEMS = 5000      # تعداد آیتم‌های ذخیره‌شده برای جلوگیری از تکرار
SEND_DELAY = 3              # ثانیه بین ارسال‌ها


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


def fetch_telegram_channel(src):
    """Public channels only, via the t.me/s/<name> web preview."""
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
        text_el = msg.select_one(".tgme_widget_message_text")
        if not post or not text_el:
            continue
        text = clean(text_el.get_text(" ", strip=True), 700)
        if len(text) < 20:
            continue
        items.append(
            {
                "id": item_id(post),
                "title": "",
                "summary": text,
                "link": f"https://t.me/{post}",
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


# ---------------------------------------------------------------- telegram
def esc(s: str) -> str:
    return html.escape(s, quote=False)


def format_message(item, src) -> str:
    parts = []
    if item["title"]:
        parts.append(f"<b>{esc(item['title'])}</b>")
    if item["summary"]:
        parts.append(esc(item["summary"]))
    source_name = esc(src.get("name", "منبع"))
    parts.append(f'🔗 <a href="{html.escape(item["link"], quote=True)}">{source_name}</a>')
    footer = os.environ.get("CHANNEL_FOOTER", "").strip()
    if footer:
        parts.append(esc(footer))
    return "\n\n".join(parts)[:4000]


def send(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for attempt in range(3):
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            print(f"rate limited, sleeping {wait}s")
            time.sleep(wait + 1)
            continue
        print(f"telegram error {r.status_code}: {r.text[:200]}")
        return False
    return False


# ---------------------------------------------------------------- main
def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        sys.exit("BOT_TOKEN and CHANNEL_ID must be set.")

    sources = load_json(SOURCES_FILE, [])
    state = load_json(STATE_FILE, {"seen": []})
    seen = set(state["seen"])
    first_run = not state["seen"]

    new_items = []
    for src in sources:
        fetcher = FETCHERS.get(src.get("type"))
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
            if it["id"] not in seen:
                new_items.append((it, src))

    if first_run:
        # اولین اجرا: همه‌ی آیتم‌های فعلی را فقط «دیده‌شده» علامت می‌زنیم
        # تا کانال با خبرهای قدیمی پر نشود.
        print("First run: marking existing items as seen without posting.")
        for it, _ in new_items:
            seen.add(it["id"])
        new_items = []

    posted = 0
    for it, src in reversed(new_items):  # قدیمی‌تر اول
        if posted >= MAX_POSTS_PER_RUN:
            break  # بقیه در اجرای بعدی ارسال می‌شوند
        if send(format_message(it, src)):
            seen.add(it["id"])
            posted += 1
            time.sleep(SEND_DELAY)

    state["seen"] = list(seen)[-MAX_STATE_ITEMS:] if len(seen) > MAX_STATE_ITEMS else list(seen)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Done. Posted {posted}, pending {max(0, len(new_items) - posted)}.")


if __name__ == "__main__":
    main()
