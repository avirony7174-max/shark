from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

CG_BASE = "https://open-api-v4.coinglass.com/api"
CG_KEY = os.environ.get("COINGLASS_API_KEY", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TV_WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "mysecret")

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


def send_telegram(message):
    try:
        token = TELEGRAM_BOT_TOKEN
        chat_id = TELEGRAM_CHAT_ID
        if not token or not chat_id:
            print("Telegram credentials missing")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def fetch_prices():
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
                    if item.get("symbol", "").upper() != coin.upper():
                        continue
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


@app.route("/tv/<secret>", methods=["POST"])
def tradingview_webhook(secret):
    if secret != TV_WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True) or {}
        signal    = data.get("signal", "").upper()
        symbol    = data.get("symbol", "BTCUSDT")
        timeframe = data.get("timeframe", "Daily")
        entry     = data.get("entry", "—")
        sl        = data.get("sl", "—")
        tp        = data.get("tp", "—")

        if signal == "LONG":
            emoji = "🟢"
            label = "LONG SIGNAL"
        elif signal == "SHORT":
            emoji = "🔴"
            label = "SHORT SIGNAL"
        else:
            return jsonify({"status": "ignored"}), 200

        message = (
            f"{emoji} <b>{label}</b>\n"
            f"<b>{symbol}</b> · {timeframe}\n\n"
            f"Entry:  <code>{entry}</code>\n"
            f"SL:       <code>{sl}</code>\n"
            f"TP:       <code>{tp}</code>\n\n"
            f"<i>Daily EMA Swing 21/50</i>"
        )

        send_telegram(message)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
