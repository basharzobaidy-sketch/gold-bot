import os
import time
import requests
from flask import Flask
import threading

# ================== إعداد التليجرام ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ================== كلمات الأخبار ==================
BREAKING_KEYWORDS = [
    "trump", "tariff", "war", "iran", "middle east",
    "fed", "emergency", "oil", "sanctions"
]

# ================== إرسال رسالة ==================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

# ================== جلب الأخبار ==================
def check_breaking_news():
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search?q=gold"
        r = requests.get(url)
        data = r.json()

        news = data.get("news", [])

        for n in news[:5]:
            title = n.get("title", "").lower()

            for word in BREAKING_KEYWORDS:
                if word in title:
                    return n.get("title")

    except:
        return None

    return None

# ================== Flask (حل مشكلة Render) ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def run_server():
    app.run(host='0.0.0.0', port=10000)

# تشغيل السيرفر بالخلفية
threading.Thread(target=run_server, daemon=True).start()

# ================== تشغيل البوت ==================
def main():
    send_telegram("✅ Gold Bot V3 started successfully")

    while True:
        news = check_breaking_news()

        if news:
            send_telegram(f"🚨 BREAKING NEWS\n\n{news}")

        time.sleep(120)

if __name__ == "__main__":
    main()
