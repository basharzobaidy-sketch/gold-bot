import os
import time
import email
import imaplib
import logging
import threading
import requests
from flask import Flask

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
EMAIL_POLL_SECONDS = int(os.getenv("EMAIL_POLL_SECONDS", "30"))
SIGNAL_SECRET = os.getenv("WEBHOOK_SECRET", "mygold123secret").strip()

NEWS_REFRESH_SECONDS = int(os.getenv("NEWS_REFRESH_SECONDS", "180"))

app = Flask(__name__)
bot_started = False

seen_email_ids = set()
seen_news_ids = set()
last_news_check = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("nahhas-email-news-bot")

BREAKING_KEYWORDS_BULLISH = [
    "trump", "tariff", "tariffs", "war", "iran", "middle east",
    "missile", "attack", "sanctions", "geopolitical", "conflict",
    "recession", "banking stress", "crisis", "default", "safe haven"
]

BREAKING_KEYWORDS_BEARISH = [
    "ceasefire", "peace deal", "cooling tensions",
    "risk-on", "trade deal", "de-escalation"
]


@app.route("/")
def home():
    return "Nahhas Email + News Bot is running"


def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=20)


def extract_body(msg):
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(errors="ignore"))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    return payload.decode(errors="ignore") if payload else ""


def parse_signal(text: str):
    if "NAHHAS_SIGNAL" not in text:
        return None

    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip().lower()] = v.strip()

    if data.get("secret") != SIGNAL_SECRET:
        return None

    return data


def build_signal_message(data):
    signal = data.get("signal", "UNKNOWN").upper()
    symbol = data.get("symbol", "XAUUSD")
    timeframe = data.get("timeframe", "-")
    price = data.get("price", "-")
    sl = data.get("sl", "-")
    tp1 = data.get("tp1", "-")
    tp2 = data.get("tp2", "-")
    tp3 = data.get("tp3", "-")
    lot = data.get("lot", "-")
    rating = data.get("rating", "-")
    typ = data.get("type", "-")
    reason = data.get("reason", "-")

    signal_ar = "شراء" if signal == "BUY" else "بيع" if signal == "SELL" else "إشارة"

    return (
        f"🔥 {signal} {symbol} ({signal_ar})\n\n"
        f"Type: {typ}\n"
        f"Rating: {rating}\n"
        f"Timeframe: {timeframe}\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"TP3: {tp3}\n"
        f"Lot: {lot}\n\n"
        f"Reason: {reason}"
    )


def classify_news(title: str):
    t = title.lower()

    for word in BREAKING_KEYWORDS_BULLISH:
        if word in t:
            return "BULLISH", f"خبر داعم للذهب بسبب: {word}"

    for word in BREAKING_KEYWORDS_BEARISH:
        if word in t:
            return "BEARISH", f"خبر ضاغط على الذهب بسبب: {word}"

    if "gold rises" in t or "gold jumps" in t or "safe haven" in t:
        return "BULLISH", "الخبر يشير إلى دعم الذهب"

    if "gold falls" in t or "gold drops" in t:
        return "BEARISH", "الخبر يشير إلى ضغط على الذهب"

    return "NEUTRAL", "خبر غير محسوم"


def check_breaking_news():
    global last_news_check

    if time.time() - last_news_check < NEWS_REFRESH_SECONDS:
        return

    last_news_check = time.time()

    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": "gold market"},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        if r.status_code != 200 or not r.text.strip():
            return

        data = r.json()
        news = data.get("news", []) or []

        for item in news[:10]:
            title = (item.get("title") or "").strip()
            if not title:
                continue

            item_id = item.get("uuid") or item.get("link") or title
            if item_id in seen_news_ids:
                continue

            seen_news_ids.add(item_id)

            bias, reason = classify_news(title)

            if bias in ["BULLISH", "BEARISH"]:
                send_telegram(
                    f"🚨 BREAKING NEWS\n\n"
                    f"Headline: {title}\n"
                    f"Impact on Gold: {bias}\n"
                    f"Reason: {reason}\n\n"
                    f"Action: انتظر تأكيد الشارت قبل الدخول."
                )
                return

    except Exception as exc:
        logger.warning("News check failed: %s", exc)


def check_email_once():
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.warning("Missing Gmail credentials.")
        return

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    status, data = mail.search(None, '(UNSEEN FROM "noreply@tradingview.com")')
    if status != "OK":
        mail.logout()
        return

    ids = data[0].split()

    for msg_id in ids:
        msg_id_str = msg_id.decode()

        if msg_id_str in seen_email_ids:
            continue

        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        body = extract_body(msg)

        signal_data = parse_signal(body)

        if signal_data:
            send_telegram(build_signal_message(signal_data))
            seen_email_ids.add(msg_id_str)

        mail.store(msg_id, "+FLAGS", "\\Seen")

    mail.logout()


def bot_loop():
    send_telegram("✅ Nahhas Email + News Bot started successfully.")
    logger.info("Bot started.")

    while True:
        try:
            check_breaking_news()
            check_email_once()
        except Exception as exc:
            logger.exception("Main loop error: %s", exc)

        time.sleep(EMAIL_POLL_SECONDS)


if __name__ == "__main__":
    bot_loop()
