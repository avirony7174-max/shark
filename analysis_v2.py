"""
Redesigned Telegram analysis output (see chat spec: multi-timeframe alignment,
layered S/R, clearer liquidity wording, OI+price interpretation, breakout
checklist, transparent capped scoring, 6-state recommendation, trade plans,
key alerts).

Kept deliberately separate from signal_bot.py's get_full_analysis() so the
existing production path (handle_message, auto_signal_loop) is untouched
until this is reviewed against live output.
"""
from concurrent.futures import ThreadPoolExecutor

import signal_bot as sb
from support_resistance import calc_support_resistance

MTF_TIMEFRAMES = ["15m", "1h", "4h", "1d"]
MACRO_RELEVANCE_PCT = 5.0        # only surface a Daily level if price is within 5% of it
LIQUIDITY_IMBALANCE_MIN = 0.05   # min buy/sell liquidity skew before we say anything
RETEST_LOOKBACK = 6
RETEST_TOLERANCE_PCT = 0.3
VOLUME_CONFIRM_MIN_PCT = 90
VOLUME_CONFIRM_MAX_PCT = 110
RR_MIN_FOR_STRONG = 2.0
OI_NOISE_PCT = 0.1
SCORE_CAP_WEAK = 55               # cap applied when volume is weak or MTF is mixed
ATR_PERIOD = 14

CONF_WEIGHTS_V2 = {
    "trend": 20, "momentum": 10, "rsi": 8, "volume": 12,
    "funding": 8, "oi": 12, "liquidity": 12, "whale": 10, "sr": 8,
}
CONF_LABELS_V2 = {
    "trend": "Trend", "momentum": "Momentum", "rsi": "RSI", "volume": "Volume",
    "funding": "Funding", "oi": "OI", "liquidity": "Liquidity", "whale": "Whale", "sr": "S/R",
}
MOMENTUM_POINTS = {"Strong": 30, "Moderate": 15, "Weak": 0}


# ============================================================================
# Scoring (v2 weights, adds "momentum" as its own transparent factor)
# ============================================================================

def score_momentum(momentum_lbl, bull_trend, bear_trend):
    pts = MOMENTUM_POINTS.get(momentum_lbl, 0)
    if bull_trend:
        return 50 + pts
    if bear_trend:
        return 50 - pts
    return 50


def calc_bullish_score_v2(scores):
    weight_sum = sum(CONF_WEIGHTS_V2.values())
    return sum(scores[k] * CONF_WEIGHTS_V2[k] for k in CONF_WEIGHTS_V2) / weight_sum


def calc_confidence_v2(scores, direction):
    bullish_total = calc_bullish_score_v2(scores)
    return round(bullish_total if direction == "LONG" else 100 - bullish_total)


def confidence_breakdown_v2(scores, direction, top_n=6, min_abs=1.0):
    weight_sum = sum(CONF_WEIGHTS_V2.values())
    contribs = {}
    for k in CONF_WEIGHTS_V2:
        raw = (scores[k] - 50) * CONF_WEIGHTS_V2[k] / weight_sum
        contribs[k] = raw if direction == "LONG" else -raw
    ranked = sorted(contribs.items(), key=lambda x: -abs(x[1]))
    parts = []
    for k, v in ranked:
        if abs(v) < min_abs or len(parts) >= top_n:
            continue
        sign = "+" if v >= 0 else ""
        parts.append(f"{CONF_LABELS_V2[k]} {sign}{round(v)}")
    return parts


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
    if bias_map.get("4h") == "Bullish" and bias_map.get("1d") == "Bullish" and bulls >= 3:
        return "Strong Bullish"
    if bias_map.get("4h") == "Bearish" and bias_map.get("1d") == "Bearish" and bears >= 3:
        return "Strong Bearish"
    return "Mixed"


# ============================================================================
# Layered support/resistance
# ============================================================================

def build_levels(mtf_candles, price):
    s_1h, r_1h = calc_support_resistance(mtf_candles.get("1h", []), price) if mtf_candles.get("1h") else ([], [])
    s_4h, r_4h = calc_support_resistance(mtf_candles.get("4h", []), price) if mtf_candles.get("4h") else ([], [])
    s_1d, r_1d = calc_support_resistance(mtf_candles.get("1d", []), price) if mtf_candles.get("1d") else ([], [])

    immediate_r = r_1h[0] if r_1h else None
    major_r     = r_4h[0] if r_4h else None
    immediate_s = s_1h[0] if s_1h else None
    major_s     = s_4h[0] if s_4h else None

    macro_r = r_1d[0] if (r_1d and price and abs(r_1d[0][0] - price) / price * 100 <= MACRO_RELEVANCE_PCT) else None
    macro_s = s_1d[0] if (s_1d and price and abs(s_1d[0][0] - price) / price * 100 <= MACRO_RELEVANCE_PCT) else None

    return {
        "immediate_r": immediate_r, "major_r": major_r, "macro_r": macro_r,
        "immediate_s": immediate_s, "major_s": major_s, "macro_s": macro_s,
    }


# ============================================================================
# Liquidity wording
# ============================================================================

def liquidity_view(buy_walls, sell_walls, levels):
    buy_total, sell_total = sb.liquidity_totals(buy_walls, sell_walls)
    if buy_total <= 0 and sell_total <= 0:
        return None, None, buy_total, sell_total, None

    imbalance = abs(buy_total - sell_total) / max(buy_total, sell_total, 1)
    if imbalance < LIQUIDITY_IMBALANCE_MIN:
        return "Balanced", None, buy_total, sell_total, None

    if buy_total < sell_total:
        # support-side liquidity is thinner -> easier to fall through -> downside sweep risk
        bias_line = "Sell liquidity overhead is stronger"
        lean = "bearish"
        frm = levels["immediate_s"][0] if levels["immediate_s"] else None
        to = levels["major_s"][0] if levels["major_s"] else (buy_walls[0]["price"] if buy_walls else None)
        sweep_line = f"Possible downside sweep: ${frm:,.2f} → ${to:,.2f}" if (frm and to) else None
    else:
        bias_line = "Buy liquidity below is stronger"
        lean = "bullish"
        frm = levels["immediate_r"][0] if levels["immediate_r"] else None
        to = levels["major_r"][0] if levels["major_r"] else (sell_walls[0]["price"] if sell_walls else None)
        sweep_line = f"Possible upside sweep: ${frm:,.2f} → ${to:,.2f}" if (frm and to) else None

    return bias_line, sweep_line, buy_total, sell_total, lean


# ============================================================================
# OI + price interpretation
# ============================================================================

def oi_price_flow(price_change_pct, oi_change_pct, noise=OI_NOISE_PCT):
    if price_change_pct is None or oi_change_pct is None:
        return "No strong fresh positioning"
    price_up, price_down = price_change_pct > noise, price_change_pct < -noise
    oi_up, oi_down = oi_change_pct > noise, oi_change_pct < -noise
    if price_up and oi_up:
        return "Fresh longs entering — bullish continuation possible"
    if price_up and oi_down:
        return "Short covering — rally may be weak"
    if price_down and oi_up:
        return "Fresh shorts entering — bearish continuation possible"
    if price_down and oi_down:
        return "Long liquidation — possible weak sell-off"
    return "No strong fresh positioning"


# ============================================================================
# Volume / breakout quality
# ============================================================================

def volume_read(c_vol, vol_avg, period=sb.VOL_SMA):
    if not vol_avg:
        return None, "Weak", f"{period}-period average"
    pct = round(c_vol / vol_avg * 100)
    quality = "Strong" if pct >= VOLUME_CONFIRM_MAX_PCT else "Moderate" if pct >= VOLUME_CONFIRM_MIN_PCT else "Weak"
    return pct, quality, f"{period}-period average"


def whale_flow_label(tt_long, taker_buy):
    if tt_long is None or taker_buy is None:
        return "Unclear"
    bull = (1 if tt_long > 55 else 0) + (1 if taker_buy > 55 else 0)
    bear = (1 if tt_long < 45 else 0) + (1 if taker_buy < 45 else 0)
    if bull == 2 and bear == 0:
        return "Strong long bias"
    if bear == 2 and bull == 0:
        return "Strong short bias"
    if bull > bear:
        return "Slight long bias"
    if bear > bull:
        return "Slight short bias"
    return "Neutral"


def taker_label(buy_pct):
    if buy_pct is None:
        return "—"
    if buy_pct >= 55:
        return "Buyers active"
    if buy_pct <= 45:
        return "Sellers active"
    return "Balanced"


# ============================================================================
# Breakout confirmation checklist
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
    volume_ok = vol_pct is not None and vol_pct >= VOLUME_CONFIRM_MIN_PCT
    retest_ok = retest_confirmed(candles, level, direction)
    checks = [
        ("Candle closed beyond level", closed_beyond),
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
    """Pool immediate/major/macro levels beyond entry, sorted nearest-first,
    so TP1<TP2<TP3 is guaranteed even when 1H/4H/1D levels don't naturally
    order themselves (e.g. a 1H swing high sitting above the nearest 4H one)."""
    pool = sorted({lv[0] for lv in candidate_levels if lv and (lv[0] > entry if above else lv[0] < entry)},
                  reverse=not above)
    tps = pool[:3]
    while len(tps) < 3:
        mult = 1.5 * (len(tps) + 1)
        tps.append(entry + risk * mult if above else entry - risk * mult)
    return tps


def build_trade_plan(direction, price, levels, atr):
    long_side = direction in ("LONG", "STRONG LONG", "LONG WATCH")
    if long_side:
        entry = levels["immediate_r"][0] if levels["immediate_r"] else price
        sl = levels["immediate_s"][0] if levels["immediate_s"] else entry - (atr * 1.5 if atr else entry * 0.02)
        risk = entry - sl
        if risk <= 0:
            return None
        tp1, tp2, tp3 = _target_ladder(entry, risk, [levels["immediate_r"], levels["major_r"], levels["macro_r"]], above=True)
    else:
        entry = levels["immediate_s"][0] if levels["immediate_s"] else price
        sl = levels["immediate_r"][0] if levels["immediate_r"] else entry + (atr * 1.5 if atr else entry * 0.02)
        risk = sl - entry
        if risk <= 0:
            return None
        tp1, tp2, tp3 = _target_ladder(entry, risk, [levels["immediate_s"], levels["major_s"], levels["macro_s"]], above=False)

    rr = abs(tp1 - entry) / risk if risk else None
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr, "invalidation": sl}


# ============================================================================
# Recommendation state machine
# ============================================================================

def determine_candidate(sig, price, levels):
    if sig:
        return sig["type"]
    if levels["immediate_r"] and price > levels["immediate_r"][0]:
        return "LONG"
    if levels["immediate_s"] and price < levels["immediate_s"][0]:
        return "SHORT"
    return None


def recommend(direction_candidate, alignment, checklist_score, rr, oi_change, liquidity_lean, risk_level, trapped):
    if trapped and risk_level == "\U0001F534 High":
        return "NO TRADE — HIGH RISK"
    if direction_candidate is None:
        return "WAIT"
    if direction_candidate == "LONG" and alignment == "Strong Bearish":
        return "WAIT"
    if direction_candidate == "SHORT" and alignment == "Strong Bullish":
        return "WAIT"

    oi_supports = (oi_change is not None and oi_change > 0) if direction_candidate == "LONG" else (oi_change is not None and oi_change < 0)
    liquidity_opposes = (
        (direction_candidate == "LONG" and liquidity_lean == "bearish") or
        (direction_candidate == "SHORT" and liquidity_lean == "bullish")
    )

    strong = (
        alignment == ("Strong Bullish" if direction_candidate == "LONG" else "Strong Bearish")
        and checklist_score >= 3
        and oi_supports
        and not liquidity_opposes
        and rr is not None and rr >= RR_MIN_FOR_STRONG
    )
    if strong:
        return "STRONG LONG" if direction_candidate == "LONG" else "STRONG SHORT"

    if checklist_score >= 1:
        return "LONG WATCH" if direction_candidate == "LONG" else "SHORT WATCH"

    return "WAIT"


def verdict_reason(verdict, alignment, cap_reasons, checklist_score, sig_candidate):
    if verdict == "NO TRADE — HIGH RISK":
        return "Price is trapped between strong opposing liquidity walls with elevated risk — avoid new entries until it resolves."
    if verdict == "WAIT":
        if alignment == "Mixed":
            return "Short-term structure looks constructive, but higher timeframes disagree — no confirmed edge yet."
        if sig_candidate and checklist_score < 2:
            return "A setup is forming but the confirmation checklist is too thin to act on."
        return "No clear directional edge right now — standing aside."
    if verdict in ("LONG WATCH", "SHORT WATCH"):
        reason = "Directional bias is forming"
        if cap_reasons:
            reason += f", but {', '.join(cap_reasons)} keeps confidence capped"
        return reason + " — wait for stronger confirmation before sizing up."
    if verdict in ("STRONG LONG", "STRONG SHORT"):
        return "Trend, volume, OI and liquidity all line up with an acceptable risk/reward — higher-conviction setup."
    return ""


def key_alerts(levels, vol_spike_pct=110, oi_spike_pct=3):
    alerts = []
    if levels["major_r"]:
        alerts.append(f"Break above ${levels['major_r'][0]:,.2f} (major resistance)")
    if levels["immediate_r"]:
        alerts.append(f"Rejection near ${levels['immediate_r'][0]:,.2f} (immediate resistance)")
    if levels["major_s"]:
        alerts.append(f"Lose ${levels['major_s'][0]:,.2f} (major support)")
    alerts.append(f"Volume spike ≥{vol_spike_pct}% average")
    alerts.append(f"OI spike ≥{oi_spike_pct}%")
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

    bull_trend = isinstance(ef, float) and isinstance(es, float) and ef > es
    bear_trend = isinstance(ef, float) and isinstance(es, float) and ef < es
    cross_label = "Bullish" if bull_trend else ("Bearish" if bear_trend else "Flat")

    vol_avg = sb.calc_sma(volumes[:-1], sb.VOL_SMA) if len(volumes) > sb.VOL_SMA else 0
    c_vol   = volumes[-2] if len(volumes) >= 2 else 0
    vol_pct, vol_quality, vol_period_label = volume_read(c_vol, vol_avg)

    whale_skew = (tt_long - 50) if tt_long is not None else None
    momentum = sb.momentum_label(ef, es, rsi)

    bias_map  = {tf: tf_bias(mtf_candles.get(tf, [])) for tf in MTF_TIMEFRAMES}
    alignment = mtf_alignment(bias_map)

    liq_bias_line, sweep_line, buy_total, sell_total, lean = liquidity_view(buy_walls, sell_walls, levels)
    oi_flow_line = oi_price_flow(change, oi_change)

    nearest_sell_tuple = (sell_walls[0]["price"], sell_walls[0]["notional"]) if sell_walls else None
    nearest_buy_tuple  = (buy_walls[0]["price"], buy_walls[0]["notional"]) if buy_walls else None
    sr_support_list    = [levels["immediate_s"]] if levels["immediate_s"] else []
    sr_resistance_list  = [levels["immediate_r"]] if levels["immediate_r"] else []

    scores = {
        "trend":     sb.score_trend(ef, es),
        "momentum":  score_momentum(momentum, bull_trend, bear_trend),
        "rsi":       sb.score_rsi(rsi),
        "volume":    sb.score_volume(c_vol, vol_avg),
        "funding":   sb.score_funding(funding),
        "oi":        sb.score_oi(oi_change),
        "liquidity": sb.score_liquidity(price, nearest_sell_tuple, nearest_buy_tuple),
        "whale":     sb.score_whale(tt_long, buy_pct),
        "sr":        sb.score_sr(price, sr_support_list, sr_resistance_list),
    }

    sig_candidate = determine_candidate(sig, price, levels)
    direction_for_score = sig_candidate or ("LONG" if bull_trend else "SHORT" if bear_trend else "LONG")
    confidence = calc_confidence_v2(scores, direction_for_score)
    breakdown_parts = confidence_breakdown_v2(scores, direction_for_score)

    cap_reasons = []
    if vol_quality == "Weak":
        cap_reasons.append("weak volume")
    if alignment == "Mixed":
        cap_reasons.append("mixed timeframes")
    if cap_reasons:
        confidence = min(confidence, SCORE_CAP_WEAK)

    risk_level = sb.calc_risk_level(funding, oi_change, rsi, bull_trend or bear_trend, whale_skew)
    trapped = sb.liquidity_trapped(buy_walls, sell_walls)

    level_for_checklist = None
    if sig_candidate == "LONG" and levels["immediate_r"]:
        level_for_checklist = levels["immediate_r"][0]
    elif sig_candidate == "SHORT" and levels["immediate_s"]:
        level_for_checklist = levels["immediate_s"][0]
    checks, checklist_score = breakout_checklist(sig_candidate, candles, level_for_checklist, oi_change, vol_pct)

    atr = calc_atr(candles)
    plan = build_trade_plan(sig_candidate, price, levels, atr) if sig_candidate else None
    rr = plan["rr"] if plan else None

    verdict = recommend(sig_candidate, alignment, checklist_score, rr, oi_change, lean, risk_level, trapped)
    reason = verdict_reason(verdict, alignment, cap_reasons, checklist_score, sig_candidate)

    alerts = key_alerts(levels)

    return render_message(
        coin=coin, tf_label=tf_label, price=price, change=change,
        ef=ef, es=es, cross_label=cross_label, rsi=rsi, momentum=momentum,
        bias_map=bias_map, alignment=alignment, levels=levels,
        buy_total=buy_total, sell_total=sell_total, liq_bias_line=liq_bias_line, sweep_line=sweep_line,
        oi=oi, oi_change=oi_change, funding=funding, oi_flow_line=oi_flow_line,
        tt_long=tt_long, buy_pct=buy_pct,
        vol_pct=vol_pct, vol_quality=vol_quality, vol_period_label=vol_period_label,
        sig_candidate=sig_candidate, checks=checks, checklist_score=checklist_score,
        breakdown_parts=breakdown_parts, confidence=confidence, cap_reasons=cap_reasons,
        plan=plan, verdict=verdict, reason=reason, alerts=alerts,
    )


def render_message(**d):
    lines = []
    ch_icon = "▲" if d["change"] >= 0 else "▼"
    ch_sign = "+" if d["change"] >= 0 else ""
    lines.append(f"\U0001F4CA <b>{d['coin']}/USDT</b> | {d['tf_label']}  <code>${d['price']:,.2f}</code>  {ch_icon}{ch_sign}{d['change']}%")
    lines.append("")

    ef_disp = d["ef"] if d["ef"] is not None else "—"
    es_disp = d["es"] if d["es"] is not None else "—"
    rsi_disp = d["rsi"] if d["rsi"] is not None else "—"
    lines.append(f"Trend: {d['cross_label']} · EMA21 <code>{ef_disp}</code> / EMA50 <code>{es_disp}</code>")
    lines.append(f"RSI {rsi_disp} · Momentum {d['momentum']}")
    lines.append("")

    bm = d["bias_map"]
    lines.append("\U0001F9ED <b>Multi-Timeframe</b>")
    lines.append(f"15m {bm.get('15m','—')} · 1H {bm.get('1h','—')} · 4H {bm.get('4h','—')} · Daily {bm.get('1d','—')}")
    lines.append(f"Alignment: {d['alignment']}")
    lines.append("")

    lv = d["levels"]
    def lvl(key):
        return f"${lv[key][0]:,.2f}" if lv.get(key) else "—"
    lines.append("\U0001F4CD <b>Key Levels</b>")
    r_parts = [f"Immediate {lvl('immediate_r')}", f"Major {lvl('major_r')}"]
    if lv.get("macro_r"):
        r_parts.append(f"Macro {lvl('macro_r')}")
    s_parts = [f"Immediate {lvl('immediate_s')}", f"Major {lvl('major_s')}"]
    if lv.get("macro_s"):
        s_parts.append(f"Macro {lvl('macro_s')}")
    lines.append(f"Resistance  {' · '.join(r_parts)}")
    lines.append(f"Support     {' · '.join(s_parts)}")
    lines.append("")

    lines.append("\U0001F4A7 <b>Liquidity</b>")
    lines.append(f"Sell {sb.fmt_notional(d['sell_total'])} · Buy {sb.fmt_notional(d['buy_total'])}"
                 + (f" · {d['liq_bias_line']}" if d["liq_bias_line"] else ""))
    if d["sweep_line"]:
        lines.append(d["sweep_line"])
    lines.append("")

    oi_disp = f"{d['oi']}B" if d["oi"] else "—"
    oi_chg = f"{'+' if d['oi_change'] is not None and d['oi_change']>=0 else ''}{d['oi_change']}%" if d["oi_change"] is not None else "—"
    f_disp = f"{'+' if d['funding'] is not None and d['funding']>=0 else ''}{d['funding']}%" if d["funding"] is not None else "—"
    lines.append("\U0001F4C8 <b>Futures</b>")
    lines.append(f"OI <code>{oi_disp}</code> ({oi_chg}) · Funding <code>{f_disp}</code>")
    lines.append(f"Flow: {d['oi_flow_line']}")
    lines.append("")

    lines.append(f"\U0001F40B Whale: {whale_flow_label(d['tt_long'], d['buy_pct'])} · Taker: {taker_label(d['buy_pct'])}")
    vol_disp = f"{d['vol_pct']}% of {d['vol_period_label']}" if d["vol_pct"] is not None else "—"
    lines.append(f"\U0001F4E6 Volume: {vol_disp} — {d['vol_quality'].lower()} breakout quality")
    lines.append("")

    if d["sig_candidate"] and d["checks"]:
        lines.append(f"✅ <b>Breakout Confirmation: {d['checklist_score']}/4</b>")
        for label, ok in d["checks"]:
            lines.append(f"{'✅' if ok else '❌'} {label}")
        lines.append("")

    lines.append("\U0001F3AF <b>Score</b>")
    if d["breakdown_parts"]:
        lines.append(" · ".join(d["breakdown_parts"]))
    cap_note = f"  (capped — {', '.join(d['cap_reasons'])})" if d.get("cap_reasons") else ""
    lines.append(f"Total: {d['confidence']}/100{cap_note}")
    lines.append("")

    if d["plan"] and d["sig_candidate"]:
        p = d["plan"]
        direction_word = "LONG" if d["sig_candidate"] == "LONG" else "SHORT"
        lines.append(f"\U0001F3AF <b>Trade Plan — {d['verdict']}</b>")
        lines.append(f"Entry ~<code>${p['entry']:,.2f}</code> ({direction_word})")
        lines.append(f"SL <code>${p['sl']:,.2f}</code> · Invalidation <code>${p['invalidation']:,.2f}</code>")
        lines.append(f"TP1 <code>${p['tp1']:,.2f}</code> · TP2 <code>${p['tp2']:,.2f}</code> · TP3 <code>${p['tp3']:,.2f}</code>")
        rr_disp = f"1:{p['rr']:.1f}" if p["rr"] else "—"
        lines.append(f"R/R {rr_disp} · Confidence {d['confidence']}%")
        lines.append("")

    lines.append(f"⚠️ <b>Verdict: {d['verdict']}</b>")
    if d.get("reason"):
        lines.append(f"Reason: {d['reason']}")
    lines.append("")

    lines.append("\U0001F514 <b>Key Alerts Armed</b>")
    for a in d["alerts"]:
        lines.append(f"• {a}")

    return "\n".join(lines)
