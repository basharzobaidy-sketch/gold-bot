import os
import time
import math
import threading
import logging
from datetime import datetime, timezone

import requests
from flask import Flask

# =========================
# إعدادات عامة
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOL = os.getenv("SYMBOL", "GC=F").strip()          # Gold futures proxy
CHART_INTERVAL = os.getenv("CHART_INTERVAL", "5m").strip()
CHART_RANGE = os.getenv("CHART_RANGE", "5d").strip()

POLL_EVERY_SECONDS = int(os.getenv("POLL_EVERY_SECONDS", "180"))

LOOKBACK = int(os.getenv("LOOKBACK", "8"))
FAST_LEN = int(os.getenv("FAST_LEN", "20"))
SLOW_LEN = int(os.getenv("SLOW_LEN", "50"))
TREND_LEN = int(os.getenv("TREND_LEN", "100"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))

SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.2"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "1.5"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "2.5"))

BREAKING_REFRESH_SECONDS = int(os.getenv("BREAKING_REFRESH_SECONDS", "180"))

BREAKING_KEYWORDS_BULLISH = [
    "trump", "tariff", "tariffs", "war", "iran", "middle east",
    "missile", "attack", "sanctions", "geopolitical", "conflict",
    "recession", "banking stress", "crisis", "default", "safe haven"
]

BREAKING_KEYWORDS_BEARISH = [
    "ceasefire", "peace deal", "cooling tensions",
    "risk-on", "trade deal", "de-escalation"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gold-bot-pro")

# =========================
# حالة داخلية
# =========================
app = Flask(__name__)
bot_started = False

last_signal_key = None
last_breaking_check_ts = 0.0
last_breaking_sent_id = None
last_breaking_bias = "neutral"
last_breaking_label = "No fresh breaking news"
seen_breaking_ids = set()

# =========================
# Flask endpoint لـ Render
# =========================
@app.route("/")
def home():
    return "Gold bot is running"

# =========================
# Helpers
# =========================
def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
        timeout=20
    )

def round_price(v: float) -> float:
    return round(float(v), 2)

def utc_now():
    return datetime.now(timezone.utc)

def in_kill_zone() -> bool:
    """
    جلسات قوية تقريبية بتوقيت UTC:
    لندن: 07:00 - 11:59
    نيويورك: 12:00 - 17:59
    """
    now = utc_now()
    h = now.hour
    return (7 <= h <= 11) or (12 <= h <= 17)

# =========================
# أخبار عاجلة
# =========================
def fetch_breaking_news_items():
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": "gold market"},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("news", []) or []
    except Exception:
        return []

def classify_breaking_headline(title: str):
    t = title.lower()

    for word in BREAKING_KEYWORDS_BULLISH:
        if word in t:
            return "bullish", f"عنوان عاجل داعم للذهب بسبب: {word}"

    for word in BREAKING_KEYWORDS_BEARISH:
        if word in t:
            return "bearish", f"عنوان عاجل ضاغط على الذهب بسبب: {word}"

    if "gold rises" in t or "gold jumps" in t or "safe haven" in t:
        return "bullish", "الخبر يشير مباشرة إلى دعم الذهب"

    if "gold falls" in t or "gold drops" in t:
        return "bearish", "الخبر يشير مباشرة إلى ضغط على الذهب"

    return "neutral", "خبر عاجل غير محسوم"

def refresh_breaking_news_if_needed():
    global last_breaking_check_ts, last_breaking_sent_id
    global last_breaking_bias, last_breaking_label

    if time.time() - last_breaking_check_ts < BREAKING_REFRESH_SECONDS:
        return

    last_breaking_check_ts = time.time()

    items = fetch_breaking_news_items()
    if not items:
        last_breaking_bias = "neutral"
        last_breaking_label = "No fresh breaking news"
        return

    for item in items[:10]:
        title = (item.get("title") or "").strip()
        if not title:
            continue

        item_id = item.get("uuid") or item.get("link") or title
        if item_id in seen_breaking_ids:
            continue

        seen_breaking_ids.add(item_id)
        bias, reason = classify_breaking_headline(title)

        if bias in {"bullish", "bearish"}:
            last_breaking_bias = bias
            last_breaking_label = f"{title} | {reason}"

            if last_breaking_sent_id != item_id:
                send_telegram(
                    f"🚨 BREAKING NEWS\n\n"
                    f"Headline: {title}\n"
                    f"Impact on Gold: {bias.upper()}\n"
                    f"Reason: {reason}\n\n"
                    f"Action: انتظر تأكيد الشارت قبل الدخول."
                )
                last_breaking_sent_id = item_id
            return

    last_breaking_bias = "neutral"
    last_breaking_label = "No relevant breaking news"

# =========================
# بيانات السوق
# =========================
def fetch_chart_data(symbol: str, interval: str, range_: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    resp = requests.get(
        url,
        params={"interval": interval, "range": range_},
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    data = resp.json()

    result = (data.get("chart", {}) or {}).get("result", [])
    if not result:
        raise RuntimeError("No chart data returned.")

    result0 = result[0]
    timestamps = result0.get("timestamp", [])
    quote = (((result0.get("indicators", {}) or {}).get("quote", []) or [{}])[0])

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])

    bars = []
    for i in range(min(len(timestamps), len(opens), len(highs), len(lows), len(closes))):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue
        bars.append({
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "ts": int(timestamps[i]),
        })

    min_needed = max(TREND_LEN + 5, ATR_LEN + 5, RSI_LEN + 5, LOOKBACK + 5)
    if len(bars) < min_needed:
        raise RuntimeError("Not enough chart data.")

    return bars

# =========================
# مؤشرات
# =========================
def ema(values, length):
    if len(values) < length:
        return []
    k = 2 / (length + 1)
    out = [float("nan")] * (length - 1)
    sma = sum(values[:length]) / length
    out.append(sma)
    prev = sma
    for i in range(length, len(values)):
        curr = values[i] * k + prev * (1 - k)
        out.append(curr)
        prev = curr
    return out

def rsi(values, length):
    if len(values) < length + 1:
        return []
    out = [float("nan")] * len(values)

    gains = []
    losses = []
    for i in range(1, length + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
    out[length] = 100 - (100 / (1 + rs))

    for i in range(length + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0)
        loss = max(-delta, 0)
        avg_gain = ((avg_gain * (length - 1)) + gain) / length
        avg_loss = ((avg_loss * (length - 1)) + loss) / length
        rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
        out[i] = 100 - (100 / (1 + rs))

    return out

def atr(highs, lows, closes, length):
    if len(closes) < length + 1:
        return []

    tr_values = [float("nan")]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_values.append(tr)

    out = [float("nan")] * len(closes)
    first_atr = sum(tr_values[1:length + 1]) / length
    out[length] = first_atr
    prev = first_atr

    for i in range(length + 1, len(closes)):
        curr = ((prev * (length - 1)) + tr_values[i]) / length
        out[i] = curr
        prev = curr

    return out

def highest_prev(values, lookback, idx):
    return max(values[max(0, idx - lookback):idx])

def lowest_prev(values, lookback, idx):
    return min(values[max(0, idx - lookback):idx])

# =========================
# منطق الإشارة
# =========================
def analyze_chart(bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]

    ema_fast = ema(closes, FAST_LEN)
    ema_slow = ema(closes, SLOW_LEN)
    ema_trend = ema(closes, TREND_LEN)
    rsi_vals = rsi(closes, RSI_LEN)
    atr_vals = atr(highs, lows, closes, ATR_LEN)

    i = len(closes) - 1
    prev_i = len(closes) - 2

    close_ = closes[i]
    open_ = opens[i]
    high_ = highs[i]
    low_ = lows[i]
    atr_ = atr_vals[i]
    rsi_ = rsi_vals[i]
    ef = ema_fast[i]
    es = ema_slow[i]
    et = ema_trend[i]

    if any(math.isnan(x) for x in [atr_, rsi_, ef, es, et]):
        raise RuntimeError("Indicators not ready.")

    ef_up = ef > ema_fast[max(0, i - 2)]
    ef_down = ef < ema_fast[max(0, i - 2)]

    bull_trend = ef > es and ef_up and close_ > es and close_ > et and 52 < rsi_ < 68
    bear_trend = ef < es and ef_down and close_ < es and close_ < et and 32 < rsi_ < 48

    break_high = highest_prev(highs, LOOKBACK, i)
    break_low = lowest_prev(lows, LOOKBACK, i)

    long_break = close_ > break_high
    short_break = close_ < break_low

    prev_break_high = highest_prev(highs, LOOKBACK, prev_i)
    prev_break_low = lowest_prev(lows, LOOKBACK, prev_i)

    long_retest = (
        highs[prev_i] > prev_break_high
        and low_ <= prev_break_high
        and close_ > prev_break_high
        and close_ > ef
    )

    short_retest = (
        lows[prev_i] < prev_break_low
        and high_ >= prev_break_low
        and close_ < prev_break_low
        and close_ < ef
    )

    candle_body = abs(close_ - open_)
    candle_range = high_ - low_
    strong_candle = candle_body >= atr_ * 0.8
    volatility_ok = candle_range >= atr_ * 0.7

    buy_score = 0
    sell_score = 0

    if bull_trend:
        buy_score += 2
    if bear_trend:
        sell_score += 2

    if long_break:
        buy_score += 2
    if short_break:
        sell_score += 2

    if long_retest:
        buy_score += 2
    if short_retest:
        sell_score += 2

    if 52 < rsi_ < 68:
        buy_score += 1
    if 32 < rsi_ < 48:
        sell_score += 1

    if strong_candle:
        if close_ > open_:
            buy_score += 1
        elif close_ < open_:
            sell_score += 1

    if volatility_ok:
        if bull_trend:
            buy_score += 1
        if bear_trend:
            sell_score += 1

    chart_bias = "neutral"
    entry_type = "none"
    strength = "WAIT"

    if buy_score >= 7 and buy_score > sell_score and bull_trend and in_kill_zone():
        chart_bias = "bullish"
        entry_type = "Breakout" if long_break else ("Retest" if long_retest else "Trend")
        strength = "STRONG" if buy_score >= 8 else "MEDIUM"

    if sell_score >= 7 and sell_score > buy_score and bear_trend and in_kill_zone():
        chart_bias = "bearish"
        entry_type = "Breakdown" if short_break else ("Retest" if short_retest else "Trend")
        strength = "STRONG" if sell_score >= 8 else "MEDIUM"

    side = "WAIT"
    if chart_bias == "bullish":
        side = "BUY"
    elif chart_bias == "bearish":
        side = "SELL"

    entry = close_
    if side == "BUY":
        sl = entry - (atr_ * SL_ATR_MULT)
        tp1 = entry + (atr_ * TP1_ATR_MULT)
        tp2 = entry + (atr_ * TP2_ATR_MULT)
    elif side == "SELL":
        sl = entry + (atr_ * SL_ATR_MULT)
        tp1 = entry - (atr_ * TP1_ATR_MULT)
        tp2 = entry - (atr_ * TP2_ATR_MULT)
    else:
        sl = tp1 = tp2 = float("nan")

    return {
        "side": side,
        "strength": strength,
        "score": max(buy_score, sell_score),
        "entry_type": entry_type,
        "entry": round_price(entry),
        "sl": round_price(sl) if not math.isnan(sl) else None,
        "tp1": round_price(tp1) if not math.isnan(tp1) else None,
        "tp2": round_price(tp2) if not math.isnan(tp2) else None,
        "rsi": round_price(rsi_),
        "atr": round_price(atr_),
    }

def decide_signal(chart):
    side = chart["side"]

    if side == "WAIT":
        return "WAIT", "WAIT"

    if side == "BUY":
        if last_breaking_bias == "bearish":
            return "WAIT", "WAIT"
        if last_breaking_bias == "bullish":
            return "STRONG", "BUY"
        return "TECHNICAL", "BUY"

    if side == "SELL":
        if last_breaking_bias == "bullish":
            return "WAIT", "WAIT"
        if last_breaking_bias == "bearish":
            return "STRONG", "SELL"
        return "TECHNICAL", "SELL"

    return "WAIT", "WAIT"

def build_signal_message(signal_type, final_side, chart):
    title = f"💥 XAUUSD {final_side}" if signal_type == "STRONG" else f"📊 XAUUSD {final_side}"

    return (
        f"{title}\n\n"
        f"Type: {signal_type}\n"
        f"Timeframe: 5m\n"
        f"Strength: {chart['strength']}\n"
        f"Score: {chart['score']}\n"
        f"Setup: {chart['entry_type']}\n"
        f"Breaking Bias: {last_breaking_bias.upper()}\n"
        f"Breaking Info: {last_breaking_label}\n\n"
        f"Entry: {chart['entry']}\n"
        f"SL: {chart['sl']}\n"
        f"TP1: {chart['tp1']}\n"
        f"TP2: {chart['tp2']}\n\n"
        f"RSI: {chart['rsi']}\n"
        f"ATR: {chart['atr']}"
    )

# =========================
# لوب البوت
# =========================
def bot_loop():
    global last_signal_key

    send_telegram("🔥 NEW VERSION V4 RUNNING")
    logger.info("Bot started.")

    while True:
        try:
            refresh_breaking_news_if_needed()

            bars = fetch_chart_data(SYMBOL, CHART_INTERVAL, CHART_RANGE)
            chart = analyze_chart(bars)
            signal_type, final_side = decide_signal(chart)

            logger.info(
                "Chart=%s | Strength=%s | Score=%s | BreakingNews=%s",
                chart["side"], chart["strength"], chart["score"], last_breaking_bias
            )

            if signal_type != "WAIT":
                msg = build_signal_message(signal_type, final_side, chart)
                key = f"{signal_type}|{final_side}|{chart['entry']}|{chart['sl']}|{chart['tp1']}|{chart['tp2']}|{chart['score']}"

                if key != last_signal_key:
                    send_telegram(msg)
                    last_signal_key = key
                    logger.info("Signal sent: %s", key)

        except Exception as exc:
            logger.exception("Loop failed: %s", exc)
            if "Not enough chart data" not in str(exc) and "Indicators not ready" not in str(exc):
                try:
                    send_telegram(f"⚠️ حصل خطأ في البوت:\n{exc}")
                except Exception:
                    pass

        time.sleep(POLL_EVERY_SECONDS)

def start_bot_once():
    global bot_started
    if not bot_started:
        bot_started = True
        threading.Thread(target=bot_loop, daemon=True).start()

start_bot_once()
