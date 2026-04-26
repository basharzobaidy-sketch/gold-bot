import os
import time
import email
import imaplib
import logging
import threading
import requests
from flask import Flask

#=========================
# ENV
#=========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()      # الخاص
TELEGRAM_GROUP_ID  = os.getenv("TELEGRAM_GROUP_ID", "").strip()     # الجروب

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

EMAIL_POLL_SECONDS = int(os.getenv("EMAIL_POLL_SECONDS", "30"))
NEWS_REFRESH_SECONDS = int(os.getenv("NEWS_REFRESH_SECONDS", "180"))

SIGNAL_SECRET = os.getenv("WEBHOOK_SECRET", "mygold123secret").strip()

app = Flask(__name__)
bot_started = False

seen_email_ids = set()
seen_news_ids = set()
last_news_check = 0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nahhas-bot")

#=========================
# TELEGRAM SENDERS
#=========================
def send_private(msg):
    if not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=20)

def send_group(msg):
    if not TELEGRAM_GROUP_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_GROUP_ID, "text": msg}, timeout=20)

#=========================
# PARSE EMAIL
#=========================
def extract_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(errors="ignore")
    return msg.get_payload(decode=True).decode(errors="ignore")

def parse_signal(text):
    if "NAHHAS_SIGNAL" not in text:
        return None

    data = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip().lower()] = v.strip()

    if data.get("secret") != SIGNAL_SECRET:
        return None

    return data

#=========================
# BUILD MESSAGE
#=========================
def build_signal_message(d):
    return (
        f"🔥 {d.get('signal')} {d.get('symbol')}\n\n"
        f"Type: {d.get('type')}\n"
        f"Timeframe: {d.get('timeframe')}\n"
        f"Entry: {d.get('price')}\n"
        f"SL: {d.get('sl')}\n"
        f"TP1: {d.get('tp1')}\n"
        f"TP2: {d.get('tp2')}\n"
        f"TP3: {d.get('tp3')}\n\n"
        f"Reason: {d.get('reason')}"
    )

#=========================
# EMAIL CHECK
#=========================
def check_email_once():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    status, data = mail.search(None, '(UNSEEN FROM "noreply@tradingview.com")')
    if status != "OK":
        mail.logout()
        return

    for num in data[0].split():
        if num in seen_email_ids:
            continue

        _, msg_data = mail.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        body = extract_body(msg)
        signal = parse_signal(body)

        if signal:
            message = build_signal_message(signal)

            #=========================
            # FILTER LOGIC 🔥
            #=========================
            signal_type = signal.get("type", "").upper()

            if signal_type == "STRONG_ORDER":
                send_private(message)
                send_group(message)   # فقط STRONG يروح للجروب
            else:
                send_private(message)  # باقي الإشارات خاص فقط

        mail.store(num, "+FLAGS", "\\Seen")
        seen_email_ids.add(num)

    mail.logout()

#=========================
# NEWS (خاص فقط)
#=========================
def check_news():
    global last_news_check

    if time.time() - last_news_check < NEWS_REFRESH_SECONDS:
        return

    last_news_check = time.time()

    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": "gold market"},
            timeout=20
        )

        if r.status_code != 200:
            return

        news = r.json().get("news", [])
        for item in news[:5]:
            title = item.get("title", "")

            if title in seen_news_ids:
                continue

            seen_news_ids.add(title)

            msg = f"🚨 NEWS\n\n{title}"
            send_private(msg)   # 🔥 خبر للخاص فقط
            break

    except:
        pass

#=========================
# LOOP
#=========================
def bot_loop():
    send_private("✅ Bot Started")

    while True:
        try:
            check_email_once()
            check_news()
        except Exception as e:
            logger.error(e)

        time.sleep(EMAIL_POLL_SECONDS)

#=========================
# START
#=========================
def start_bot_once():
    global bot_started
    if not bot_started:
        bot_started = True
        threading.Thread(target=bot_loop, daemon=True).start()

start_bot_once()

@app.route("/")
def home():
    return "Bot Running"
