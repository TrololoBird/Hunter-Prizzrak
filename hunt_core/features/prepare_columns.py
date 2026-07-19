"""Prepare-frame columns, alignment, structure, latch, and frame fallbacks."""
from __future__ import annotations



import math
from typing import TYPE_CHECKING, Any

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Iterable

# Groups map to blocks inside _add_advanced_indicators and tail pipeline stages.
ALL_PREPARE_GROUPS: frozenset[str] = frozenset(
    {
        "supertrend",
        "obv",
        "bb",
        "keltner",
        "hma",
        "psar",
        "aroon",
        "stoch",
        "oscillators",
        "fisher",
        "squeeze",
        "chandelier",
        "volume_profile",
        "zscore",
        "candles",
        "microstructure",
        "ols",
        "session",
        "tail_metrics",
        "stoch_rsi",
        "ichimoku",
        "kama",
        "heikin_ashi",
        "pivot_points",
        "polars_ta_extended",
        "polars_wq_features",
    }
)

# Single source of truth — the config-driven pinned set from data/universe.py. Was a
# hardcoded duplicate here; if an operator added an 8th analyst asset in config, universe
# picked it up but this frozenset did not, and the new anchor silently got the LEAN prepare
# (no extended columns) while universe's consumers wrote it as a continuous pinned symbol.
from hunt_core.data.universe import PINNED_SYMBOLS as _UNIVERSE_PINNED

PINNED_SYMBOLS: frozenset[str] = frozenset(_UNIVERSE_PINNED)

# Not referenced by strategies or enrichment telemetry — safe to skip on live path.
LIVE_SKIPPABLE_GROUPS: frozenset[str] = frozenset(
    {
        "psar",
        "aroon",
        "hma",
        "stoch_rsi",
        "ichimoku",
        "kama",
        "heikin_ashi",
        "pivot_points",
        "polars_wq_features",
        "fisher",
    }
)

GROUP_DEPENDENCIES: dict[str, frozenset[str]] = {
    "squeeze": frozenset({"bb", "keltner"}),
}

# Columns owned by skippable groups (audit completeness for lean live path).
SKIPPABLE_GROUP_COLUMNS: frozenset[str] = frozenset(
    {
        "aroon_down14",
        "aroon_osc14",
        "aroon_up14",
        "chikou",
        "hma21",
        "hma9",
        "kama10",
        "pivot_point",
        "pivot_r1",
        "pivot_r2",
        "pivot_s1",
        "pivot_s2",
        "psar_long",
        "psar_reversal",
        "psar_short",
        "senkou_a",
        "senkou_b",
        "stoch_rsi14",
        "tenkan",
        "kijun",
        "volume_profile",
        "volume_profile_vah",
        "volume_profile_val",
    }
)

STRATEGY_PREPARE_GROUPS: dict[str, frozenset[str]] = {
    "squeeze_setup": frozenset(
        {
            "squeeze",
            "bb",
            "keltner",
            "supertrend",
            "obv",
            "stoch",
            "oscillators",
            "zscore",
            "candles",
        }
    ),
    "bb_squeeze": frozenset({"squeeze", "bb", "keltner", "zscore", "obv"}),
    "wick_trap_reversal": frozenset({"supertrend", "stoch", "oscillators", "candles"}),
    "liquidity_sweep": frozenset({"supertrend", "bb", "candles"}),
    "order_block": frozenset({"bb", "keltner", "candles"}),
    "breaker_block": frozenset({"bb", "keltner", "candles"}),
    "stop_hunt_detection": frozenset({"supertrend", "bb", "candles"}),
    "volume_climax_reversal": frozenset({"oscillators", "stoch", "candles"}),
    "volume_anomaly": frozenset({"oscillators", "bb", "candles"}),
    "supertrend_follow": frozenset({"supertrend", "obv", "ichimoku", "heikin_ashi"}),
    "price_velocity": frozenset({"zscore", "ols"}),
    "keltner_breakout": frozenset({"keltner", "bb", "squeeze"}),
    "multi_tf_trend": frozenset({"ichimoku"}),
    "hidden_divergence": frozenset({"stoch_rsi"}),
    "indicator_divergence": frozenset({"stoch_rsi"}),
    "ema_bounce": frozenset({"heikin_ashi", "kama"}),
    "vwap_trend": frozenset({"heikin_ashi"}),
    "atr_expansion": frozenset({"obv"}),
}

_BASE_LIVE_GROUPS: frozenset[str] = frozenset(
    {
        "supertrend",
        "obv",
        "bb",
        "keltner",
        "stoch",
        "oscillators",
        "fisher",
        "squeeze",
        "chandelier",
        "zscore",
        "candles",
        "microstructure",
        "ols",
        "session",
        "tail_metrics",
    }
)


def _expand_group_dependencies(groups: Iterable[str]) -> frozenset[str]:
    expanded = set(groups)
    changed = True
    while changed:
        changed = False
        for group in tuple(expanded):
            for dependency in GROUP_DEPENDENCIES.get(group, frozenset()):
                if dependency not in expanded:
                    expanded.add(dependency)
                    changed = True
    return frozenset(expanded)


def resolve_hunt_live_groups() -> frozenset[str]:
    """Lean indicator set for Hunter live ticks (confirm + gate + lifecycle)."""
    return _expand_group_dependencies(_BASE_LIVE_GROUPS - LIVE_SKIPPABLE_GROUPS)


def resolve_prepare_groups_for_symbol(symbol: str) -> frozenset[str] | None:
    """Pinned anchors get full prepare + polars_ta extended; alts stay lean."""
    sym = str(symbol or "").strip().upper()
    if sym in PINNED_SYMBOLS:
        return _expand_group_dependencies(ALL_PREPARE_GROUPS)
    return resolve_hunt_live_groups()


def resolve_prepare_groups(
    enabled_setup_ids: tuple[str, ...] | None,
    *,
    full: bool = False,
) -> frozenset[str] | None:
    """Return active indicator groups for the live prepare path.

    ``None`` means compute every group (backtests / tests).
    """
    if full or not enabled_setup_ids:
        return None

    groups = set(_BASE_LIVE_GROUPS)
    for setup_id in enabled_setup_ids:
        groups |= STRATEGY_PREPARE_GROUPS.get(setup_id, _BASE_LIVE_GROUPS)

    groups -= LIVE_SKIPPABLE_GROUPS
    return _expand_group_dependencies(groups)


def group_active(active_groups: frozenset[str] | None, group: str) -> bool:
    return active_groups is None or group in active_groups


def expected_indicator_columns_for_symbol(
    symbol: str,
    *,
    full_columns: frozenset[str],
) -> frozenset[str]:
    """Indicator columns expected after prepare for this symbol's active groups."""
    sym = symbol.strip().upper()
    if sym in PINNED_SYMBOLS:
        return full_columns
    return full_columns - SKIPPABLE_GROUP_COLUMNS


# ---------------------------------------------------------------------------
# Time-series alignment (ex align.py)
# ---------------------------------------------------------------------------


def align_series_to_klines(
    left: pl.DataFrame,
    right: pl.DataFrame,
    *,
    on: str = "time",
    left_cols: tuple[str, ...] | None = None,
    right_cols: tuple[str, ...] | None = None,
) -> pl.DataFrame:
    """Backward as-of join of two OHLCV-like frames on ``on`` timestamp."""
    if left.is_empty() or right.is_empty():
        return pl.DataFrame()
    l_on = on if on in left.columns else "open_time"
    r_on = on if on in right.columns else "open_time"
    l_pick = [c for c in (left_cols or left.columns) if c in left.columns]
    r_pick = [c for c in (right_cols or right.columns) if c in right.columns and c != r_on]
    ldf = left.select([l_on, *l_pick]).sort(l_on)
    rdf = right.select([r_on, *r_pick]).sort(r_on)
    if l_on != r_on:
        rdf = rdf.rename({r_on: l_on})
    return ldf.join_asof(rdf, on=l_on, strategy="backward")


def attach_metric_series(
    klines: pl.DataFrame,
    values: list[float],
    *,
    time_col: str = "time",
    value_name: str = "metric",
) -> pl.DataFrame:
    """Align a simple float series to kline rows by index (same length tail)."""
    if klines.is_empty() or not values:
        return klines
    n = min(klines.height, len(values))
    if n <= 0:
        return klines
    metric = pl.Series(value_name, values[-n:])
    base = klines.tail(n)
    return base.with_columns(metric)


def join_derivative_series_asof(
    klines: pl.DataFrame,
    values: list[float] | None,
    *,
    timestamps: list[int] | None = None,
    value_name: str = "oi",
) -> pl.DataFrame:
    """Align OI/GLS 5m series to klines — join_asof when timestamps present."""
    if klines.is_empty() or not values:
        return klines
    on = "open_time" if "open_time" in klines.columns else "time"
    if timestamps and len(timestamps) == len(values) and on in klines.columns:
        rdf = pl.DataFrame({on: timestamps, value_name: values})
        keep = [c for c in klines.columns if c != on]
        return align_series_to_klines(
            klines.select([on, *keep]),
            rdf,
            on=on,
            right_cols=(value_name,),
        )
    return attach_metric_series(klines, values, value_name=value_name)


# ---------------------------------------------------------------------------
# Structure indicators (ex structure.py)
# ---------------------------------------------------------------------------


def ichimoku_lines(
    df: pl.DataFrame,
) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series]:
    tenkan = (
        (df["high"].rolling_max(window_size=9) + df["low"].rolling_min(window_size=9)) / 2.0
    ).rename("tenkan")
    kijun = (
        (df["high"].rolling_max(window_size=26) + df["low"].rolling_min(window_size=26)) / 2.0
    ).rename("kijun")
    senkou_a = (((tenkan + kijun) / 2.0).shift(26)).rename("senkou_a")
    senkou_b = (
        ((df["high"].rolling_max(window_size=52) + df["low"].rolling_min(window_size=52)) / 2.0)
        .shift(26)
        .rename("senkou_b")
    )
    return tenkan, kijun, senkou_a, senkou_b


def weighted_moving_average(series: pl.Series, period: int, *, name: str) -> pl.Series:
    """Linear-weighted MA ``sum(w·x)/sum(w)`` with weights ``[1..n]`` (newest bar weight ``n``).

    Implemented as a **null-safe** shift-based weighted sum, NOT ``rolling_mean(weights=…)``: Polars'
    weighted rolling panics ("weights not yet supported on array with null values") on ANY null in the
    input — and HMA feeds it ``2·wma_half − wma_full``, which always carries leading warm-up nulls. The
    shift form is arithmetically identical where the window is null-free and propagates a null for any
    window containing one (matching ``min_samples=n``), so a partial/warm-up window yields ``None`` and
    HMA no longer crashes.
    """
    n = max(1, int(period))
    weight_sum = float(n * (n + 1) // 2)
    acc: pl.Series | None = None
    for k in range(n):  # shift k: element x[i-k]; newest (k=0) weight n, oldest (k=n-1) weight 1
        term = series.shift(k) * float(n - k)
        acc = term if acc is None else acc + term
    assert acc is not None  # n >= 1
    return (acc / weight_sum).fill_nan(None).rename(name)


def hull_moving_average(close: pl.Series, period: int = 21, *, name: str = "hma21") -> pl.Series:
    period = max(2, int(period))
    half = max(1, period // 2)
    sqrt_n = max(1, int(math.sqrt(period)))

    wma_half = weighted_moving_average(close, half, name=f"{name}_half")
    wma_full = weighted_moving_average(close, period, name=f"{name}_full")
    raw = (2.0 * wma_half - wma_full).rename(f"{name}_raw")
    return weighted_moving_average(raw, sqrt_n, name=name)


# ---------------------------------------------------------------------------
# Young-listing frame fallbacks (ex frame_fallback.py)
# ---------------------------------------------------------------------------

MIN_1H_BARS_FOR_SYNTH = 48
MIN_SYNTH_4H_BARS = 12
THIN_4H_RAW_BARS = 100


def synth_4h_from_1h(df_1h: Any) -> pl.DataFrame | None:
    """Roll up 1h OHLCV into synthetic 4h bars (4×1h) for indicator warmup."""
    if df_1h is None:
        return None
    if hasattr(df_1h, "is_empty") and df_1h.is_empty():
        return None
    if not isinstance(df_1h, pl.DataFrame):
        df_1h = pl.DataFrame(df_1h)
    if df_1h.height < MIN_1H_BARS_FOR_SYNTH:
        return None
    sort_col = "open_time" if "open_time" in df_1h.columns else None
    work = df_1h.sort(sort_col) if sort_col else df_1h
    agg: list[pl.Expr] = [
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
    ]
    if "volume" in work.columns:
        agg.append(pl.col("volume").sum())
    if sort_col:
        agg.insert(0, pl.col(sort_col).first())
    out = (
        work.with_row_index("_i")
        .with_columns((pl.col("_i") // 4).alias("_g"))
        .group_by("_g")
        .agg(agg)
        .sort(sort_col if sort_col else "_g")
        .drop("_g", strict=False)
    )
    return out if out.height >= MIN_SYNTH_4H_BARS else None


def _work_empty(work: Any) -> bool:
    if work is None:
        return True
    if hasattr(work, "is_empty"):
        return bool(work.is_empty())
    return False


def patch_work_4h(prepared: Any, kline_map: dict[str, Any], *, symbol: str = "") -> bool:
    """Attach synth or raw 4h work frame when native work_4h is missing."""
    if not _work_empty(getattr(prepared, "work_4h", None)):
        return False
    df_1h = kline_map.get("1h")
    synth = synth_4h_from_1h(df_1h)
    if synth is None:
        return False
    groups = resolve_prepare_groups_for_symbol(symbol) if symbol else None
    from hunt_core.features.prepare_frame import _prepare_frame

    work = _prepare_frame(synth, active_groups=groups)
    if _work_empty(work):
        work = synth
    setattr(prepared, "work_4h", work)
    return True


def should_use_young_lite_path(*, bars_4h: int, bars_1h: int) -> bool:
    """Skip doomed relaxed prepare when 4h history is thin but 1h is usable."""
    return bars_1h >= MIN_1H_BARS_FOR_SYNTH and bars_4h < THIN_4H_RAW_BARS


def should_bypass_kline_integrity(*, bars_4h: int, bars_1h: int, bars_15m: int = 0) -> bool:
    """Young/partial listings — do not hard-reject before lite prepare path."""
    if should_use_young_lite_path(bars_4h=bars_4h, bars_1h=bars_1h):
        return True
    if bars_1h >= MIN_1H_BARS_FOR_SYNTH and bars_15m >= 96 and bars_4h < THIN_4H_RAW_BARS * 2:
        return True
    return False


def violations_are_partial_history_only(violations: list[str] | tuple[str, ...]) -> bool:
    """Allow lite prepare when gaps are only ``min_raw`` / ``min_prepared`` shortfalls."""
    if not violations:
        return False
    for raw in violations:
        v = str(raw or "")
        if "stale" in v or "fetch" in v or "unavailable" in v or "missing_column" in v:
            return False
        if "<min_raw=" not in v and "<min_prepared=" not in v:
            return False
    return True


# ---------------------------------------------------------------------------
# Feature-vector latching (ex latch.py)
# ---------------------------------------------------------------------------

TOP_BOOK_WALL_LEVELS = 5


def feature_vector_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Compact labelled feature snapshot from a watch tick row."""
    market = row.get("market") or row.get("positioning") or {}
    regime = row.get("regime") or {}
    lifecycle = row.get("lifecycle") or {}
    session = row.get("session") or {}
    m = market if isinstance(market, dict) else {}
    liq_long = m.get("liquidation_long_notional_5m")
    liq_short = m.get("liquidation_short_notional_5m")
    liq_magnitude = None
    if liq_long is not None or liq_short is not None:
        liq_magnitude = float(liq_long or 0.0) + float(liq_short or 0.0)
    return {
        "ts": row.get("ts"),
        "price": row.get("price"),
        "market": dict(market) if isinstance(market, dict) else {},
        "regime": dict(regime) if isinstance(regime, dict) else {},
        "lifecycle_phase": lifecycle.get("phase"),
        "lifecycle_bias": lifecycle.get("recommended_bias"),
        "fall_from_high_pct": lifecycle.get("fall_from_high_pct"),
        "bounce_from_low_pct": lifecycle.get("bounce_from_low_pct"),
        "pos_in_range": session.get("pos_in_range"),
        "oi_slope_5m": m.get("oi_slope_5m"),
        "oi_chg_5m": m.get("oi_chg_5m"),
        "oi_z": m.get("oi_z"),
        "basis_pct": m.get("basis_pct"),
        "premium_slope_5m": m.get("premium_slope_5m"),
        "premium_zscore_5m": m.get("premium_zscore_5m"),
        "spot_lead_return_1m": m.get("spot_lead_return_1m"),
        "spot_futures_spread_bps": m.get("spot_futures_spread_bps"),
        "spot_quote_volume_24h": m.get("spot_quote_volume_24h"),
        "liquidation_score_5m": m.get("liquidation_score_5m"),
        "liquidation_long_notional_5m": liq_long,
        "liquidation_short_notional_5m": liq_short,
        "liquidation_magnitude_5m": liq_magnitude,
    }


def book_walls_from_depth(
    depth: dict[str, Any] | None,
    *,
    top_n: int = TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any] | None:
    """Top-N bid/ask notional walls from REST depth snapshot."""
    if not isinstance(depth, dict) or depth.get("bid_price") is None:
        return None
    bid_levels = depth.get("bid_levels")
    ask_levels = depth.get("ask_levels")

    def _norm(levels: Any) -> list[dict[str, Any]]:
        if not isinstance(levels, list):
            return []
        out: list[dict[str, Any]] = []
        for item in levels[:top_n]:
            if isinstance(item, dict):
                out.append(dict(item))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                p, q = float(item[0]), float(item[1])
                out.append({"price": p, "qty": q, "notional_usd": round(p * q, 2)})
        return out

    walls: dict[str, Any] = {
        "bid_price": depth.get("bid_price"),
        "ask_price": depth.get("ask_price"),
        "bid_levels": _norm(bid_levels),
        "ask_levels": _norm(ask_levels),
        "exchange": depth.get("exchange") or "binance",
    }
    if not walls["bid_levels"] and not walls["ask_levels"]:
        walls["note"] = "aggregated_l1_only"
    return walls


def book_walls_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("book_walls")
    return dict(raw) if isinstance(raw, dict) else None


__all__ = [
    "ALL_PREPARE_GROUPS",
    "GROUP_DEPENDENCIES",
    "LIVE_SKIPPABLE_GROUPS",
    "MIN_1H_BARS_FOR_SYNTH",
    "MIN_SYNTH_4H_BARS",
    "PINNED_SYMBOLS",
    "SKIPPABLE_GROUP_COLUMNS",
    "STRATEGY_PREPARE_GROUPS",
    "THIN_4H_RAW_BARS",
    "TOP_BOOK_WALL_LEVELS",
    "align_series_to_klines",
    "attach_metric_series",
    "book_walls_from_depth",
    "book_walls_from_row",
    "expected_indicator_columns_for_symbol",
    "feature_vector_from_row",
    "group_active",
    "hull_moving_average",
    "ichimoku_lines",
    "patch_work_4h",
    "resolve_hunt_live_groups",
    "resolve_prepare_groups",
    "resolve_prepare_groups_for_symbol",
    "should_bypass_kline_integrity",
    "should_use_young_lite_path",
    "violations_are_partial_history_only",
    "synth_4h_from_1h",
    "weighted_moving_average",
]
