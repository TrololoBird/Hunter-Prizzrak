# mypy: ignore-errors
"""Low-level manipulation primitives — Polars-first, zero Python loops for TA.

All functions accept ``pl.DataFrame`` with columns ``[ts, open, high, low, close, volume]``.
Feature computations use Polars expressions + polars_ta — no manual TA.
"""
from __future__ import annotations

from typing import Any

import polars as pl
import polars_ta.ta as plta


def ohlcv_to_df(ohlcv: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "ts": [float(r[0]) for r in ohlcv],
        "open": [float(r[1]) for r in ohlcv],
        "high": [float(r[2]) for r in ohlcv],
        "low": [float(r[3]) for r in ohlcv],
        "close": [float(r[4]) for r in ohlcv],
        "volume": [float(r[5]) for r in ohlcv],
    })


def _pl_features() -> list[pl.Expr]:
    return [
        (pl.col("close") - pl.col("open")).abs().alias("_body"),
        (pl.when(pl.col("open") > 0)
         .then((pl.col("close") - pl.col("open")).abs() / pl.col("open") * 100)
         .otherwise(0.0)).alias("_body_pct"),
        (pl.col("high") - pl.col("low")).alias("_range"),
    ]


def _swing_exprs() -> list[pl.Expr]:
    return [
        ((pl.col("high") > pl.col("high").shift(1)) &
         (pl.col("high") > pl.col("high").shift(2)) &
         (pl.col("high") >= pl.col("high").shift(-1)) &
         (pl.col("high") >= pl.col("high").shift(-2))).alias("_swing_high"),
        ((pl.col("low") < pl.col("low").shift(1)) &
         (pl.col("low") < pl.col("low").shift(2)) &
         (pl.col("low") <= pl.col("low").shift(-1)) &
         (pl.col("low") <= pl.col("low").shift(-2))).alias("_swing_low"),
    ]


def compute_features(df: pl.DataFrame) -> pl.DataFrame:
    """Augment DataFrame with computed columns. Mutate-free (returns new)."""
    return df.with_columns(_pl_features() + _swing_exprs())


def _resolve_scalar(df: pl.DataFrame, expr: pl.Expr) -> float:
    """Materialize a Polars expression to a scalar float (last non-null value)."""
    result = df.select(expr)
    col = result.get_column(result.columns[0])
    last_ = col.drop_nulls().last()
    return float(last_) if last_ is not None else 0.0


def atr(df: pl.DataFrame, period: int = 14) -> float:
    """ATR via polars_ta.ATR — materialized to scalar."""
    return _resolve_scalar(
        df,
        plta.ATR(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=period),
    )


def atr_pct(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """ATR as % of open — per-bar, resolved against DataFrame. Zero-open bars → 0."""
    expr = (pl.when(pl.col("open") > 0)
            .then(plta.ATR(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=period)
                  / pl.col("open") * 100)
            .otherwise(0.0).alias("_atr_pct"))
    return df.select(expr).get_column("_atr_pct")


def detect_impulse(
    df: pl.DataFrame, *, lookback: int = 30, direction: str | None = None,
) -> tuple[bool, int | None]:
    """Impulse = candle body ≥ 1.5× ATR% within lookback window.
    
    Scans the last ``lookback`` complete bars (not the current forming bar).
    If ``direction`` is ''up'', only green candles qualify; ''down'' → only red.
    Per-bar normalization: threshold = 1.5 × ATR(14) / open × 100.
    """
    if len(df) < 20:
        return False, None
    df_c = compute_features(df)
    atr_val = atr(df_c, 14)
    if atr_val <= 0:
        return False, None
    threshold = 1.5 * atr_val / df_c["open"] * 100.0
    body_ok = df_c["_body_pct"] >= threshold
    if direction == "up":
        dir_ok = df_c["close"] > df_c["open"]
    elif direction == "down":
        dir_ok = df_c["close"] < df_c["open"]
    else:
        dir_ok = pl.repeat(True, len(df_c))
    impulse = body_ok & dir_ok
    start = max(0, len(df_c) - lookback - 1)
    true_idx = impulse.arg_true()
    true_in_window = true_idx.filter(true_idx >= start)
    if len(true_in_window) > 0:
        return True, int(true_in_window[-1])
    return False, None


def detect_consecutive_impulse(
    df: pl.DataFrame, min_count: int = 3, *, direction: str | None = None,
) -> tuple[bool, int | None]:
    """≥min_count consecutive same-direction candles with body ≥ noise floor.

    The run is anchored to the most recent bar's direction. Pass ``direction``
    (''up'' → green, ''down'' → red) to require that anchor direction — without
    it the function is direction-agnostic, so a pure downtrend would satisfy an
    up-move check (and vice versa). Callers that seed a directional pattern must
    pass the matching direction.
    """
    if len(df) < min_count + 10:
        return False, None
    df_c = compute_features(df)
    avg_body = float(df_c["_body_pct"].tail(30).mean())
    if avg_body <= 0:
        return False, None
    above_noise = df_c["_body_pct"] >= avg_body * 0.8
    is_green = (df_c["close"] > df_c["open"]).cast(pl.Int8)
    n = min(len(df_c), 12)
    tail_noise = above_noise.tail(n)
    tail_dir = is_green.tail(n)
    noise_rev = tail_noise[::-1]
    dir_rev = tail_dir[::-1]
    anchor_dir = int(dir_rev[0])
    if direction == "up" and anchor_dir != 1:
        return False, None
    if direction == "down" and anchor_dir != 0:
        return False, None
    dir_match = dir_rev == dir_rev[0]
    valid_seq = noise_rev & dir_match
    breaks = (~valid_seq).arg_true()
    run_len = int(breaks[0]) if len(breaks) > 0 else n
    if run_len >= min_count:
        return True, len(df_c) - run_len
    return False, None


def detect_absorption(df: pl.DataFrame, impulse_idx: int, *, absorb_pct: float = 0.80) -> bool:
    """Price EVER retraced ≥ absorb_pct of the impulse range AFTER the impulse.

    Uses the EXTREME opposite price reached in the bars *after* impulse_idx (not
    latest close), because absorption is a past event — once detected, it stays
    detected even if price later reverses back toward the impulse extreme.

    The retrace window starts at impulse_idx + 1: the impulse bar's own base
    (its low for a green impulse) is not a retrace of itself. Including the
    impulse bar made the ratio saturate near 1.0 and the check a no-op — for a
    green impulse the bar's own low ≈ the pre-impulse price, so it always looked
    like a full retrace. Measuring only subsequent bars restores the gate.
    """
    if impulse_idx < 1 or impulse_idx >= len(df):
        return False
    pre = float(df["close"][impulse_idx - 1])
    imp_open = float(df["open"][impulse_idx])
    imp_close = float(df["close"][impulse_idx])
    is_green = imp_close > imp_open
    extreme = float(df["high"][impulse_idx]) if is_green else float(df["low"][impulse_idx])
    imp_range = abs(extreme - pre)
    if imp_range <= 0:
        return False
    post = df.slice(impulse_idx + 1)  # bars after the impulse only
    if post.is_empty():
        return False  # impulse is the latest bar — nothing has retraced yet
    if is_green:
        retrace_high = float(post["low"].min())  # opposite: price went down
    else:
        retrace_high = float(post["high"].max())
    return abs(retrace_high - extreme) >= imp_range * absorb_pct


def detect_one_candle_absorption(df: pl.DataFrame, impulse_range_pct: float) -> bool:
    """Single candle body ≥ 60 % of impulse range among last 4 bars."""
    if len(df) < 2:
        return False
    n = min(4, len(df) - 1)
    body_pct = (df["close"] - df["open"]).abs() / df["open"] * 100
    return (body_pct.tail(n) >= impulse_range_pct * 0.60).any()


def detect_bokovik(df: pl.DataFrame, *, window: int = 30, min_touches: int = 3, max_width_pct: float = 15.0, max_atr_ratio: float = 0.70, start_idx: int | None = None) -> dict[str, Any] | None:
    """Sideways range with ATR compression — all stats via Polars.

    If ``start_idx`` is set, the window starts from that index (post-event),
    rather than from the tail — avoids including the impulse/absorption candles
    that would inflate the range.

    Default thresholds calibrated against research/reports/detector_calibration.md:
    of the 18/107 real events with a sideways range in their pre-event window,
    observed width_pct median 4.6% (max 21.8%), touches median 3, atr_ratio
    median 0.61 — all comfortably inside the current 1-15% / ≥3 / ≤0.70 bands.
    """
    if len(df) < window * 2:
        return None
    if start_idx is not None:
        recent = df.slice(start_idx, window)
    else:
        recent = df.tail(window)
    if recent.is_empty() or recent.height < 3:
        return None
    lo = float(recent["low"].min())
    hi = float(recent["high"].max())
    mid = (lo + hi) / 2.0
    width_pct = (hi - lo) / mid * 100.0 if mid > 0 else 0.0
    if width_pct < 1.0 or width_pct > max_width_pct:
        return None
    touch_buf = width_pct * 0.05 / 100.0 * mid
    touches_lo = int(((recent["low"] - lo).abs() <= touch_buf).sum())
    touches_hi = int(((recent["high"] - hi).abs() <= touch_buf).sum())
    touches = touches_lo + touches_hi
    if touches < min_touches:
        return None
    if start_idx is not None:
        prior = df.slice(max(0, start_idx - window), window)
    else:
        prior = df.slice(len(df) - window * 2, window)
    current_atr = atr(recent, 14)
    prior_atr = atr(prior, 14)
    atr_ratio = current_atr / prior_atr if prior_atr > 0 else 1.0
    if atr_ratio > max_atr_ratio:
        return None
    return {
        "lo": lo, "hi": hi, "mid": mid, "width_pct": round(width_pct, 2),
        "touches": touches, "atr_ratio": round(atr_ratio, 3),
    }


def _sweep_check(df: pl.DataFrame, level: float, *, side: str, wick_ratio: float = 0.30) -> tuple[bool, float, float]:
    """Unified sweep check. side='low' → sweep below level; 'high' → sweep above."""
    if side == "low":
        candidates = df.filter(pl.col("low") < level)
    else:
        candidates = df.filter(pl.col("high") > level)
    if candidates.is_empty():
        return False, 0.0, 0.0
    candidates = candidates.with_columns([
        (pl.min_horizontal("close", "open") - pl.col("low")).alias("_wick"),
        (pl.col("high") - pl.col("low")).alias("_rng"),
    ]).filter(pl.col("_rng") > 0)
    if candidates.is_empty():
        return False, 0.0, 0.0
    if side == "low":
        mask = (pl.col("_wick") / pl.col("_rng") >= wick_ratio) & (pl.col("close") >= level)
    else:
        mask = ((pl.col("high") - pl.max_horizontal("close", "open")) / pl.col("_rng") >= wick_ratio) & (pl.col("close") <= level)
    match = candidates.filter(mask)
    if match.is_empty():
        return False, 0.0, 0.0
    last_ = match.tail(1)
    if last_.is_empty():
        return False, 0.0, 0.0
    val_col = "low" if side == "low" else "high"
    return True, float(last_[val_col].item()), float(last_["ts"].item())


def detect_sweep_low(df: pl.DataFrame, level: float, *, wick_ratio: float = 0.30) -> tuple[bool, float, float]:
    return _sweep_check(df, level, side="low", wick_ratio=wick_ratio)


def detect_sweep_high(df: pl.DataFrame, level: float, *, wick_ratio: float = 0.30) -> tuple[bool, float, float]:
    return _sweep_check(df, level, side="high", wick_ratio=wick_ratio)


def candle_fade_ratio(df: pl.DataFrame, n: int = 8, *, peak_high: float | None = None) -> tuple[float, float]:
    """Body / range fade ratio.
    
    If peak_high is given, finds the peak bar and uses its body vs prior avg
    (checks for single-candle exhaustion at the pump top, not the dump bars).
    """
    if peak_high is not None:
        peak_series: pl.Series = (df["high"] - peak_high).abs()
        peak_idx = int(peak_series.arg_min()) if len(peak_series) > 0 else -1
        if 2 <= peak_idx < len(df) - 1:
            df_c = compute_features(df)
            peak_body = float(df_c["_body_pct"][peak_idx])
            pre_avg = float(df_c["_body_pct"].slice(max(0, peak_idx - 4), 4).mean())
            body_r = peak_body / pre_avg if pre_avg > 0 else 1.0
            peak_range = float(df_c["_range"][peak_idx])
            pre_range_avg = float(df_c["_range"].slice(max(0, peak_idx - 4), 4).mean())
            range_r = peak_range / pre_range_avg if pre_range_avg > 0 else 1.0
            return min(body_r, 2.0), min(range_r, 2.0)
    if len(df) < n * 2 + 1:
        return 1.0, 1.0
    df_c = compute_features(df)
    avg_body_rec = float(df_c["_body_pct"].tail(n).mean())
    avg_body_pri = float(df_c["_body_pct"].slice(len(df_c) - n * 2, n).mean()) if len(df_c) >= n * 2 else 0.0
    avg_range_rec = float(df_c["_range"].tail(n).mean())
    avg_range_pri = float(df_c["_range"].slice(len(df_c) - n * 2, n).mean()) if len(df_c) >= n * 2 else 0.0
    body_ratio = avg_body_rec / avg_body_pri if avg_body_pri > 0 else 1.0
    range_ratio = avg_range_rec / avg_range_pri if avg_range_pri > 0 else 1.0
    return body_ratio, range_ratio


def _adaptive_buffer(df: pl.DataFrame, base: float = 0.003) -> float:
    """ATR-based buffer for BOS/choch: 10 % of ATR%, min ``base``, max 1.5%."""
    atr_val = atr(df, 14)
    if atr_val <= 0:
        return base
    last_close = float(df["close"][-1])
    if last_close <= 0:
        return base
    atr_pct_val = atr_val / last_close
    return min(max(base, atr_pct_val * 0.10), 0.015)


def bos_up(df: pl.DataFrame, *, buffer: float | None = None) -> bool:
    """Break of structure up — close above the most recent swing high."""
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_high"))["high"]
    if swing_vals.is_empty():
        return False
    buf = _adaptive_buffer(df_c) if buffer is None else buffer
    sh_val = float(swing_vals.tail(1)[0])
    return float(df_c["close"][-1]) > sh_val * (1.0 + buf) and float(df_c["close"][-2]) <= sh_val


def rejection_at_peak(df: pl.DataFrame, peak_high: float) -> bool:
    """Instant rejection at pump peak: 1-bar reversal (pump → dump in same bar).
    
    Criteria:
    1. Peak bar's close < open (red candle / rejection)
    2. Peak bar's close < previous bar's close
    3. Peak bar's range > 1.5x avg range of prior 3 bars
    """
    peak_series: pl.Series = (df["high"] - peak_high).abs()
    peak_idx = int(peak_series.arg_min())
    if peak_idx < 3 or peak_idx >= len(df):
        return False
    df_c = compute_features(df)
    c_peak = float(df_c["close"][peak_idx])
    o_peak = float(df_c["open"][peak_idx])
    if c_peak >= o_peak:
        return False  # not a rejection candle (close >= open)
    if peak_idx == 0:
        return False
    c_prev = float(df_c["close"][peak_idx - 1])
    if c_peak >= c_prev:
        return False  # close didn't drop below prior close
    rng_peak = float(df_c["_range"][peak_idx])
    rng_avg = float(df_c["_range"].slice(peak_idx - 3, 3).mean())
    return rng_peak > rng_avg * 1.5 if rng_avg > 0 else False


def bos_down(df: pl.DataFrame, *, buffer: float | None = None) -> bool:
    """Break of structure down — close below the most recent swing low, just now.

    Mirrors bos_up. The previous version checked whether the dataset's global
    minimum close ever violated ANY historical swing low — on volatile crypto
    data that's true almost by construction, which let Pattern B's "LTF
    confirmation" step pass on stale/irrelevant structure instead of an
    actual break happening now (backtested against 15 known manipulations:
    this let Pattern B fire on real long pumps before Pattern A matured).
    """
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_low"))["low"]
    if swing_vals.is_empty():
        return False
    buf = _adaptive_buffer(df_c) if buffer is None else buffer
    sl_val = float(swing_vals.tail(1)[0])
    return float(df_c["close"][-1]) < sl_val * (1.0 - buf) and float(df_c["close"][-2]) >= sl_val


def choch_bull(df: pl.DataFrame, *, buffer: float | None = None) -> bool:
    """Change of character bullish — close above the last lower high."""
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_high"))["high"]
    if len(swing_vals) < 2:
        return False
    buf = _adaptive_buffer(df_c) if buffer is None else buffer
    last_sh = float(swing_vals.tail(1)[0])
    prev_sh = float(swing_vals.tail(2)[0])
    if last_sh >= prev_sh:
        return False
    return float(df_c["close"][-1]) > last_sh * (1.0 + buf) and float(df_c["close"][-2]) <= last_sh


def choch_bear(df: pl.DataFrame, *, buffer: float | None = None) -> bool:
    """Change of character bearish — close below the last higher low, just now.

    Mirrors choch_bull. See bos_down for why "ever violated historically"
    (the previous implementation) is too permissive to serve as a gate.
    """
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_low"))["low"]
    if len(swing_vals) < 2:
        return False
    buf = _adaptive_buffer(df_c) if buffer is None else buffer
    last_sl = float(swing_vals.tail(1)[0])
    prev_sl = float(swing_vals.tail(2)[0])
    if last_sl <= prev_sl:
        return False  # not a higher low
    return float(df_c["close"][-1]) < last_sl * (1.0 - buf) and float(df_c["close"][-2]) >= last_sl


def break_above_level_recent(df: pl.DataFrame, level: float, *, window: int = 5) -> bool:
    """Close has been above level (with adaptive buffer) within the last window bars."""
    if len(df) < 2:
        return False
    df_c = compute_features(df)
    recent = df_c.tail(window)
    buf = _adaptive_buffer(df_c)
    return bool((recent["close"] > level * (1.0 + buf)).any())


def no_liquidity_above(df: pl.DataFrame, current_price: float, *, pump_high: float | None = None) -> bool:
    """No near-term swing highs above the pump peak (liquidity exhausted by the sweep).
    
    The pump blew through all intermediate levels; only check if there are
    swing highs still standing above the pump peak within the lookback window.
    """
    df_c = compute_features(df)
    peak = pump_high if pump_high is not None else current_price
    above = df_c.filter(df_c["_swing_high"] & (df_c["high"] > peak * 1.005))
    return above.height <= 1


def no_liquidity_below(df: pl.DataFrame, current_price: float, *, pump_low: float | None = None) -> bool:
    """No near-term swing lows below the pump trough (liquidity exhausted by the sweep)."""
    df_c = compute_features(df)
    trough = pump_low if pump_low is not None else current_price
    below = df_c.filter(df_c["_swing_low"] & (df_c["low"] < trough * 0.995))
    return below.height <= 1


def two_bar_reversal(df: pl.DataFrame, peak_high: float) -> bool:
    """2-bar reversal: massive green pump bar → immediate red confirmation bar.

    The transcript's pattern: pump sweeps liquidity (green, large body),
    next candle closes red with impulse down (body < -1% of open).
    One of three OR'd conditions for Pattern B step 4 — calibrated against
    107 real events in research/reports/detector_calibration.md: fires alone
    on ~10% of short events, ~47% combined with candle_fade/rejection_at_peak.

    Criteria:
    1. Peak bar (closest to peak_high) is GREEN (body > 0)
    2. Next bar is RED (body < 0) 
    3. Red bar body < -1% of its open (significant impulse)
    """
    peak_series: pl.Series = (df["high"] - peak_high).abs()
    peak_idx = int(peak_series.arg_min())
    if peak_idx < 1 or peak_idx >= len(df) - 1:
        return False
    peak_body = float(df["close"][peak_idx] - df["open"][peak_idx])
    if peak_body <= 0:
        return False  # peak bar must be green (pump)
    next_body = float(df["close"][peak_idx + 1] - df["open"][peak_idx + 1])
    if next_body >= 0:
        return False  # next bar must be red
    next_body_pct = next_body / float(df["open"][peak_idx + 1]) * 100 if float(df["open"][peak_idx + 1]) > 0 else 0
    return next_body_pct < -1.0


def bullish_volume(df: pl.DataFrame, lookback: int = 20, min_z: float = 0.5) -> bool:
    """An UP bar in the recent window shows volume >= min_z stdev above the mean.

    The "бычьи объёмы" gate described in the course: a закреп above the prior high
    is only valid when buyers back it, otherwise «цена может пойти дальше вниз».

    The spike must land on a bar that CLOSED UP. A direction-blind volume z-score
    is not a bullish-volume test: in a post-pump window the highest-volume bar is
    normally the red candle that absorbed the pump, so an unfiltered spike reports
    "bullish volume" for exactly the distribution it is meant to reject.

    Checks the WHOLE recent window (not just the last bar) because the relevant
    spike is on the impulse / sweep / breakout bar, which may be several bars
    before the current one by the time the pattern emits. Uses a z-score relative
    to recent history so it works across all coins and timeframes.
    """
    if len(df) < lookback + 2:
        return False
    recent = df.tail(lookback + 1)
    vol = recent["volume"]
    up = recent["close"] > recent["open"]
    window = vol[:-1]
    mean = float(window.mean())
    std = float(window.std())
    if std <= 0 or mean <= 0:
        spike = vol > mean * 1.5  # fallback: 50% above average
    else:
        spike = ((vol - mean) / std) >= min_z
    return bool((spike & up).any())


def post_peak_fade_ratio(df: pl.DataFrame, peak_high: float, n: int = 3) -> tuple[float, float]:
    """Candle fade AFTER the peak, not at the peak itself.
    
    Returns (avg_body_pct_of_post_peak_bars, avg_range_pct_of_post_peak_bars).
    Checks the N bars immediately after the peak bar, rather than comparing
    peak bar to prior bars (which fails for climax-style peaks).

    The transcript: "свечи начали затухать" — candles fade AFTER the pump peak.
    Not currently wired into patterns.py (candle_fade_ratio is used instead,
    calibrated in research/reports/detector_calibration.md); kept for future use.
    """
    peak_series: pl.Series = (df["high"] - peak_high).abs()
    peak_idx = int(peak_series.arg_min())
    if peak_idx < 1 or peak_idx + n >= len(df):
        return 999.0, 999.0
    post = df.slice(peak_idx + 1, n)
    if post.is_empty() or post.height < 1:
        return 999.0, 999.0
    avg_body_pct = float((post["close"] - post["open"]).abs().mean() / post["open"].mean() * 100)
    avg_range_pct = float((post["high"] - post["low"]).mean() / post["open"].mean() * 100)
    return avg_body_pct, avg_range_pct


def is_ascending_channel(df: pl.DataFrame, *, lookback: int = 30, min_swings: int = 2) -> bool:
    """Recent swings show higher highs AND higher lows (ascending channel).

    Used for Pattern C / Type 2 context: confirms an uptrend preceded
    the prior swing high, making a subsequent break more valid.
    """
    if df is None or df.height < lookback:
        return False
    feat = compute_features(df.tail(lookback))
    highs = feat.filter(pl.col("_swing_high"))["high"]
    lows = feat.filter(pl.col("_swing_low"))["low"]
    if len(highs) < min_swings or len(lows) < min_swings:
        return False
    sh = highs.tail(min_swings).to_list()
    sl = lows.tail(min_swings).to_list()
    return all(sh[i] > sh[i - 1] for i in range(1, len(sh))) and all(sl[i] > sl[i - 1] for i in range(1, len(sl)))


def is_descending_channel(df: pl.DataFrame, *, lookback: int = 30, min_swings: int = 2) -> bool:
    """Recent swings show lower highs AND lower lows (descending channel).

    Used for Pattern C / Type 2 context (post-peak pullback) and
    Pattern A3 / Type 3 (pre-accumulation downtrend).
    """
    if df is None or df.height < lookback:
        return False
    feat = compute_features(df.tail(lookback))
    highs = feat.filter(pl.col("_swing_high"))["high"]
    lows = feat.filter(pl.col("_swing_low"))["low"]
    if len(highs) < min_swings or len(lows) < min_swings:
        return False
    sh = highs.tail(min_swings).to_list()
    sl = lows.tail(min_swings).to_list()
    return all(sh[i] < sh[i - 1] for i in range(1, len(sh))) and all(sl[i] < sl[i - 1] for i in range(1, len(sl)))

