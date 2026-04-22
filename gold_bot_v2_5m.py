import os
import time
import math
import threading
import logging
from datetime import datetime, timezone

import requests
from flask import Flask

# =========================
# ENV
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOL = os.getenv("SYMBOL", "GC=F").strip()
CHART_INTERVAL = os.getenv("CHART_INTERVAL", "5m").strip()
CHART_RANGE = os.getenv("CHART_RANGE", "5d").strip()

POLL_EVERY_SECONDS = int(os.getenv("POLL_EVERY_SECONDS", "120"))
BREAKING_REFRESH_SECONDS = int(os.getenv("BREAKING_REFRESH_SECONDS", "180"))

LOOKBACK = int(os.getenv("LOOKBACK", "8"))
FAST_LEN = int(os.getenv("FAST_LEN", "20"))
SLOW_LEN = int(os.getenv("SLOW_LEN", "50"))
TREND_LEN = int(os.getenv("TREND_LEN", "100"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))

SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.2"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "1.5"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "2.5"))

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gold-bot-v5-full")

# =========================
# GLOBAL STATE
# =========================
app = Flask(__name__)
bot_started = False

last_signal_key = None
last_breaking_check_ts = 0.0
last_breaking_sent_id = None
last_breaking_bias = "neutral"
last_breaking_label = "No fresh breaking news"
seen_breaking_ids = set()

BREAKING_KEYWORDS_BULLISH = [
    "trump", "tariff", "tariffs", "war", "iran", "middle east",
    "missile", "attack", "sanctions", "geopolitical", "conflict",
    "recession", "banking stress", "crisis", "default", "safe haven"
]

BREAKING_KEYWORDS_BEARISH = [
    "ceasefire", "peace deal", "cooling tensions",
    "risk-on", "trade deal", "de-escalation"
]

# =========================
# FLASK FOR RENDER
# =========================
@app.route("/")
def home():
    return "Gold Bot with signals is running"

# =========================
# HELPERS
# =========================
def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=20
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)

def round_price(v: float) -> float:
    return round(float(v), 2)

def utc_now():
    return datetime.now(timezone.utc)

# =========================
# BREAKING NEWS
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
            logger.warning("Breaking news HTTP error: %s", resp.status_code)
            return []

        if not resp.text.strip():
            logger.warning("Breaking news empty response")
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.warning("Breaking news JSON error: %s", e)
            return []

        return data.get("news", []) or []

    except Exception as e:
        logger.warning("Breaking news request failed: %s", e)
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
# MARKET DATA
# =========================
def fetch_data():
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
        r = requests.get(
            url,
            params={"interval": CHART_INTERVAL, "range": CHART_RANGE},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} from Yahoo chart API")

        if not r.text.strip():
            raise RuntimeError("Empty response from Yahoo chart API")

        try:
            payload = r.json()
        except Exception as e:
            raise RuntimeError(f"Invalid JSON from Yahoo chart API: {e}")

        chart = payload.get("chart", {})
        result = chart.get("result")

        if not result:
            raise RuntimeError("No chart result returned")

        data = result[0]
        quote = data["indicators"]["quote"][0]

        closes = quote.get("close", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        opens = quote.get("open", [])

        bars = []
        for i in range(len(closes)):
            if None in (closes[i], highs[i], lows[i], opens[i]):
                continue
            bars.append({
                "close": float(closes[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "open": float(opens[i]),
            })

        min_needed = max(TREND_LEN + 5, ATR_LEN + 5, RSI_LEN + 5, LOOKBACK + 5)
        if len(bars) < min_needed:
            raise RuntimeError("Not enough chart data")

        return bars

    except Exception as e:
        raise RuntimeError(f"fetch_data failed: {e}")

# =========================
# INDICATORS
# =========================
def ema(data, length):
    if len(data) < length:
        return []

    k = 2 / (length + 1)
    ema_vals = [None] * (length - 1)
    sma = sum(data[:length]) / length
    ema_vals.append(sma)

    for price in data[length:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))

    return ema_vals

def rsi(data, length=14):
    if len(data) < length + 1:
        return []

    gains, losses = [], []
    for i in range(1, length + 1):
        diff = data[i] - data[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length

    rsis = [None] * length
    rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
    rsis.append(100 - (100 / (1 + rs)))

    for i in range(length + 1, len(data)):
        diff = data[i] - data[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)

        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length

        rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
        rsis.append(100 - (100 / (1 + rs)))

    return rsis

def atr(highs, lows, closes, length=14):
    if len(closes) < length + 1:
        return []

    trs = [None]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)

    atr_vals = [None] * len(closes)
    first_atr = sum(trs[1:length + 1]) / length
    atr_vals[length] = first_atr

    prev = first_atr
    for i in range(length + 1, len(closes)):
        curr = ((prev * (length - 1)) + trs[i]) / length
        atr_vals[i] = curr
        prev = curr

    return atr_vals

# =========================
# ANALYSIS
# =========================
def analyze(bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]

    ef = ema(closes, FAST_LEN)
    es = ema(closes, SLOW_LEN)
    et = ema(closes, TREND_LEN)
    rsi_vals = rsi(closes, RSI_LEN)
    atr_vals = atr(highs, lows, closes, ATR_LEN)

    i = len(closes) - 1

    if any(x is None for x in [ef[i], es[i], et[i], rsi_vals[i], atr_vals[i]]):
        raise RuntimeError("Indicators not ready")

    score_buy = 0
    score_sell = 0

    if 50 < rsi_vals[i] < 72:
        score_buy += 1
    if 28 < rsi_vals[i] < 50:
        score_sell += 1

    bull_trend = ef[i] > es[i] and closes[i] > et[i]
    bear_trend = ef[i] < es[i] and closes[i] < et[i]

    if bull_trend:
        score_buy += 2
    if bear_trend:
        score_sell += 2

    prev_high = max(highs[-LOOKBACK:])
    prev_low = min(lows[-LOOKBACK:])

    if closes[i] > prev_high:
        score_buy += 2
    if closes[i] < prev_low:
        score_sell += 2

    body = abs(closes[i] - opens[i])
    if body >= (atr_vals[i] * 0.6):
        if closes[i] > opens[i]:
            score_buy += 1
        elif closes[i] < opens[i]:
            score_sell += 1

    candle_range = highs[i] - lows[i]
    if candle_range >= (atr_vals[i] * 0.5):
        if bull_trend:
            score_buy += 1
        if bear_trend:
            score_sell += 1

    signal = "WAIT"
    score = max(score_buy, score_sell)
    entry = closes[i]
    atr_value = atr_vals[i]

    sl = None
    tp1 = None
    tp2 = None

    if score_buy >= 3 and score_buy > score_sell:
        signal = "BUY"
        sl = entry - (atr_value * SL_ATR_MULT)
        tp1 = entry + (atr_value * TP1_ATR_MULT)
        tp2 = entry + (atr_value * TP2_ATR_MULT)

    elif score_sell >= 3 and score_sell > score_buy:
        signal = "SELL"
        sl = entry + (atr_value * SL_ATR_MULT)
        tp1 = entry - (atr_value * TP1_ATR_MULT)
        tp2 = entry - (atr_value * TP2_ATR_MULT)

    return {
        "signal": signal,
        "score": score,
        "entry": round_price(entry),
        "sl": round_price(sl) if sl is not None else None,
        "tp1": round_price(tp1) if tp1 is not None else None,
        "tp2": round_price(tp2) if tp2 is not None else None,
        "rsi": round_price(rsi_vals[i]),
        "atr": round_price(atr_value),
    }

# =========================
# BOT LOOP
# =========================
def bot_loop():
    global last_signal_key

    send_telegram("✅ Gold Bot with TP/SL started successfully.")
    logger.info("Bot started.")

    while True:
        try:
            refresh_breaking_news_if_needed()

            bars = fetch_data()
            result = analyze(bars)

            signal = result["signal"]
            score = result["score"]

            logger.info("Signal=%s | Score=%s | BreakingNews=%s", signal, score, last_breaking_bias)

            if signal != "WAIT":
                key = f"{signal}_{score}_{result['entry']}_{result['sl']}_{result['tp1']}_{result['tp2']}"

                if key != last_signal_key:
                    msg = (
                        f"🔥 {signal} GOLD\n\n"
                        f"Score: {score}\n"
                        f"Entry: {result['entry']}\n"
                        f"SL: {result['sl']}\n"
                        f"TP1: {result['tp1']}\n"
                        f"TP2: {result['tp2']}\n\n"
                        f"RSI: {result['rsi']}\n"
                        f"ATR: {result['atr']}\n"
                        f"Breaking Bias: {last_breaking_bias.upper()}\n"
                        f"Info: {last_breaking_label}"
                    )
                    send_telegram(msg)
                    last_signal_key = key

        except Exception as exc:
            logger.exception("Loop failed: %s", exc)

            ignored_errors = [
                "Not enough chart data",
                "Indicators not ready",
                "Not enough bars after filtering",
                "Empty response from Yahoo chart API",
                "Invalid JSON from Yahoo chart API",
                "No chart result returned",
            ]

            if not any(err in str(exc) for err in ignored_errors):
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
