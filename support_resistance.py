SR_LOOKBACK  = 5      # candles each side to confirm a swing high/low
SR_TOLERANCE = 0.015  # cluster swing points within 1.5% as one level
SR_LEVELS    = 2      # how many S/R levels to show each side


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
