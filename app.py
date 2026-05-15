from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, os

app = Flask(__name__)
CORS(app)

CG_KEY = os.environ.get("COINGLASS_API_KEY", "91c37972bd3d4cfcb77bcd50e29a84f1")
SYMBOLS = ["BTC","ETH","SOL","BNB","XRP","DOGE","AVAX","LTC"]
CG_BASE = "https://open-api-v4.coinglass.com/api"

@app.route("/")
def home():
    return jsonify({"status": "CryptoEdge AI Backend Running"})

@app.route("/api/scan")
def scan():
    key = request.headers.get("CG-API-KEY") or CG_KEY
    out = []
    for sym in SYMBOLS:
        try:
            r = requests.get(
                f"{CG_BASE}/futures/pairs-markets?symbol={sym}",
                headers={"CG-API-KEY": key},
                timeout=10
            )
            data = r.json()
            print(f"{sym}: {data.get('code')} {data.get('msg')}")
            items = data.get("data", [])
            binance = next((x for x in items if x.get("exchange_name") == "Binance"), None)
            if binance:
                out.append({
                    "symbol": sym+"USDT",
                    "price": str(binance.get("current_price", 0)),
                    "change": str(binance.get("price_change_percent_24h", 0)),
                    "volume": str(binance.get("volume_usd", 0)),
                    "oi": str(binance.get("open_interest_usd", 0)),
                    "oi_change": str(binance.get("open_interest_change_percent_24h", 0)),
                    "long_liq": str(binance.get("long_liquidation_usd_24h", 0)),
                    "short_liq": str(binance.get("short_liquidation_usd_24h", 0)),
                    "long_vol": str(binance.get("long_volume_usd", 0)),
                    "short_vol": str(binance.get("short_volume_usd", 0)),
                    "funding": str(binance.get("avg_funding_rate_by_oi", 0)),
                })
        except Exception as e:
            print(f"Error {sym}: {e}")
    out.sort(key=lambda x: abs(float(x["change"])), reverse=True)
    return jsonify({"coins": out, "status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
