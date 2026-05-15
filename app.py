from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, os

app = Flask(__name__)
CORS(app)

COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","AVAXUSDT","LTCUSDT"]
BASE = "https://fapi.binance.com/fapi/v1/ticker/24hr"
CG = "https://open-api.coinglass.com/public/v2"
CG_KEY = os.environ.get("COINGLASS_API_KEY","")

@app.route("/")
def home():
    return jsonify({"status":"CryptoEdge AI Backend Running"})

@app.route("/api/scan")
def scan():
    out=[]
    for s in COINS:
        try:
            d=requests.get(f"{BASE}?symbol={s}",timeout=8).json()
            out.append({"symbol":s,"price":d["lastPrice"],"change":d["priceChangePercent"],"volume":d["quoteVolume"],"high":d["highPrice"],"low":d["lowPrice"]})
        except Exception as e:
            print(f"Error {s}: {e}")
            out.append({"symbol":s,"price":"0","change":"0","volume":"0","high":"0","low":"0"})
    out.sort(key=lambda x:abs(float(x["change"])),reverse=True)
    return jsonify({"coins":out,"status":"ok"})

@app.route("/api/coinglass/funding")
def funding():
    sym=request.args.get("symbol","BTC")
    key=request.headers.get("CG-API-KEY") or CG_KEY
    r=requests.get(f"{CG}/indicator/funding_rate?symbol={sym}&exchange=Binance",headers={"CG-API-KEY":key},timeout=8)
    return jsonify(r.json())

@app.route("/api/coinglass/oi")
def oi():
    sym=request.args.get("symbol","BTC")
    key=request.headers.get("CG-API-KEY") or CG_KEY
    r=requests.get(f"{CG}/indicator/open_interest?symbol={sym}&exchange=Binance",headers={"CG-API-KEY":key},timeout=8)
    return jsonify(r.json())

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
