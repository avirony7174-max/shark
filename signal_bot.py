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

SR_LOOKBACK    = 5      # candles each side to confirm a swing high/low
SR_TOLERANCE   = 0.015  # cluster swing points within 1.5% as one level
SR_LEVELS      = 2      # how many S/R levels to show each side

LIQ_BUCKET_PCT = 0.002  # 0.2% price bucket width for order book clustering
LIQ_CLUSTERS   = 2      # how many liquidity walls to show each side
LIQ_DEPTH      = 500    # order book depth to fetch


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
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list):
            print(f"Binance klines error {symbol}: {data}")
            return []
        candles = []
        for c in data:
            candles.append({
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        return candles
    except Exception as e:
        print(f"Binance candle error {symbol}: {e}")
        return []


def fetch_price_ticker(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=10
        )
        d = r.json()
        return {
            "price":  float(d.get("lastPrice", 0)),
            "change": float(d.get("priceChangePercent", 0)),
        }
    except Exception as e:
        print(f"Ticker error {symbol}: {e}")
        return {}


def fetch_oi(symbol):
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=10
        )
        data = r.json()
        oi = float(data.get("openInterest", 0))
        price_r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=10
        )
        price = float(price_r.json().get("price", 0))
        oi_usd = round(oi * price / 1e9, 2)
        return oi_usd
    except Exception as e:
        print(f"OI error {symbol}: {e}")
    return None


def fetch_funding(symbol):
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=10
        )
        data = r.json()
        if data and isinstance(data, list):
            rate = float(data[0].get("fundingRate", 0)) * 100
            return round(rate, 4)
    except Exception as e:
        print(f"Funding error {symbol}: {e}")
    return None


def fetch_taker_volume(symbol):
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol": symbol, "period": "1h", "limit": 1},
            timeout=10
        )
        data = r.json()
        if data and isinstance(data, list):
            buy  = float(data[0].get("buyVol", 0))
            sell = float(data[0].get("sellVol", 0))
            total = buy + sell
            if total > 0:
                return round(buy/total*100, 1), round(sell/total*100, 1)
    except Exception as e:
        print(f"Taker error {symbol}: {e}")
    return None, None


def fetch_ls_ratio(symbol):
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "1h", "limit": 1},
            timeout=10
        )
        data = r.json()
        if data and isinstance(data, list):
            ratio = float(data[0].get("longShortRatio", 0))
            long  = float(data[0].get("longAccount", 0)) * 100
            short = float(data[0].get("shortAccount", 0)) * 100
            return round(ratio, 2), round(long, 1), round(short, 1)
    except Exception as e:
        print(f"L/S error {symbol}: {e}")
    return None, None, None


def fetch_top_trader(symbol):
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
            params={"symbol": symbol, "period": "1h", "limit": 1},
            timeout=10
        )
        data = r.json()
        if data and isinstance(data, list):
            long  = float(data[0].get("longAccount", 0)) * 100
            short = float(data[0].get("shortAccount", 0)) * 100
            return round(long, 1), round(short, 1)
    except Exception as e:
        print(f"Top trader error {symbol}: {e}")
    return None, None


def fetch_order_book_clusters(symbol, current_price, bucket_pct=LIQ_BUCKET_PCT,
                               num_clusters=LIQ_CLUSTERS, depth_limit=LIQ_DEPTH):
    if not current_price:
        return [], []
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": symbol, "limit": depth_limit},
            timeout=10
        )
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        bucket_size = current_price * bucket_pct
        if bucket_size <= 0:
            return [], []

        def cluster_side(orders):
            buckets = {}
            for p_str, q_str in orders:
                p = float(p_str)
                q = float(q_str)
                notional = p * q
                key = round(p / bucket_size) * bucket_size
                buckets[key] = buckets.get(key, 0) + notional
            ranked = sorted(buckets.items(), key=lambda x: -x[1])
            return ranked[:num_clusters]

        bid_clusters = cluster_side(bids)   # support-side liquidity (below price)
        ask_clusters = cluster_side(asks)   # resistance-side liquidity (above price)

        ask_clusters.sort(key=lambda x: x[0])    # nearest resistance wall first
        bid_clusters.sort(key=lambda x: -x[0])   # nearest support wall first

        return bid_clusters, ask_clusters
    except Exception as e:
        print(f"Order book error {symbol}: {e}")
        return [], []


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


def calc_support_resistance(candles, current_price, lookback=SR_LOOKBACK,
                             tolerance=SR_TOLERANCE, num_levels=SR_LEVELS):
    if not current_price or len(candles) < lookback * 2 + 3:
        return [], []

    scan = candles[:-1]  # exclude current/incomplete candle
    highs = [c["high"] for c in scan]
    lows  = [c["low"]  for c in scan]
    n = len(scan)

    swing_highs, swing_lows = [], []
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback:i + lookback + 1]
        window_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_h):
            swing_highs.append(highs[i])
        if lows[i] == min(window_l):
            swing_lows.append(lows[i])

    def cluster(levels):
        if not levels:
            return []
        levels = sorted(levels)
        clusters, bucket = [], [levels[0]]
        for lvl in levels[1:]:
            if lvl <= bucket[-1] * (1 + tolerance):
                bucket.append(lvl)
            else:
                clusters.append(bucket)
                bucket = [lvl]
        clusters.append(bucket)
        return [(sum(c) / len(c), len(c)) for c in clusters]

    resistance = [c for c in cluster(swing_highs) if c[0] > current_price]
    support    = [c for c in cluster(swing_lows)  if c[0] < current_price]

    resistance.sort(key=lambda x: -x[1])  # strongest (most touches) first
    support.sort(key=lambda x: -x[1])

    resistance = sorted(resistance[:num_levels], key=lambda x: x[0])   # nearest first
    support    = sorted(support[:num_levels],    key=lambda x: -x[0])  # nearest first

    return support, resistance


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

    support, resistance     = calc_support_resistance(candles, price) if candles else ([], [])
    bid_clusters, ask_clusters = fetch_order_book_clusters(symbol, price)
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

    r_parts = [f"R{i}: <code>${lvl:,.2f}</code> ({t}x)" for i, (lvl, t) in enumerate(resistance, 1)]
    s_parts = [f"S{i}: <code>${lvl:,.2f}</code> ({t}x)" for i, (lvl, t) in enumerate(support, 1)]
    r_line  = "   ".join(r_parts) if r_parts else "—"
    s_line  = "   ".join(s_parts) if s_parts else "—"

    ask_parts = [f"Ask{i}: <code>${lvl:,.2f}</code> (${v/1e6:.1f}M)" for i, (lvl, v) in enumerate(ask_clusters, 1)]
    bid_parts = [f"Bid{i}: <code>${lvl:,.2f}</code> (${v/1e6:.1f}M)" for i, (lvl, v) in enumerate(bid_clusters, 1)]
    ask_line  = "   ".join(ask_parts) if ask_parts else "—"
    bid_line  = "   ".join(bid_parts) if bid_parts else "—"

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
        f"📐 <b>Support/Resistance</b>\n"
        f"{r_line}\n"
        f"{s_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 <b>Futures Data</b>\n"
        f"OI:          {oi_line}\n"
        f"Funding:  {funding_line}\n"
        f"L/S:          {ls_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💧 <b>Liquidity Clusters</b> <i>(order book)</i>\n"
        f"{ask_line}\n"
        f"{bid_line}\n"
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
