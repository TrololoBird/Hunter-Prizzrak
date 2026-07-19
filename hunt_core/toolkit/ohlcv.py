"""Pure OHLCV transforms — ccxt rows → Polars frames, resample, closed-bar guards.

Extracted verbatim from ``hunt_core.market.factory`` (S0 of the native-module
rewrite, ADR-0004). Pure functions over ccxt OHLCV rows / Polars frames; the
``exchange`` argument is used only for ccxt's ``parse_timeframe`` interval math.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import polars as pl

from hunt_core import clock

__all__ = [
    "interval_to_seconds",
    "ccxt_ohlcv_to_frame",
    "drop_unclosed_ohlcv_tail",
    "min_1m_bars_for_resample",
    "resample_ohlcv_from_1m",
    "finalize_kline_frame",
]


_KLINE_FRAME_SCHEMA: dict[str, Any] = {
    "time": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "close_time": pl.Datetime("us", "UTC"),
    "quote_volume": pl.Float64,
    "num_trades": pl.Int64,
    "taker_buy_base_volume": pl.Float64,
    "taker_buy_quote_volume": pl.Float64,
    "open_time": pl.Datetime("us", "UTC"),
}


def interval_to_seconds(interval: str, exchange: Any) -> int:
    if exchange is None:
        raise TypeError("interval_to_seconds requires a CCXT exchange instance")
    parse_tf = getattr(exchange, "parse_timeframe", None)
    if not callable(parse_tf):
        raise TypeError(f"{getattr(exchange, 'id', 'exchange')}: parse_timeframe is not available")
    return int(parse_tf(interval))


def _close_time_ms(open_ms: int, interval: str, exchange: Any) -> int:
    step = interval_to_seconds(interval, exchange) * 1000
    return open_ms + step - 1


def ccxt_ohlcv_to_frame(
    rows: list[list[Any]],
    interval: str,
    *,
    exchange: Any,
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    built: list[dict[str, Any]] = []
    for row in rows:
        if not row or len(row) < 6:
            continue
        try:
            open_ms = int(row[0])
            o, h, low, c, v = (
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        except (TypeError, ValueError, IndexError):
            continue
        if open_ms <= 0 or c <= 0:
            continue
        # Full-fidelity rows (raw fapi klines / extended WS capture) carry
        # [6]=closeTime [7]=quoteVolume [8]=numTrades [9]=takerBuyBase
        # [10]=takerBuyQuote. ccxt's standard 6-element OHLCV drops them, which
        # zeroed taker_buy_base_volume and silently degenerated the orderflow
        # delta features (delta_ratio→0, bar delta→−volume). Zero-fill remains
        # only for genuinely 6-element rows.
        close_ms = _close_time_ms(open_ms, interval, exchange)
        quote_v = 0.0
        trades = 0
        taker_base = 0.0
        taker_quote = 0.0
        if len(row) >= 11:
            try:
                raw_close = int(row[6])
                if raw_close > open_ms:
                    close_ms = raw_close
                quote_v = float(row[7] or 0.0)
                trades = int(row[8] or 0)
                taker_base = float(row[9] or 0.0)
                taker_quote = float(row[10] or 0.0)
            except (TypeError, ValueError, IndexError):
                quote_v, trades, taker_base, taker_quote = 0.0, 0, 0.0, 0.0
        built.append(
            {
                "time": open_ms,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
                "close_time": close_ms,
                "quote_volume": quote_v,
                "num_trades": trades,
                "taker_buy_base_volume": taker_base,
                "taker_buy_quote_volume": taker_quote,
            }
        )
    if not built:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    frame = pl.DataFrame(built)
    return frame.with_columns(
        pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC").alias("time"),
        pl.from_epoch(pl.col("close_time"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("close_time"),
        pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC").alias("open_time"),
    )


def _ohlcv_frame_has_incomplete_tail(
    df: pl.DataFrame,
    timeframe: str,
    *,
    exchange: Any,
) -> bool:
    if df.is_empty():
        return False
    if "close_time" in df.columns:
        last_close = df["close_time"].tail(1).item()
        if isinstance(last_close, datetime):
            return clock.now_utc() <= last_close
    timeframe_seconds = interval_to_seconds(timeframe, exchange)
    last_open = df["time"].tail(1).item()
    if not isinstance(last_open, datetime):
        return False
    return clock.now_utc() < last_open + timedelta(seconds=timeframe_seconds)


def _drop_incomplete_ohlcv_tail(
    df: pl.DataFrame,
    timeframe: str,
    *,
    exchange: Any,
) -> pl.DataFrame:
    if df.is_empty():
        return df
    if "close_time" in df.columns:
        now = clock.now_utc()
        closed = df.filter(pl.col("close_time") < pl.lit(now))
        if closed.height != df.height:
            return closed
    if _ohlcv_frame_has_incomplete_tail(df, timeframe, exchange=exchange):
        return df.head(df.height - 1)
    return df


def drop_unclosed_ohlcv_tail(
    rows: list[list[Any]],
    timeframe: str,
    *,
    exchange: Any,
    now_ms: int | None = None,
) -> list[list[Any]]:
    """Drop the still-forming last kline from a raw list-path OHLCV window.

    The list path (``fetch_ohlcv_list`` / ``fetch_ohlcv_list_cached``) bypasses
    ``finalize_kline_frame``'s incomplete-tail drop, so Binance's in-progress
    candle reaches consumers and repaints (closed-bar discipline violation —
    a mid-bar close beyond a level would count as a confirmed break, then the
    bar can close back). Every detector-facing list consumer must pass its
    window through this before analysis.

    ``rows`` are ascending ``[open_ms, o, h, l, c, v, ...]``; a bar is closed
    once ``open_ms + timeframe_duration <= now``.
    """
    if not rows:
        return rows
    step_ms = interval_to_seconds(timeframe, exchange) * 1000
    now = int(clock.now_utc().timestamp() * 1000) if now_ms is None else int(now_ms)
    if int(rows[-1][0]) + step_ms > now:
        return rows[:-1]
    return rows


_RESAMPLE_FROM_1M_INTERVALS = frozenset({"5m", "15m", "1h", "4h", "1d"})


def min_1m_bars_for_resample(interval: str, target_limit: int, *, exchange: Any) -> int:
    """How many 1m bars are needed to derive ``target_limit`` bars at ``interval``."""
    if interval == "1m":
        return max(1, int(target_limit))
    step_s = interval_to_seconds(interval, exchange)
    bars_per_bucket = max(1, step_s // 60)
    return min(1500, int(target_limit) * bars_per_bucket + bars_per_bucket)


def resample_ohlcv_from_1m(
    df_1m: pl.DataFrame,
    interval: str,
    *,
    exchange: Any,
    limit: int | None = None,
) -> pl.DataFrame:
    """U3: derive higher TF OHLCV from 1m via Polars ``group_by_dynamic`` (MTF-consistent)."""
    if df_1m.is_empty() or interval == "1m" or interval not in _RESAMPLE_FROM_1M_INTERVALS:
        return df_1m
    work = df_1m
    if "open_time" not in work.columns and "time" in work.columns:
        work = work.with_columns(pl.col("time").alias("open_time"))
    if "open_time" not in work.columns:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    if "close_time" in work.columns:
        now = clock.now_utc()
        work = work.filter(pl.col("close_time") < pl.lit(now))
    if work.height < 2:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    step_s = interval_to_seconds(interval, exchange)
    every = f"{step_s}s"
    agg_exprs: list[pl.Expr] = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    if "quote_volume" in work.columns:
        agg_exprs.append(pl.col("quote_volume").sum().alias("quote_volume"))
    else:
        agg_exprs.append(pl.lit(0.0).alias("quote_volume"))
    if "num_trades" in work.columns:
        agg_exprs.append(pl.col("num_trades").sum().alias("num_trades"))
    else:
        agg_exprs.append(pl.lit(0).alias("num_trades"))
    if "taker_buy_base_volume" in work.columns:
        agg_exprs.append(pl.col("taker_buy_base_volume").sum().alias("taker_buy_base_volume"))
    else:
        agg_exprs.append(pl.lit(0.0).alias("taker_buy_base_volume"))
    if "taker_buy_quote_volume" in work.columns:
        agg_exprs.append(pl.col("taker_buy_quote_volume").sum().alias("taker_buy_quote_volume"))
    else:
        agg_exprs.append(pl.lit(0.0).alias("taker_buy_quote_volume"))
    resampled = (
        work.sort("open_time")
        .group_by_dynamic("open_time", every=every, closed="left")
        .agg(agg_exprs)
    )
    if resampled.is_empty():
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    resampled = resampled.with_columns(
        pl.col("open_time").dt.epoch(time_unit="ms").alias("time"),
        (
            pl.col("open_time").dt.epoch(time_unit="ms")
            + pl.lit(step_s * 1000 - 1)
        ).alias("close_time"),
    )
    if limit is not None and resampled.height > int(limit):
        resampled = resampled.tail(int(limit))
    return finalize_kline_frame(resampled, interval, exchange=exchange)


def finalize_kline_frame(frame: pl.DataFrame, interval: str, *, exchange: Any) -> pl.DataFrame:
    return _drop_incomplete_ohlcv_tail(frame, interval, exchange=exchange)
