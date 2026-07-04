"""
Telegram analysis output — v2 spec: banded RSI/volume interpretation, layered
support/resistance (immediate/major/macro) guaranteed to order correctly
regardless of which timeframe each level came from, liquidity with an
explicit reliability rating (missing order-book side no longer treated as a
real signal), OI+price flow with a "flat OI" floor, a transparent 100-point
category-weighted score with explicit caps/deductions, a 4-item breakout
checklist, and a 6-state recommendation engine with generated trade plans.

Kept separate from signal_bot.py so that module's fetchers/primitives
(fetch_candles, score_trend, check_signal, calc_risk_level, ...) are reused
rather than duplicated. handle_message() lazily imports this module.
"""
from concurrent.futures import ThreadPoolExecutor

import signal_bot as sb
from support_resistance import calc_support_resistance

MTF_TIMEFRAMES = ["15m", "1h", "4h", "1d"]
MACRO_RELEVANCE_PCT = 5.0        # only surface a Daily level if price is within 5% of it
LIQUIDITY_IMBALANCE_MIN = 0.05   # min buy/sell liquidity skew before we say anything directional
RETEST_LOOKBACK = 6
RETEST_TOLERANCE_PCT = 0.3
RR_MIN_FOR_TRADE = 2.0           # no trade plan at all below this R/R
OI_FLAT_PCT = 0.5                # |OI change| below this = "flat positioning", regardless of price
FUNDING_EXTREME_PCT = 0.05       # matches signal_bot.calc_risk_level's existing threshold
PROXIMITY_PCT = 0.3              # "too close to a level" for score deductions / plan gating
ATR_PERIOD = 14

VOLUME_WEAK_PCT = 60
VOLUME_BELOW_AVG_PCT = 90
VOLUME_CONFIRM_PCT = 110
VOLUME_STRONG_PCT = 150
VOLUME_CLIMAX_PCT = 250

SCORE_WEIGHTS = {"trend": 20, "momentum_rsi": 15, "volume": 20, "oi": 15,
                  "liquidity": 10, "whale": 10, "sr": 10}   # sums to 100
MOMENTUM_POINTS = {"Strong": 30, "Moderate": 15, "Weak": 0}


# ============================================================================
# Multi-timeframe alignment
# ============================================================================

def tf_bias(candles):
    if not candles:
        return "Neutral"
    closes = [c["close"] for c in candles]
    if len(closes) < sb.EMA_SLOW:
        return "Neutral"
    ef = sb.calc_ema_series(closes, sb.EMA_FAST)[-1]
    es = sb.calc_ema_series(closes, sb.EMA_SLOW)[-1]
    if ef > es:
        return "Bullish"
    if ef < es:
        return "Bearish"
    return "Neutral"


def mtf_alignment(bias_map):
    bulls = sum(1 for v in bias_map.values() if v == "Bullish")
    bears = sum(1 for v in bias_map.values() if v == "Bearish")
    if bulls == 4:
        return "Strong Bullish"
    if bears == 4:
        return "Strong Bearish"
    if bias_map.get("15m") == "Bullish" and bias_map.get("1h") == "Bullish" and bias_map.get("4h") == "Bullish" and bias_map.get("1d") == "Bearish":
        return "Mixed bullish structure"
    if bias_map.get("15m") == "Bearish" and bias_map.get("1h") == "Bearish" and bias_map.get("4h") == "Bearish" and bias_map.get("1d") == "Bullish":
        return "Mixed bearish structure"
    if bias_map.get("4h") == "Bullish" and bias_map.get("1d") == "Bullish" and bulls >= 3:
        return "Bullish"
    if bias_map.get("4h") == "Bearish" and bias_map.get("1d") == "Bearish" and bears >= 3:
        return "Bearish"
    return "Mixed"


# ============================================================================
# Layered support/resistance — pool 1H/4H/1D candidates and assign
# Immediate/Major/Macro purely by distance, so the ordering rule
# ("Immediate must be closer than Major") holds by construction rather than
# by assuming 1H always finds a closer level than 4H.
# ============================================================================

def _relevant_daily(one_d_list, price):
    if not one_d_list or not price:
        return []
    lvl = one_d_list[0]
    return [lvl] if abs(lvl[0] - price) / price * 100 <= MACRO_RELEVANCE_PCT else []


def _assign_levels(one_h, four_h, one_d, price, above):
    pool = []
    if one_h:
        pool.append(one_h[0])
    if four_h:
        pool.append(four_h[0])
    pool.extend(_relevant_daily(one_d, price))

    uniq, seen = [], set()
    for lvl in pool:
        key = round(lvl[0], 6)
        if key not in seen:
            seen.add(key)
            uniq.append(lvl)
    uniq.sort(key=lambda lv: lv[0], reverse=not above)  # nearest-to-price first

    immediate = uniq[0] if len(uniq) > 0 else None
    major     = uniq[1] if len(uniq) > 1 else None
    macro     = uniq[2] if len(uniq) > 2 else None
    return immediate, major, macro


def build_levels(mtf_candles, price):
    s_1h, r_1h = calc_support_resistance(mtf_candles.get("1h", []), price) if mtf_candles.get("1h") else ([], [])
    s_4h, r_4h = calc_support_resistance(mtf_candles.get("4h", []), price) if mtf_candles.get("4h") else ([], [])
    s_1d, r_1d = calc_support_resistance(mtf_candles.get("1d", []), price) if mtf_candles.get("1d") else ([], [])

    immediate_r, major_r, macro_r = _assign_levels(r_1h, r_4h, r_1d, price, above=True)
    immediate_s, major_s, macro_s = _assign_levels(s_1h, s_4h, s_1d, price, above=False)

    return {
        "immediate_r": immediate_r, "major_r": major_r, "macro_r": macro_r,
        "immediate_s": immediate_s, "major_s": major_s, "macro_s": macro_s,
    }


def _near(level_tuple, price, pct=PROXIMITY_PCT):
    return level_tuple is not None and price and abs(level_tuple[0] - price) / price * 100 <= pct


# ============================================================================
# Liquidity — reliability rating + graded bias + directional sweep wording
# ============================================================================

def liquidity_view(buy_walls, sell_walls, levels):
    buy_total, sell_total = sb.liquidity_totals(buy_walls, sell_walls)

    if not buy_walls and not sell_walls:
        return {"reliability": "Low", "bias": "Neutral", "note": "Liquidity data insufficient.",
                "buy_total": buy_total, "sell_total": sell_total, "sweep": None, "lean": None}

    if not buy_walls or not sell_walls:
        return {"reliability": "Low", "bias": "Not reliable",
                "note": "⚠️ One-side data missing — liquidity bias ignored",
                "buy_total": buy_total, "sell_total": sell_total, "sweep": None, "lean": None}

    ratio = buy_total / sell_total if sell_total > 0 else float("inf")
    if ratio >= 1.3:
        bias, lean = "Bullish", "bullish"
    elif ratio >= 1.1:
        bias, lean = "Slight Bullish", "bullish"
    elif ratio <= 0.77:
        bias, lean = "Bearish", "bearish"
    elif ratio <= 0.91:
        bias, lean = "Slight Bearish", "bearish"
    else:
        bias, lean = "Neutral", None

    reliability = "High" if (len(buy_walls) >= 2 and len(sell_walls) >= 2) else "Medium"

    sweep = None
    imbalance = abs(buy_total - sell_total) / max(buy_total, sell_total, 1)
    if imbalance >= LIQUIDITY_IMBALANCE_MIN:
        if buy_total < sell_total:
            frm = levels["immediate_s"]
            to = levels["major_s"] or levels["macro_s"]
            if frm and to:
                sweep = f"Possible downside sweep: ${frm[0]:,.2f} → ${to[0]:,.2f}"
        else:
            frm = levels["immediate_r"]
            to = levels["major_r"] or levels["macro_r"]
            if frm and to:
                sweep = f"Possible upside sweep: ${frm[0]:,.2f} → ${to[0]:,.2f}"
    elif bias == "Neutral":
        sweep = "Liquidity is balanced — ranging conditions likely."

    return {"reliability": reliability, "bias": bias, "note": None,
            "buy_total": buy_total, "sell_total": sell_total, "sweep": sweep, "lean": lean}


# ============================================================================
# OI + price interpretation
# ============================================================================

def oi_price_flow(price_change_pct, oi_change_pct, flat_pct=OI_FLAT_PCT):
    if price_change_pct is None or oi_change_pct is None:
        return "No strong fresh positioning"
    if abs(oi_change_pct) < flat_pct:
        return "Flat positioning — weak confirmation"
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > 0
    if price_up and oi_up:
        return "Fresh longs entering — bullish continuation possible"
    if price_up and not oi_up:
        return "Short covering — rally may be weaker"
    if not price_up and oi_up:
        return "Fresh shorts entering — bearish continuation possible"
    return "Long liquidation — weak sell-off"


def funding_crowd_note(funding, direction):
    if funding is None:
        return None
    if funding > FUNDING_EXTREME_PCT:
        return "Long crowding risk" if direction != "SHORT" else None
    if funding < -FUNDING_EXTREME_PCT:
        return "Short crowding risk" if direction != "LONG" else None
    return None


# ============================================================================
# RSI / momentum / volume interpretation text
# ============================================================================

def rsi_interpretation(rsi):
    if rsi is None:
        return "—"
    if rsi < 30:
        return f"{rsi} — oversold, possible bounce (not automatic long)"
    if rsi < 45:
        return f"{rsi} — weak / bearish pressure"
    if rsi < 55:
        return f"{rsi} — neutral"
    if rsi < 65:
        return f"{rsi} — bullish momentum"
    if rsi < 70:
        return f"{rsi} — bullish momentum, approaching overbought"
    if rsi <= 80:
        return f"{rsi} — overbought risk, do not chase longs"
    return f"{rsi} — high reversal risk"


def score_momentum(momentum_lbl, bull_trend, bear_trend):
    pts = MOMENTUM_POINTS.get(momentum_lbl, 0)
    if bull_trend:
        return 50 + pts
    if bear_trend:
        return 50 - pts
    return 50


def volume_quality_text(pct):
    if pct is None:
        return "no volume data"
    if pct < VOLUME_WEAK_PCT:
        return "weak, low participation"
    if pct < VOLUME_BELOW_AVG_PCT:
        return "below average"
    if pct <= VOLUME_CONFIRM_PCT:
        return "normal confirmation range"
    if pct <= VOLUME_STRONG_PCT:
        return "strong confirmation"
    if pct <= VOLUME_CLIMAX_PCT:
        return "very strong participation"
    return "possible climax volume — watch for exhaustion"


def whale_flow_label(tt_long, taker_buy):
    if tt_long is None or taker_buy is None:
        return "Unclear"
    bull = (1 if tt_long > 55 else 0) + (1 if taker_buy > 55 else 0)
    bear = (1 if tt_long < 45 else 0) + (1 if taker_buy < 45 else 0)
    if bull == 2 and bear == 0:
        return "strong long bias"
    if bear == 2 and bull == 0:
        return "strong short bias"
    if bull > bear:
        return "slight long bias"
    if bear > bull:
        return "slight short bias"
    return "neutral"


def taker_label(buy_pct):
    if buy_pct is None:
        return "—"
    if buy_pct >= 55:
        return "buyers active"
    if buy_pct <= 45:
        return "sellers active"
    return "balanced"


# ============================================================================
# Breakout confirmation checklist (targets the IMMEDIATE level — the nearest
# actionable trigger, not the distant major/macro one)
# ============================================================================

def candle_closed_beyond(candles, level, direction):
    if not candles or level is None or len(candles) < 2:
        return False
    last_closed = candles[-2]["close"]   # -1 is the still-forming candle
    return last_closed > level if direction == "LONG" else last_closed < level


def retest_confirmed(candles, level, direction, lookback=RETEST_LOOKBACK, tolerance_pct=RETEST_TOLERANCE_PCT):
    if not candles or level is None or len(candles) < lookback + 2:
        return False
    window = candles[-(lookback + 1):-1]
    broke_idx = None
    for i, c in enumerate(window):
        beyond = c["close"] > level if direction == "LONG" else c["close"] < level
        if beyond:
            broke_idx = i
            break
    if broke_idx is None:
        return False
    for c in window[broke_idx + 1:]:
        touch_price = c["low"] if direction == "LONG" else c["high"]
        near = abs(touch_price - level) / level * 100 <= tolerance_pct
        held = c["close"] > level if direction == "LONG" else c["close"] < level
        if near and held:
            return True
    return False


def breakout_checklist(direction, candles, level, oi_change, vol_pct):
    if level is None or direction is None:
        return [], 0
    closed_beyond = candle_closed_beyond(candles, level, direction)
    oi_supports = (oi_change is not None and oi_change > 0) if direction == "LONG" else (oi_change is not None and oi_change < 0)
    volume_ok = vol_pct is not None and vol_pct >= VOLUME_CONFIRM_PCT
    retest_ok = retest_confirmed(candles, level, direction)
    side = "resistance" if direction == "LONG" else "support"
    verb = "above" if direction == "LONG" else "below"
    checks = [
        (f"1H close {verb} ${level:,.2f} ({side})", closed_beyond),
        ("OI increasing in direction", oi_supports),
        ("Volume ≥ confirmation threshold", volume_ok),
        ("Retest held", retest_ok),
    ]
    return checks, sum(1 for _, ok in checks if ok)


# ============================================================================
# ATR + trade plan
# ============================================================================

def calc_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 2:
        return None
    vals = [candles[i]["high"] - candles[i]["low"] for i in range(-(period + 1), -1)]
    return sum(vals) / len(vals)


def _target_ladder(entry, risk, candidate_levels, above):
    """Nearest real levels beyond entry, nearest first; padded out to 3 with
    risk-multiples when there aren't enough real levels. The first padded
    multiple is RR_MIN_FOR_TRADE itself, not something below it — otherwise a
    thin level map (e.g. no confirmed major/macro level yet) would silently
    produce a sub-2:1 TP1 and the whole plan gets dropped for no real reason."""
    pool = sorted({lv[0] for lv in candidate_levels if lv and (lv[0] > entry if above else lv[0] < entry)},
                  reverse=not above)
    tps = pool[:3]
    while len(tps) < 3:
        mult = RR_MIN_FOR_TRADE + 1.5 * len(tps)
        tps.append(entry + risk * mult if above else entry - risk * mult)
    return tps


def build_trade_plan(direction, price, levels, atr, checklist_score):
    if direction is None:
        return None
    long_side = direction == "LONG"

    if long_side:
        entry_level, sl_level, major_opposing = levels["immediate_r"], levels["immediate_s"], levels["major_r"]
    else:
        entry_level, sl_level, major_opposing = levels["immediate_s"], levels["immediate_r"], levels["major_s"]

    entry = entry_level[0] if entry_level else price
    if not entry:
        return None

    # Too close to the next major level with no confirmation yet = no room to run, no plan.
    if major_opposing and _near(major_opposing, entry) and checklist_score < 3:
        return None

    if long_side:
        sl = sl_level[0] if sl_level else entry - (atr * 1.5 if atr else entry * 0.02)
        risk = entry - sl
        if risk <= 0:
            return None
        tp1, tp2, tp3 = _target_ladder(entry, risk, [levels["immediate_r"], levels["major_r"], levels["macro_r"]], above=True)
    else:
        sl = sl_level[0] if sl_level else entry + (atr * 1.5 if atr else entry * 0.02)
        risk = sl - entry
        if risk <= 0:
            return None
        tp1, tp2, tp3 = _target_ladder(entry, risk, [levels["immediate_s"], levels["major_s"], levels["macro_s"]], above=False)

    rr = abs(tp1 - entry) / risk if risk else None
    if rr is None or rr < RR_MIN_FOR_TRADE:
        return None   # never recommend a trade below the minimum R/R

    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr, "invalidation": sl}


# ============================================================================
# Scoring — transparent, per-category points out of SCORE_WEIGHTS, then
# capped/adjusted by higher-timeframe disagreement, weak volume, unreliable
# liquidity, extreme funding, and proximity to the next major level.
# ============================================================================

def _to_points(score_0_100, weight, direction):
    directional = score_0_100 if direction == "LONG" else 100 - score_0_100
    return max(0.0, min(weight, directional / 100 * weight))


def build_score(direction, ef, es, rsi, momentum, bull_trend, bear_trend, c_vol, vol_avg,
                 oi_change, price, nearest_sell_tuple, nearest_buy_tuple, tt_long, buy_pct,
                 sr_support, sr_resistance, bias_map, vol_pct, liquidity_reliability,
                 funding, levels):
    momentum_rsi_blend = (sb.score_rsi(rsi) + score_momentum(momentum, bull_trend, bear_trend)) / 2

    points = {
        "trend":        _to_points(sb.score_trend(ef, es), SCORE_WEIGHTS["trend"], direction),
        "momentum_rsi": _to_points(momentum_rsi_blend, SCORE_WEIGHTS["momentum_rsi"], direction),
        "volume":       _to_points(sb.score_volume(c_vol, vol_avg), SCORE_WEIGHTS["volume"], direction),
        "oi":           _to_points(sb.score_oi(oi_change), SCORE_WEIGHTS["oi"], direction),
        "liquidity":    _to_points(sb.score_liquidity(price, nearest_sell_tuple, nearest_buy_tuple), SCORE_WEIGHTS["liquidity"], direction),
        "whale":        _to_points(sb.score_whale(tt_long, buy_pct), SCORE_WEIGHTS["whale"], direction),
        "sr":           _to_points(sb.score_sr(price, sr_support, sr_resistance), SCORE_WEIGHTS["sr"], direction),
    }

    # A missing order-book side is not usable signal — force the floor
    # regardless of what score_liquidity computed from the remaining side.
    if liquidity_reliability == "Low":
        points["liquidity"] = min(points["liquidity"], 2.0)

    raw_total = sum(points.values())

    cap_reasons = []
    max_score = 100

    conflicting_tfs = []
    if direction == "LONG":
        if bias_map.get("4h") == "Bearish":
            conflicting_tfs.append("4H")
        if bias_map.get("1d") == "Bearish":
            conflicting_tfs.append("Daily")
    else:
        if bias_map.get("4h") == "Bullish":
            conflicting_tfs.append("4H")
        if bias_map.get("1d") == "Bullish":
            conflicting_tfs.append("Daily")
    if conflicting_tfs:
        max_score = min(max_score, 65)
        cap_reasons.append(f"{' and '.join(conflicting_tfs)} trend opposes intraday setup")

    if vol_pct is not None and vol_pct < VOLUME_WEAK_PCT:
        max_score = min(max_score, 55)
        cap_reasons.append("volume below 60% average")

    if liquidity_reliability == "Low":
        max_score = min(max_score, 70)
        cap_reasons.append("liquidity data unreliable")

    adjustment = 0.0
    funding_against = funding is not None and (
        (direction == "LONG" and funding > FUNDING_EXTREME_PCT) or
        (direction == "SHORT" and funding < -FUNDING_EXTREME_PCT)
    )
    if funding_against:
        adjustment -= 8
        cap_reasons.append("funding crowding against setup")

    opposing_major = levels.get("major_r") if direction == "LONG" else levels.get("major_s")
    if _near(opposing_major, price):
        adjustment -= 5
        cap_reasons.append("price close to opposing major level")

    score = max(0, min(max_score, round(raw_total + adjustment)))
    return score, points, cap_reasons




# ============================================================================
# Recommendation state machine
# ============================================================================

def determine_candidate(sig, price, levels, bull_trend, bear_trend):
    if sig:
        return sig["type"]
    if levels["immediate_r"] and price > levels["immediate_r"][0]:
        return "LONG"
    if levels["immediate_s"] and price < levels["immediate_s"][0]:
        return "SHORT"
    # No breakout/pullback signal yet, but a clear intraday trend (price above
    # or below both EMAs) is still a WATCH candidate — e.g. bullish 15m/1H/4H
    # sitting below resistance, waiting for the close that would confirm it.
    if bull_trend:
        return "LONG"
    if bear_trend:
        return "SHORT"
    return None


def recommend(direction, price, candles, levels, bias_map, checklist_score, rr,
               oi_change, funding, liquidity, vol_pct, score, trapped, risk_level):
    extreme_funding = funding is not None and abs(funding) > FUNDING_EXTREME_PCT

    if trapped and risk_level == "\U0001F534 High":
        return "NO TRADE — HIGH RISK"
    if extreme_funding and liquidity["reliability"] == "Low":
        return "NO TRADE — HIGH RISK"
    both_sides_tight = (
        levels["major_r"] and levels["major_s"] and price and
        abs(levels["major_r"][0] - price) / price * 100 < 1.0 and
        abs(price - levels["major_s"][0]) / price * 100 < 1.0
    )
    if both_sides_tight:
        return "NO TRADE — HIGH RISK"

    if direction is None:
        return "WAIT"

    all_bullish = all(v == "Bullish" for v in bias_map.values())
    all_bearish = all(v == "Bearish" for v in bias_map.values())

    major_level = levels["major_r"] if direction == "LONG" else levels["major_s"]
    closed_beyond_major = candle_closed_beyond(candles, major_level[0], direction) if major_level else False

    oi_supports = (oi_change is not None and oi_change > 0) if direction == "LONG" else (oi_change is not None and oi_change < 0)
    liquidity_opposes = (
        liquidity["reliability"] != "Low" and (
            (direction == "LONG" and liquidity["lean"] == "bearish") or
            (direction == "SHORT" and liquidity["lean"] == "bullish")
        )
    )
    vol_strong = vol_pct is not None and vol_pct >= VOLUME_CONFIRM_PCT
    rr_ok = rr is not None and rr >= RR_MIN_FOR_TRADE

    strong_ready = (
        (all_bullish if direction == "LONG" else all_bearish)
        and closed_beyond_major and vol_strong and oi_supports
        and not liquidity_opposes and rr_ok and score >= 75
    )
    if strong_ready:
        return "STRONG LONG" if direction == "LONG" else "STRONG SHORT"

    if checklist_score >= 1 or score >= 50:
        return "LONG WATCH" if direction == "LONG" else "SHORT WATCH"

    return "WAIT"


def verdict_reason(verdict, direction, levels, checklist_score):
    if verdict == "NO TRADE — HIGH RISK":
        return "High-risk conditions — avoid new entries."
    if verdict == "WAIT":
        if direction and checklist_score < 2:
            return "Setup forming, but confirmation is too thin to act on."
        return "Signals mixed or unconfirmed — no clear setup."
    if verdict in ("LONG WATCH", "SHORT WATCH"):
        level = levels.get("immediate_r") if verdict == "LONG WATCH" else levels.get("immediate_s")
        verb = "above" if verdict == "LONG WATCH" else "below"
        if level:
            return f"Strong intraday flow, but wait for 1H close {verb} ${level[0]:,.2f}."
        return "Strong intraday flow, but breakout not yet confirmed."
    if verdict in ("STRONG LONG", "STRONG SHORT"):
        return "Trend, volume, OI, and liquidity all align with acceptable risk/reward."
    return ""


def verdict_action(verdict):
    if verdict == "LONG WATCH":
        return "Do not chase below resistance."
    if verdict == "SHORT WATCH":
        return "Do not chase above support."
    return None


def key_alerts(levels, funding, direction, vol_spike_pct=110, oi_spike_pct=3):
    alerts = []
    ir, iss = levels["immediate_r"], levels["immediate_s"]
    if ir:
        alerts.append(f"Break above ${ir[0]:,.2f}")
        alerts.append(f"Rejection near ${ir[0]:,.2f}")
    if iss:
        alerts.append(f"Lose ${iss[0]:,.2f}")
    alerts.append(f"Volume above {vol_spike_pct}% average")
    alerts.append(f"OI increase above {oi_spike_pct}%")
    note = funding_crowd_note(funding, direction)
    if note:
        alerts.append(f"Funding extreme — {note}")
    return alerts


# ============================================================================
# Orchestration
# ============================================================================

def get_full_analysis_v2(symbol, tf_label=sb.DEFAULT_TF_LABEL, tf_binance="1h"):
    coin = symbol.replace("USDT", "")

    with ThreadPoolExecutor(max_workers=11) as ex:
        f_ticker  = ex.submit(sb.fetch_price_ticker, symbol)
        f_oi      = ex.submit(sb.fetch_oi, symbol)
        f_oichg   = ex.submit(sb.fetch_oi_change, symbol, tf_binance)
        f_funding = ex.submit(sb.fetch_funding, symbol)
        f_taker   = ex.submit(sb.fetch_taker_volume, symbol)
        f_tt      = ex.submit(sb.fetch_top_trader, symbol)
        f_mtf     = {tf: ex.submit(sb.fetch_candles, symbol, tf, sb.CANDLE_LIMIT) for tf in MTF_TIMEFRAMES}

        ticker    = f_ticker.result()
        oi        = f_oi.result()
        oi_change = f_oichg.result()
        funding   = f_funding.result()
        buy_pct, sell_pct = f_taker.result()
        tt_long, tt_short = f_tt.result()
        mtf_candles = {tf: f.result() for tf, f in f_mtf.items()}

    price  = ticker.get("price", 0)
    change = ticker.get("change", 0)
    candles = mtf_candles.get(tf_binance) or mtf_candles.get("1h") or []

    with ThreadPoolExecutor(max_workers=1) as ex:
        f_liq = ex.submit(sb.fetch_liquidity_walls, symbol, price)
        levels = build_levels(mtf_candles, price)
        buy_walls, sell_walls = f_liq.result()

    sig = sb.check_signal(candles) if candles else None

    closes  = [c["close"]  for c in candles] if candles else []
    volumes = [c["volume"] for c in candles] if candles else []

    ema21_series = sb.calc_ema_series(closes, sb.EMA_FAST) if len(closes) >= sb.EMA_FAST else []
    ema50_series = sb.calc_ema_series(closes, sb.EMA_SLOW) if len(closes) >= sb.EMA_SLOW else []
    ef  = round(ema21_series[-1], 2) if ema21_series else None
    es  = round(ema50_series[-1], 2) if ema50_series else None
    rsi = sb.calc_rsi(closes)

    bull_trend = isinstance(ef, float) and isinstance(es, float) and ef > es and price > ef and price > es
    bear_trend = isinstance(ef, float) and isinstance(es, float) and ef < es and price < ef and price < es
    cross_label = "Bullish" if bull_trend else ("Bearish" if bear_trend else "Neutral")

    vol_avg = sb.calc_sma(volumes[:-1], sb.VOL_SMA) if len(volumes) > sb.VOL_SMA else 0
    c_vol   = volumes[-2] if len(volumes) >= 2 else 0
    vol_pct = round(c_vol / vol_avg * 100) if vol_avg else None

    whale_skew = (tt_long - 50) if tt_long is not None else None
    momentum = sb.momentum_label(ef, es, rsi)

    bias_map  = {tf: tf_bias(mtf_candles.get(tf, [])) for tf in MTF_TIMEFRAMES}
    alignment = mtf_alignment(bias_map)

    liquidity = liquidity_view(buy_walls, sell_walls, levels)
    oi_flow_line = oi_price_flow(change, oi_change)

    nearest_sell_tuple = (sell_walls[0]["price"], sell_walls[0]["notional"]) if sell_walls else None
    nearest_buy_tuple  = (buy_walls[0]["price"], buy_walls[0]["notional"]) if buy_walls else None
    sr_support_list    = [levels["immediate_s"]] if levels["immediate_s"] else []
    sr_resistance_list = [levels["immediate_r"]] if levels["immediate_r"] else []

    sig_candidate = determine_candidate(sig, price, levels, bull_trend, bear_trend)
    direction_for_score = sig_candidate or ("LONG" if bull_trend else "SHORT" if bear_trend else "LONG")

    score, points, cap_reasons = build_score(
        direction_for_score, ef, es, rsi, momentum, bull_trend, bear_trend, c_vol, vol_avg,
        oi_change, price, nearest_sell_tuple, nearest_buy_tuple, tt_long, buy_pct,
        sr_support_list, sr_resistance_list, bias_map, vol_pct, liquidity["reliability"],
        funding, levels,
    )

    risk_level = sb.calc_risk_level(funding, oi_change, rsi, bull_trend or bear_trend, whale_skew)
    trapped = sb.liquidity_trapped(buy_walls, sell_walls)

    level_for_checklist = None
    if sig_candidate == "LONG" and levels["immediate_r"]:
        level_for_checklist = levels["immediate_r"][0]
    elif sig_candidate == "SHORT" and levels["immediate_s"]:
        level_for_checklist = levels["immediate_s"][0]
    checks, checklist_score = breakout_checklist(sig_candidate, candles, level_for_checklist, oi_change, vol_pct)

    atr = calc_atr(candles)
    plan = build_trade_plan(sig_candidate, price, levels, atr, checklist_score) if sig_candidate else None
    rr = plan["rr"] if plan else None

    verdict = recommend(sig_candidate, price, candles, levels, bias_map, checklist_score, rr,
                         oi_change, funding, liquidity, vol_pct, score, trapped, risk_level)
    reason = verdict_reason(verdict, sig_candidate, levels, checklist_score)
    action = verdict_action(verdict)

    alerts = key_alerts(levels, funding, sig_candidate)

    return render_message(
        coin=coin, tf_label=tf_label, price=price, change=change,
        ef=ef, es=es, cross_label=cross_label, rsi=rsi, momentum=momentum,
        bias_map=bias_map, alignment=alignment, levels=levels,
        liquidity=liquidity,
        oi=oi, oi_change=oi_change, funding=funding, oi_flow_line=oi_flow_line,
        tt_long=tt_long, buy_pct=buy_pct,
        vol_pct=vol_pct,
        sig_candidate=sig_candidate, checks=checks, checklist_score=checklist_score,
        points=points, score=score, cap_reasons=cap_reasons,
        plan=plan, verdict=verdict, reason=reason, action=action, alerts=alerts,
    )


def render_message(**d):
    lines = []
    ch_icon = "▲" if d["change"] >= 0 else "▼"
    ch_sign = "+" if d["change"] >= 0 else ""
    lines.append(f"\U0001F4CA <b>{d['coin']}/USDT</b> | {d['tf_label']}  <code>${d['price']:,.2f}</code>  {ch_icon}{ch_sign}{d['change']}%")
    lines.append("")

    ef_disp = d["ef"] if d["ef"] is not None else "—"
    es_disp = d["es"] if d["es"] is not None else "—"
    lines.append(f"Trend: {d['cross_label']}")
    lines.append(f"EMA21 <code>{ef_disp}</code> / EMA50 <code>{es_disp}</code>")
    lines.append(f"RSI {rsi_interpretation(d['rsi'])}")
    lines.append(f"Momentum: {d['momentum']}")
    lines.append("")

    bm = d["bias_map"]
    lines.append("\U0001F9ED <b>Multi-Timeframe</b>")
    lines.append(f"15m: {bm.get('15m','—')} · 1H: {bm.get('1h','—')} · 4H: {bm.get('4h','—')} · Daily: {bm.get('1d','—')}")
    lines.append(f"Alignment: {d['alignment']}")
    lines.append("")

    lv = d["levels"]
    def lvl(key):
        return f"${lv[key][0]:,.2f}" if lv.get(key) else None
    lines.append("\U0001F4CD <b>Key Levels</b>")
    r_immediate = lvl("immediate_r") or "Not confirmed"
    r_parts = [f"Immediate {r_immediate}"]
    if lvl("major_r"):
        r_parts.append(f"Major {lvl('major_r')}")
    s_immediate = lvl("immediate_s") or "Not confirmed"
    s_parts = [f"Immediate {s_immediate}"]
    if lvl("major_s"):
        s_parts.append(f"Major {lvl('major_s')}")
    lines.append(f"Resistance: {' · '.join(r_parts)}")
    lines.append(f"Support: {' · '.join(s_parts)}")
    lines.append("")

    liq = d["liquidity"]
    lines.append("\U0001F4A7 <b>Liquidity</b>")
    if liq["note"]:
        buy_disp = sb.fmt_notional(liq["buy_total"]) if liq["buy_total"] else "data unavailable"
        sell_disp = sb.fmt_notional(liq["sell_total"]) if liq["sell_total"] else "data unavailable"
        lines.append(f"Buy {buy_disp} | Sell {sell_disp}")
        lines.append(f"Reliability: Low")
        lines.append(f"Bias: {liq['bias']}")
        lines.append(liq["note"])
    else:
        lines.append(f"Buy {sb.fmt_notional(liq['buy_total'])} · Sell {sb.fmt_notional(liq['sell_total'])}")
        lines.append(f"Bias: {liq['bias']} · Reliability: {liq['reliability']}")
        if liq["sweep"]:
            lines.append(liq["sweep"])
    lines.append("")

    oi_disp = f"{d['oi']}B" if d["oi"] else "—"
    oi_chg = f"{'+' if d['oi_change'] is not None and d['oi_change']>=0 else ''}{d['oi_change']}%" if d["oi_change"] is not None else "—"
    f_disp = f"{'+' if d['funding'] is not None and d['funding']>=0 else ''}{d['funding']}%" if d["funding"] is not None else "—"
    lines.append("\U0001F4C8 <b>Futures</b>")
    lines.append(f"OI <code>{oi_disp}</code> ({oi_chg}) · Funding <code>{f_disp}</code>")
    lines.append(f"Flow: {d['oi_flow_line']}")
    lines.append("")

    vol_disp = f"{d['vol_pct']}% of {sb.VOL_SMA}-period average — {volume_quality_text(d['vol_pct'])}" if d["vol_pct"] is not None else "—"
    lines.append(f"\U0001F40B Flow: Whales {whale_flow_label(d['tt_long'], d['buy_pct'])} · Takers {taker_label(d['buy_pct'])}")
    lines.append(f"\U0001F4E6 Volume: {vol_disp}")
    lines.append("")

    if d["sig_candidate"] and d["checks"]:
        lines.append(f"✅ <b>Breakout Confirmation: {d['checklist_score']}/4</b>")
        for label, ok in d["checks"]:
            lines.append(f"{'✅' if ok else '❌'} {label}")
        if d["checklist_score"] < 3:
            lines.append("Action: Wait for retest or stronger volume")
        lines.append("")

    p = d["points"]
    lines.append("\U0001F3AF <b>Score</b>")
    lines.append(
        f"Trend {p['trend']:.0f}/{SCORE_WEIGHTS['trend']} · "
        f"Mom/RSI {p['momentum_rsi']:.0f}/{SCORE_WEIGHTS['momentum_rsi']} · "
        f"Volume {p['volume']:.0f}/{SCORE_WEIGHTS['volume']}"
    )
    lines.append(
        f"Futures {p['oi']:.0f}/{SCORE_WEIGHTS['oi']} · "
        f"Liquidity {p['liquidity']:.0f}/{SCORE_WEIGHTS['liquidity']} · "
        f"Whale {p['whale']:.0f}/{SCORE_WEIGHTS['whale']} · "
        f"S/R {p['sr']:.0f}/{SCORE_WEIGHTS['sr']}"
    )
    cap_note = f" — capped: {'; '.join(d['cap_reasons'])}" if d["cap_reasons"] else ""
    lines.append(f"Total: {d['score']}/100{cap_note}")
    lines.append("")

    if d["plan"]:
        pl = d["plan"]
        direction_word = "Long" if d["sig_candidate"] == "LONG" else "Short"
        lines.append(f"\U0001F3AF <b>{direction_word} Plan</b>")
        verb = "above" if d["sig_candidate"] == "LONG" else "below"
        lines.append(f"Trigger: 1H close {verb} <code>${pl['entry']:,.2f}</code>")
        lines.append(f"Entry Zone: <code>${pl['entry']:,.2f}</code>")
        lines.append(f"SL <code>${pl['sl']:,.2f}</code>")
        lines.append(f"TP1 <code>${pl['tp1']:,.2f}</code> · TP2 <code>${pl['tp2']:,.2f}</code> · TP3 <code>${pl['tp3']:,.2f}</code>")
        lines.append(f"R/R 1:{pl['rr']:.1f}")
        lines.append(f"Invalidation: 1H close back {'below' if d['sig_candidate']=='LONG' else 'above'} <code>${pl['invalidation']:,.2f}</code>")
        lines.append("")
    elif d["sig_candidate"]:
        lines.append("\U0001F3AF No active trade plan — wait for confirmation.")
        lines.append("")

    lines.append(f"⚠️ <b>Verdict: {d['verdict']}</b>")
    if d.get("reason"):
        lines.append(f"Reason: {d['reason']}")
    if d.get("action"):
        lines.append(f"Action: {d['action']}")
    lines.append("")

    lines.append("\U0001F514 <b>Key Alerts Armed</b>")
    for a in d["alerts"]:
        lines.append(f"• {a}")

    return "\n".join(lines)
