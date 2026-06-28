import asyncio
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
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def fetch_daily_candles(symbol, limit=60):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": "D", "limit": limit},
            timeout=10
        )
        data = r.json()
        if data.get("retCode") != 0:
            print(f"Bybit klines error {symbol}: {data}")
            return []
        candles = []
        for row in reversed(data["result"]["list"]):
            candles.append({
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            })
        return candles
    except Exception as e:
        print(f"Bybit candle error {symbol}: {e}")
        return []


def fetch_price_ticker(symbol):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=10
        )
        d = r.json()
        if d.get("retCode") != 0 or not d["result"]["list"]:
            print(f"Bybit ticker error {symbol}: {d}")
            return {}
        t = d["result"]["list"][0]
        return {
            "price":  float(t.get("lastPrice", 0)),
            "change": round(float(t.get("price24hPcnt", 0)) * 100, 2),
        }
    except Exception as e:
        print(f"Ticker error {symbol}: {e}")
        return {}


def _bybit_linear_ticker(symbol):
    r = requests.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear", "symbol": symbol},
        timeout=10
    )
    d = r.json()
    if d.get("retCode") != 0 or not d["result"]["list"]:
        return None
    return d["result"]["list"][0]


def fetch_oi(symbol):
    try:
        t = _bybit_linear_ticker(symbol)
        if t is None:
            return None
        return round(float(t.get("openInterestValue", 0)) / 1e9, 2)
    except Exception as e:
        print(f"OI error {symbol}: {e}")
    return None


def fetch_funding(symbol):
    try:
        t = _bybit_linear_ticker(symbol)
        if t is None:
            return None
        return round(float(t.get("fundingRate", 0)) * 100, 4)
    except Exception as e:
        print(f"Funding error {symbol}: {e}")
    return None


def fetch_taker_volume(symbol):
    return None, None


def fetch_ls_ratio(symbol):
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/account-ratio",
            params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1},
            timeout=10
        )
        d = r.json()
        if d.get("retCode") != 0 or not d["result"]["list"]:
            return None, None, None
        item = d["result"]["list"][0]
        buy   = float(item.get("buyRatio", 0))
        sell  = float(item.get("sellRatio", 0))
        ratio = round(buy / sell, 2) if sell > 0 else 0
        return ratio, round(buy * 100, 1), round(sell * 100, 1)
    except Exception as e:
        print(f"L/S error {symbol}: {e}")
    return None, None, None


def fetch_top_trader(symbol):
    return None, None



def calc_ema_series(closes, period):
    k = 2 / (period + 1)
    ema = [closes[0]]
    for price in closes[1:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calc_sma(values, period):
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period


def check_signal(candles):
    if len(candles) < EMA_SLOW + 5:
        return None

    closes  = [c["close"]  for c in candles]
    opens   = [c["open"]   for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]

    ema_fast_s = calc_ema_series(closes, EMA_FAST)
    ema_slow_s = calc_ema_series(closes, EMA_SLOW)

    ef  = ema_fast_s[-2];  es  = ema_slow_s[-2]
    ef1 = ema_fast_s[-3];  es1 = ema_slow_s[-3]

    c_close = closes[-2];  c_open  = opens[-2]
    c_high  = highs[-2];   c_low   = lows[-2]
    c_vol   = volumes[-2]; p_low   = lows[-3]; p_high = highs[-3]

    vol_avg      = calc_sma(volumes[:-1], VOL_SMA)
    body         = abs(c_close - c_open)
    candle_range = c_high - c_low
    body_ratio   = body / candle_range if candle_range > 0 else 0

    bull_trend    = ef > es
    vol_ok        = c_vol > vol_avg
    body_ok       = body_ratio >= 0.6
    long_close_ok = candle_range > 0 and (c_high - c_close) / candle_range <= 0.35
    long_pullback = bull_trend and (p_low <= ef1 or (p_low <= es1 and p_high >= ef1))
    long_cond     = (bull_trend and long_pullback and c_close > ef
                     and c_close > c_open and vol_ok and body_ok and long_close_ok)

    atr_vals = [candles[i]["high"] - candles[i]["low"] for i in range(-15, -1)]
    atr = sum(atr_vals) / len(atr_vals)

    if long_cond:
        sl = c_close - atr * 1.5
        tp = c_close + (c_close - sl) * 3.0
        return {
            "entry": round(c_close, 2),
            "sl":    round(sl, 2),
            "tp":    round(tp, 2),
        }
    return None


def get_full_analysis(symbol):
    coin    = symbol.replace("USDT", "")
    ticker  = fetch_price_ticker(symbol)
    candles = fetch_daily_candles(symbol, limit=60)
    oi                          = fetch_oi(symbol)
    funding                     = fetch_funding(symbol)
    buy_pct, sell_pct           = fetch_taker_volume(symbol)
    ls_ratio, ls_long, ls_short = fetch_ls_ratio(symbol)
    tt_long, tt_short           = fetch_top_trader(symbol)
    sig                         = check_signal(candles) if candles else None

    price  = ticker.get("price", 0)
    change = ticker.get("change", 0)
    ch_icon = "▲" if change >= 0 else "▼"
    ch_sign = "+" if change >= 0 else ""

    closes = [c["close"] for c in candles] if candles else []
    ef = round(calc_ema_series(closes, EMA_FAST)[-1], 2) if len(closes) >= EMA_FAST else "—"
    es = round(calc_ema_series(closes, EMA_SLOW)[-1], 2) if len(closes) >= EMA_SLOW else "—"
    bias = "Bullish ✅" if (isinstance(ef, float) and isinstance(es, float) and ef > es) else "Bearish ❌"

    oi_line      = f"<code>{oi}B</code>" if oi else "—"
    f_sign       = "+" if funding and funding >= 0 else ""
    f_pay        = "Longs pay" if funding and funding >= 0 else "Shorts pay"
    funding_line = f"<code>{f_sign}{funding}%</code>  ({f_pay})" if funding is not None else "—"
    taker_line   = f"Buy <code>{buy_pct}%</code>  Sell <code>{sell_pct}%</code>" if buy_pct else "—"
    ls_line      = f"<code>{ls_ratio}</code>  (Long <code>{ls_long}%</code> · Short <code>{ls_short}%</code>)" if ls_ratio else "—"
    tt_line      = f"Long <code>{tt_long}%</code>  Short <code>{tt_short}%</code>" if tt_long else "—"

    if sig:
        signal_line = (
            f"🎯 <b>LONG Setup Active</b>\n"
            f"Entry: <code>{sig['entry']}</code>\n"
            f"SL:      <code>{sig['sl']}</code>\n"
            f"TP:      <code>{sig['tp']}</code>"
        )
    else:
        signal_line = "⏳ No signal — waiting for setup"

    return (
        f"📊 <b>{coin}/USDT Analysis</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Price:   <code>${price:,.2f}</code>  {ch_icon} {ch_sign}{change}%\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 <b>Trend</b>\n"
        f"EMA 21:  <code>{ef}</code>\n"
        f"EMA 50:  <code>{es}</code>\n"
        f"Bias:      {bias}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 <b>Futures Data</b>\n"
        f"OI:          {oi_line}\n"
        f"Funding:  {funding_line}\n"
        f"L/S:          {ls_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🐋 <b>Whale Activity</b>\n"
        f"Taker:       {taker_line}\n"
        f"Top Trader: {tt_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{signal_line}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text in VALID_COINS:
        symbol = VALID_COINS[text]
        await update.message.reply_text("⏳ Fetching data...")
        try:
            loop = asyncio.get_running_loop()
            msg = await loop.run_in_executor(None, get_full_analysis, symbol)
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            print(f"handle_message error: {e}")
            await update.message.reply_text("❌ Error fetching data. Try again.")
    else:
        await update.message.reply_text(
            "Send a coin name:\n<b>BTC · ETH · SOL · LINK</b>",
            parse_mode="HTML"
        )


def auto_signal_loop():
    armed         = {coin: True for coin in COINS}
    last_sig_type = {coin: None for coin in COINS}

    def loop():
        while True:
            now = datetime.now(timezone.utc)
            print(f"[{now.strftime('%H:%M UTC')}] Auto signal check...")
            for symbol in COINS:
                try:
                    candles = fetch_daily_candles(symbol, limit=60)
                    if not candles:
                        continue
                    sig = check_signal(candles)
                    if sig:
                        if armed[symbol] and last_sig_type[symbol] != "LONG":
                            oi      = fetch_oi(symbol)
                            funding = fetch_funding(symbol)
                            coin    = symbol.replace("USDT", "")
                            f_sign  = "+" if funding and funding >= 0 else ""
                            f_pay   = "Longs pay" if funding and funding >= 0 else "Shorts pay"
                            msg = (
                                f"🟢 <b>LONG SIGNAL</b>\n"
                                f"<b>{coin}/USDT</b> · Daily\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"Entry:  <code>{sig['entry']}</code>\n"
                                f"SL:       <code>{sig['sl']}</code>\n"
                                f"TP:       <code>{sig['tp']}</code>\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"OI:        <code>{oi}B</code>\n"
                                f"Funding: <code>{f_sign}{funding}%</code>  ({f_pay})\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"<i>EMA Swing 21/50 · RR 1:3</i>"
                            )
                            send_telegram(msg)
                            print(f"Signal sent: {symbol} LONG")
                            armed[symbol]         = False
                            last_sig_type[symbol] = "LONG"
                    else:
                        armed[symbol] = True
                except Exception as e:
                    print(f"Error {symbol}: {e}")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


def main():
    print("Signal Bot starting...")
    send_telegram(
        "✅ <b>Signal Bot চালু হয়েছে</b>\n"
        "BTC · ETH · SOL · LINK monitoring\n\n"
        "যেকোনো সময় লিখো:\n"
        "<b>BTC</b> বা <b>ETH</b> বা <b>SOL</b> বা <b>LINK</b>"
    )

    auto_signal_loop()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot polling started")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
