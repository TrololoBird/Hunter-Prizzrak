"""Technical analysis feature preparation (Polars-native runtime path).

Indicators stay Polars-native with optional `polars_ta` for EMA/ROC/OBV.
Pure-Polars formulas are canonical for Wilder RSI/ATR/ADX, MACD, BB (ddof=1, sample std), and structure.
"""
from __future__ import annotations



import logging
import math
import os
import threading
from collections import OrderedDict
from typing import Any, cast

import polars as pl
import structlog

from hunt_core.toolkit.adx_thresholds import ADX_RANGE_MAX, ADX_TREND_MIN
from hunt_core.toolkit.trend import bias_from_ema_row
from hunt_core.features.pivots import _swing_points

from ..domain.schemas import PreparedSymbol, SymbolFrames, UniverseSymbol
from ..market.client import (
    depth_imbalance_by_zone,
    depth_imbalance_from_book,
    detect_wall_clusters,
    microprice_bias_from_book,
    normalize_depth_levels,
    wall_cluster_to_dict,
)
def _configured_primary_timeframe(settings: Any, symbol: str, default: str = "15m") -> str:
    assets = getattr(settings, "assets", None)
    if not isinstance(assets, dict):
        return default
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return default
    entry = assets.get(normalized)
    return entry.get("primary_timeframe", default) if isinstance(entry, dict) else default


def _configured_context_timeframes(settings: Any, symbol: str, default: tuple[str, ...] = ("1h", "4h")) -> tuple[str, ...]:
    assets = getattr(settings, "assets", None)
    if not isinstance(assets, dict):
        return default
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return default
    entry = assets.get(normalized)
    if isinstance(entry, dict) and "context_timeframes" in entry:
        return tuple(entry["context_timeframes"])
    return default
from .microstructure import add_microstructure_features
from .prepare_columns import resolve_prepare_groups, resolve_prepare_groups_for_symbol
from .prepare_frame import (
    _add_advanced_indicators,
    _as_optional_float,
    _numeric_item,
    _prepare_frame,
    _tail_value_signature,
    _timestamp_ns,
    add_rolling_cvd_24h,
    add_session_cvd,
    has_minimum_bars,
    min_required_bars,
)

LOG = structlog.get_logger("hunt_core.features.prepare")

# Frame-level indicator cache - LRU with unique frame-window keys.
# ---------------------------------------------------------------------------

_MAX_CACHE_ENTRIES = 1200
_FrameCacheValue = float | None
_FrameCacheKey = tuple[
    str,
    str,
    int,
    int,
    int,
    tuple[_FrameCacheValue, ...],
    tuple[object, ...] | None,
    tuple[str, ...] | None,
]
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
_CRITICAL_SIGNAL_COLS = frozenset(
    {
        "close",
        "open",
        "high",
        "low",
        "volume",
        "atr14",
        "rsi14",
        "adx14",
        "ema20",
        "ema50",
        "ema200",
    }
)


class _FrameCache:
    """Best-effort LRU cache for prepared frames.

    Keys include symbol, interval, row count, first/last close time, and the
    latest OHLCV-like values. `_prepare_frame` depends on the whole history
    window, and live partial kline updates keep the same close time while the
    current candle values change.

    Cache access never waits for a contended lock. Missing a cache hit is
    cheaper than blocking the async analysis loop behind another frame update.
    """

    __slots__ = ("_hits", "_lock", "_max_size", "_misses", "_store")

    def __init__(self, max_size: int = 500) -> None:
        self._store: OrderedDict[_FrameCacheKey, pl.DataFrame] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: _FrameCacheKey) -> pl.DataFrame | None:
        if not self._lock.acquire(blocking=False):
            self._misses += 1
            return None
        try:
            if key not in self._store:
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return self._store[key]
        finally:
            self._lock.release()

    def put(self, key: _FrameCacheKey, value: pl.DataFrame) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = value
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
        finally:
            self._lock.release()

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            hits = int(self._hits)
            misses = int(self._misses)
            total = hits + misses
            return {
                "hits": hits,
                "misses": misses,
                "size": len(self._store),
                "hit_rate": round(hits / total, 6) if total else 0.0,
            }

    def cache_stats(self) -> dict[str, float | int]:
        return self.stats()


# Module-level singleton kept for backward compatibility.
_FRAME_CACHE = _FrameCache(max_size=_MAX_CACHE_ENTRIES)


def cache_stats() -> dict[str, float | int]:
    """Return frame preparation cache hit/miss counters for health telemetry."""
    return _FRAME_CACHE.stats()


def _bias_4h(work_4h: pl.DataFrame) -> str:
    """Determine 4h bias from canonical EMA stack + ADX filter."""
    if work_4h.is_empty():
        return "neutral"
    last = work_4h.row(-1, named=True)
    return bias_from_ema_row(
        float(last["close"]),
        float(last["ema20"]),
        float(last["ema50"]),
        float(last["ema200"]),
        float(last.get("adx14") or 0.0),
    )


def _bias_1h(work_1h: pl.DataFrame) -> str:
    """Determine 1h bias from canonical EMA stack + ADX filter."""
    if work_1h.is_empty():
        return "neutral"
    last = work_1h.row(-1, named=True)
    return bias_from_ema_row(
        float(last["close"]),
        float(last["ema20"]),
        float(last["ema50"]),
        float(last["ema200"]),
        float(last.get("adx14") or 0.0),
    )


def _market_regime(
    work_4h: pl.DataFrame,
    work_1h: pl.DataFrame | None = None,
    work_15m: pl.DataFrame | None = None,
    threshold_choppy: float = ADX_RANGE_MAX,
    threshold_trending: float = ADX_TREND_MIN,
) -> str:
    """Classify regime from 4h strength plus 1h/15m structure."""
    if work_4h.is_empty() or "adx14" not in work_4h.columns:
        return "neutral"

    adx_4h = _numeric_item(work_4h, -1, "adx14")
    bias_4h = _bias_4h(work_4h)
    regime_1h = _regime_1h_confirmed(work_1h if work_1h is not None else pl.DataFrame())
    atr_pct_15m = _numeric_item(work_15m if work_15m is not None else pl.DataFrame(), -1, "atr_pct")

    if (
        adx_4h >= threshold_trending
        and bias_4h in {"uptrend", "downtrend"}
        and regime_1h in {"uptrend", "downtrend"}
    ):
        return "trending"
    if adx_4h < threshold_choppy and regime_1h == "ranging":
        return "choppy"
    if atr_pct_15m >= 3.0 and regime_1h == "ranging":
        return "choppy"
    return "neutral"


# ---------------------------------------------------------------------------
# Structure-based helpers
# ---------------------------------------------------------------------------


def _market_structure_1h(work_1h: pl.DataFrame) -> str:
    """Determine 1h market structure from swing points."""
    if len(work_1h) < 20:
        return "ranging"

    swing_high, swing_low = _swing_points(work_1h, n=3)

    # Get swing high/low values
    last_highs = work_1h.filter(swing_high)["high"].tail(2)
    last_lows = work_1h.filter(swing_low)["low"].tail(2)

    if last_highs.len() < 2 or last_lows.len() < 2:
        return "ranging"

    hh = last_highs[1] > last_highs[0]  # higher high
    hl = last_lows[1] > last_lows[0]  # higher low
    lh = last_highs[1] < last_highs[0]  # lower high
    ll = last_lows[1] < last_lows[0]  # lower low

    if hh and hl:
        return "uptrend"
    if lh and ll:
        return "downtrend"
    return "ranging"


def _regime_4h_confirmed(work_4h: pl.DataFrame, min_bars: int = 3) -> str:
    """Strict 4h regime requiring consecutive bars in same trend."""
    if len(work_4h) < min_bars:
        return "ranging"

    tail = work_4h.tail(min_bars)

    # Check uptrend condition
    uptrend_count = tail.filter(
        (pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200"))
    ).height

    # Check downtrend condition
    downtrend_count = tail.filter(
        (pl.col("ema20") < pl.col("ema50")) & (pl.col("ema50") < pl.col("ema200"))
    ).height

    if uptrend_count == min_bars:
        return "uptrend"
    if downtrend_count == min_bars:
        return "downtrend"
    return "ranging"


def _regime_1h_confirmed(work_1h: pl.DataFrame, min_bars: int = 3) -> str:
    """Strict 1h regime requiring consecutive bars in same trend for 15M signal context."""
    if len(work_1h) < min_bars:
        return "ranging"

    tail = work_1h.tail(min_bars)

    # Check uptrend condition
    uptrend_count = tail.filter(
        (pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200"))
    ).height

    # Check downtrend condition
    downtrend_count = tail.filter(
        (pl.col("ema20") < pl.col("ema50")) & (pl.col("ema50") < pl.col("ema200"))
    ).height

    if uptrend_count == min_bars:
        return "uptrend"
    if downtrend_count == min_bars:
        return "downtrend"
    return "ranging"


from hunt_core.features.volume_profile import (
    VP_BUCKETS_DEFAULT,
    VP_LOOKBACK_15M,
    volume_profile_levels as _volume_profile_levels,
    volume_profile_with_direction as _volume_profile_with_direction,
)


def _volume_poc(work: pl.DataFrame, lookback: int = 96, buckets: int = VP_BUCKETS_DEFAULT) -> float | None:
    poc, _vah, _val = _volume_profile_levels(work, lookback=lookback, buckets=buckets)
    return poc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_EXPECTED_ZERO_COLUMNS = frozenset(
    {
        "macd_hist",
        "obv",
        "obv_ema20",
        "obv_above_ema",
        "squeeze_on",
        "squeeze_off",
        "squeeze_no",
        "squeeze_hist",
        "psar_reversal",
        "chandelier_dir",
        "session_asia",
        "session_london",
        "session_ny",
        "session_overlap",
        "session_asia_vol_20",
        "session_london_vol_20",
        "session_ny_vol_20",
        "session_overlap_vol_20",
        "delta_ratio",
        "roc10",
        "realized_vol_20",
        "vwap_deviation_z20",
        "aggression_shift",
        "depth_imbalance",
        "microprice_bias",
        "depth_wall_pressure",
        "tob_imbalance",
        "microprice_deviation_pct",
    }
)


def _series_numeric_bounds(series: pl.Series) -> tuple[float | None, float | None]:
    numeric = series.cast(pl.Float64, strict=False).drop_nulls()
    if numeric.is_empty():
        return None, None
    try:
        return _as_optional_float(numeric.min()), _as_optional_float(numeric.max())
    except (TypeError, ValueError):
        return None, None


def _sanity_check_prepared_frame(work: pl.DataFrame, symbol: str, interval: str) -> list[str]:
    """Return frame quality defects that must block signal preparation.

    Any non-empty list means the symbol cannot be analyzed safely: missing OHLCV,
    empty frame, null/NaN/inf on the signal bar, or impossible indicator ranges.
    """
    defects: list[str] = []
    missing = REQUIRED_COLS - set(work.columns)
    if missing:
        defects.append(f"Missing required columns: {missing}")
    if work.is_empty():
        defects.append(f"{symbol}/{interval}: prepared frame is empty")
        return defects

    signal_bar_cols = ("close", "atr14", "rsi14", "adx14")
    for column in signal_bar_cols:
        if column not in work.columns:
            continue
        raw = work[column][-1]
        if raw is None:
            defects.append(f"{column}: last bar is null")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            defects.append(f"{column}: last bar non-numeric ({raw!r})")
            continue
        if not math.isfinite(value):
            defects.append(f"{column}: last bar non-finite ({value})")

    if interval == "15m":
        flow_bar_cols = (
            "signed_order_flow",
            "session_cvd",
            "tob_imbalance",
            "rolling_cvd_24h",
        )
        for column in flow_bar_cols:
            if column not in work.columns:
                continue
            raw = work[column][-1]
            if raw is None:
                defects.append(f"{column}: last bar is null")
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                defects.append(f"{column}: last bar non-numeric ({raw!r})")
                continue
            if not math.isfinite(value):
                defects.append(f"{column}: last bar non-finite ({value})")

    def _range_defect(column: str, low: float, high: float) -> None:
        if column not in work.columns:
            return
        min_value, max_value = _series_numeric_bounds(work[column])
        if min_value is None or max_value is None:
            defects.append(f"{column}: all values null or non-numeric")
            return
        if min_value < low or max_value > high:
            defects.append(
                f"{column}: out of range [{low}, {high}] min={min_value:.6f} max={max_value:.6f}"
            )

    def _positive_defect(column: str, *, allow_zero: bool) -> None:
        if column not in work.columns:
            return
        min_value, _ = _series_numeric_bounds(work[column])
        if min_value is None:
            defects.append(f"{column}: all values null or non-numeric")
            return
        if allow_zero:
            if min_value < 0.0:
                defects.append(f"{column}: negative value detected min={min_value:.6f}")
        elif min_value <= 0.0:
            defects.append(f"{column}: non-positive value detected min={min_value:.6f}")

    _range_defect("rsi14", 0.0, 100.0)
    _range_defect("adx14", 0.0, 100.0)
    _range_defect("stoch_k14", 0.0, 100.0)
    _range_defect("stoch_d14", 0.0, 100.0)
    _range_defect("mfi14", 0.0, 100.0)
    _positive_defect("atr14", allow_zero=False)
    _positive_defect("ema20", allow_zero=False)
    _positive_defect("ema50", allow_zero=False)
    _positive_defect("ema200", allow_zero=False)
    _positive_defect("volume_ratio20", allow_zero=True)
    _positive_defect("close", allow_zero=False)

    for column in work.columns:
        if column.startswith("_") or column not in _CRITICAL_SIGNAL_COLS:
            continue
        series = work[column]
        if series.null_count() == work.height:
            defects.append(f"{column}: column is entirely null")
            continue
        if column in _EXPECTED_ZERO_COLUMNS:
            continue
        dtype = work.schema.get(column)
        if dtype is None or not (
            getattr(dtype, "is_numeric", lambda: False)() or dtype == pl.Boolean
        ):
            continue
    return defects


def _sanity_check_all_frames(prepared: PreparedSymbol) -> dict[str, list[str]]:
    """Run prepared-frame sanity checks for every available timeframe."""
    frames: dict[str, pl.DataFrame | None] = {
        "5m": prepared.work_5m,
        "15m": prepared.work_15m,
        "1h": prepared.work_1h,
        "4h": prepared.work_4h,
    }
    report: dict[str, list[str]] = {}
    for interval, frame in frames.items():
        if frame is None:
            continue
        warnings = _sanity_check_prepared_frame(frame, prepared.symbol, interval)
        if warnings:
            report[interval] = warnings
    return report


def _log_frame_defect(symbol: str, interval: str, warning: str) -> None:
    """Optional 4h defects are debug-only; required TFs stay at warning."""
    if interval == "4h":
        LOG.debug(
            "prepared frame quality defect | symbol=%s interval=%s defect=%s",
            symbol,
            interval,
            warning,
        )
        return
    LOG.warning(
        "prepared frame quality defect | symbol=%s interval=%s defect=%s",
        symbol,
        interval,
        warning,
    )


def _cached_prepare_frame(
    frame: pl.DataFrame,
    *,
    symbol: str = "",
    interval: str = "",
    cache: _FrameCache | None = None,
    fallback_book: tuple[float | None, float | None, float | None, float | None] | None = None,
    active_groups: frozenset[str] | None = None,
) -> pl.DataFrame:
    """_prepare_frame with LRU cache keyed on (symbol, interval, close_time)."""
    enrich_book = fallback_book if interval in {"15m", "5m"} else None
    if frame.is_empty() or "close_time" not in frame.columns or "close" not in frame.columns:
        result = _enrich_with_book_data(
            _prepare_frame(frame, active_groups=active_groups),
            fallback_book=enrich_book,
        )
        for warning in _sanity_check_prepared_frame(result, symbol, interval):
            _log_frame_defect(symbol, interval, warning)
        return result

    last = frame.row(-1, named=True)
    first = frame.row(0, named=True)
    try:
        first_close_time_ns = _timestamp_ns(first["close_time"])
        close_time_ns = _timestamp_ns(last["close_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        result = _prepare_frame(frame, active_groups=active_groups)
        for warning in _sanity_check_prepared_frame(result, symbol, interval):
            _log_frame_defect(symbol, interval, warning)
        return result

    tail_signature = _tail_value_signature(last)
    groups_key = None if active_groups is None else tuple(sorted(active_groups))
    key = (
        symbol,
        interval,
        frame.height,
        first_close_time_ns,
        close_time_ns,
        tail_signature,
        _book_enrichment_signature(enrich_book),
        groups_key,
    )
    target_cache = cache or _FRAME_CACHE
    cached = target_cache.get(key)
    if cached is not None:
        return cached

    result = _enrich_with_book_data(
        _prepare_frame(frame, active_groups=active_groups),
        fallback_book=enrich_book,
    )
    for warning in _sanity_check_prepared_frame(result, symbol, interval):
        _log_frame_defect(symbol, interval, warning)
    target_cache.put(key, result)
    return result


def _book_enrichment_signature(
    fallback_book: tuple[float | None, float | None, float | None, float | None] | None,
) -> tuple[object, ...] | None:
    if fallback_book is None:
        return None

    def _rounded(value: object) -> float | None:
        numeric = _as_optional_float(value)
        return None if numeric is None else round(numeric, 8)

    return tuple(_rounded(v) for v in fallback_book)


def _enrich_with_book_data(
    work: pl.DataFrame,
    *,
    fallback_book: tuple[float | None, float | None, float | None, float | None] | None = None,
) -> pl.DataFrame:
    """Merge CCXT book columns from tick assembly ``fallback_book``."""
    if work.is_empty() or fallback_book is None:
        return work

    fb_bid, fb_ask, fb_bid_qty, fb_ask_qty = fallback_book
    work = work.with_columns(
        [
            pl.lit(_as_optional_float(fb_bid)).cast(pl.Float64).alias("bid_price"),
            pl.lit(_as_optional_float(fb_ask)).cast(pl.Float64).alias("ask_price"),
            pl.lit(_as_optional_float(fb_bid_qty)).cast(pl.Float64).alias("bid_qty"),
            pl.lit(_as_optional_float(fb_ask_qty)).cast(pl.Float64).alias("ask_qty"),
        ]
    )
    work = add_microstructure_features(work)
    work = add_session_cvd(work)
    work = add_rolling_cvd_24h(work)
    return work


def _to_polars(df: object) -> pl.DataFrame:
    """Normalize supported frame-like values to Polars."""
    if isinstance(df, pl.DataFrame):
        return df
    if type(df).__module__.startswith("pandas"):
        msg = "prepare_symbol expects Polars frames; pandas inputs are unsupported"
        raise TypeError(msg)
    return pl.DataFrame(cast("Any", df))


def prepare_symbol(
    universe_symbol: UniverseSymbol,
    frames: SymbolFrames,
    *,
    minimums: dict[str, int] | None = None,
    settings: Any | None = None,
) -> PreparedSymbol | None:
    """Prepare a symbol for signal detection by computing all indicators.

    Returns None if there is insufficient historical data.
    """
    _log = logging.getLogger("hunt_core.features.prepare")

    sym = universe_symbol.symbol

    minimums = minimums or min_required_bars()
    len_4h = frames.df_4h.height if frames.df_4h is not None else 0
    len_1h = frames.df_1h.height
    len_15m = frames.df_15m.height
    len_5m = frames.df_5m.height if frames.df_5m is not None else 0

    required_timeframes = ("5m", "15m", "1h", "4h")
    if not has_minimum_bars(
        frames,
        minimums=minimums,
        required_timeframes=required_timeframes,
    ):
        _log.info(
            "%s: insufficient frame data | 1h=%d/%d 15m=%d/%d 5m=%d/%d 4h=%d/%d",
            sym,
            len_1h,
            minimums["1h"],
            len_15m,
            minimums["15m"],
            len_5m,
            minimums["5m"],
            len_4h,
            minimums["4h"],
        )
        return None

    active_groups = resolve_prepare_groups_for_symbol(sym)
    if os.getenv("HUNT_FULL_PREPARE", "").strip().lower() in {"1", "true", "yes"}:
        if settings is not None and hasattr(settings, "setups"):
            active_groups = resolve_prepare_groups(settings.setups.enabled_setup_ids())

    work_1h = _cached_prepare_frame(
        _to_polars(frames.df_1h),
        symbol=sym,
        interval="1h",
        active_groups=active_groups,
    )
    fallback_book = (frames.bid_price, frames.ask_price, frames.bid_qty, frames.ask_qty)
    work_15m = _cached_prepare_frame(
        _to_polars(frames.df_15m),
        symbol=sym,
        interval="15m",
        fallback_book=fallback_book,
        active_groups=active_groups,
    )
    work_5m = None
    if frames.df_5m is not None and not frames.df_5m.is_empty():
        work_5m = _cached_prepare_frame(
            _to_polars(frames.df_5m),
            symbol=sym,
            interval="5m",
            active_groups=active_groups,
        )
        # P1.11: overlay fresh 5m WS book/aggTrade/liquidation context onto the
        # confirm frame; falls back to REST frame untouched when WS is absent.
        if work_5m is not None and (
            frames.bid_qty is not None or frames.ask_qty is not None
        ):
            work_5m = _enrich_with_book_data(work_5m, fallback_book=fallback_book)
    work_4h = None
    if frames.df_4h is not None and not frames.df_4h.is_empty():
        work_4h = _cached_prepare_frame(
            _to_polars(frames.df_4h),
            symbol=sym,
            interval="4h",
            active_groups=active_groups,
        )

    prepared_frames: list[tuple[str, pl.DataFrame | None]] = [
        ("1h", work_1h),
        ("15m", work_15m),
    ]
    if work_5m is not None:
        prepared_frames.append(("5m", work_5m))
    if work_4h is not None:
        prepared_frames.append(("4h", work_4h))
    for interval, frame in prepared_frames:
        if frame is None:
            if interval == "4h":
                work_4h = None
                _log.debug("%s: optional 4h frame skipped | frame=None", sym)
                continue
            _log.warning("%s: prepare rejected | interval=%s frame=None", sym, interval)
            return None
        defects = _sanity_check_prepared_frame(frame, sym, interval)
        if defects:
            if interval == "4h":
                work_4h = None
                _log.debug(
                    "%s: optional 4h frame skipped | defects=%s",
                    sym,
                    defects,
                )
                continue
            _log.warning(
                "%s: prepare rejected - frame quality defects | interval=%s defects=%s",
                sym,
                interval,
                defects,
            )
            return None

    work_len_1h = len(work_1h) if work_1h is not None else 0
    work_len_15m = len(work_15m) if work_15m is not None else 0
    work_len_5m = len(work_5m) if work_5m is not None else 0
    work_len_4h = len(work_4h) if work_4h is not None else 0

    if min(work_len_1h, work_len_15m) < 30:
        _log.info(
            (
                "%s: insufficient processed data | work_1h=%d work_15m=%d "
                "optional_5m=%d optional_4h=%d need=30"
            ),
            sym,
            work_len_1h,
            work_len_15m,
            work_len_5m,
            work_len_4h,
        )
        return None

    configured_primary = _configured_primary_timeframe(settings, sym)
    context_timeframes = _configured_context_timeframes(settings, sym)
    primary_timeframe = configured_primary
    primary_work = work_15m
    if configured_primary == "1h":
        primary_work = work_1h
    elif configured_primary == "4h" and work_4h is not None and work_len_4h >= 30:
        primary_work = work_4h
    elif configured_primary == "5m" and work_5m is not None and work_len_5m >= 30:
        primary_work = work_5m
    elif configured_primary != "15m":
        primary_timeframe = "15m"
        _log.info(
            "%s: primary timeframe fallback | requested=%s fallback=15m work_5m=%d work_4h=%d",
            sym,
            configured_primary,
            work_len_5m,
            work_len_4h,
        )

    _log.info(
        (
            "%s: prepared symbol successfully | primary_timeframe=%s work_primary=%d "
            "work_15m=%d work_1h=%d work_5m=%d optional_4h=%d"
        ),
        sym,
        primary_timeframe,
        len(primary_work) if primary_work is not None else 0,
        work_len_15m,
        work_len_1h,
        work_len_5m,
        work_len_4h,
    )

    # Calculate spread and orderbook metrics (prefer REST frames, fall back to enriched 15m)
    def _frame_last(col: str) -> float | None:
        if work_15m is None or work_15m.is_empty() or col not in work_15m.columns:
            return None
        try:
            value = float(work_15m[col][-1])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    bid_price = frames.bid_price if frames.bid_price is not None else _frame_last("bid_price")
    ask_price = frames.ask_price if frames.ask_price is not None else _frame_last("ask_price")
    bid_qty = frames.bid_qty if frames.bid_qty is not None else _frame_last("bid_qty")
    ask_qty = frames.ask_qty if frames.ask_qty is not None else _frame_last("ask_qty")

    spread_bps = None
    if bid_price is not None and ask_price is not None and bid_price > 0 and ask_price > 0:
        midpoint = (bid_price + ask_price) / 2.0
        if midpoint > 0:
            spread_bps = ((ask_price - bid_price) / midpoint) * 10_000.0
    book_depth_imbalance = depth_imbalance_from_book(
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        delta_ratio=None,
    )
    book_microprice_bias = microprice_bias_from_book(
        bid=bid_price,
        ask=ask_price,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        delta_ratio=None,
    )

    nearest_bid_wall: dict[str, Any] | None = None
    nearest_ask_wall: dict[str, Any] | None = None
    depth_zone_imbalance: dict[str, float] = {}
    book_bids = (
        list(frames.book_bids)
        if frames.book_bids
        else normalize_depth_levels(getattr(frames, "bid_levels", None))
    )
    book_asks = (
        list(frames.book_asks)
        if frames.book_asks
        else normalize_depth_levels(getattr(frames, "ask_levels", None))
    )
    mid_price = None
    if bid_price is not None and ask_price is not None and bid_price > 0 and ask_price > 0:
        mid_price = (bid_price + ask_price) / 2.0
    elif work_15m is not None and not work_15m.is_empty():
        mid_price = _frame_last("close")
    daily_volume = float(universe_symbol.quote_volume or 0.0)
    if mid_price and mid_price > 0 and book_bids and book_asks:
        depth_zone_imbalance = depth_imbalance_by_zone(book_bids, book_asks, mid_price)
        bid_clusters = detect_wall_clusters(
            book_bids,
            current_price=mid_price,
            daily_volume=daily_volume,
            side="bid",
        )
        ask_clusters = detect_wall_clusters(
            book_asks,
            current_price=mid_price,
            daily_volume=daily_volume,
            side="ask",
        )
        if bid_clusters:
            nearest_bid_wall = wall_cluster_to_dict(bid_clusters[0])
        if ask_clusters:
            nearest_ask_wall = wall_cluster_to_dict(ask_clusters[0])

    liquidation_score = _frame_last("liquidation_score")

    work_4h_frame = work_4h if work_4h is not None else pl.DataFrame()
    regime = _market_regime(work_4h_frame, work_1h=work_1h, work_15m=work_15m)
    profile_1h = _volume_profile_with_direction(work_1h, lookback=48, buckets=VP_BUCKETS_DEFAULT)
    profile_15m = _volume_profile_with_direction(
        work_15m, lookback=VP_LOOKBACK_15M, buckets=VP_BUCKETS_DEFAULT
    )
    from hunt_core.features.prepare_frame import factor_panel_from_frames

    factor_panel = factor_panel_from_frames(work_15m, work_1h)

    return PreparedSymbol(
        universe=universe_symbol,
        work_1h=work_1h,
        work_15m=work_15m,
        bid_price=bid_price,
        ask_price=ask_price,
        spread_bps=spread_bps,
        work_5m=work_5m,
        work_4h=work_4h,
        work_primary=primary_work,
        bias_4h=_bias_4h(work_4h_frame),
        bias_1h=_bias_1h(work_1h),  # 1H context for 15M signals
        market_regime=regime,
        structure_1h=_market_structure_1h(work_1h),
        regime_4h_confirmed=_regime_4h_confirmed(work_4h_frame),
        regime_1h_confirmed=_regime_1h_confirmed(work_1h),  # 1H context for 15M signals
        poc_1h=profile_1h[0],
        poc_15m=profile_15m[0],
        poc_direction_1h=profile_1h[3],
        poc_direction_15m=profile_15m[3],
        vah_1h=profile_1h[1],
        val_1h=profile_1h[2],
        vah_15m=profile_15m[1],
        val_15m=profile_15m[2],
        depth_imbalance=book_depth_imbalance,
        microprice_bias=book_microprice_bias,
        nearest_bid_wall=nearest_bid_wall,
        nearest_ask_wall=nearest_ask_wall,
        depth_zone_imbalance=depth_zone_imbalance,
        depth_imbalance_source="rest_book_l1" if book_depth_imbalance is not None else None,
        microprice_bias_source="rest_book_l1" if book_microprice_bias is not None else None,
        liquidation_score=liquidation_score,
        liquidation_score_source="force_order" if liquidation_score is not None else None,
        primary_timeframe=primary_timeframe,
        context_timeframes=context_timeframes,
        settings=settings,
        data_quality_flags=[],
        factor_panel=factor_panel,
    )


__all__ = [
    "PreparedSymbol",
    "_add_advanced_indicators",
    "_cached_prepare_frame",
    "_prepare_frame",
    "_swing_points",
    "cache_stats",
    "min_required_bars",
    "prepare_symbol",
]
