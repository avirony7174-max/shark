from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

# Config
BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr"
CG_BASE = "https://open-api-v4.coinglass.com/api"
CG_KEY = os.environ.get("COINGLASS_API_KEY", "")

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LTC"]
COINS_USDT = [c + "USDT" for c in COINS]


def cg_headers():
    return {
        "CG-API-KEY": CG_KEY,
        "accept": "application/json"
    }


def fetch_liquidation_data():
    """Fetch liquidation data for all coins — confirmed Hobbyist endpoint"""
    try:
        r = requests.get(
            f"{CG_BASE}/futures/liquidation/coin-list",
            headers=cg_headers(),
            timeout=10
        )
        data = r.json()
        if data.get("code") == "0":
            result = {}
            for item in data.get("data", []):
                sym = item.get("symbol", "").upper()
                result[sym] = {
                    "long_liq": str(item.get("long_liquidation_usd_24h", 0)),
                    "short_liq": str(item.get("short_liquidation_usd_24h", 0)),
                }
            return result
    except Exception as e:
        print(f"Liquidation fetch error: {e}")
    return {}


def fetch_funding_data():
    """Fetch funding rate data — confirmed Hobbyist endpoint"""
    try:
        result = {}
        for coin in COINS:
            r = requests.get(
                f"{CG_BASE}/futures/funding-rate/exchange-list",
                headers=cg_headers(),
                params={"symbol": coin},
                timeout=10
            )
            data = r.json()
            if data.get("code") == "0":
                items = data.get("data", [])
                # Get Binance funding rate
                binance = next(
                    (x for x in items
                     if isinstance(x, dict) and x.get("exchange") == "Binance"),
                    None
                )
                if binance:
                    result[coin] = str(binance.get("funding_rate", 0))
                elif items:
                    # fallback: first item
                    first = items[0] if isinstance(items[0], dict) else {}
                    result[coin] = str(first.get("funding_rate", 0))
        return result
    except Exception as e:
        print(f"Funding fetch error: {e}")
    return {}


def fetch_oi_data():
    """Fetch open interest data — confirmed Hobbyist endpoint"""
    try:
        result = {}
        for coin in COINS:
            r = requests.get(
                f"{CG_BASE}/futures/open-interest/exchange-list",
                headers=cg_headers(),
                params={"symbol": coin},
                timeout=10
            )
            data = r.json()
            if data.get("code") == "0":
                items = data.get("data", [])
                # Get "All" exchange for total OI
                all_ex = next(
                    (x for x in items
                     if isinstance(x, dict) and x.get("exchange") == "All"),
                    None
                )
                if all_ex:
                    result[coin] = {
                        "oi": str(all_ex.get("open_interest_usd", 0)),
                        "oi_change": str(all_ex.get("open_interest_usd_change_percent", 0)),
                    }
        return result
    except Exception as e:
        print(f"OI fetch error: {e}")
    return {}


@app.route("/")
def home():
    return jsonify({"status": "CryptoEdge AI Backend Running v3"})


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


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
    # Fetch all CoinGlass data in parallel batches
    liq_data = fetch_liquidation_data()
    oi_data = fetch_oi_data()
    funding_data = fetch_funding_data()

    out = []
    for sym_usdt in COINS_USDT:
        sym = sym_usdt.replace("USDT", "")
        try:
            # Binance price data
            r = requests.get(f"{BINANCE_URL}?symbol={sym_usdt}", timeout=8)
            d = r.json()

            # CoinGlass data
            liq = liq_data.get(sym, {})
            oi = oi_data.get(sym, {})

            out.append({
                "symbol": sym_usdt,
                "price": d.get("lastPrice", "0"),
                "change": d.get("priceChangePercent", "0"),
                "volume": d.get("quoteVolume", "0"),
                "high": d.get("highPrice", "0"),
                "low": d.get("lowPrice", "0"),
                "oi": oi.get("oi", "0"),
                "oi_change": oi.get("oi_change", "0"),
                "long_liq": liq.get("long_liq", "0"),
                "short_liq": liq.get("short_liq", "0"),
                "funding": funding_data.get(sym, "0"),
            })
        except Exception as e:
            print(f"Error {sym_usdt}: {e}")

    out.sort(key=lambda x: abs(float(x["change"])), reverse=True)
    return jsonify({"coins": out, "status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
