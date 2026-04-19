import os
import time
import threading
import requests
from flask import Flask

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BREAKING_KEYWORDS = [
    "trump", "tariff", "war", "iran", "middle east",
    "fed", "emergency", "oil", "sanctions"
]

app = Flask(__name__)
bot_started = False

@app.route("/")
def home():
    return "Bot is running"

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=20)

def check_breaking_news():
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search?q=gold"
        r = requests.get(url, timeout=20)
        data = r.json()
        news = data.get("news", [])

        for n in news[:5]:
            title = n.get("title", "")
            title_lower = title.lower()
            for word in BREAKING_KEYWORDS:
                if word in title_lower:
                    return title
    except Exception:
        return None
    return None

def bot_loop():
    send_telegram("✅ Gold Bot V3 started successfully")
    last_sent = None

    while True:
        news = check_breaking_news()
        if news and news != last_sent:
            send_telegram(f"🚨 BREAKING NEWS\n\n{news}")
            last_sent = news
        time.sleep(120)

def start_bot_once():
    global bot_started
    if not bot_started:
        bot_started = True
        threading.Thread(target=bot_loop, daemon=True).start()

start_bot_once()
