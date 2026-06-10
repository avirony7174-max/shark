from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

CG_BASE = "https://open-api-v4.coinglass.com/api"
CG_KEY = os.environ.get("COINGLASS_API_KEY", "")

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LTC"]

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "LTC": "litecoin"
}


def cg_headers():
    return {
        "CG-API-KEY": CG_KEY,
        "accept": "application/json"
    }


def fetch_prices():
    """CoinGecko free API — price, change, volume"""
    try:
        ids = ",".join(COINGECKO_IDS.values())
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
                "include_24h_high": "true",
                "include_24h_low": "true",
            },
            timeout=10
        )
        data = r.json()
        result = {}
        for sym, gecko_id in COINGECKO_IDS.items():
            d = data.get(gecko_id, {})
            result[sym] = {
                "price": str(d.get("usd", 0)),
                "change": str(round(d.get("usd_24h_change", 0), 2)),
                "volume": str(d.get("usd_24h_vol", 0)),
            }
        return result
    except Exception as e:
        print(f"CoinGecko error: {e}")
    return {}


def fetch_liquidation_data():
    """Liquidation data — confirmed Hobbyist"""
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
        print(f"Liquidation error: {e}")
    return {}


def fetch_oi_data():
    """Open Interest — confirmed Hobbyist"""
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
                all_ex = next(
                    (x for x in items if isinstance(x, dict) and x.get("exchange") == "All"),
                    None
                )
                if all_ex:
                    result[coin] = {
                        "oi": str(all_ex.get("open_interest_usd", 0)),
                        "oi_change": str(all_ex.get("open_interest_usd_change_percent", 0)),
                    }
        return result
    except Exception as e:
        print(f"OI error: {e}")
    return {}


def fetch_funding_data():
    """Funding Rate — confirmed Hobbyist
    Response: data = [{symbol, stablecoin_margin_list: [{exchange, funding_rate}], token_margin_list}]
    """
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
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    # Match symbol
                    if item.get("symbol", "").upper() != coin.upper():
                        continue
                    # Get Binance from stablecoin_margin_list
                    stablelist = item.get("stablecoin_margin_list", [])
                    binance = next(
                        (x for x in stablelist if x.get("exchange") == "Binance"),
                        None
                    )
                    if binance:
                        result[coin] = str(binance.get("funding_rate", 0))
                    elif stablelist:
                        result[coin] = str(stablelist[0].get("funding_rate", 0))
                    break
        return result
    except Exception as e:
        print(f"Funding error: {e}")
    return {}


@app.route("/")
def home():
    return jsonify({"status": "CryptoEdge AI Backend Running v3"})


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


@app.route("/api/scan")
def scan():
    prices = fetch_prices()
    liq_data = fetch_liquidation_data()
    oi_data = fetch_oi_data()
    funding_data = fetch_funding_data()

    out = []
    for sym in COINS:
        price_info = prices.get(sym, {})
        liq = liq_data.get(sym, {})
        oi = oi_data.get(sym, {})

        out.append({
            "symbol": sym + "USDT",
            "price": price_info.get("price", "0"),
            "change": price_info.get("change", "0"),
            "volume": price_info.get("volume", "0"),
            "oi": oi.get("oi", "0"),
            "oi_change": oi.get("oi_change", "0"),
            "long_liq": liq.get("long_liq", "0"),
            "short_liq": liq.get("short_liq", "0"),
            "funding": funding_data.get(sym, "0"),
        })

    out.sort(key=lambda x: abs(float(x["change"])), reverse=True)
    return jsonify({"coins": out, "status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
