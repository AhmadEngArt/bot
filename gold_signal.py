# -*- coding: utf-8 -*-
"""
ربات سیگنال طلا (Gold H1 Signal - AhmadEngArt)
هر ساعت کندل‌های یک‌ساعته‌ی طلا را از تریدینگ‌ویو می‌گیرد، همان شرط‌های اندیکاتور را چک می‌کند
و اگر سیگنال خرید یا فروش بود، در تلگرام پیام می‌دهد.
روی GitHub Actions اجرا می‌شود (workflow: gold.yml).
"""

import os
import re
import sys
import json
import time
import random
import string
import traceback
from datetime import datetime, timedelta, timezone

import requests

# ====================================================================
#                              تنظیمات
# ====================================================================
TV_SYMBOL   = os.environ.get("TV_SYMBOL") or "OANDA:XAUUSD"   # همان نمادی که در تریدینگ‌ویو می‌بینید
RR          = float(os.environ.get("GOLD_RR") or 1.5)        # Reward/Risk
ATR_MULT    = float(os.environ.get("GOLD_ATR_MULT") or 2.0)  # SL = ATR x
ADX_MIN     = float(os.environ.get("GOLD_ADX_MIN") or 20)    # Min ADX
USE_SESSION = True
SESSION_START_UTC = 7       # 07:00 UTC
SESSION_END_UTC   = 20      # 20:00 UTC

ACCOUNT_USC  = float(os.environ.get("GOLD_ACCOUNT_USC") or 20000)  # موجودی حساب سنت (سنت)
RISK_PERCENT = float(os.environ.get("GOLD_RISK") or 1.0)

RUN_MINUTES = int(os.environ.get("GOLD_RUN_MINUTES") or 340)       # هر اجرا ~۵ ساعت و ۴۰ دقیقه
STATE_FILE  = "gold_state.json"
# ====================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID   = (os.environ.get("GOLD_CHAT_ID") or os.environ.get("ALERT_CHAT_ID") or "").strip()
TEST_MODE = os.environ.get("GOLD_TEST", "").lower() == "true"

UTC = timezone.utc
TEHRAN = timezone(timedelta(hours=3, minutes=30))
H1 = 3600
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def log(*a):
    print(datetime.now(UTC).strftime("%H:%M:%S"), *a, flush=True)


def fa(x):
    return str(x).translate(FA_DIGITS)


# ------------------------------------------------------------ telegram --
def send(text):
    if not (BOT_TOKEN and CHAT_ID):
        log("Telegram not configured (BOT_TOKEN / ALERT_CHAT_ID). Message was:\n" + text)
        return False
    for attempt in range(4):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
            if r.ok:
                return True
            log("Telegram error", r.status_code, r.text[:200])
            if r.status_code < 500 and r.status_code != 429:
                return False
        except requests.ConnectionError as e:          # never retry a timeout (could double-send)
            log("Telegram connection error:", e)
        except requests.Timeout:
            log("Telegram timeout")
            return False
        time.sleep(5 * (attempt + 1))
    return False


# ------------------------------------------------------- market data --
def _tv_frames(msg):
    out, i = [], 0
    while i < len(msg):
        m = re.match(r"~m~(\d+)~m~", msg[i:])
        if not m:
            break
        n = int(m.group(1))
        start = i + m.end()
        out.append(msg[start:start + n])
        i = start + n
    return out


def candles_tradingview(symbol=TV_SYMBOL, interval="60", n=1000):
    """Candles straight from TradingView's own data feed (same as the chart)."""
    import websocket
    ws = websocket.create_connection("wss://data.tradingview.com/socket.io/websocket",
                                     header=["Origin: https://www.tradingview.com"], timeout=20)

    def call(func, params):
        m = json.dumps({"m": func, "p": params}, separators=(",", ":"))
        ws.send(f"~m~{len(m)}~m~{m}")

    cs = "cs_" + "".join(random.choice(string.ascii_lowercase) for _ in range(12))
    call("set_auth_token", ["unauthorized_user_token"])
    call("chart_create_session", [cs, ""])
    call("resolve_symbol", [cs, "symbol_1", '={"symbol":"%s","adjustment":"splits"}' % symbol])
    call("create_series", [cs, "s1", "s1", "symbol_1", interval, n])

    bars, done, deadline = {}, False, time.time() + 40
    try:
        while not done and time.time() < deadline:
            msg = ws.recv()
            for f in _tv_frames(msg):
                if f.startswith("~h~"):
                    ws.send(f"~m~{len(f)}~m~{f}")       # heartbeat
                    continue
                try:
                    d = json.loads(f)
                except ValueError:
                    continue
                kind = d.get("m")
                if kind in ("symbol_error", "critical_error", "series_error", "protocol_error"):
                    raise RuntimeError(f"TradingView {kind}: {d.get('p')}")
                if kind in ("timescale_update", "du"):
                    s = (d.get("p") or [None, {}])[1].get("s1", {}).get("s", [])
                    for b in s:
                        v = b["v"]
                        bars[int(v[0])] = dict(t=int(v[0]), open=v[1], high=v[2], low=v[3], close=v[4])
                if kind == "series_completed":
                    done = True
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not bars:
        raise RuntimeError("TradingView returned no candles")
    return [bars[k] for k in sorted(bars)]


def candles_yahoo():
    """Backup source: Yahoo gold futures (GC=F). Price is a little different from spot."""
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=60m&range=730d",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append(dict(t=int(t) - int(t) % H1, open=o, high=h, low=l, close=c))
    return out[-1000:]


def get_closed_candles():
    """Returns (closed candles, source name)."""
    now = time.time()
    try:
        c = candles_tradingview()
        src = TV_SYMBOL
    except Exception as e:
        log("TradingView failed:", repr(e), "-> using Yahoo GC=F")
        c = candles_yahoo()
        src = "Yahoo GC=F (futures)"
    return [b for b in c if b["t"] + H1 <= now], src


# ------------------------------------------------------- indicators --
# Same formulas as TradingView ta.ema / ta.rma / ta.rsi / ta.atr / ta.dmi
def ema(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    k, e = 2.0 / (n + 1), sum(v[:n]) / n
    out[n - 1] = e
    for i in range(n, len(v)):
        e = v[i] * k + e * (1 - k)
        out[i] = e
    return out


def rma(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    r = sum(v[:n]) / n
    out[n - 1] = r
    for i in range(n, len(v)):
        r = (r * (n - 1) + v[i]) / n
        out[i] = r
    return out


def rsi(c, n=14):
    up = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    dn = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    out = [None]
    for u, d in zip(rma(up, n), rma(dn, n)):
        out.append(None if u is None else (100.0 if d == 0 else (0.0 if u == 0 else 100 - 100 / (1 + u / d))))
    return out


def true_ranges(h, l, c):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def atr(h, l, c, n=14):
    return rma(true_ranges(h, l, c), n)


def adx(h, l, c, n=14, sm=14):
    tr = true_ranges(h, l, c)[1:]
    pdm, mdm = [], []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    dx, lp, lm = [], 0.0, 0.0
    for t, a, b in zip(rma(tr, n), rma(pdm, n), rma(mdm, n)):
        if t is None:
            continue
        p = 100 * a / t if t else lp
        m = 100 * b / t if t else lm
        lp, lm = p, m
        s = p + m
        dx.append(abs(p - m) / (s if s else 1))
    a = [None if x is None else 100 * x for x in rma(dx, sm)]
    return [None] * (len(c) - len(a)) + a


def in_session(t):
    if not USE_SESSION:
        return True
    hour = datetime.fromtimestamp(t, UTC).hour
    if SESSION_START_UTC < SESSION_END_UTC:
        return SESSION_START_UTC <= hour < SESSION_END_UTC
    return hour >= SESSION_START_UTC or hour < SESSION_END_UTC


def signal_at(candles):
    """Signal on the LAST candle of the list (all candles must be closed)."""
    c = [b["close"] for b in candles]
    h = [b["high"] for b in candles]
    l = [b["low"] for b in candles]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    r = rsi(c)[-1]
    a = atr(h, l, c)[-1]
    x = adx(h, l, c)[-1]
    trend_ok = x > ADX_MIN
    sess = in_session(candles[-1]["t"])
    up = e20[-1] > e50[-1] and e20[-2] <= e50[-2]
    dn = e20[-1] < e50[-1] and e20[-2] >= e50[-2]
    buy = up and c[-1] > e200[-1] and 50 < r < 70 and trend_ok and sess
    sell = dn and c[-1] < e200[-1] and 30 < r < 50 and trend_ok and sess
    info = dict(close=c[-1], e20=e20[-1], e50=e50[-1], e200=e200[-1], rsi=r, adx=x, atr=a, session=sess)
    return (1 if buy else -1 if sell else 0), info


# ------------------------------------------------------------ message --
def signal_message(sig, bar, info, src, late_min=0):
    entry = info["close"]
    sl_d = info["atr"] * ATR_MULT
    tp_d = sl_d * RR
    sl = entry - sig * sl_d
    tp = entry + sig * tp_d
    risk_usc = ACCOUNT_USC * RISK_PERCENT / 100
    lots = int(risk_usc / (sl_d * 100) / 0.01) * 0.01          # cent account: 1 lot, $1 move = 100 USC
    lots_txt = f"{lots:.2f}" if lots >= 0.01 else "۰.۰۱ (کمتر از حداقل؛ ریسک بیشتر از " + fa(f"{RISK_PERCENT:g}") + "٪)"
    t_open = datetime.fromtimestamp(bar["t"], TEHRAN)
    t_close = t_open + timedelta(hours=1)
    head = "🟢 <b>سیگنال خرید طلا</b> | H1" if sig == 1 else "🔴 <b>سیگنال فروش طلا</b> | H1"
    lines = [
        head,
        "",
        f"💰 ورود: <code>{entry:.2f}</code>",
        f"🛑 حد ضرر: <code>{sl:.2f}</code>  ({sl_d:.2f}$)",
        f"🎯 حد سود: <code>{tp:.2f}</code>  ({tp_d:.2f}$)",
        f"📦 حجم برای حساب سنت {fa(int(ACCOUNT_USC / 100))}$ با ریسک {fa(f"{RISK_PERCENT:g}")}٪: <b>{lots_txt}</b> لات",
        "",
        f"📊 RSI {info['rsi']:.1f} | ADX {info['adx']:.1f} | ATR {info['atr']:.2f}",
        f"🕐 کندل {fa(t_open.strftime('%H:%M'))} تا {fa(t_close.strftime('%H:%M'))} تهران ({fa(t_open.strftime('%m/%d'))})",
        f"📡 {src}",
    ]
    if late_min > 20:
        lines.append(f"⚠️ این پیام {fa(late_min)} دقیقه دیر رسید؛ قبل از ورود قیمت فعلی را چک کنید.")
    return "\n".join(lines)


# --------------------------------------------------------------- state --
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- main --
def check_once(state):
    """Fetch candles and process every closed candle we have not processed yet."""
    candles, src = get_closed_candles()
    if len(candles) < 260:
        raise RuntimeError(f"only {len(candles)} candles")
    last_done = state.get("last_bar", 0)
    new = [i for i, b in enumerate(candles) if b["t"] > last_done]
    if not last_done:                                   # first run ever: just remember where we are
        new = new[-1:]
    for i in new:
        bar = candles[i]
        window = candles[max(0, i - 999):i + 1]
        sig, info = signal_at(window)
        t_txt = datetime.fromtimestamp(bar["t"], TEHRAN).strftime("%m-%d %H:%M")
        log(f"bar {t_txt} Tehran close {info['close']:.2f} | EMA20 {info['e20']:.2f} EMA50 {info['e50']:.2f} "
            f"EMA200 {info['e200']:.2f} RSI {info['rsi']:.1f} ADX {info['adx']:.1f} ATR {info['atr']:.2f} "
            f"session {info['session']} -> {('BUY', 'NONE', 'SELL')[1 - sig]}")
        late_min = int((time.time() - (bar["t"] + H1)) / 60)
        if sig and late_min <= 180 and state.get("last_signal_bar") != bar["t"]:
            if send(signal_message(sig, bar, info, src, late_min)):
                state["last_signal_bar"] = bar["t"]
                state.setdefault("signals", []).append(
                    dict(t=bar["t"], dir=sig, price=round(info["close"], 2)))
                state["signals"] = state["signals"][-50:]
        elif sig:
            log("signal too old to send (", late_min, "min )")
        state["last_bar"] = bar["t"]
    state["last_ok"] = int(time.time())
    state["source"] = src
    return candles, src


def main():
    state = load_state()
    if TEST_MODE:
        candles, src = get_closed_candles()
        sig, info = signal_at(candles[-1000:])
        b = candles[-1]
        t = datetime.fromtimestamp(b["t"], TEHRAN)
        log(f"TEST: {len(candles)} closed candles from {src}; last {t:%m-%d %H:%M} Tehran close {info['close']:.2f} "
            f"EMA20 {info['e20']:.2f} EMA50 {info['e50']:.2f} EMA200 {info['e200']:.2f} RSI {info['rsi']:.1f} "
            f"ADX {info['adx']:.1f} ATR {info['atr']:.2f} signal {sig}")
        ok1 = send("✅ <b>ربات سیگنال طلا کار می‌کند</b>\n\n"
             f"آخرین کندل بسته‌شده: {fa(t.strftime('%H:%M'))} تهران\n"
             f"قیمت: <code>{info['close']:.2f}</code>\n"
             f"EMA20 {info['e20']:.2f} | EMA50 {info['e50']:.2f} | EMA200 {info['e200']:.2f}\n"
             f"RSI {info['rsi']:.1f} | ADX {info['adx']:.1f} | ATR {info['atr']:.2f}\n"
             f"سیگنال الان: {'خرید' if sig == 1 else 'فروش' if sig == -1 else 'ندارد'}\n"
             f"📡 {src}\n\n"
             "نمونه‌ی پیام سیگنال:")
        ok2 = send(signal_message(sig or 1, b, info, src))
        log("TEST: telegram messages sent:", ok1, ok2)
        return

    if not state.get("welcome_sent"):
        if send("🤖 <b>ربات سیگنال طلا روشن شد</b>\n"
                "از این به بعد هر وقت اندیکاتور Gold H1 Signal سیگنال خرید یا فروش بدهد، همین‌جا خبر می‌دهم.\n"
                f"تنظیمات: RR {fa(f"{RR:g}")} | حد ضرر {fa(f"{ATR_MULT:g}")}×ATR | ADX بالای {fa(int(ADX_MIN))} | "
                f"سشن {fa(SESSION_START_UTC)} تا {fa(SESSION_END_UTC)} UTC"):
            state["welcome_sent"] = True

    end = time.time() + RUN_MINUTES * 60
    fails = 0
    next_check = 0
    while time.time() < end:
        now = time.time()
        due = now - state.get("last_bar", 0) >= 2 * H1 + 20     # a newer candle should have closed
        if due and now >= next_check:
            try:
                check_once(state)
                save_state(state)
                fails = 0
                still_due = time.time() - state.get("last_bar", 0) >= 2 * H1 + 20
                # candle not published yet -> retry soon; market closed (weekend/break) -> every 10 min
                overdue = time.time() - (state.get("last_bar", 0) + 2 * H1)
                next_check = time.time() + (0 if not still_due else 30 if overdue < 600 else 600)
            except Exception:
                fails += 1
                log("check failed:\n" + traceback.format_exc())
                next_check = time.time() + 60
                if fails == 30 and time.time() - state.get("fail_alert", 0) > 6 * H1:
                    send("⚠️ ربات سیگنال طلا ۳۰ دقیقه است که نمی‌تواند قیمت طلا را بگیرد. خودش دوباره تلاش می‌کند.")
                    state["fail_alert"] = int(time.time())
                    save_state(state)
        time.sleep(15)
    save_state(state)
    log("run finished")


if __name__ == "__main__":
    main()
