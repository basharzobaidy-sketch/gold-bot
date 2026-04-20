# 🔥 Gold Bot V5.2 (More Signals Version)

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

SYMBOL = "GC=F"
CHART_INTERVAL = "5m"
CHART_RANGE = "5d"

POLL_EVERY_SECONDS = 120

LOOKBACK = 8
FAST_LEN = 20
SLOW_LEN = 50
TREND_LEN = 100
RSI_LEN = 14
ATR_LEN = 14

# =========================
# APP
# =========================
app = Flask(__name__)
bot_started = False
last_signal_key = None

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=20)

# =========================
# DATA
# =========================
def fetch_data():
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    r = requests.get(url, params={"interval": CHART_INTERVAL, "range": CHART_RANGE})
    data = r.json()["chart"]["result"][0]

    closes = data["indicators"]["quote"][0]["close"]
    highs = data["indicators"]["quote"][0]["high"]
    lows = data["indicators"]["quote"][0]["low"]
    opens = data["indicators"]["quote"][0]["open"]

    bars = []
    for i in range(len(closes)):
        if None in (closes[i], highs[i], lows[i], opens[i]):
            continue
        bars.append({
            "close": closes[i],
            "high": highs[i],
            "low": lows[i],
            "open": opens[i],
        })
    return bars

# =========================
# INDICATORS
# =========================
def ema(data, length):
    k = 2 / (length + 1)
    ema_vals = []
    sma = sum(data[:length]) / length
    ema_vals = [None]*(length-1) + [sma]

    for price in data[length:]:
        ema_vals.append(price * k + ema_vals[-1]*(1-k))
    return ema_vals

def rsi(data, length=14):
    gains, losses = [], []
    for i in range(1, length+1):
        diff = data[i] - data[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains)/length
    avg_loss = sum(losses)/length

    rsis = [None]*length
    rs = avg_gain/avg_loss if avg_loss != 0 else 0
    rsis.append(100 - (100/(1+rs)))

    for i in range(length+1, len(data)):
        diff = data[i] - data[i-1]
        gain = max(diff, 0)
        loss = max(-diff, 0)

        avg_gain = (avg_gain*(length-1)+gain)/length
        avg_loss = (avg_loss*(length-1)+loss)/length

        rs = avg_gain/avg_loss if avg_loss != 0 else 0
        rsis.append(100 - (100/(1+rs)))

    return rsis

# =========================
# ANALYSIS (🔥 المعدل)
# =========================
def analyze(bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]

    ef = ema(closes, FAST_LEN)
    es = ema(closes, SLOW_LEN)
    et = ema(closes, TREND_LEN)
    rsi_vals = rsi(closes)

    i = len(closes) - 1

    score_buy = 0
    score_sell = 0

    # 🔥 RSI أوسع
    if 50 < rsi_vals[i] < 72:
        score_buy += 1
    if 28 < rsi_vals[i] < 50:
        score_sell += 1

    # ترند
    if ef[i] > es[i] and closes[i] > et[i]:
        score_buy += 2
    if ef[i] < es[i] and closes[i] < et[i]:
        score_sell += 2

    # كسر
    if closes[i] > max(highs[-LOOKBACK:]):
        score_buy += 2
    if closes[i] < min(lows[-LOOKBACK:]):
        score_sell += 2

    # 🔥 شمعة أخف
    body = abs(closes[i] - opens[i])
    if body > 0.6:
        if closes[i] > opens[i]:
            score_buy += 1
        else:
            score_sell += 1

    # =========================
    # 🔥 شرط الدخول الجديد
    # =========================
    if score_buy >= 5 and score_buy > score_sell:
        return "BUY", score_buy

    if score_sell >= 5 and score_sell > score_buy:
        return "SELL", score_sell

    return "WAIT", max(score_buy, score_sell)

# =========================
# BOT LOOP
# =========================
def bot_loop():
    global last_signal_key

    send_telegram("✅ Gold Bot V5.2 started")

    while True:
        try:
            bars = fetch_data()
            signal, score = analyze(bars)

            print(f"Signal={signal} Score={score}")

            if signal != "WAIT":
                key = f"{signal}_{score}"

                if key != last_signal_key:
                    send_telegram(f"🔥 {signal} GOLD\nScore: {score}")
                    last_signal_key = key

        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_EVERY_SECONDS)

# =========================
# START
# =========================
def start():
    global bot_started
    if not bot_started:
        bot_started = True
        threading.Thread(target=bot_loop).start()

start()
