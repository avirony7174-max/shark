import os
import time
import requests
import threading
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT"]
VALID_COINS = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "LINK": "LINKUSDT",
}

EMA_FAST       = 21
EMA_SLOW       = 50
VOL_SMA        = 10
CHECK_INTERVAL = 900


def send_telegram(message):
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print("Telegram error: " + str(e))


def fetch_daily_candles(symbol, limit=60):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=10
        )
        candles = []
        for c in r.json():
            candles.append({
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        return candles
    except Exception as e:
        print("Candle error " + symbol + ": " + str(e))
        return []


def fetch_price_ticker(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=10
        )
        d = r.json()
        return {
            "price":  float(d.get("lastPrice", 0)),
            "change": float(d.get("priceChangePercent", 0)),
        }
    except Exception as e:
        print("Ticker error: " + str(e))
        return {}


def fetch_oi(symbol):
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=10
        )
        oi = float(r.json().get("openInterest", 0))
        p  = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=10
        )
        price =
