from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, os

app = Flask(__name__)
CORS(app)

CG_KEY = os.environ.get("91c37972bd3d4cfcb77bcd50e29a84f1", "")
COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","AVAXUSDT","LTCUSDT"]
CG_BASE = "https://open-api.coinglass.com/api"

@app.route("/")
def home():
    return jsonify({"status": "CryptoEdge AI Backend Running"})

@app.route("/api/scan")
def scan():
    key = request.headers.get("CG-API-KEY") or CG_KEY
    try:
        r = requests.get(
            f"{CG_BASE}/futures/pairs/markets?exchanges=Binance",
            headers={"CG-API-KEY": key},
            timeout=10
        )
        data = r.json()
        out = []
        for item in data.get("data", []):
            sym = item.get("instrument_id","")
            if sym in COINS:
                out.append({
                    "symbol": sym,
                    "price": str(item.get("current_price", 0)),
                    "change": str(item.get("price_change_percent_24h", 0)),
                    "volume": str(item.get("volume_usd", 0)),
                    "oi": str(item.get("open_interest_usd", 0)),
                    "oi_change": str(item.get("open_interest_change_percent_24h", 0)),
                    "long_liq": str(item.get("long_liquidation_usd_24h", 0)),
                    "short_liq": str(item.get("short_liquidation_usd_24h", 0)),
                    "long_vol": str(item.get("long_volume_usd", 0)),
                    "short_vol": str(item.get("short_volume_usd", 0)),
                    "funding": str(item.get("avg_funding_rate_by_oi", 0)),
                })
        out.sort(key=lambda x: abs(float(x["change"])), reverse=True)
        return jsonify({"coins": out, "status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/api/funding")
def funding():
    key = request.headers.get("CG-API-KEY") or CG_KEY
    sym = request.args.get("symbol", "BTC")
    try:
        r = requests.get(
            f"{CG_BASE}/futures/funding-rate/current?symbol={sym}",
            headers={"CG-API-KEY": key},
            timeout=10
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
