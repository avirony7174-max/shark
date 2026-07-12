import asyncio
import os
import smtplib
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.mime.text import MIMEText
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from support_resistance import calc_support_resistance

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Email alerts (Gmail SMTP + App Password) run alongside Telegram. All three
# must be set (in Railway's env vars, never in code) or notify() just skips
# the email leg and Telegram-only behavior is unchanged.
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL_TO     = os.environ.get("ALERT_EMAIL_TO", "")

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT"]
VALID_COINS = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "LINK": "LINKUSDT",
}

# ---- Timeframes -----------------------------------------------------------
# User can type "BTC", "BTC 1H", "BTC 15M" etc. Default is 1H.
# Binance klines + openInterestHist both accept these exact period strings,
# so one alias map covers both candle fetching and OI-change lookups.
TIMEFRAME_ALIASES = {"15M": "15m", "30M": "30m", "1H": "1h", "4H": "4h", "1D": "1d"}
DEFAULT_TF_LABEL  = "1H"
AUTO_SIGNAL_TF    = "1d"   # auto signal loop stays on Daily (kept configurable here)

EMA_FAST       = 21
EMA_SLOW       = 50
RSI_PERIOD     = 14
VOL_SMA        = 20
CHECK_INTERVAL = 900
CANDLE_LIMIT   = 100

SR_ALERT_TF            = "4h"    # timeframe the S/R alert levels are computed from
SR_ALERT_CHECK_INTERVAL = 4 * 3600  # check once every 4 hours
SR_TOUCH_PCT           = 0.3    # price within 0.3% of a level (but not past it) counts as a "hit"
SR_STRONG_TOUCHES      = 3      # level touches >= this => "Strong", else "Light"

OPPORTUNITY_TF             = "1h"   # timeframe the 24/7 opportunity scanner analyzes
OPPORTUNITY_CHECK_INTERVAL = 900    # 15 min, matches CHECK_INTERVAL's cadence

LIQ_BUCKET_PCT   = 0.002  # 0.2% price bucket width for order book clustering
LIQ_DEPTH        = 500    # order book depth to fetch
LIQ_MIN_GAP_MULT = 0.5    # buckets within 0.5 bucket-widths of price are "at market", not a wall
LIQ_SIG_MULT     = 1.5    # a bucket must be >= 1.5x the median bucket size to count as "significant"
LIQ_MAX_WALLS    = 3      # how many significant walls to surface per side


# ============================================================================
# Telegram
# ============================================================================

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


def send_email(subject, message):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not ALERT_EMAIL_TO:
        print("Email credentials missing")
        return
    try:
        recipients = [addr.strip() for addr in ALERT_EMAIL_TO.split(",") if addr.strip()]
        msg = MIMEText(message, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
    except Exception as e:
        print(f"Email error: {e}")


def notify(subject, message):
    """Fan-out to every configured channel. Telegram and email each fail
    independently (one being down/misconfigured never blocks the other)."""
    send_telegram(message)
    send_email(subject, message)


# ============================================================================
# Supabase signal tracking
# ============================================================================

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }


def log_signal(symbol, sig_type, entry, sl, tp, timeframe):
    try:
        payload = {
            "symbol": symbol, "signal_type": sig_type,
            "entry": entry, "sl": sl, "tp": tp,
            "timeframe": timeframe, "status": "OPEN",
        }
        r = requests.post(f"{SUPABASE_URL}/rest/v1/signal_log",
                           headers=supabase_headers(), json=payload, timeout=10)
        if r.status_code not in (200, 201):
            print(f"Supabase log error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Supabase log error: {e}")


def fetch_open_signals():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/signal_log",
                          headers=supabase_headers(),
                          params={"status": "eq.OPEN", "select": "*"}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"Supabase fetch error: {e}")
    return []


def close_signal(row_id, status, exit_price, pnl_pct):
    try:
        payload = {"status": status, "exit_price": exit_price, "pnl_pct": pnl_pct,
                   "closed_at": datetime.now(timezone.utc).isoformat()}
        requests.patch(f"{SUPABASE_URL}/rest/v1/signal_log",
                        headers=supabase_headers(),
                        params={"id": f"eq.{row_id}"}, json=payload, timeout=10)
    except Exception as e:
        print(f"Supabase close error: {e}")


def signal_tracker_loop():
    def loop():
        while True:
            try:
                for sig in fetch_open_signals():
                    symbol = sig["symbol"]
                    ticker = fetch_price_ticker(symbol)
                    price = ticker.get("price", 0)
                    if not price:
                        continue
                    sig_type = sig["signal_type"]
                    entry, sl, tp = float(sig["entry"]), float(sig["sl"]), float(sig["tp"])

                    if sig_type == "LONG":
                        if price >= tp:
                            close_signal(sig["id"], "TP_HIT", tp, round((tp - entry) / entry * 100, 2))
                        elif price <= sl:
                            close_signal(sig["id"], "SL_HIT", sl, round((sl - entry) / entry * 100, 2))
                    else:
                        if price <= tp:
                            close_signal(sig["id"], "TP_HIT", tp, round((entry - tp) / entry * 100, 2))
                        elif price >= sl:
                            close_signal(sig["id"], "SL_HIT", sl, round((entry - sl) / entry * 100, 2))
            except Exception as e:
                print(f"Signal tracker error: {e}")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


def format_stats():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/signal_log",
                          headers=supabase_headers(),
                          params={"status": "neq.OPEN", "select": "*"}, timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception as e:
        print(f"Stats fetch error: {e}")
        rows = []

    if not rows:
        return "📊 <b>Signal Track Record</b>\nNo closed signals yet."

    wins = [r for r in rows if r["status"] == "TP_HIT"]
    losses = [r for r in rows if r["status"] == "SL_HIT"]
    total = len(wins) + len(losses)
    win_rate = (len(wins) / total * 100) if total else 0
    avg_pnl = sum(float(r["pnl_pct"]) for r in rows) / len(rows)

    return (
        f"📊 <b>Signal Track Record</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Total closed: <code>{total}</code>\n"
        f"Wins (TP): <code>{len(wins)}</code>   Losses (SL): <code>{len(losses)}</code>\n"
        f"Win rate: <code>{win_rate:.1f}%</code>\n"
        f"Avg PnL: <code>{avg_pnl:+.2f}%</code>"
    )


# ============================================================================
# Binance fetchers
# ============================================================================

def fetch_candles(symbol, interval="1h", limit=CANDLE_LIMIT):
    """Generic replacement for the old fetch_daily_candles() — any timeframe."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list):
            print(f"Binance klines error {symbol} {interval}: {data}")
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
        print(f"Binance candle error {symbol} {interval}: {e}")
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


def fetch_oi_change(symbol, period="1h", limit=2):
    """% change in open interest over one period, using Binance's OI history."""
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
            timeout=10
        )
        data = r.json()
        if isinstance(data, list) and len(data) >= 2:
            old = float(data[0].get("sumOpenInterestValue", 0))
            new = float(data[-1].get("sumOpenInterestValue", 0))
            if old > 0:
                return round((new - old) / old * 100, 2)
    except Exception as e:
        print(f"OI change error {symbol}: {e}")
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


LIQ_STRENGTH_BANDS = [
    (6.0, "Extreme"),
    (3.0, "Strong"),
    (1.5, "Moderate"),
]


def classify_wall_strength(notional, median_notional):
    if median_notional <= 0:
        return "Weak"
    ratio = notional / median_notional
    for threshold, label in LIQ_STRENGTH_BANDS:
        if ratio >= threshold:
            return label
    return "Weak"


def fetch_liquidity_walls(symbol, current_price, bucket_pct=LIQ_BUCKET_PCT, depth_limit=LIQ_DEPTH,
                           min_gap_mult=LIQ_MIN_GAP_MULT, sig_mult=LIQ_SIG_MULT, max_walls=LIQ_MAX_WALLS):
    """Returns (buy_walls, sell_walls) — each a list of up to max_walls dicts
    {price, notional, distance_pct, strength}, nearest-to-price first."""
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
        min_gap = bucket_size * min_gap_mult

        def build_buckets(orders):
            buckets = {}
            for p_str, q_str in orders:
                p = float(p_str)
                q = float(q_str)
                key = round(p / bucket_size) * bucket_size
                buckets[key] = buckets.get(key, 0) + p * q
            return buckets

        def significant_walls(buckets, direction):
            if not buckets:
                return []
            values = sorted(buckets.values())
            mid = len(values) // 2
            median_val = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
            threshold = median_val * sig_mult

            if direction == "above":
                candidates = {lvl: n for lvl, n in buckets.items() if lvl > current_price + min_gap}
            else:
                candidates = {lvl: n for lvl, n in buckets.items() if lvl < current_price - min_gap}
            if not candidates:
                return []

            significant = {lvl: n for lvl, n in candidates.items() if n >= threshold}
            pool = significant if significant else candidates

            ordered = sorted(pool.items(), key=lambda x: x[0] if direction == "above" else -x[0])
            walls = []
            for lvl, notional in ordered[:max_walls]:
                walls.append({
                    "price": lvl,
                    "notional": notional,
                    "distance_pct": abs(lvl - current_price) / current_price * 100,
                    "strength": classify_wall_strength(notional, median_val),
                })
            return walls

        ask_buckets = build_buckets(asks)
        bid_buckets = build_buckets(bids)
        sell_walls = significant_walls(ask_buckets, "above")
        buy_walls  = significant_walls(bid_buckets, "below")
        return buy_walls, sell_walls
    except Exception as e:
        print(f"Liquidity wall error {symbol}: {e}")
        return [], []


def fmt_notional(v):
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def liquidity_totals(buy_walls, sell_walls):
    return sum(w["notional"] for w in buy_walls), sum(w["notional"] for w in sell_walls)


def liquidity_trapped(buy_walls, sell_walls, trap_distance_pct=2.0):
    if not buy_walls or not sell_walls:
        return False
    nb, ns = buy_walls[0], sell_walls[0]
    strong_enough = nb["strength"] in ("Strong", "Extreme") and ns["strength"] in ("Strong", "Extreme")
    close_enough = nb["distance_pct"] <= trap_distance_pct and ns["distance_pct"] <= trap_distance_pct
    return strong_enough and close_enough


# ============================================================================
# Indicators
# ============================================================================

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


def calc_rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI. Returns None if there isn't enough history yet."""
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def momentum_label(ef, es, rsi):
    """Heuristic composite of EMA-gap size + RSI distance from neutral 50."""
    if ef is None or es is None or not es:
        return "Weak"
    gap_pct = abs(ef - es) / es * 100
    rsi_dev = abs((rsi if rsi is not None else 50) - 50)
    score = gap_pct * 8 + rsi_dev * 0.6
    if score >= 12:
        return "Strong"
    if score >= 5:
        return "Moderate"
    return "Weak"


# ============================================================================
# Signal engine — LONG and SHORT
# ============================================================================

def check_signal(candles):
    """
    Returns {"type": "LONG"|"SHORT", "entry":.., "sl":.., "tp":..} or None.
    LONG and SHORT use mirrored logic:
      LONG:  bullish EMA trend, pullback INTO EMA support, bullish rejection
             candle closing near its high, confirmed by volume + body size.
      SHORT: bearish EMA trend, pullback INTO EMA resistance, bearish rejection
             candle closing near its low, confirmed by volume + body size.
    """
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

    vol_ok  = c_vol > vol_avg
    body_ok = body_ratio >= 0.6

    atr_vals = [candles[i]["high"] - candles[i]["low"] for i in range(-15, -1)]
    atr = sum(atr_vals) / len(atr_vals)

    # --- LONG ---
    bull_trend    = ef > es
    long_close_ok = candle_range > 0 and (c_high - c_close) / candle_range <= 0.35
    long_pullback = bull_trend and (p_low <= ef1 or (p_low <= es1 and p_high >= ef1))
    long_cond     = (bull_trend and long_pullback and c_close > ef
                     and c_close > c_open and vol_ok and body_ok and long_close_ok)

    # --- SHORT (mirror of LONG) ---
    bear_trend     = ef < es
    short_close_ok = candle_range > 0 and (c_close - c_low) / candle_range <= 0.35
    short_pullback = bear_trend and (p_high >= ef1 or (p_high >= es1 and p_low <= ef1))
    short_cond      = (bear_trend and short_pullback and c_close < ef
                       and c_close < c_open and vol_ok and body_ok and short_close_ok)

    if long_cond:
        sl = c_close - atr * 1.5
        tp = c_close + (c_close - sl) * 3.0
        return {"type": "LONG", "entry": round(c_close, 2), "sl": round(sl, 2), "tp": round(tp, 2)}

    if short_cond:
        sl = c_close + atr * 1.5
        tp = c_close - (sl - c_close) * 3.0
        return {"type": "SHORT", "entry": round(c_close, 2), "sl": round(sl, 2), "tp": round(tp, 2)}

    return None


# ============================================================================
# Scoring primitives / Risk
# ============================================================================
# Every score_* function returns 0-100 "bullishness". For a SHORT-favored
# read, analysis_v2.calc_confidence_v2() inverts (100 - score) so one set of
# functions serves both directions consistently.

def score_trend(ef, es):
    if ef is None or es is None or not isinstance(ef, (int, float)) or es == 0:
        return 50
    gap_pct = (ef - es) / es * 100
    return max(0, min(100, 50 + gap_pct * 8))


def score_rsi(rsi):
    if rsi is None:
        return 50
    if rsi <= 30:
        return 80
    if rsi >= 70:
        return 20
    return max(0, min(100, 80 - (rsi - 30) * 1.5))


def score_volume(c_vol, vol_avg):
    if not vol_avg:
        return 50
    ratio = c_vol / vol_avg
    if ratio >= 1.5:
        return 80
    if ratio <= 0.7:
        return 30
    return 50 + (ratio - 1) * 40


def score_funding(funding):
    if funding is None:
        return 50
    if funding > 0.03:
        return 30   # longs overheated -> contrarian bearish tilt
    if funding < -0.03:
        return 70   # shorts overheated -> contrarian bullish tilt
    return 50


def score_oi(oi_change):
    if oi_change is None:
        return 50
    if oi_change > 3:
        return 65
    if oi_change < -3:
        return 35
    return 50


def score_liquidity(price, sell_wall, buy_wall):
    if not sell_wall and not buy_wall:
        return 50
    ask_dist = (sell_wall[0] - price) / price * 100 if sell_wall else 999
    bid_dist = (price - buy_wall[0]) / price * 100 if buy_wall else 999
    if ask_dist < bid_dist:
        return 35   # resistance wall closer -> bearish tilt
    if bid_dist < ask_dist:
        return 65   # support wall closer -> bullish tilt
    return 50


def score_whale(tt_long, taker_buy):
    if tt_long is None or taker_buy is None:
        return 50
    return (tt_long + taker_buy) / 2


def score_sr(price, support, resistance):
    if not support and not resistance:
        return 50
    r_dist = (resistance[0][0] - price) / price * 100 if resistance else 999
    s_dist = (price - support[0][0]) / price * 100 if support else 999
    if r_dist < s_dist:
        return 35
    if s_dist < r_dist:
        return 65
    return 50


def calc_risk_level(funding, oi_change, rsi, trend_clear, whale_skew):
    points = 0
    if funding is not None and abs(funding) > 0.05:
        points += 1
    if oi_change is not None and abs(oi_change) > 5:
        points += 1
    if rsi is not None and (rsi > 75 or rsi < 25):
        points += 1
    if not trend_clear:
        points += 1
    if whale_skew is not None and abs(whale_skew) < 5:
        points += 1

    if points >= 3:
        return "🔴 High"
    if points >= 1:
        return "🟡 Medium"
    return "🟢 Low"


# ============================================================================
# Message parsing
# ============================================================================

def parse_message(text):
    """'BTC' -> (BTCUSDT, '1H', '1h'); 'ETH 4H' -> (ETHUSDT, '4H', '4h')."""
    parts = (text or "").strip().upper().split()
    if not parts or parts[0] not in VALID_COINS:
        return None, None, None

    symbol = VALID_COINS[parts[0]]
    tf_label = DEFAULT_TF_LABEL
    if len(parts) >= 2 and parts[1] in TIMEFRAME_ALIASES:
        tf_label = parts[1]
    return symbol, tf_label, TIMEFRAME_ALIASES[tf_label]


def parse_trade_command(text):
    """'trade' -> ('1H', '1h'); 'trade 4H' -> ('4H', '4h'); else None."""
    parts = (text or "").strip().upper().split()
    if not parts or parts[0] != "TRADE":
        return None
    tf_label = DEFAULT_TF_LABEL
    if len(parts) >= 2 and parts[1] in TIMEFRAME_ALIASES:
        tf_label = parts[1]
    return tf_label, TIMEFRAME_ALIASES[tf_label]


# ============================================================================
# Telegram handlers
# ============================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import analysis_v2  # lazy import: analysis_v2 imports this module, so importing
                         # it back at module load time would be circular.
    text = update.message.text
    if (text or "").strip().upper() == "STATS":
        await update.message.reply_text(format_stats(), parse_mode="HTML")
        return

    trade_cmd = parse_trade_command(text)
    if trade_cmd:
        tf_label, tf_binance = trade_cmd
        await update.message.reply_text("⏳ Scanning BTC, ETH, SOL, LINK...")
        try:
            loop = asyncio.get_running_loop()
            msg = await loop.run_in_executor(None, analysis_v2.compare_coins, COINS, tf_label, tf_binance)
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            print(f"handle_message trade error: {e}")
            await update.message.reply_text("❌ Error scanning coins. Try again.")
        return

    symbol, tf_label, tf_binance = parse_message(text)
    if symbol:
        await update.message.reply_text("⏳ Fetching data...")
        try:
            loop = asyncio.get_running_loop()
            msg = await loop.run_in_executor(None, analysis_v2.get_full_analysis_v2, symbol, tf_label, tf_binance)
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            print(f"handle_message error: {e}")
            await update.message.reply_text("❌ Error fetching data. Try again.")
    else:
        await update.message.reply_text(
            "Send a coin (optionally with timeframe):\n"
            "<b>BTC · ETH · SOL · LINK</b>\n"
            "e.g. <code>BTC</code>, <code>BTC 1H</code>, <code>ETH 4H</code>, <code>SOL 15M</code>\n"
            "Timeframes: 15M · 30M · 1H · 4H · 1D (default 1H)\n\n"
            "Or send <b>TRADE</b> to compare all coins and see which has the best setup right now.",
            parse_mode="HTML"
        )


def auto_signal_loop():
    """Background loop on AUTO_SIGNAL_TF (Daily by default). Fires both
    LONG and SHORT alerts, and re-arms whenever the signal direction flips
    or clears: last_sig_type is the sole gate, so a direct LONG<->SHORT
    reversal alerts immediately instead of waiting for a no-signal cycle."""
    last_sig_type = {coin: None for coin in COINS}

    def loop():
        while True:
            now = datetime.now(timezone.utc)
            print(f"[{now.strftime('%H:%M UTC')}] Auto signal check...")
            for symbol in COINS:
                try:
                    candles = fetch_candles(symbol, AUTO_SIGNAL_TF, limit=CANDLE_LIMIT)
                    if not candles:
                        continue
                    sig = check_signal(candles)
                    if sig:
                        if last_sig_type[symbol] != sig["type"]:
                            oi      = fetch_oi(symbol)
                            funding = fetch_funding(symbol)
                            coin    = symbol.replace("USDT", "")
                            f_sign  = "+" if funding is not None and funding >= 0 else ""
                            f_pay   = "Longs pay" if funding is not None and funding >= 0 else "Shorts pay"
                            emoji   = "🟢" if sig["type"] == "LONG" else "🔴"
                            msg = (
                                f"{emoji} <b>{sig['type']} SIGNAL</b>\n"
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
                            notify(f"{coin} {sig['type']} Signal", msg)
                            log_signal(symbol, sig["type"], sig["entry"], sig["sl"], sig["tp"], "DAILY")
                            print(f"Signal sent: {symbol} {sig['type']}")
                            last_sig_type[symbol] = sig["type"]
                    else:
                        last_sig_type[symbol] = None
                except Exception as e:
                    print(f"Error {symbol}: {e}")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


def sr_strength_label(touches):
    return "Strong" if touches >= SR_STRONG_TOUCHES else "Light"


def sr_alert_loop():
    """Checks every 4 hours, using S/R levels from the 4H timeframe. Alerts once per crossing on:
      - price dipping below the nearest support (breakdown)
      - price hitting the nearest support (within SR_TOUCH_PCT, still above it)
      - price crossing above the nearest resistance (breakout)
    Each condition re-arms only after price moves back out of that state,
    same edge-triggered debounce pattern as auto_signal_loop.
    """
    state = {
        symbol: {"below_support": False, "touched_support": False, "above_resistance": False}
        for symbol in COINS
    }

    def loop():
        while True:
            now = datetime.now(timezone.utc)
            print(f"[{now.strftime('%H:%M UTC')}] S/R alert check...")
            for symbol in COINS:
                try:
                    price = fetch_price_ticker(symbol).get("price", 0)
                    if not price:
                        continue
                    candles = fetch_candles(symbol, SR_ALERT_TF, limit=CANDLE_LIMIT)
                    if not candles:
                        continue
                    support, resistance = calc_support_resistance(candles, price)
                    coin = symbol.replace("USDT", "")
                    s = state[symbol]

                    nearest_s = support[0] if support else None
                    nearest_r = resistance[0] if resistance else None

                    below_support = nearest_s is not None and price < nearest_s[0]
                    touching_support = (
                        nearest_s is not None and not below_support
                        and (price - nearest_s[0]) / nearest_s[0] * 100 <= SR_TOUCH_PCT
                    )
                    above_resistance = nearest_r is not None and price > nearest_r[0]

                    if below_support and not s["below_support"]:
                        strength = sr_strength_label(nearest_s[1])
                        notify(
                            f"{coin} broke below support",
                            f"🔴 <b>{coin}</b> crossing below support <code>${nearest_s[0]:,.2f}</code> — {strength} support level"
                        )
                    s["below_support"] = below_support

                    if touching_support and not s["touched_support"]:
                        strength = sr_strength_label(nearest_s[1])
                        notify(
                            f"{coin} hit support",
                            f"🟡 <b>{coin}</b> hit support <code>${nearest_s[0]:,.2f}</code> — {strength} support level"
                        )
                    s["touched_support"] = touching_support

                    if above_resistance and not s["above_resistance"]:
                        strength = sr_strength_label(nearest_r[1])
                        notify(
                            f"{coin} broke resistance",
                            f"🟢 <b>{coin}</b> crossing resistance <code>${nearest_r[0]:,.2f}</code> — {strength} resistance level"
                        )
                    s["above_resistance"] = above_resistance

                except Exception as e:
                    print(f"S/R alert error {symbol}: {e}")
            time.sleep(SR_ALERT_CHECK_INTERVAL)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


def opportunity_scan_loop():
    """24/7 scanner using analysis_v2's full pipeline (multi-timeframe
    alignment, volume/OI/liquidity confirmation, breakout checklist, R/R
    gating) on the 1H timeframe. Alerts ONLY on STRONG LONG / STRONG SHORT —
    recommend()'s strict gate already requires all 4 timeframes aligned,
    volume >=110% average, OI supporting, liquidity not opposing, R/R >= 2,
    and score >= 75, so a STRONG verdict here is never a lone indicator.

    The alert includes the generated trade plan (entry/SL/TP1-3/R:R) and the
    underlying 0-100 score, labeled as a technical confidence score — it is
    NOT a backtested win rate, since this bot has no historical trade log to
    derive one from.

    Re-arms on flip/clear (last_alert_type is the sole gate), same
    debounce pattern as auto_signal_loop — a direct LONG<->SHORT reversal
    alerts immediately instead of waiting for the state to clear first.
    """
    last_alert_type = {symbol: None for symbol in COINS}

    def loop():
        import analysis_v2  # lazy: analysis_v2 imports this module
        while True:
            now = datetime.now(timezone.utc)
            print(f"[{now.strftime('%H:%M UTC')}] Opportunity scan...")
            for symbol in COINS:
                try:
                    data = analysis_v2.analyze(symbol, "1H", OPPORTUNITY_TF)
                    verdict = data["verdict"]
                    direction = "LONG" if verdict == "STRONG LONG" else "SHORT" if verdict == "STRONG SHORT" else None

                    if direction:
                        if last_alert_type[symbol] != direction:
                            plan = data.get("plan")
                            coin = symbol.replace("USDT", "")
                            if plan:
                                emoji = "🟢" if direction == "LONG" else "🔴"
                                msg = (
                                    f"{emoji} <b>{verdict}</b> — <b>{coin}/USDT</b> · 1H\n"
                                    f"━━━━━━━━━━━━━━━\n"
                                    f"Entry: <code>${plan['entry']:,.2f}</code>\n"
                                    f"SL:      <code>${plan['sl']:,.2f}</code>\n"
                                    f"TP1:    <code>${plan['tp1']:,.2f}</code>\n"
                                    f"TP2:    <code>${plan['tp2']:,.2f}</code>\n"
                                    f"TP3:    <code>${plan['tp3']:,.2f}</code>\n"
                                    f"R/R:     1:{plan['rr']:.1f}\n"
                                    f"━━━━━━━━━━━━━━━\n"
                                    f"Confidence Score: <code>{data['score']}/100</code>\n"
                                    f"<i>Technical confidence score, not a guaranteed win rate</i>"
                                )
                                notify(f"{coin} {verdict}", msg)
                                print(f"Opportunity alert sent: {symbol} {verdict}")
                        last_alert_type[symbol] = direction
                    else:
                        last_alert_type[symbol] = None
                except Exception as e:
                    print(f"Opportunity scan error {symbol}: {e}")
            time.sleep(OPPORTUNITY_CHECK_INTERVAL)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


def main():
    print("Signal Bot starting...")
    notify(
        "Signal Bot started",
        "✅ <b>Signal Bot চালু হয়েছে</b>\n"
        "BTC · ETH · SOL · LINK monitoring\n\n"
        "যেকোনো সময় লিখো:\n"
        "<b>BTC</b> বা <b>ETH</b> বা <b>SOL</b> বা <b>LINK</b>\n"
        "(টাইমফ্রেম যোগ করতে পারো: <b>BTC 1H</b>, <b>ETH 4H</b>)"
    )

    auto_signal_loop()
    sr_alert_loop()
    opportunity_scan_loop()
    signal_tracker_loop()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot polling started")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
