"""Per-frame OHLCV indicator pipeline (_prepare_frame)."""
from __future__ import annotations



from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import polars as pl
import polars_ols
import polars_ols.least_squares as polars_ols_ls
import structlog

from .candle_patterns import add_candle_pattern_columns
from .microstructure import add_microstructure_features
from .polars_ta_bridge import (
    adx_from_polars_ta,
    aroon_series,
    atr_series,
    bbands_series,
    cci_from_polars_ta,
    ema_series,
    macd_series,
    mfi_from_polars_ta,
    obv_series,
    polars_ta_extended_exprs,
    polars_wq_exprs,
    roc_series,
    rsi_series,
    stochastic_series,
    willr_from_polars_ta,
)
from .prepare_columns import group_active, hull_moving_average, ichimoku_lines
from .shared import supertrend_series, wilder_mean

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..domain.schemas import SymbolFrames

LOG = structlog.get_logger("hunt_core.features.prepare_frame")

_FrameCacheValue = float | None
_FRAME_CACHE_TAIL_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "num_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)
REQUIRED_COLS = {"open", "high", "low", "close", "volume"}


def _clean_non_finite(series: pl.Series, *, fill: float) -> pl.Series:
    """Replace NaN/inf/null values with a stable fill value."""
    return series.replace([float("inf"), float("-inf")], None).fill_nan(fill).fill_null(fill)


def _timestamp_ns(value: object) -> int:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1e9)
    return int(cast("Any", value))


def _tail_value_signature(row: dict[str, object]) -> tuple[_FrameCacheValue, ...]:
    values: list[_FrameCacheValue] = []
    for column in _FRAME_CACHE_TAIL_COLUMNS:
        raw = row.get(column)
        if raw is None:
            values.append(None)
            continue
        try:
            value = float(cast("Any", raw))
        except (TypeError, ValueError):
            values.append(None)
            continue
        values.append(None if value != value else value)
    return tuple(values)


def _materialize_series(
    value: pl.Series | pl.Expr | float,
    *,
    df: pl.DataFrame,
    name: str,
) -> pl.Series:
    if isinstance(value, pl.Series):
        return value.rename(name)
    if isinstance(value, pl.Expr):
        return df.select(value.alias(name)).to_series()
    return pl.Series(name, [value] * df.height, dtype=pl.Float64)


def _numeric_item(df: pl.DataFrame, row: int, column: str, default: float = 0.0) -> float:
    try:
        value = df.item(row, column)
    except (IndexError, ValueError):
        return default
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _as_float_like(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_optional_float(value: object) -> float | None:
    try:
        numeric = float(cast("Any", value)) if value is not None else None
    except (TypeError, ValueError):
        return None
    if numeric is None or not np.isfinite(numeric):
        return None
    return numeric


def _finite_float(value: object, default: float = 0.0) -> float:
    numeric = _as_optional_float(value)
    return default if numeric is None else numeric


def min_required_bars(
    *,
    min_bars_15m: int = 500,
    min_bars_1h: int = 300,
    min_bars_5m: int = 200,
    min_bars_4h: int = 250,
) -> dict[str, int]:
    return {
        "15m": int(min_bars_15m),
        "1h": int(min_bars_1h),
        "5m": int(min_bars_5m),
        "4h": int(min_bars_4h),
    }


def has_minimum_bars(
    frames: SymbolFrames,
    *,
    minimums: dict[str, int],
    required_timeframes: Iterable[str] | None = None,
) -> bool:
    required = set(required_timeframes) if required_timeframes is not None else set(minimums)
    frame_by_timeframe = {
        "5m": frames.df_5m,
        "15m": frames.df_15m,
        "1h": frames.df_1h,
        "4h": frames.df_4h,
    }
    for timeframe, frame in frame_by_timeframe.items():
        if timeframe not in required:
            continue
        required_bars = int(minimums.get(timeframe, 0) or 0)
        if required_bars <= 0:
            continue
        available = 0 if frame is None else frame.height
        if available < required_bars:
            return False
    return True


def _vwap_session_key(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return None


def _is_temporal_dtype(dtype: pl.DataType | None) -> bool:
    return bool(dtype is not None and getattr(dtype, "is_temporal", lambda: False)())


def _infer_epoch_time_unit(values: pl.Series) -> str | None:
    values = values.drop_nulls()
    if values.is_empty():
        return None
    try:
        max_abs = float(values.abs().max())
    except (TypeError, ValueError):
        return None
    if max_abs >= 1_000_000_000_000_000_000:
        return "ns"
    if max_abs >= 1_000_000_000_000_000:
        return "us"
    if max_abs >= 100_000_000_000:
        return "ms"
    return "s"


def _coerce_temporal_columns(df: pl.DataFrame) -> pl.DataFrame:
    conversions: list[pl.Expr] = []
    for column in ("time", "open_time", "close_time"):
        dtype = df.schema.get(column)
        if dtype is None or _is_temporal_dtype(dtype):
            continue
        if (
            getattr(dtype, "is_integer", lambda: False)()
            or getattr(dtype, "is_float", lambda: False)()
        ):
            unit = _infer_epoch_time_unit(df[column])
            if unit is not None:
                conversions.append(
                    pl.from_epoch(pl.col(column).cast(pl.Int64), time_unit=unit)
                    .dt.replace_time_zone("UTC")
                    .alias(column)
                )
        elif dtype == pl.String:
            conversions.append(
                pl.col(column).str.to_datetime(strict=False, time_zone="UTC").alias(column)
            )
    if not conversions:
        return df
    return df.with_columns(conversions)


def _session_time_column(df: pl.DataFrame) -> str | None:
    return next(
        (
            column
            for column in ("close_time", "time", "open_time")
            if column in df.columns and _is_temporal_dtype(df.schema.get(column))
        ),
        None,
    )


def _bar_delta_expr(df: pl.DataFrame) -> pl.Expr | None:
    if {"taker_buy_base_volume", "volume"}.issubset(df.columns):
        return 2.0 * pl.col("taker_buy_base_volume") - pl.col("volume")
    if {"delta_ratio", "volume"}.issubset(df.columns):
        return (pl.col("delta_ratio") - 0.5) * 2.0 * pl.col("volume")
    return None


def add_rolling_cvd_24h(df: pl.DataFrame) -> pl.DataFrame:
    """Rolling 24h cumulative volume delta (answers50b Q8 — algo intraday path)."""
    if df.is_empty():
        return df.with_columns(pl.lit(0.0).alias("rolling_cvd_24h"))
    bar_delta = _bar_delta_expr(df)
    if bar_delta is None:
        return df.with_columns(pl.lit(0.0).alias("rolling_cvd_24h"))
    filled_delta = bar_delta.fill_null(0.0).fill_nan(0.0)
    time_column = _session_time_column(df)
    if time_column is not None:
        temp = df.sort(time_column).with_columns(filled_delta.alias("_cvd_bar_delta"))
        return temp.with_columns(
            pl.col("_cvd_bar_delta")
            .rolling_sum_by(time_column, window_size="24h")
            .alias("rolling_cvd_24h")
        ).drop("_cvd_bar_delta")
    return df.with_columns(
        filled_delta.rolling_sum(window_size=96, min_periods=1).alias("rolling_cvd_24h")
    )


def add_session_cvd(df: pl.DataFrame) -> pl.DataFrame:
    """Cumulative volume delta reset at each UTC calendar date (session CVD)."""
    if df.is_empty():
        return df
    bar_delta = _bar_delta_expr(df)
    if bar_delta is None:
        return df.with_columns(pl.lit(0.0).alias("session_cvd"))

    filled_delta = bar_delta.fill_null(0.0).fill_nan(0.0)
    time_column = _session_time_column(df)
    if time_column is not None:
        temp = df.with_columns(
            [
                filled_delta.alias("_cvd_bar_delta"),
                pl.col(time_column).dt.date().alias("_cvd_session"),
            ]
        )
        return temp.with_columns(
            pl.col("_cvd_bar_delta").cum_sum().over("_cvd_session").alias("session_cvd")
        ).drop("_cvd_bar_delta", "_cvd_session")

    return df.with_columns(filled_delta.cum_sum().alias("session_cvd"))


def _cmf(df: pl.DataFrame, period: int = 20) -> pl.Series:
    """Chaikin Money Flow — canonical hot-path impl (plta CMF not used on prepare path)."""
    width = df["high"] - df["low"]
    mfm = (
        pl.when(width > 0.0)
        .then(((df["close"] - df["low"]) - (df["high"] - df["close"])) / width)
        .otherwise(0.0)
    )

    money_flow_volume = mfm * df["volume"]
    volume_sum = df["volume"].rolling_sum(window_size=period)

    return _materialize_series(
        (money_flow_volume.rolling_sum(window_size=period) / volume_sum).fill_nan(0.0),
        df=df,
        name=f"cmf{period}",
    )


def _ultimate_oscillator(df: pl.DataFrame, p1: int = 7, p2: int = 14, p3: int = 28) -> pl.Series:
    prev_close = df["close"].shift(1)
    min_low = _materialize_series(
        pl.min_horizontal(df["low"], prev_close), df=df, name="uo_min_low"
    )
    max_high = _materialize_series(
        pl.max_horizontal(df["high"], prev_close), df=df, name="uo_max_high"
    )
    bp = (df["close"] - min_low).rename("uo_bp")
    tr = (max_high - min_low).rename("uo_tr")
    avg1 = bp.rolling_sum(window_size=p1) / tr.rolling_sum(window_size=p1)
    avg2 = bp.rolling_sum(window_size=p2) / tr.rolling_sum(window_size=p2)
    avg3 = bp.rolling_sum(window_size=p3) / tr.rolling_sum(window_size=p3)
    uo = (100.0 * ((4.0 * avg1) + (2.0 * avg2) + avg3) / 7.0).rename("uo")
    return _clean_non_finite(uo, fill=50.0)


def _realized_volatility(df: pl.DataFrame, period: int = 20) -> pl.Series:
    log_returns = df["close"].log() - df["close"].shift(1).log()
    return _materialize_series(
        (log_returns.rolling_std(window_size=period) * float(np.sqrt(period)) * 100.0).fill_nan(
            0.0
        ),
        df=df,
        name=f"realized_vol_{period}",
    )


def _add_session_features(work: pl.DataFrame, period: int = 20) -> pl.DataFrame:
    if "close_time" not in work.columns or not _is_temporal_dtype(work.schema.get("close_time")):
        return work.with_columns(
            [
                pl.lit(0.0).alias("session_asia"),
                pl.lit(0.0).alias("session_london"),
                pl.lit(0.0).alias("session_ny"),
                pl.lit(0.0).alias("session_overlap"),
                pl.lit(0.0).alias("session_asia_vol_20"),
                pl.lit(0.0).alias("session_london_vol_20"),
                pl.lit(0.0).alias("session_ny_vol_20"),
                pl.lit(0.0).alias("session_overlap_vol_20"),
            ]
        )

    hour = pl.col("close_time").dt.hour()
    log_return = pl.col("close").log() - pl.col("close").shift(1).log()
    work = work.with_columns(
        [
            hour.is_between(0, 8, closed="left").cast(pl.Float64).alias("session_asia"),
            hour.is_between(7, 16, closed="left").cast(pl.Float64).alias("session_london"),
            hour.is_between(13, 22, closed="left").cast(pl.Float64).alias("session_ny"),
            hour.is_between(13, 16, closed="left").cast(pl.Float64).alias("session_overlap"),
        ]
    )
    scale = float(np.sqrt(period) * 100.0)
    return work.with_columns(
        [
            (
                pl.when(pl.col("session_asia") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_asia_vol_20"),
            (
                pl.when(pl.col("session_london") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_london_vol_20"),
            (
                pl.when(pl.col("session_ny") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_ny_vol_20"),
            (
                pl.when(pl.col("session_overlap") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_overlap_vol_20"),
        ]
    )


# ---------------------------------------------------------------------------
# Advanced indicators via pure Polars implementations.
# ---------------------------------------------------------------------------


def _keltner_channels(
    df: pl.DataFrame,
    period: int = 20,
    multiplier: float = 2.0,
    atr_period: int = 10,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Keltner Channels - pure Polars implementation using ATR.

    Returns (upper, middle, lower) channels.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    middle = typical_price.ewm_mean(span=period, adjust=False).rename("kc_middle")

    # ATR for channel width
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pl.max_horizontal(
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    )
    tr_series = _materialize_series(tr, df=df, name="true_range")
    atr = _materialize_series(
        wilder_mean(tr_series, period=atr_period, name="kc_atr"),
        df=df,
        name="kc_atr",
    )

    upper = middle + multiplier * atr
    lower = middle - multiplier * atr

    return upper, middle, lower


def _parabolic_sar(
    df: pl.DataFrame,
    *,
    step: float = 0.02,
    max_step: float = 0.2,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    high_vals = [_finite_float(v) for v in df["high"]]
    low_vals = [_finite_float(v) for v in df["low"]]
    close_vals = [_finite_float(v) for v in df["close"]]
    size = len(close_vals)
    if size == 0:
        empty = pl.Series("psar_long", [], dtype=pl.Float64)
        return (
            empty,
            pl.Series("psar_short", [], dtype=pl.Float64),
            pl.Series("psar_reversal", [], dtype=pl.Float64),
        )

    long_psar: list[float | None] = [None] * size
    short_psar: list[float | None] = [None] * size
    reversals: list[float] = [0.0] * size

    is_long = True if size < 2 else close_vals[1] >= close_vals[0]
    af = step
    ep = high_vals[0] if is_long else low_vals[0]
    psar = low_vals[0] if is_long else high_vals[0]

    for i in range(size):
        if i == 0:
            if is_long:
                long_psar[i] = psar
            else:
                short_psar[i] = psar
            continue
        prev_psar = psar
        psar = prev_psar + af * (ep - prev_psar)
        if is_long:
            psar = min(psar, low_vals[i - 1], low_vals[i - 2] if i > 1 else low_vals[i - 1])
            if low_vals[i] < psar:
                is_long = False
                reversals[i] = -1.0
                psar = ep
                ep = low_vals[i]
                af = step
                short_psar[i] = psar
                continue
            if high_vals[i] > ep:
                ep = high_vals[i]
                af = min(af + step, max_step)
            long_psar[i] = psar
        else:
            psar = max(psar, high_vals[i - 1], high_vals[i - 2] if i > 1 else high_vals[i - 1])
            if high_vals[i] > psar:
                is_long = True
                reversals[i] = 1.0
                psar = ep
                ep = high_vals[i]
                af = step
                long_psar[i] = psar
                continue
            if low_vals[i] < ep:
                ep = low_vals[i]
                af = min(af + step, max_step)
            short_psar[i] = psar

    # Inactive side = 0.0 (never null) so live/scoring paths never see missing PSAR.
    long_filled = [0.0 if v is None else float(v) for v in long_psar]
    short_filled = [0.0 if v is None else float(v) for v in short_psar]
    return (
        pl.Series("psar_long", long_filled, dtype=pl.Float64),
        pl.Series("psar_short", short_filled, dtype=pl.Float64),
        pl.Series("psar_reversal", reversals, dtype=pl.Float64),
    )


def _fisher_transform(df: pl.DataFrame, period: int = 10) -> tuple[pl.Series, pl.Series]:
    hh = df["high"].rolling_max(window_size=period)
    ll = df["low"].rolling_min(window_size=period)
    width = (hh - ll).clip(lower_bound=1e-9)
    price_norm = ((df["close"] - ll) / width).fill_nan(0.5).fill_null(0.5)
    raw_arr = (price_norm * 2.0 - 1.0).clip(-0.999, 0.999).to_numpy()
    size = raw_arr.shape[0]
    values = np.zeros(size, dtype=np.float64)
    fisher = np.zeros(size, dtype=np.float64)
    for i in range(size):
        prev_v = values[i - 1] if i > 0 else 0.0
        smoothed = 0.33 * float(raw_arr[i]) + 0.67 * prev_v
        smoothed = float(np.clip(smoothed, -0.999, 0.999))
        values[i] = smoothed
        prev_f = fisher[i - 1] if i > 0 else 0.0
        fisher[i] = 0.5 * np.log((1.0 + smoothed) / (1.0 - smoothed)) + 0.5 * prev_f
    fisher_series = pl.Series("fisher", fisher, dtype=pl.Float64)
    fisher_signal = fisher_series.ewm_mean(span=5, adjust=False).rename("fisher_signal")
    return fisher_series, fisher_signal


def _squeeze_momentum(
    df: pl.DataFrame, period: int = 20
) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series]:
    bb_upper, bb_mid, bb_lower = bbands_series(df, period=period, nbdev=2.0)
    kc_upper, _, kc_lower = _keltner_channels(df, period=period, multiplier=1.5)
    squeeze_on = (
        ((bb_lower > kc_lower) & (bb_upper < kc_upper)).cast(pl.Float64).rename("squeeze_on")
    )
    squeeze_off = (
        ((bb_lower < kc_lower) & (bb_upper > kc_upper)).cast(pl.Float64).rename("squeeze_off")
    )
    squeeze_no = pl.DataFrame({"on": squeeze_on, "off": squeeze_off}).select(
        (pl.lit(1.0) - pl.max_horizontal("on", "off")).clip(0.0, 1.0).alias("squeeze_no")
    )["squeeze_no"]
    basis = (
        (df["high"].rolling_max(window_size=period) + df["low"].rolling_min(window_size=period))
        / 2.0
        + bb_mid
    ) / 2.0
    hist = _clean_non_finite((df["close"] - basis).ewm_mean(span=5, adjust=False), fill=0.0).rename(
        "squeeze_hist"
    )
    return hist, squeeze_on, squeeze_off, squeeze_no


def _chandelier_exit(
    df: pl.DataFrame, period: int = 22, atr_mult: float = 3.0
) -> tuple[pl.Series, pl.Series, pl.Series]:
    atr = atr_series(df, period)
    long_exit = (df["high"].rolling_max(window_size=period) - atr * atr_mult).rename(
        "chandelier_long"
    )
    short_exit = (df["low"].rolling_min(window_size=period) + atr * atr_mult).rename(
        "chandelier_short"
    )
    # Vectorized direction: stay in current trend until opposite stop is hit.
    signals = (
        pl.when(df["close"] > short_exit)
        .then(1.0)
        .when(df["close"] < long_exit)
        .then(-1.0)
        .otherwise(None)
    )
    direction = signals.forward_fill().fill_null(0.0)

    return (
        long_exit,
        short_exit,
        _materialize_series(direction, df=df, name="chandelier_dir"),
    )


def _stochastic_rsi(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Stochastic RSI = (RSI - min(RSI, N)) / (max(RSI, N) - min(RSI, N))."""
    rsi = rsi_series(df, period=period)
    min_rsi = rsi.rolling_min(window_size=period)
    max_rsi = rsi.rolling_max(window_size=period)
    denom = max_rsi - min_rsi
    stoch = (rsi - min_rsi) / denom.replace(0.0, float("nan"))
    return stoch.fill_nan(0.5).fill_null(0.5).alias("stoch_rsi14")


def _ichimoku_cloud(df: pl.DataFrame) -> pl.DataFrame:
    """Compute Ichimoku Cloud components (TradingView-style displacement)."""
    tenkan, kijun, senkou_a, senkou_b = ichimoku_lines(df)
    chikou = df["close"].alias("chikou")
    return df.select(
        tenkan.alias("tenkan"),
        kijun.alias("kijun"),
        senkou_a.alias("senkou_a"),
        senkou_b.alias("senkou_b"),
        chikou.alias("chikou"),
    )


def _kama(df: pl.DataFrame, period: int = 10, fast: int = 2, slow: int = 30) -> pl.Series:
    """Kaufman Adaptive Moving Average — canonical (plta.KAMA broken on Py3.14)."""
    close = df["close"]
    change = close.diff(period)
    volatility = close.diff().abs().rolling_sum(window_size=period)
    er = (change.abs() / volatility.replace(0.0, float("nan"))).fill_nan(0.0).fill_null(0.0)
    fastest = 2.0 / (fast + 1.0)
    slowest = 2.0 / (slow + 1.0)
    sc_raw = er * (fastest - slowest) + slowest
    sc = sc_raw**2
    close_np = close.to_numpy()
    sc_np = sc.to_numpy()
    kama_np = np.empty_like(close_np)
    if len(kama_np) > 0:
        kama_np[0] = close_np[0]
        for i in range(1, len(kama_np)):
            kama_np[i] = kama_np[i - 1] + sc_np[i] * (close_np[i] - kama_np[i - 1])
    return pl.Series("kama10", kama_np).fill_nan(float("nan"))


def _heikin_ashi(df: pl.DataFrame) -> pl.DataFrame:
    """Heikin Ashi candles from OHLC."""
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = ha_close.shift(1).fill_null(ha_close[0]).ewm_mean(alpha=0.5, adjust=False)
    ha_high = pl.max_horizontal(df["high"], ha_open, ha_close)
    ha_low = pl.min_horizontal(df["low"], ha_open, ha_close)
    return df.select(
        ha_open.alias("ha_open"),
        ha_high.alias("ha_high"),
        ha_low.alias("ha_low"),
        ha_close.alias("ha_close"),
    )


def _add_advanced_indicators(
    df: pl.DataFrame,
    *,
    active_groups: frozenset[str] | None = None,
) -> pl.DataFrame:
    """Add advanced technical indicators using pure Polars implementations."""
    result = df

    # --- SuperTrend ---------------------------------------------------------
    if group_active(active_groups, "supertrend"):
        st, st_dir = supertrend_series(df, period=10, multiplier=3.0)
        result = result.with_columns(
            [
                st.alias("supertrend"),
                st_dir.alias("supertrend_dir"),
            ]
        )

    # --- OBV ---------------------------------------------------------------
    if group_active(active_groups, "obv"):
        obv = obv_series(df)
        obv_ema = obv.ewm_mean(span=20, adjust=False)
        result = result.with_columns(
            [
                obv.alias("obv"),
                obv_ema.alias("obv_ema20"),
                (obv > obv_ema).cast(pl.Float64).alias("obv_above_ema"),
                (obv > obv_ema).alias("obv_rising"),
            ]
        )

    # --- Bollinger Bands - pure Polars implementation ------------------------
    if group_active(active_groups, "bb"):
        upper, middle, lower = bbands_series(df, period=20, nbdev=2.0)
        bb_pct_b = (df["close"] - lower) / (upper - lower)
        middle_safe = _clean_non_finite(middle.abs(), fill=1e-10).clip(lower_bound=1e-10)
        bb_width = (upper - lower) / middle_safe * 100.0
        bb_width_clean = _clean_non_finite(bb_width, fill=0.0)
        result = result.with_columns(
            [
                upper.alias("bb_upper"),
                middle.alias("bb_mid"),
                lower.alias("bb_lower"),
                _clean_non_finite(bb_pct_b, fill=0.5).alias("bb_pct_b"),
                bb_width_clean.alias("bb_width"),
                (bb_width_clean.rolling_rank(window_size=50, method="average") / 50.0).alias(
                    "bb_width_pctile50"
                ),
            ]
        )

    # --- Keltner Channels - pure Polars implementation -----------------------
    if group_active(active_groups, "keltner"):
        kc_upper, _kc_middle, kc_lower = _keltner_channels(df, period=20, multiplier=2.0)
        close_safe = _clean_non_finite(df["close"].abs(), fill=1e-10).clip(lower_bound=1e-10)
        kc_width = (kc_upper - kc_lower) / close_safe
        result = result.with_columns(
            [
                kc_upper.alias("kc_upper"),
                kc_lower.alias("kc_lower"),
                _clean_non_finite(kc_width, fill=0.04).alias("kc_width"),
            ]
        )

    # --- Stochastic RSI (п.10) ------------------------------------------------
    if group_active(active_groups, "stoch_rsi"):
        stoch_rsi = _stochastic_rsi(df, period=14)
        result = result.with_columns(stoch_rsi.alias("stoch_rsi14"))

    # --- Ichimoku Cloud (п.11) ------------------------------------------------
    if group_active(active_groups, "ichimoku"):
        ichi = _ichimoku_cloud(df)
        if ichi is not None and not ichi.is_empty():
            for col in ichi.columns:
                result = result.with_columns(ichi[col].alias(col))

    # --- KAMA (Kaufman Adaptive Moving Average, п.13) -------------------------
    if group_active(active_groups, "kama"):
        kama = _kama(df, period=10, fast=2, slow=30)
        result = result.with_columns(kama.alias("kama10"))

    # --- Heikin Ashi candles (п.15) -------------------------------------------
    if group_active(active_groups, "heikin_ashi"):
        ha = _heikin_ashi(df)
        if ha is not None and not ha.is_empty():
            for col in ha.columns:
                result = result.with_columns(ha[col].alias(col))

    # --- HMA (Hull Moving Average) --------------------------------------------
    if group_active(active_groups, "hma"):
        close = df["close"]
        hma9 = hull_moving_average(close, 9, name="hma9")
        hma21 = hull_moving_average(close, 21, name="hma21")
        result = result.with_columns(
            [
                hma9.alias("hma9"),
                hma21.alias("hma21"),
            ]
        )

    # --- PSAR (Parabolic SAR) -------------------------------------------------
    if group_active(active_groups, "psar"):
        psar_long, psar_short, psar_reversal = _parabolic_sar(df, step=0.02, max_step=0.2)
        result = result.with_columns(
            [
                psar_long.alias("psar_long"),
                psar_short.alias("psar_short"),
                psar_reversal.alias("psar_reversal"),
            ]
        )

    # --- Aroon ---------------------------------------------------------------
    if group_active(active_groups, "aroon"):
        aroon_up, aroon_down, aroon_osc = aroon_series(df, period=14)
        result = result.with_columns(
            [
                aroon_up.alias("aroon_up14"),
                aroon_down.alias("aroon_down14"),
                aroon_osc.alias("aroon_osc14"),
            ]
        )

    # --- Stochastic ---------------------------------------------------------
    if group_active(active_groups, "stoch"):
        stoch_k, stoch_d = stochastic_series(df, period=14, smooth_k=3, smooth_d=3)
        result = result.with_columns(
            [
                stoch_k.alias("stoch_k14"),
                stoch_d.alias("stoch_d14"),
                (stoch_k - stoch_d).fill_nan(0.0).alias("stoch_h14"),
            ]
        )

    # --- CCI, Williams %R, MFI, CMF, Ultimate Oscillator --------------------
    if group_active(active_groups, "oscillators"):
        result = result.with_columns(
            [
                cci_from_polars_ta(df, 20).fill_nan(0.0).alias("cci20"),
                willr_from_polars_ta(df, 14).alias("willr14"),
                mfi_from_polars_ta(df, 14).fill_nan(50.0).alias("mfi14"),
                _cmf(df, 20).fill_nan(0.0).alias("cmf20"),
                _ultimate_oscillator(df, 7, 14, 28).fill_nan(50.0).alias("uo"),
            ]
        )

    # --- Fisher Transform -----------------------------------------------------
    if group_active(active_groups, "fisher"):
        fisher, fisher_signal = _fisher_transform(df, period=10)
        result = result.with_columns(
            [
                fisher.alias("fisher"),
                fisher_signal.alias("fisher_signal"),
            ]
        )

    # --- Squeeze Momentum ----------------------------------------------------
    if group_active(active_groups, "squeeze"):
        squeeze_hist, squeeze_on, squeeze_off, squeeze_no = _squeeze_momentum(df, period=20)
        result = result.with_columns(
            [
                squeeze_hist.alias("squeeze_hist"),
                squeeze_on.alias("squeeze_on"),
                squeeze_off.alias("squeeze_off"),
                squeeze_no.alias("squeeze_no"),
            ]
        )

    # --- Chandelier Exit -----------------------------------------------------
    if group_active(active_groups, "chandelier"):
        chandelier_long, chandelier_short, chandelier_dir = _chandelier_exit(
            df, period=22, atr_mult=3.0
        )
        result = result.with_columns(
            [
                chandelier_long.alias("chandelier_long"),
                chandelier_short.alias("chandelier_short"),
                chandelier_dir.alias("chandelier_dir"),
            ]
        )

    if group_active(active_groups, "volume_profile"):
        poc, vah, val = _volume_profile_levels(
            result,
            bins=VP_BUCKETS_DEFAULT,
            lookback=VP_LOOKBACK_15M,
        )
        result = result.with_columns(
            [
                pl.lit(poc).cast(pl.Float64).alias("volume_profile"),
                pl.lit(vah).cast(pl.Float64).alias("volume_profile_vah"),
                pl.lit(val).cast(pl.Float64).alias("volume_profile_val"),
            ]
        )

    if group_active(active_groups, "pivot_points"):
        pp, r1, r2, s1, s2 = _classic_pivot_points(result)
        result = result.with_columns(
            [
                pl.lit(pp).cast(pl.Float64).alias("pivot_point"),
                pl.lit(r1).cast(pl.Float64).alias("pivot_r1"),
                pl.lit(r2).cast(pl.Float64).alias("pivot_r2"),
                pl.lit(s1).cast(pl.Float64).alias("pivot_s1"),
                pl.lit(s2).cast(pl.Float64).alias("pivot_s2"),
            ]
        )

    # --- Z-Score and Slope -------------------------------------------------
    if group_active(active_groups, "zscore"):
        zscore30 = (
            (df["close"] - df["close"].rolling_mean(window_size=30))
            / df["close"].rolling_std(window_size=30)
        ).fill_nan(0.0)
        result = result.with_columns(
            [
                _clean_non_finite(zscore30, fill=0.0).alias("zscore30"),
                roc_series(df, 5).fill_nan(0.0).alias("slope5"),
            ]
        )

    if group_active(active_groups, "polars_ta_extended"):
        extended = polars_ta_extended_exprs(df)
        if extended:
            result = result.with_columns(extended)

    if group_active(active_groups, "polars_wq_features"):
        wq_cols = polars_wq_exprs(result)
        if wq_cols:
            result = result.with_columns(wq_cols)

    return result


from .volume_profile import (
    VP_BUCKETS_DEFAULT,
    VP_LOOKBACK_15M,
    VP_VALUE_AREA_PCT,
    volume_profile_levels as _volume_profile_levels_core,
)


def _volume_profile_levels(
    df: pl.DataFrame,
    bins: int = VP_BUCKETS_DEFAULT,
    *,
    lookback: int | None = VP_LOOKBACK_15M,
    value_area_pct: float = VP_VALUE_AREA_PCT,
) -> tuple[float | None, float | None, float | None]:
    """Scalar POC/VAH/VAL — same algorithm as PreparedSymbol poc_* fields."""
    return _volume_profile_levels_core(
        df,
        lookback=lookback,
        buckets=bins,
        value_area_pct=value_area_pct,
    )


def _volume_profile(df: pl.DataFrame, bins: int = 12) -> pl.Expr:
    poc, _vah, _val = _volume_profile_levels(df, bins=bins)
    return pl.lit(0.0 if poc is None else poc).cast(pl.Float64).alias("volume_profile")


def _classic_pivot_points(
    df: pl.DataFrame,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Classic daily pivot points from the last ~24h of data (or frame range).

    Returns (PP, R1, R2, S1, S2). Returns (None, None, None, None, None) if
    insufficient data.
    """
    if df.is_empty() or not {"high", "low", "close"}.issubset(df.columns):
        return None, None, None, None, None
    tail = df.tail(min(df.height, 48))
    prev_high = float(tail["high"].max() or 0.0)
    prev_low = float(tail["low"].min() or 0.0)
    prev_close = float(tail["close"][-1] or 0.0)
    if prev_high <= 0.0 or prev_low <= 0.0 or prev_close <= 0.0:
        return None, None, None, None, None
    pp = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2.0 * pp - prev_low
    r2 = pp + (prev_high - prev_low)
    s1 = 2.0 * pp - prev_high
    s2 = pp - (prev_high - prev_low)
    return pp, r1, r2, s1, s2


def _add_polars_ols_features(df: pl.DataFrame) -> pl.DataFrame:
    """Add shared regression-slope features using polars_ols."""
    if df.is_empty() or "close" not in df.columns:
        return df
    index_expr = pl.int_range(0, pl.len()).cast(pl.Float64)
    rolling_kwargs = polars_ols_ls.RollingKwargs(
        window_size=20,
        min_periods=20,
        use_woodbury=None,
        alpha=None,
        null_policy="drop",
    )
    slope_struct = polars_ols.compute_rolling_least_squares(
        pl.col("close"),
        index_expr,
        add_intercept=True,
        mode="coefficients",
        rolling_kwargs=rolling_kwargs,
    )
    work = df.with_columns(slope_struct.alias("_ols_close20"))
    work = work.with_columns(
        pl.col("_ols_close20").struct.field("literal").alias("close_ols_slope20")
    )
    return work.drop("_ols_close20").with_columns(
        [
            (pl.col("close_ols_slope20") / pl.col("close") * 100.0)
            .fill_nan(0.0)
            .fill_null(0.0)
            .alias("close_ols_slope_pct20"),
            (
                pl.col("close_ols_slope20")
                / pl.when(pl.col("atr14") > 0.0).then(pl.col("atr14")).otherwise(None)
            )
            .fill_nan(0.0)
            .fill_null(0.0)
            .alias("close_ols_slope_atr20"),
        ]
    )


# ---------------------------------------------------------------------------
# Main frame preparation
# ---------------------------------------------------------------------------


def _prepare_frame(
    df: pl.DataFrame,
    *,
    active_groups: frozenset[str] | None = None,
    warmup_ema: int = 200,
) -> pl.DataFrame:
    """Compute all technical indicators for a single OHLCV DataFrame.

    Returns a new DataFrame with NaN-seeded rows dropped.
    All backward-compatible column names are preserved.

    When ``active_groups`` is set, optional indicator blocks may be skipped
    (see ``bot.features.prepare_columns``). ``None`` computes every group.

    Incremental hook: callers may pass ``df.lazy()`` through a future
    ``_prepare_frame_lazy`` path; hot path stays eager Polars for now.
    """
    df = _coerce_temporal_columns(df)

    adx14, plus_di14, minus_di14 = adx_from_polars_ta(df, 14)

    macd_line, macd_signal, macd_hist = macd_series(df)

    vwap_time_column = next(
        (
            column
            for column in ("close_time", "time", "open_time")
            if column in df.columns and _is_temporal_dtype(df.schema.get(column))
        ),
        None,
    )
    vwap_window = max(20, min(96, df.height))
    vwap_min_periods = max(10, vwap_window // 4)
    typical_price = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
    vwap_expr = (
        (typical_price * pl.col("volume")).rolling_sum(
            window_size=vwap_window,
            min_periods=vwap_min_periods,
        )
        / pl.col("volume").rolling_sum(
            window_size=vwap_window,
            min_periods=vwap_min_periods,
        )
    ).forward_fill()
    if vwap_time_column is not None:
        vwap_std_expr = (
            pl.col("_vwap_dev_sq").cum_sum().over("_vwap_session")
            / pl.col("_vwap_dev_sq").cum_count().over("_vwap_session")
        ).sqrt()
    elif df.height:
        vwap_std_expr = (
            ((pl.col("close") - pl.col("vwap")) ** 2).cum_sum()
            / pl.int_range(1, pl.len() + 1)
        ).sqrt()
    else:
        vwap_std_expr = (pl.col("close") - pl.col("vwap")) ** 2

    delta_ratio_exprs = (
        [
            (
                (pl.col("taker_buy_base_volume") / pl.col("volume"))
                .rolling_mean(window_size=5)
                .clip(0.0, 1.0)
                .alias("delta_ratio")
            ),
        ]
        if "taker_buy_base_volume" in df.columns
        else [pl.lit(0.5).alias("delta_ratio")]
    )
    close_position_expr = (
        (
            (pl.col("close") - pl.col("low").rolling_min(window_size=20))
            / (pl.col("high").rolling_max(window_size=20) - pl.col("low").rolling_min(window_size=20))
        )
        .replace([float("inf"), float("-inf")], None)
        .fill_nan(0.5)
        .fill_null(0.5)
        .clip(0.0, 1.0)
        .alias("close_position")
    )

    lf = (
        df.lazy()
        # Batch 1 — polars-ta eager series + MACD
        .with_columns(
            [
                ema_series(df, 20).alias("ema20"),
                ema_series(df, 50).alias("ema50"),
                ema_series(df, 200).alias("ema200"),
                rsi_series(df, 14).alias("rsi14"),
                adx14.alias("adx14"),
                plus_di14.alias("plus_di14"),
                minus_di14.alias("minus_di14"),
                atr_series(df, 14).alias("atr14"),
                macd_line.alias("macd_line"),
                macd_signal.alias("macd_signal"),
                macd_hist.alias("macd_hist"),
            ]
        )
        # Batch 2 — Donchian + volume (pure expr)
        .with_columns(
            [
                pl.col("low").rolling_min(window_size=20).alias("donchian_low20"),
                pl.col("high").rolling_max(window_size=20).alias("donchian_high20"),
                pl.col("low").rolling_min(window_size=20).shift(1).alias("prev_donchian_low20"),
                pl.col("high").rolling_max(window_size=20).shift(1).alias("prev_donchian_high20"),
                pl.col("volume").rolling_mean(window_size=20).alias("volume_mean20"),
                (pl.col("volume") / pl.col("volume").rolling_mean(window_size=20)).alias(
                    "volume_ratio20"
                ),
            ]
        )
        # Batch 3 — VWAP
        .with_columns([vwap_expr.alias("vwap")])
    )
    if vwap_time_column is not None:
        lf = (
            lf.with_columns(
                [
                    ((pl.col("close") - pl.col("vwap")) ** 2).alias("_vwap_dev_sq"),
                    pl.col(vwap_time_column).dt.date().alias("_vwap_session"),
                ]
            )
            .with_columns([vwap_std_expr.alias("vwap_std")])
            .drop("_vwap_dev_sq", "_vwap_session")
        )
    else:
        lf = lf.with_columns([vwap_std_expr.alias("vwap_std")])
    work = (
        lf
        # Batch 4 — VWAP bands + deviation
        .with_columns(
            [
                (pl.col("vwap") + pl.col("vwap_std")).alias("vwap_upper1"),
                (pl.col("vwap") - pl.col("vwap_std")).alias("vwap_lower1"),
                (pl.col("vwap") + 2.0 * pl.col("vwap_std")).alias("vwap_upper2"),
                (pl.col("vwap") - 2.0 * pl.col("vwap_std")).alias("vwap_lower2"),
                (((pl.col("close") - pl.col("vwap")) / pl.col("vwap")) * 100.0)
                .fill_nan(0.0)
                .alias("vwap_deviation_pct"),
                ((pl.col("close") - pl.col("vwap")) / pl.col("atr14"))
                .fill_nan(0.0)
                .alias("vwap_deviation_atr14"),
            ]
        )
        # Batch 5 — delta ratio + ATR% + close position
        .with_columns(delta_ratio_exprs)
        .with_columns(
            [
                ((pl.col("atr14") / pl.col("close")) * 100.0)
                .clip(lower_bound=0.001)
                .alias("atr_pct"),
            ]
        )
        .with_columns([close_position_expr])
        .collect()
    )

    work = add_session_cvd(work)
    work = add_rolling_cvd_24h(work)

    # Advanced indicators
    work = _add_advanced_indicators(work, active_groups=active_groups)
    if group_active(active_groups, "microstructure"):
        work = add_microstructure_features(work)
    if group_active(active_groups, "ols"):
        work = _add_polars_ols_features(work)
    if group_active(active_groups, "tail_metrics"):
        work = work.with_columns(
            [
                roc_series(work, 10).fill_nan(0.0).alias("roc10"),
                _realized_volatility(work, 20).fill_nan(0.0).alias("realized_vol_20"),
                (
                    (
                        pl.col("vwap_deviation_pct")
                        - pl.col("vwap_deviation_pct").rolling_mean(window_size=20)
                    )
                    / pl.col("vwap_deviation_pct").rolling_std(window_size=20, ddof=1)
                )
                .fill_nan(0.0)
                .alias("vwap_deviation_z20"),
            ]
        )
    if group_active(active_groups, "session"):
        work = _add_session_features(work)
    if group_active(active_groups, "candles"):
        work = add_candle_pattern_columns(work)

    # Drop rows with insufficient data (pinned HTF may use ema50 warmup)
    trim_col = "ema50" if warmup_ema <= 50 else "ema200"
    if trim_col not in work.columns:
        trim_col = "ema200"
    work = work.filter(pl.col(trim_col).is_not_null() & pl.col("donchian_low20").is_not_null())

    return work


def factor_panel_from_frames(
    work_15m: pl.DataFrame,
    work_1h: pl.DataFrame,
    *,
    market: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """Build normalized factor panel from prepared Polars frames (§E.1)."""
    from hunt_core.features.factors import build_factor_panel

    def _last_val(df: pl.DataFrame, col: str) -> float | None:
        if df.is_empty() or col not in df.columns:
            return None
        try:
            v = float(df[col][-1])
        except (TypeError, ValueError):
            return None
        return v if v == v else None

    row: dict[str, Any] = {
        "timeframes": {
            "15m": {
                "rsi14": _last_val(work_15m, "rsi14"),
                "cmf20": _last_val(work_15m, "cmf20"),
                "kama10": _last_val(work_15m, "kama10"),
            },
            "1h": {
                "adx14": _last_val(work_1h, "adx14"),
                "rsi14": _last_val(work_1h, "rsi14"),
            },
        },
        "market": market or {},
    }
    return build_factor_panel(row)


# ---------------------------------------------------------------------------
# 4h bias helper
# ---------------------------------------------------------------------------


__all__ = [
    "_add_advanced_indicators",
    "_as_float_like",
    "_as_optional_float",
    "_finite_float",
    "_numeric_item",
    "_prepare_frame",
    "add_rolling_cvd_24h",
    "add_session_cvd",
    "factor_panel_from_frames",
    "has_minimum_bars",
    "min_required_bars",
]
