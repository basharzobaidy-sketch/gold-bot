import os
import logging
from flask import Flask, request, jsonify
import requests

# =========================
# ENV
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me").strip()

# =========================
# APP
# =========================
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gocharting-webhook-bot")

# =========================
# HELPERS
# =========================
def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
        timeout=20
    )

def parse_plain_text_payload(text: str) -> dict:
    """
    Expected plain text format from GoCharting alert message:

    secret=YOUR_SECRET
    signal=BUY
    symbol=XAUUSD
    timeframe=5m
    price=3365.40
    sl=3358.20
    tp1=3373.10
    tp2=3380.50
    reason=Breakout retest

    """
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip().lower()] = v.strip()
    return data

def build_message(data: dict) -> str:
    signal = data.get("signal", "UNKNOWN").upper()
    symbol = data.get("symbol", "XAUUSD")
    timeframe = data.get("timeframe", "")
    price = data.get("price", "-")
    sl = data.get("sl", "-")
    tp1 = data.get("tp1", "-")
    tp2 = data.get("tp2", "-")
    reason = data.get("reason", "-")

    signal_ar = "شراء" if signal == "BUY" else ("بيع" if signal == "SELL" else "إشارة")

    return (
        f"🔥 {signal} {symbol} ({signal_ar})\n\n"
        f"Timeframe: {timeframe}\n"
        f"Current Price: {price}\n"
        f"Entry: {price}\n"
        f"SL: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n\n"
        f"Reason: {reason}"
    )

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "GoCharting webhook bot is running"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_data(as_text=True) or ""
        logger.info("Received webhook raw payload: %s", raw)

        data = parse_plain_text_payload(raw)

        if not data:
            return jsonify({"ok": False, "error": "empty or invalid payload"}), 400

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "invalid secret"}), 403

        msg = build_message(data)
        send_telegram(msg)

        return jsonify({"ok": True}), 200

    except Exception as exc:
        logger.exception("Webhook failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
