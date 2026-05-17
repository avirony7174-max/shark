from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

BINANCE_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","AVAXUSDT","LTCUSDT"]

@app.route("/")
def home():
    return jsonify({"status": "CryptoEdge AI Backend Running v2"})

@app.route("/api/market-data")
def market_data():
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    try:
        r = requests.get(f"{BINANCE_URL}?symbol={symbol}", timeout=10)
        d = r.json()
        return jsonify({
            "symbol": symbol,
            "price": d.get("lastPrice"),
            "change": d.get("priceChangePercent"),
            "volume": d.get("quoteVolume"),
            "high": d.get("highPrice"),
            "low": d.get("lowPrice"),
            "status": "connected"
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route("/api/scan")
def scan():
    out = []
    for sym in COINS:
        try:
            r = requests.get(f"{BINANCE_URL}?symbol={sym}", timeout=8)
            d = r.json()
            out.append({
                "symbol": sym,
                "price": d.get("lastPrice", "0"),
                "change": d.get("priceChangePercent", "0"),
                "volume": d.get("quoteVolume", "0"),
                "high": d.get("highPrice", "0"),
                "low": d.get("lowPrice", "0"),
                "oi": "0",
                "oi_change": "0",
                "long_liq": "0",
                "short_liq": "0",
                "funding": "0",
                "long_vol": "0",
                "short_vol": "0",
            })
        except Exception as e:
            print(f"Error {sym}: {e}")
    out.sort(key=lambda x: abs(float(x["change"])), reverse=True)
    return jsonify({"coins": out, "status": "ok"})

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# v2
