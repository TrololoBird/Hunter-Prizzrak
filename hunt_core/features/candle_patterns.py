"""Candlestick pattern columns via polars_ta.candles + Polars engulfing/star logic."""
from __future__ import annotations



import polars as pl
import polars_ta.candles as ptc

_CANDLE_OUTPUT_COLUMNS: tuple[str, ...] = (
    "candle_doji",
    "candle_dragonfly",
    "candle_gravestone",
    "candle_hammer",
    "candle_shooting_star",
    "candle_bullish_engulfing",
    "candle_bearish_engulfing",
    "candle_morning_star",
    "candle_evening_star",
)

_CANDLE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "candle_hammer",
    "candle_shooting_star",
    "candle_bullish_engulfing",
    "candle_bearish_engulfing",
    "candle_morning_star",
    "candle_evening_star",
)


def _candle_exprs(open_: pl.Expr, high: pl.Expr, low: pl.Expr, close: pl.Expr) -> list[pl.Expr]:
    prev_open = open_.shift(1)
    prev_close = close.shift(1)
    prev_body_low = pl.min_horizontal(prev_open, prev_close)
    prev_body_high = pl.max_horizontal(prev_open, prev_close)
    curr_body_low = pl.min_horizontal(open_, close)
    curr_body_high = pl.max_horizontal(open_, close)

    prev2_open = open_.shift(2)
    prev2_close = close.shift(2)
    prev2_body = (prev2_close - prev2_open).abs()
    prev_body = (prev_close - prev_open).abs()
    (close - open_).abs()
    mid_high = pl.max_horizontal(prev_open, prev_close)
    mid_low = pl.min_horizontal(prev_open, prev_close)

    doji = ptc.doji(open_, high, low, close).cast(pl.Float64)
    dragonfly = ptc.dragonfly(open_, high, low, close).cast(pl.Float64)
    gravestone = ptc.gravestone(open_, high, low, close).cast(pl.Float64)
    hammer = (dragonfly * (close >= open_).cast(pl.Float64)).alias("candle_hammer")
    shooting_star = (gravestone * (close <= open_).cast(pl.Float64)).alias("candle_shooting_star")

    bullish_engulf = (
        (prev_close < prev_open)
        & (close > open_)
        & (curr_body_low <= prev_body_low)
        & (curr_body_high >= prev_body_high)
    ).cast(pl.Float64)
    bearish_engulf = (
        (prev_close > prev_open)
        & (close < open_)
        & (curr_body_low <= prev_body_low)
        & (curr_body_high >= prev_body_high)
    ).cast(pl.Float64)
    morning_star = (
        (prev2_close < prev2_open)
        & (prev_body <= prev2_body * 0.45)
        & (close > open_)
        & (close >= mid_high)
    ).cast(pl.Float64)
    evening_star = (
        (prev2_close > prev2_open)
        & (prev_body <= prev2_body * 0.45)
        & (close < open_)
        & (close <= mid_low)
    ).cast(pl.Float64)

    return [
        doji.alias("candle_doji"),
        dragonfly.alias("candle_dragonfly"),
        gravestone.alias("candle_gravestone"),
        hammer,
        shooting_star,
        bullish_engulf.alias("candle_bullish_engulfing"),
        bearish_engulf.alias("candle_bearish_engulfing"),
        morning_star.alias("candle_morning_star"),
        evening_star.alias("candle_evening_star"),
    ]


def add_candle_pattern_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Add shared candle pattern flags used by SMC / liquidity strategies."""
    if df.is_empty() or not {"open", "high", "low", "close"}.issubset(df.columns):
        return df.with_columns([pl.lit(0.0).alias(name) for name in _CANDLE_OUTPUT_COLUMNS])

    open_ = pl.col("open")
    high = pl.col("high")
    low = pl.col("low")
    close = pl.col("close")
    return df.with_columns(_candle_exprs(open_, high, low, close))


def candle_snapshot_from_row(row: dict[str, object]) -> dict[str, float]:
    """Extract candle pattern flags for delivery snapshots."""
    out: dict[str, float] = {}
    for key in _CANDLE_SNAPSHOT_KEYS:
        raw = row.get(key)
        try:
            val = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            val = 0.0
        out[key] = 1.0 if val > 0.5 else 0.0
    return out


def candle_pattern_snapshot(df: pl.DataFrame, *, idx: int = -1) -> dict[str, float]:
    """Candle pattern flags at ``idx`` (default last bar)."""
    if df is None or df.is_empty():
        return {key: 0.0 for key in _CANDLE_SNAPSHOT_KEYS}
    work = (
        df
        if all(name in df.columns for name in _CANDLE_OUTPUT_COLUMNS)
        else add_candle_pattern_columns(df)
    )
    bar_idx = work.height + idx if idx < 0 else idx
    if bar_idx < 0 or bar_idx >= work.height:
        return {key: 0.0 for key in _CANDLE_SNAPSHOT_KEYS}
    row = {key: work[key][bar_idx] for key in _CANDLE_SNAPSHOT_KEYS if key in work.columns}
    return candle_snapshot_from_row(row)


__all__ = [
    "add_candle_pattern_columns",
    "candle_pattern_snapshot",
    "candle_snapshot_from_row",
]
