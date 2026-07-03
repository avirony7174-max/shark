import asyncio
import os
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
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
VOL_SMA        = 10
CHECK_INTERVAL = 900
CANDLE_LIMIT   = 100

SR_LOOKBACK    = 5      # candles each side to confirm a swing high/low
SR_TOLERANCE   = 0.015  # cluster swing points within 1.5% as one level
SR_LEVELS      = 2      # how many S/R levels to keep internally each side

LIQ_BUCKET_PCT   = 0.002  # 0.2% price bucket width for order book clustering
LIQ_DEPTH        = 500    # order book depth to fetch
LIQ_MIN_GAP_MULT = 0.5    # buckets within 0.5 bucket-widths of price are "at market", not a wall
LIQ_SIG_MULT     = 1.5    # a bucket must be >= 1.5x the median bucket size to count as "significant"
LIQ_MAX_WALLS    = 3      # how many significant walls to surface per side

# Confidence weights (section 10 of spec). NOTE: as given these summed to
# 110%, not 100% (25+10+10+10+15+15+15+10). Rather than silently rescale the
# numbers you gave, calc_confidence() below normalizes by the *actual* sum
# of these weights, so the output is still a clean 0-100 score regardless.
CONF_WEIGHTS = {
    "trend":     25,
    "rsi":       10,
    "volume":    10,
    "funding":   10,
    "oi":        15,
    "liquidity": 15,
    "whale":     15,
    "sr":        10,
}


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
LIQ_STRENGTH_POINTS = {"Weak": 25, "Moderate": 50, "Strong": 75, "Extreme": 100}


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


def liquidity_ratio(buy_total, sell_total):
    if sell_total <= 0:
        return None
    return buy_total / sell_total


def liquidity_bias(ratio):
    if ratio is None:
        return "Unclear ⚪"
    if ratio >= 1.15:
        return "Bullish 🟢"
    if ratio <= 0.87:
        return "Bearish 🔴"
    return "Neutral ⚪"


def liquidity_sweep(buy_total, sell_total):
    if buy_total <= 0 or sell_total <= 0:
        return "Unclear"
    if abs(buy_total - sell_total) / max(buy_total, sell_total, 1) < 0.05:
        return "Unclear"
    return "Buy-side" if buy_total < sell_total else "Sell-side"


def liquidity_trapped(buy_walls, sell_walls, trap_distance_pct=2.0):
    if not buy_walls or not sell_walls:
        return False
    nb, ns = buy_walls[0], sell_walls[0]
    strong_enough = nb["strength"] in ("Strong", "Extreme") and ns["strength"] in ("Strong", "Extreme")
    close_enough = nb["distance_pct"] <= trap_distance_pct and ns["distance_pct"] <= trap_distance_pct
    return strong_enough and close_enough


def liquidity_score(buy_walls, sell_walls, ratio):
    all_walls = buy_walls + sell_walls
    if not all_walls:
        return 0
    avg_strength = sum(LIQ_STRENGTH_POINTS[w["strength"]] for w in all_walls) / len(all_walls)
    completeness = len(all_walls) / (2 * LIQ_MAX_WALLS) * 100
    imbalance = min(100, abs((ratio if ratio is not None else 1) - 1) * 100)
    score = 0.5 * avg_strength + 0.3 * completeness + 0.2 * imbalance
    return max(0, min(100, round(score)))


def liquidity_verdict(trapped, bias):
    if trapped:
        return "Price trapped between strong liquidity.\nBreakout confirmation required."
    if bias.startswith("Bullish"):
        return "Liquidity favors upside continuation."
    if bias.startswith("Bearish"):
        return "Liquidity favors downside pressure."
    return "No dominant liquidity bias — range conditions likely."


def format_liquidity_block(buy_walls, sell_walls):
    if not buy_walls and not sell_walls:
        return "💧 <b>Liquidity</b>\nLiquidity data insufficient."

    def wall_line(w, sign):
        return f"{w['price']:.2f} {sign}{w['distance_pct']:.2f}% • {fmt_notional(w['notional'])} • {w['strength']}"

    sell_lines = "\n".join(wall_line(w, "+") for w in sell_walls) if sell_walls else "—"
    buy_lines  = "\n".join(wall_line(w, "-") for w in buy_walls) if buy_walls else "—"

    buy_total, sell_total = liquidity_totals(buy_walls, sell_walls)
    ratio    = liquidity_ratio(buy_total, sell_total)
    bias     = liquidity_bias(ratio)
    sweep    = liquidity_sweep(buy_total, sell_total)
    trapped  = liquidity_trapped(buy_walls, sell_walls)
    score    = liquidity_score(buy_walls, sell_walls, ratio)
    verdict  = liquidity_verdict(trapped, bias)
    ratio_disp = f"{ratio:.2f}" if ratio is not None else "—"

    return (
        f"💧 <b>Liquidity</b>\n"
        f"🟥 Sell\n{sell_lines}\n"
        f"🟩 Buy\n{buy_lines}\n"
        f"Balance\nBuy {fmt_notional(buy_total)}\nSell {fmt_notional(sell_total)}\n"
        f"Ratio {ratio_disp}\nBias: {bias}\nLikely Sweep: {sweep}\nScore: {score}/100\n"
        f"Verdict:\n{verdict}"
    )


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


def rsi_label(rsi):
    if rsi is None:
        return "—"
    if rsi < 30:
        return f"<code>{rsi}</code> (Oversold)"
    if rsi > 70:
        return f"<code>{rsi}</code> (Overbought)"
    return f"<code>{rsi}</code> (Neutral)"


def ema_arrow(series, lookback=3):
    """▲/▼ based on whether the EMA is higher or lower than `lookback` candles ago."""
    if not series or len(series) <= lookback:
        return ""
    return "▲" if series[-1] > series[-1 - lookback] else "▼"


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


def oi_trend_label(oi_change):
    if oi_change is None:
        return "—"
    if oi_change > 1:
        return "Increasing"
    if oi_change < -1:
        return "Decreasing"
    return "Flat"


def crowd_label(funding, oi_change):
    if funding is None:
        return ""
    rising = oi_change is None or oi_change >= 0
    if funding > 0.02 and rising:
        return "Crowded Long"
    if funding < -0.02 and rising:
        return "Crowded Short"
    return "Balanced"


def volume_status(c_vol, vol_avg):
    """Returns (pct_of_avg, status_label)."""
    if not vol_avg:
        return None, "—"
    pct = round(c_vol / vol_avg * 100)
    if pct >= 130:
        return pct, "High Volume"
    if pct <= 70:
        return pct, "Low Volume"
    return pct, "Above Average" if pct >= 100 else "Below Average"


def dot(score):
    """Generic 0-100 bullish-scale score -> 🟢/🟡/🔴."""
    if score is None:
        return "⚪"
    if score >= 60:
        return "🟢"
    if score <= 40:
        return "🔴"
    return "🟡"


def momentum_dot(mom_label, bull_trend, bear_trend):
    if mom_label == "Strong":
        return "🟢" if bull_trend else ("🔴" if bear_trend else "🟡")
    if mom_label == "Weak":
        return "🔴"
    return "🟡"


def volume_dot(status):
    if status == "High Volume":
        return "🟢"
    if status == "Low Volume":
        return "🔴"
    return "🟡"


def overall_bias_label(score):
    if score >= 70:
        return "Strongly bullish"
    if score >= 55:
        return "Bullish but waiting for confirmation"
    if score >= 45:
        return "Neutral / ranging"
    if score >= 30:
        return "Bearish but waiting for confirmation"
    return "Strongly bearish"



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
# Confidence / Risk / Reasoning
# ============================================================================
# Every score_* function returns 0-100 "bullishness". For a SHORT-favored
# read, calc_confidence() inverts (100 - score) so one set of functions
# serves both directions consistently.

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


def calc_bullish_score(scores):
    """Direction-agnostic 0-100 'how bullish is the market overall' score —
    used both as the Market Score and as the base for calc_confidence()."""
    weight_sum = sum(CONF_WEIGHTS.values())
    return sum(scores[k] * CONF_WEIGHTS[k] for k in CONF_WEIGHTS) / weight_sum


def calc_confidence(scores, direction):
    """scores: dict matching CONF_WEIGHTS keys, each 0-100 bullishness.
    Normalizes by the *actual* weight sum (see CONF_WEIGHTS comment)."""
    bullish_total = calc_bullish_score(scores)
    return round(bullish_total if direction == "LONG" else 100 - bullish_total)


CONF_LABELS = {
    "trend": "Trend", "rsi": "RSI", "volume": "Vol", "funding": "Fund",
    "oi": "OI", "liquidity": "Liq", "whale": "Whale", "sr": "S/R",
}


def confidence_breakdown(scores, direction, top_n=3):
    """Signed point contribution of each factor toward the confidence score,
    e.g. 'Trend+25 RSI+7 Fund-3'. Contributions sum exactly to (confidence-50)."""
    weight_sum = sum(CONF_WEIGHTS.values())
    contribs = {}
    for k in CONF_WEIGHTS:
        raw = (scores[k] - 50) * CONF_WEIGHTS[k] / weight_sum
        contribs[k] = raw if direction == "LONG" else -raw

    ranked = sorted(contribs.items(), key=lambda x: -abs(x[1]))
    parts = []
    for k, v in ranked[:top_n]:
        if abs(v) < 0.5:
            continue
        sign = "+" if v >= 0 else ""
        parts.append(f"{CONF_LABELS[k]}{sign}{round(v)}")
    return " ".join(parts)


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


def whale_interpretation(tt_long, taker_buy):
    if tt_long is None or taker_buy is None:
        return "Neutral ⚪"
    bull, bear = 0, 0
    if tt_long > 55:
        bull += 1
    elif tt_long < 45:
        bear += 1
    if taker_buy > 55:
        bull += 1
    elif taker_buy < 45:
        bear += 1
    if bull > bear:
        return "Bullish 🟢"
    if bear > bull:
        return "Bearish 🔴"
    return "Neutral ⚪"


def build_wait_levels(bull_trend, bear_trend, ef, es, nearest_r, nearest_s):
    """Actionable trigger levels for the WAIT recommendation."""
    bull_entry = nearest_r[0] if nearest_r else ef
    bear_entry = nearest_s[0] if nearest_s else es
    invalidation = es if (bull_trend or bear_trend) else ef
    return bull_entry, bear_entry, invalidation


def build_wait_reasons(bull_trend, bear_trend, near_resistance, near_support, funding):
    reasons = []
    reasons.append("Trend bullish" if bull_trend else "Trend bearish" if bear_trend else "Trend unclear")
    if near_resistance:
        reasons.append("Resistance overhead")
    if near_support:
        reasons.append("Support below — watching for reaction")
    if funding is not None and abs(funding) > 0.02:
        payer = "Longs" if funding > 0 else "Shorts"
        reasons.append(f"{payer} paying elevated funding")
    reasons.append("Waiting for confirmed breakout")
    return reasons[:4]


def build_signal_reasons(sig_type, vol_ok, body_ok):
    reasons = []
    if sig_type == "LONG":
        reasons.append("Bullish EMA trend (21 > 50)")
        reasons.append("Pullback held at EMA support")
    else:
        reasons.append("Bearish EMA trend (21 < 50)")
        reasons.append("Pullback rejected at EMA resistance")
    if vol_ok:
        reasons.append("Volume confirms the move")
    if body_ok:
        reasons.append("Strong-bodied confirmation candle")
    return reasons


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


# ============================================================================
# Main analysis builder
# ============================================================================

def get_full_analysis(symbol, tf_label=DEFAULT_TF_LABEL, tf_binance="1h"):
    coin = symbol.replace("USDT", "")

    # Independent API calls run concurrently to cut total latency.
    with ThreadPoolExecutor(max_workers=7) as ex:
        f_ticker  = ex.submit(fetch_price_ticker, symbol)
        f_candles = ex.submit(fetch_candles, symbol, tf_binance, CANDLE_LIMIT)
        f_oi      = ex.submit(fetch_oi, symbol)
        f_oichg   = ex.submit(fetch_oi_change, symbol, tf_binance)
        f_funding = ex.submit(fetch_funding, symbol)
        f_taker   = ex.submit(fetch_taker_volume, symbol)
        f_tt      = ex.submit(fetch_top_trader, symbol)

        ticker    = f_ticker.result()
        candles   = f_candles.result()
        oi        = f_oi.result()
        oi_change = f_oichg.result()
        funding   = f_funding.result()
        buy_pct, sell_pct = f_taker.result()
        tt_long, tt_short = f_tt.result()

    price  = ticker.get("price", 0)
    change = ticker.get("change", 0)

    # Liquidity walls + S/R both need `price`; walls are still a network call
    # so they run concurrently with the (local, non-network) S/R calc.
    with ThreadPoolExecutor(max_workers=1) as ex:
        f_liq = ex.submit(fetch_liquidity_walls, symbol, price)
        support, resistance = calc_support_resistance(candles, price) if candles else ([], [])
        buy_walls, sell_walls = f_liq.result()

    sig = check_signal(candles) if candles else None

    closes  = [c["close"]  for c in candles] if candles else []
    volumes = [c["volume"] for c in candles] if candles else []

    ema21_series = calc_ema_series(closes, EMA_FAST) if len(closes) >= EMA_FAST else []
    ema50_series = calc_ema_series(closes, EMA_SLOW) if len(closes) >= EMA_SLOW else []
    ef  = round(ema21_series[-1], 2) if ema21_series else None
    es  = round(ema50_series[-1], 2) if ema50_series else None
    rsi = calc_rsi(closes)

    bull_trend = isinstance(ef, float) and isinstance(es, float) and ef > es
    bear_trend = isinstance(ef, float) and isinstance(es, float) and ef < es
    cross_label = "Bullish" if bull_trend else ("Bearish" if bear_trend else "Flat")

    vol_avg = calc_sma(volumes[:-1], VOL_SMA) if len(volumes) > VOL_SMA else 0
    c_vol   = volumes[-2] if len(volumes) >= 2 else 0
    vol_ok  = c_vol > vol_avg if vol_avg else False
    vol_pct, vol_status_label = volume_status(c_vol, vol_avg)

    nearest_r = resistance[0] if resistance else None
    nearest_s = support[0] if support else None
    r_dist = (nearest_r[0] - price) / price * 100 if (nearest_r and price) else None
    s_dist = (price - nearest_s[0]) / price * 100 if (nearest_s and price) else None
    near_resistance = r_dist is not None and r_dist < 1.5
    near_support    = s_dist is not None and s_dist < 1.5

    whale_skew = (tt_long - 50) if tt_long is not None else None
    momentum   = momentum_label(ef, es, rsi)

    # ---- Scoring: Confidence, Market Score, Risk ----
    nearest_sell_tuple = (sell_walls[0]["price"], sell_walls[0]["notional"]) if sell_walls else None
    nearest_buy_tuple  = (buy_walls[0]["price"], buy_walls[0]["notional"]) if buy_walls else None
    scores = {
        "trend":     score_trend(ef, es),
        "rsi":       score_rsi(rsi),
        "volume":    score_volume(c_vol, vol_avg),
        "funding":   score_funding(funding),
        "oi":        score_oi(oi_change),
        "liquidity": score_liquidity(price, nearest_sell_tuple, nearest_buy_tuple),
        "whale":     score_whale(tt_long, buy_pct),
        "sr":        score_sr(price, support, resistance),
    }
    bullish_score = calc_bullish_score(scores)   # 0-100, direction-agnostic
    direction     = sig["type"] if sig else ("LONG" if bull_trend else "SHORT")
    confidence    = calc_confidence(scores, direction)
    conf_why      = confidence_breakdown(scores, direction)
    risk_level    = calc_risk_level(funding, oi_change, rsi, bull_trend or bear_trend, whale_skew)

    # ---- Display formatting ----
    ch_icon = "▲" if change >= 0 else "▼"
    ch_sign = "+" if change >= 0 else ""
    ef_disp = ef if ef is not None else "—"
    es_disp = es if es is not None else "—"
    ef_arr  = ema_arrow(ema21_series)
    es_arr  = ema_arrow(ema50_series)

    r_disp = f"<code>${nearest_r[0]:,.2f}</code> +{r_dist:.2f}%" if nearest_r else "—"
    s_disp = f"<code>${nearest_s[0]:,.2f}</code> -{s_dist:.2f}%" if nearest_s else "—"

    oi_disp     = f"<code>{oi}B</code>" if oi else "—"
    oi_chg_sign = "+" if oi_change is not None and oi_change >= 0 else ""
    oi_line     = f"OI {oi_disp}"
    if oi_change is not None:
        oi_line += f" <code>{oi_chg_sign}{oi_change}%</code>  {oi_trend_label(oi_change)}"

    f_sign  = "+" if funding is not None and funding >= 0 else ""
    f_pay   = "Longs pay" if funding is not None and funding >= 0 else "Shorts pay"
    f_crowd = crowd_label(funding, oi_change)
    funding_line = (f"Funding <code>{f_sign}{funding}%</code> {f_pay} · {f_crowd}"
                     if funding is not None else "Funding —")

    liquidity_block = format_liquidity_block(buy_walls, sell_walls)

    whale_read = whale_interpretation(tt_long, buy_pct)
    tt_disp    = f"{tt_long}%" if tt_long is not None else "—"
    buy_disp   = f"{buy_pct}%" if buy_pct is not None else "—"
    whale_line = f"Top <code>{tt_disp}</code> Long · Taker <code>{buy_disp}</code> Buy → {whale_read}"

    vol_disp = f"<code>{vol_pct}%</code> avg — {vol_status_label}" if vol_pct is not None else "—"

    market_dots = (
        f"Trend{dot(scores['trend'])} Mom{momentum_dot(momentum, bull_trend, bear_trend)} "
        f"Liq{dot(scores['liquidity'])} Fund{dot(scores['funding'])} "
        f"Whale{dot(scores['whale'])} Vol{volume_dot(vol_status_label)}"
    )
    overall_score = round(bullish_score)
    signal_tag    = "  (Signal Active)" if sig else ""
    overall_line  = f"Overall <code>{overall_score}/100</code> — {overall_bias_label(overall_score)}{signal_tag} · Risk {risk_level}"

    conf_disp = f"Confidence <code>{confidence}%</code>" + (f"  ({conf_why})" if conf_why else "")

    if sig:
        emoji = "🎯" if sig["type"] == "LONG" else "🔻"
        reasons = build_signal_reasons(sig["type"], vol_ok, True)
        risk_reward = abs(sig["tp"] - sig["entry"]) / abs(sig["entry"] - sig["sl"]) if sig["entry"] != sig["sl"] else 0
        rec_block = (
            f"{emoji} <b>Recommendation: {sig['type']}</b>\n"
            f"{conf_disp}\n"
            f"Entry <code>{sig['entry']}</code>  SL <code>{sig['sl']}</code>  TP <code>{sig['tp']}</code>\n"
            f"Risk:Reward <code>1:{risk_reward:.1f}</code>\n"
            f"Reason: {' · '.join(reasons)}"
        )
    else:
        bull_entry, bear_entry, invalidation = build_wait_levels(bull_trend, bear_trend, ef, es, nearest_r, nearest_s)
        reasons = build_wait_reasons(bull_trend, bear_trend, near_resistance, near_support, funding)
        be_disp = f"<code>${bull_entry:,.2f}</code>" if bull_entry else "—"
        se_disp = f"<code>${bear_entry:,.2f}</code>" if bear_entry else "—"
        inv_disp = f"<code>${invalidation:,.2f}</code>" if invalidation else "—"
        rec_block = (
            f"⏳ <b>Recommendation: WAIT</b>\n"
            f"{conf_disp}\n"
            f"Bull Entry Above {be_disp}   Bear Entry Below {se_disp}\n"
            f"Invalidation {inv_disp}\n"
            f"Reason: {' · '.join(reasons)}"
        )

    return (
        f"📊 <b>{coin}/USDT</b> ⏰ <b>{tf_label}</b>   <code>${price:,.2f}</code>  {ch_icon}{ch_sign}{change}%\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 <b>Trend</b>\n"
        f"EMA21{ef_arr} <code>{ef_disp}</code>  EMA50{es_arr} <code>{es_disp}</code>\n"
        f"Cross {cross_label} · RSI {rsi_label(rsi)} · Momentum {momentum}\n"
        f"Resistance {r_disp}   Support {s_disp}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 <b>Futures</b>\n"
        f"{oi_line}\n"
        f"{funding_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{liquidity_block}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🐋 <b>Whales</b>\n"
        f"{whale_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 Volume {vol_disp}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Market Score</b>\n"
        f"{market_dots}\n"
        f"{overall_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{rec_block}"
    )


# ============================================================================
# Telegram handlers
# ============================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, tf_label, tf_binance = parse_message(update.message.text)
    if symbol:
        await update.message.reply_text("⏳ Fetching data...")
        try:
            loop = asyncio.get_running_loop()
            msg = await loop.run_in_executor(None, get_full_analysis, symbol, tf_label, tf_binance)
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            print(f"handle_message error: {e}")
            await update.message.reply_text("❌ Error fetching data. Try again.")
    else:
        await update.message.reply_text(
            "Send a coin (optionally with timeframe):\n"
            "<b>BTC · ETH · SOL · LINK</b>\n"
            "e.g. <code>BTC</code>, <code>BTC 1H</code>, <code>ETH 4H</code>, <code>SOL 15M</code>\n"
            "Timeframes: 15M · 30M · 1H · 4H · 1D (default 1H)",
            parse_mode="HTML"
        )


def auto_signal_loop():
    """Background loop on AUTO_SIGNAL_TF (Daily by default). Now fires both
    LONG and SHORT alerts, and re-arms whenever the signal direction flips
    or clears, same debounce pattern as before."""
    armed         = {coin: True for coin in COINS}
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
                        if armed[symbol] and last_sig_type[symbol] != sig["type"]:
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
                            send_telegram(msg)
                            print(f"Signal sent: {symbol} {sig['type']}")
                            armed[symbol]         = False
                            last_sig_type[symbol] = sig["type"]
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
        "<b>BTC</b> বা <b>ETH</b> বা <b>SOL</b> বা <b>LINK</b>\n"
        "(টাইমফ্রেম যোগ করতে পারো: <b>BTC 1H</b>, <b>ETH 4H</b>)"
    )

    auto_signal_loop()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot polling started")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
