from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

# =========================
# CONFIG
# =========================

BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"

COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")

HEADERS = {
    "accept": "application/json"
}

# =========================
# ROOT
# =========================

@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "message": "AI Crypto Backend Online"
    })


# =========================
# MARKET DATA
# =========================

@app.route("/api/market-data")
def market_data():

    symbol = request.args.get("symbol", "BTCUSDT").upper()

    try:

        response = requests.get(
            f"{BINANCE_TICKER_URL}?symbol={symbol}",
            timeout=10
        )

        data = response.json()

        result = {
            "exchange": "Binance Futures",
            "symbol": symbol,
            "price": data.get("lastPrice"),
            "24h_change_percent": data.get("priceChangePercent"),
            "24h_volume": data.get("volume"),
            "high": data.get("highPrice"),
            "low": data.get("lowPrice"),
            "status": "connected"
        }

        return jsonify(result)

    except Exception as e:

        return jsonify({
            "status": "failed",
            "error": str(e)
        })


# =========================
# LIVE PRICE
# =========================

@app.route("/api/price")
def live_price():

    symbol = request.args.get("symbol", "BTCUSDT").upper()

    try:

        response = requests.get(
            f"{BINANCE_PRICE_URL}?symbol={symbol}",
            timeout=10
        )

        data = response.json()

        return jsonify({
            "symbol": symbol,
            "price": data.get("price"),
            "status": "connected"
        })

    except Exception as e:

        return jsonify({
            "status": "failed",
            "error": str(e)
        })


# =========================
# TOP GAINERS
# =========================

@app.route("/api/top-gainers")
def top_gainers():

    try:

        response = requests.get(
            BINANCE_TICKER_URL,
            timeout=15
        )

        data = response.json()

        sorted_data = sorted(
            data,
            key=lambda x: float(x["priceChangePercent"]),
            reverse=True
        )

        top_10 = []

        for coin in sorted_data[:10]:

            top_10.append({
                "symbol": coin["symbol"],
                "change_percent": coin["priceChangePercent"],
                "price": coin["lastPrice"]
            })

        return jsonify({
            "status": "connected",
            "top_gainers": top_10
        })

    except Exception as e:

        return jsonify({
            "status": "failed",
            "error": str(e)
        })


# =========================
# COINGLASS FUNDING RATE
# =========================

@app.route("/api/funding")
def funding_rate():

    symbol = request.args.get("symbol", "BTC")

    if not COINGLASS_API_KEY:

        return jsonify({
            "status": "failed",
            "error": "Missing COINGLASS_API_KEY"
        })

    try:

        url = f"https://open-api.coinglass.com/public/v2/funding_usd_history?symbol={symbol}"

        headers = {
            "accept": "application/json",
            "coinglassSecret": COINGLASS_API_KEY
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        data = response.json()

        return jsonify({
            "status": "connected",
            "data": data
        })

    except Exception as e:

        return jsonify({
            "status": "failed",
            "error": str(e)
        })


# =========================
# HEALTH CHECK
# =========================

@app.route("/health")
def health():

    return jsonify({
        "status": "healthy"
    })


# =========================
# RUN SERVER
# =========================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 3000))

    app.run(
        host="0.0.0.0",
        port=port
    )
