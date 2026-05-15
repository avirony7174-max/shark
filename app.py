from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

BINANCE_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
COINGLASS_URL = "https://open-api.coinglass.com/public/v2"
CG_KEY = os.environ.get("COINGLASS_API_KEY", "")
COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","AVAXUSDT","LTCUSDT"]

@app.route("/")
def home():
    return jsonify({"status": "CryptoEdge AI Backend Running"})

@app.route("/api/scan")
def scan():
    results = []
    for sym in COINS:
        try:
            r = requests.get(f"{BINANCE_URL}?symbol={sym}", timeout=8)
            d = r.json()
            results.append({
                "symbol": sym,
                "price": str(d.get("lastPrice", "0")),
                "change": str(d.get("priceChangePercent", "0")),
                "volume": str(d.get("quoteVolume", "0")),
                "high": str(d.get("highPrice", "0")),
                "low": str(d.get("lowPrice", "0")),
            })
        except:
            results.append({"symbol": sym,"price":"0","change":"0","volume":"0","high":"0","low":"0"})
    results.sort(key=lambda x: abs(float(x["change"] or 0)), reverse=True)
    return jsonify({"coins": results, "status": "ok"})

@app.route("/api/coinglass/funding")
def cg_funding():
    symbol = request.args.get("symbol", "BTC")
    cg_key = request.headers.get("CG-API-KEY") or CG_KEY
    if not cg_key:
        return jsonify({"status": "error", "msg": "No CoinGlass API key"}), 401
    try:
        r = requests.get(f"{COINGLASS_URL}/indicator/funding_rate?symbol={symbol}&exchange=Binance", headers={"CG-API-KEY": cg_key}, timeout=8)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/api/coinglass/oi")
def cg_oi():
    symbol = request.args.get("symbol", "BTC")
    cg_key = request.headers.get("CG-API-KEY") or CG_KEY
    if not cg_key:
        return jsonify({"status": "error", "msg": "No CoinGlass API key"}), 401
    try:
        r = requests.get(f"{COINGLASS_URL}/indicator/open_interest?symbol={symbol}&exchange=Binance", headers={"CG-API-KEY": cg_key}, timeout=8)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
