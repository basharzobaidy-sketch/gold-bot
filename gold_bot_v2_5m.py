import os
import time
import math
import logging
import threading
import requests
import xml.etree.ElementTree as ET
from flask import Flask
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# =========================================================
# ENV
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYMBOL = os.getenv("SYMBOL", "GC=F").strip()  # Gold futures proxy
CHART_INTERVAL = os.getenv("CHART_INTERVAL", "5m").strip()
CHART_RANGE = os.getenv("CHART_RANGE", "2d").strip()

POLL_EVERY_SECONDS = int(os.getenv("POLL_EVERY_SECONDS", "180"))
NEWS_REFRESH_SECONDS = int(os.getenv("NEWS_REFRESH_SECONDS", "900"))
BREAKING_REFRESH_SECONDS = int(os.getenv("BREAKING_REFRESH_SECONDS", "180"))

LOOKBACK = int(os.getenv("LOOKBACK", "6"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
FAST_LEN = int(os.getenv("FAST_LEN", "20"))
SLOW_LEN = int(os.getenv("SLOW_LEN", "50"))
TREND_LEN = int(os.getenv("TREND_LEN", "100"))

SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.2"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "1.5"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "2.5"))

FF_CALENDAR_URL = os.getenv(
    "FF_CALENDAR_URL",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
).strip()

# =========================================================
# LOGGING
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gold-bot-v3-full")

# =========================================================
# STATE
# =========================================================
LAST_SENT_KEY: Optional[str] = None

LAST_SCHEDULED_REFRESH_TS = 0.0
LAST_SCHEDULED_BIAS = "neutral"
LAST_SCHEDULED_LABEL = "No fresh scheduled news"
LAST_SCHEDULED_EVENT_ID: Optional[str] = None
LAST_SCHEDULED_ONLY_SENT_ID: Optional[str] = None

LAST_BREAKING_REFRESH_TS = 0.0
LAST_BREAKING_BIAS = "neutral"
LAST_BREAKING_LABEL = "No fresh breaking news"
LAST_BREAKING_SENT_ID: Optional[str] = None
SEEN_BREAKING_IDS: set[str] = set()

WATCH_TERMS = [
    "cpi", "core cpi", "consumer price index", "inflation",
    "ppi", "producer price index", "nfp", "nonfarm payrolls",
    "non farm payrolls", "employment change", "unemployment rate",
    "interest rate", "fomc", "federal reserve", "fed chair",
    "powell", "retail sales", "gdp", "adp"
]

BREAKING_KEYWORDS_BULLISH = [
    "trump", "tariff", "tariffs", "war", "iran", "middle east",
    "missile", "attack", "sanctions", "geopolitical", "conflict",
    "recession", "banking stress", "crisis", "default", "safe haven"
]

BREAKING_KEYWORDS_BEARISH = [
    "ceasefire", "peace deal", "cooling tensions",
    "risk-on", "trade deal", "de-escalation"
]

# =========================================================
# HELPERS
# =========================================================
def require_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def safe_text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def round_price(v: float) -> float:
    return round(float(v), 2)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_float(value: Any) -> Optional[float]:
    s = safe_text(value).replace("%", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=30
    )
    resp.raise_for_status()


# =========================================================
# TELEGRAM + WEB
# =========================================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Gold Bot V3 is running"


# =========================================================
# MARKET DATA (YAHOO)
# =========================================================
def fetch_chart_data(symbol: str, interval: str, range_: str) -> List[Dict[str, float]]:
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

    r0 = result[0]
    timestamps = r0.get("timestamp", [])
    quote = (((r0.get("indicators", {}) or {}).get("quote", []) or [{}])[0])

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    bars: List[Dict[str, float]] = []
    for i in range(min(len(timestamps), len(opens), len(highs), len(lows), len(closes))):
        o = opens[i]
        h = highs[i]
        l = lows[i]
        c = closes[i]
        v = volumes[i] if i < len(volumes) else None

        if None in (o, h, l, c):
            continue

        bars.append({
            "ts": float(timestamps[i]),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v) if v is not None else 0.0,
        })

    min_needed = max(TREND_LEN + 5, ATR_LEN + 5, RSI_LEN + 5, LOOKBACK + 5)
    if len(bars) < min_needed:
        raise RuntimeError("Not enough chart data.")

    return bars


# =========================================================
# INDICATORS
# =========================================================
def ema(values: List[float], length: int) -> List[float]:
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


def rsi(values: List[float], length: int) -> List[float]:
    if len(values) < length + 1:
        return []

    out = [float("nan")] * len(values)
    gains, losses = [], []

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


def atr(highs: List[float], lows: List[float], closes: List[float], length: int) -> List[float]:
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


def highest_prev(values: List[float], lookback: int, idx: int) -> float:
    return max(values[max(0, idx - lookback):idx])


def lowest_prev(values: List[float], lookback: int, idx: int) -> float:
    return min(values[max(0, idx - lookback):idx])


# =========================================================
# SCHEDULED NEWS (FOREX FACTORY XML)
# =========================================================
def parse_ff_xml(xml_text: str) -> List[Dict[str, str]]:
    root = ET.fromstring(xml_text)
    events = []

    for event in root.findall(".//event"):
        events.append({
            "title": safe_text(event.findtext("title")),
            "country": safe_text(event.findtext("country")),
            "date": safe_text(event.findtext("date")),
            "time": safe_text(event.findtext("time")),
            "impact": safe_text(event.findtext("impact")),
            "actual": safe_text(event.findtext("actual")),
            "forecast": safe_text(event.findtext("forecast")),
            "previous": safe_text(event.findtext("previous")),
            "ffevent_id": safe_text(event.findtext("FFevent_ID")),
        })

    return events


def fetch_ff_events() -> List[Dict[str, str]]:
    resp = requests.get(
        FF_CALENDAR_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 GoldBotV3"}
    )

    if resp.status_code == 429:
        logger.warning("Scheduled news source rate-limited (429).")
        return []

    resp.raise_for_status()
    return parse_ff_xml(resp.text)


def is_gold_relevant_event(title: str) -> bool:
    t = safe_text(title).lower()
    return any(term in t for term in WATCH_TERMS)


def is_high_impact(event: Dict[str, str]) -> bool:
    return safe_text(event.get("impact")).lower() == "high"


def parse_event_datetime(event: Dict[str, str]) -> Optional[datetime]:
    date_s = safe_text(event.get("date"))
    time_s = safe_text(event.get("time")).lower()

    if not date_s or not time_s or time_s in {"all day", "tentative"}:
        return None

    raw = f"{date_s} {time_s.replace(' ', '')}"
    formats = [
        "%m-%d-%Y %I:%M%p",
        "%Y-%m-%d %I:%M%p",
        "%m/%d/%Y %I:%M%p"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def classify_scheduled_news_bias(event_name: str, actual: Any, forecast: Any) -> Tuple[str, str]:
    name = safe_text(event_name).lower()
    a = to_float(actual)
    f = to_float(forecast)

    if a is None or f is None:
        return "neutral", "البيانات غير كافية"

    inflation_terms = ["cpi", "inflation", "ppi", "producer price index", "consumer price index", "core cpi"]
    labor_terms = ["nfp", "non farm payrolls", "nonfarm payrolls", "employment change", "adp"]
    unemployment_terms = ["unemployment rate"]
    rates_terms = ["interest rate", "fomc", "federal reserve", "fed chair", "powell"]

    if any(term in name for term in inflation_terms):
        if a < f:
            return "bullish", "التضخم أقل من المتوقع ويدعم الذهب"
        if a > f:
            return "bearish", "التضخم أعلى من المتوقع ويضغط على الذهب"
        return "neutral", "القراءة مطابقة تقريبًا"

    if any(term in name for term in unemployment_terms):
        if a > f:
            return "bullish", "البطالة أعلى من المتوقع وتدعم الذهب"
        if a < f:
            return "bearish", "البطالة أقل من المتوقع وتضغط على الذهب"
        return "neutral", "القراءة مطابقة تقريبًا"

    if any(term in name for term in labor_terms):
        if a < f:
            return "bullish", "الوظائف أضعف من المتوقع وتدعم الذهب"
        if a > f:
            return "bearish", "الوظائف أقوى من المتوقع وتضغط على الذهب"
        return "neutral", "القراءة مطابقة تقريبًا"

    if any(term in name for term in rates_terms):
        if a < f:
            return "bullish", "القرار أقل تشددًا من المتوقع"
        if a > f:
            return "bearish", "القرار أكثر تشددًا من المتوقع"
        return "neutral", "القرار مطابق تقريبًا"

    return "neutral", "خبر غير مصنف"


def refresh_scheduled_news_if_needed() -> None:
    global LAST_SCHEDULED_REFRESH_TS, LAST_SCHEDULED_BIAS, LAST_SCHEDULED_LABEL
    global LAST_SCHEDULED_EVENT_ID, LAST_SCHEDULED_ONLY_SENT_ID

    if time.time() - LAST_SCHEDULED_REFRESH_TS < NEWS_REFRESH_SECONDS:
        return

    LAST_SCHEDULED_REFRESH_TS = time.time()

    try:
        events = fetch_ff_events()
        watched = [
            e for e in events
            if safe_text(e.get("country")).upper() == "USD"
            and is_high_impact(e)
            and is_gold_relevant_event(safe_text(e.get("title")))
        ]

        best_event = None
        best_dt = None

        for e in watched:
            if not safe_text(e.get("actual")):
                continue

            evt_dt = parse_event_datetime(e)
            if evt_dt is None:
                best_event = e
                break

            age = utc_now() - evt_dt
            if timedelta(minutes=0) <= age <= timedelta(hours=8):
                if best_dt is None or evt_dt > best_dt:
                    best_event = e
                    best_dt = evt_dt

        if best_event:
            bias, reason = classify_scheduled_news_bias(
                best_event.get("title", ""),
                best_event.get("actual"),
                best_event.get("forecast"),
            )

            LAST_SCHEDULED_BIAS = bias
            LAST_SCHEDULED_LABEL = f"{safe_text(best_event.get('title'))} | {reason}"
            LAST_SCHEDULED_EVENT_ID = safe_text(best_event.get("ffevent_id")) or safe_text(best_event.get("title"))

            logger.info("Scheduled news bias updated: %s | %s", LAST_SCHEDULED_BIAS, LAST_SCHEDULED_LABEL)

            if LAST_SCHEDULED_BIAS in {"bullish", "bearish"} and LAST_SCHEDULED_ONLY_SENT_ID != LAST_SCHEDULED_EVENT_ID:
                send_telegram(
                    f"📰 NEWS ONLY SIGNAL\n\n"
                    f"Bias: {LAST_SCHEDULED_BIAS.upper()} GOLD\n"
                    f"Event: {safe_text(best_event.get('title'))}\n"
                    f"Actual: {safe_text(best_event.get('actual'))}\n"
                    f"Forecast: {safe_text(best_event.get('forecast'))}\n"
                    f"Previous: {safe_text(best_event.get('previous'))}\n\n"
                    f"Action: انتظر تأكيد الشارت قبل الدخول."
                )
                LAST_SCHEDULED_ONLY_SENT_ID = LAST_SCHEDULED_EVENT_ID
        else:
            LAST_SCHEDULED_BIAS = "neutral"
            LAST_SCHEDULED_LABEL = "No fresh high-impact USD scheduled news"
            LAST_SCHEDULED_EVENT_ID = None

    except Exception as exc:
        logger.warning("Scheduled news refresh failed: %s", exc)


# =========================================================
# BREAKING NEWS (YAHOO SEARCH)
# =========================================================
def fetch_breaking_news_items() -> List[Dict[str, Any]]:
    resp = requests.get(
        "https://query1.finance.yahoo.com/v1/finance/search",
        params={"q": "gold market"},
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    if resp.status_code == 429:
        logger.warning("Breaking news source rate-limited (429).")
        return []

    resp.raise_for_status()
    data = resp.json()
    return data.get("news", []) or []


def classify_breaking_headline(title: str) -> Tuple[str, str]:
    t = safe_text(title).lower()

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


def refresh_breaking_news_if_needed() -> None:
    global LAST_BREAKING_REFRESH_TS, LAST_BREAKING_BIAS, LAST_BREAKING_LABEL, LAST_BREAKING_SENT_ID

    if time.time() - LAST_BREAKING_REFRESH_TS < BREAKING_REFRESH_SECONDS:
        return

    LAST_BREAKING_REFRESH_TS = time.time()

    try:
        items = fetch_breaking_news_items()

        if not items:
            LAST_BREAKING_BIAS = "neutral"
            LAST_BREAKING_LABEL = "No fresh breaking news"
            return

        for item in items[:10]:
            title = safe_text(item.get("title"))
            if not title:
                continue

            item_id = safe_text(item.get("uuid")) or safe_text(item.get("link")) or title
            if item_id in SEEN_BREAKING_IDS:
                continue

            bias, reason = classify_breaking_headline(title)
            SEEN_BREAKING_IDS.add(item_id)

            if bias in {"bullish", "bearish"}:
                LAST_BREAKING_BIAS = bias
                LAST_BREAKING_LABEL = f"{title} | {reason}"

                logger.info("Breaking news bias updated: %s | %s", LAST_BREAKING_BIAS, LAST_BREAKING_LABEL)

                if LAST_BREAKING_SENT_ID != item_id:
                    send_telegram(
                        f"🚨 BREAKING NEWS\n\n"
                        f"Headline: {title}\n"
                        f"Impact on Gold: {bias.upper()}\n"
                        f"Reason: {reason}\n\n"
                        f"Action: انتظر تأكيد الشارت قبل الدخول."
                    )
                    LAST_BREAKING_SENT_ID = item_id
                return

        LAST_BREAKING_BIAS = "neutral"
        LAST_BREAKING_LABEL = "No relevant breaking news"

    except Exception as exc:
        logger.warning("Breaking news refresh failed: %s", exc)


# =========================================================
# CHART ANALYSIS
# =========================================================
def analyze_chart(bars: List[Dict[str, float]]) -> Dict[str, Any]:
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
        raise RuntimeError("Indicator values not ready.")

    ef_up = ef > ema_fast[max(0, i - 2)]
    ef_down = ef < ema_fast[max(0, i - 2)]

    bull_trend = ef > es and ef_up and close_ > es and close_ > et and 52 < rsi_ < 70
    bear_trend = ef < es and ef_down and close_ < es and close_ < et and 30 < rsi_ < 48

    break_high = highest_prev(highs, LOOKBACK, i)
    break_low = lowest_prev(lows, LOOKBACK, i)

    long_break = close_ > break_high
    short_break = close_ < break_low

    prev_break_high = highest_prev(highs, LOOKBACK, prev_i)
    prev_break_low = lowest_prev(lows, LOOKBACK, prev_i)

    long_retest = highs[prev_i] > prev_break_high and low_ <= prev_break_high and close_ > prev_break_high and close_ > ef
    short_retest = lows[prev_i] < prev_break_low and high_ >= prev_break_low and close_ < prev_break_low and close_ < ef

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

    if 52 < rsi_ < 70:
        buy_score += 1
    if 30 < rsi_ < 48:
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

    if buy_score >= 6 and buy_score > sell_score and bull_trend:
        chart_bias = "bullish"
        entry_type = "Breakout" if long_break else ("Retest" if long_retest else "Trend")
        strength = "STRONG" if buy_score >= 8 else "MEDIUM"

    if sell_score >= 6 and sell_score > buy_score and bear_trend:
        chart_bias = "bearish"
        entry_type = "Breakdown" if short_break else ("Retest" if short_retest else "Trend")
        strength = "STRONG" if sell_score >= 8 else "MEDIUM"

    signal_side = "WAIT"
    if chart_bias == "bullish":
        signal_side = "BUY"
    elif chart_bias == "bearish":
        signal_side = "SELL"

    entry = close_

    if signal_side == "BUY":
        sl = entry - (atr_ * SL_ATR_MULT)
        tp1 = entry + (atr_ * TP1_ATR_MULT)
        tp2 = entry + (atr_ * TP2_ATR_MULT)
    elif signal_side == "SELL":
        sl = entry + (atr_ * SL_ATR_MULT)
        tp1 = entry - (atr_ * TP1_ATR_MULT)
        tp2 = entry - (atr_ * TP2_ATR_MULT)
    else:
        sl = float("nan")
        tp1 = float("nan")
        tp2 = float("nan")

    return {
        "side": signal_side,
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


# =========================================================
# DECISION LAYER
# =========================================================
def combined_news_bias() -> Tuple[str, str]:
    if LAST_BREAKING_BIAS in {"bullish", "bearish"}:
        return LAST_BREAKING_BIAS, LAST_BREAKING_LABEL
    return LAST_SCHEDULED_BIAS, LAST_SCHEDULED_LABEL


def decide_signal(chart: Dict[str, Any], news_bias: str) -> Tuple[str, str]:
    side = chart["side"]

    if side == "WAIT":
        return "WAIT", "WAIT"

    if side == "BUY":
        if news_bias == "bullish":
            return "STRONG", "BUY"
        if news_bias == "bearish":
            return "WAIT", "WAIT"
        return "TECHNICAL", "BUY"

    if side == "SELL":
        if news_bias == "bearish":
            return "STRONG", "SELL"
        if news_bias == "bullish":
            return "WAIT", "WAIT"
        return "TECHNICAL", "SELL"

    return "WAIT", "WAIT"


def build_message(signal_type: str, final_side: str, chart: Dict[str, Any], news_bias: str, news_label: str) -> str:
    if signal_type == "STRONG":
        title = f"💥 XAUUSD {final_side}"
        kind = "STRONG (News + Chart)"
    elif signal_type == "TECHNICAL":
        title = f"📊 XAUUSD {final_side}"
        kind = "TECHNICAL ONLY"
    else:
        title = "⚠️ XAUUSD WAIT"
        kind = "WAIT"

    parts = [
        title,
        "",
        f"Type: {kind}",
        "Timeframe: 5m",
        f"Chart strength: {chart['strength']}",
        f"Score: {chart['score']}",
        f"Setup: {chart['entry_type']}",
        f"News Bias: {news_bias.upper()}",
        f"News Info: {news_label}",
        ""
    ]

    if final_side in {"BUY", "SELL"}:
        parts += [
            f"Entry: {chart['entry']}",
            f"SL: {chart['sl']}",
            f"TP1: {chart['tp1']}",
            f"TP2: {chart['tp2']}",
            "",
            f"RSI: {chart['rsi']}",
            f"ATR: {chart['atr']}",
        ]

    return "\n".join(parts)


def make_signal_key(signal_type: str, final_side: str, chart: Dict[str, Any], news_label: str) -> str:
    return f"{signal_type}|{final_side}|{chart['entry']}|{chart['sl']}|{chart['tp1']}|{chart['tp2']}|{chart['score']}|{news_label}"


# =========================================================
# BOT LOOP
# =========================================================
def bot_loop() -> None:
    global LAST_SENT_KEY

    require_env()
    send_telegram("✅ Gold Bot V3 started successfully.")
    logger.info("Bot started.")

    while True:
        try:
            refresh_scheduled_news_if_needed()
            refresh_breaking_news_if_needed()

            active_news_bias, active_news_label = combined_news_bias()

            bars = fetch_chart_data(SYMBOL, CHART_INTERVAL, CHART_RANGE)
            chart = analyze_chart(bars)
            signal_type, final_side = decide_signal(chart, active_news_bias)

            logger.info(
                "Chart=%s | Strength=%s | Score=%s | ScheduledNews=%s | BreakingNews=%s",
                chart["side"], chart["strength"], chart["score"], LAST_SCHEDULED_BIAS, LAST_BREAKING_BIAS
            )

            if signal_type != "WAIT":
                msg = build_message(signal_type, final_side, chart, active_news_bias, active_news_label)
                key = make_signal_key(signal_type, final_side, chart, active_news_label)

                if key != LAST_SENT_KEY:
                    send_telegram(msg)
                    LAST_SENT_KEY = key
                    logger.info("Signal sent: %s", key)

        except Exception as exc:
            logger.exception("Loop failed: %s", exc)
            try:
                send_telegram(f"⚠️ حصل خطأ في البوت:\n{exc}")
            except Exception:
                pass

        time.sleep(POLL_EVERY_SECONDS)


# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
