"""Volume profile (POC / VAH / VAL) — shared by prepare paths.

Custom Polars histogram (bar-range volume split). ``polars_pbv`` evaluation lives in
Optional POC evaluation helpers live offline only — not on hot path until Prizrak POC parity is confirmed.
"""
from __future__ import annotations



from typing import Any

import polars as pl

VP_BUCKETS_DEFAULT = 20
VP_LOOKBACK_15M = 96
VP_LOOKBACK_1H = 48
VP_VALUE_AREA_PCT = 0.70


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric else None


def volume_profile_levels(
    work: pl.DataFrame,
    *,
    lookback: int | None = None,
    buckets: int = 20,
    value_area_pct: float = 0.70,
) -> tuple[float | None, float | None, float | None]:
    """POC, VAH, VAL from a volume histogram.

    Each bar distributes its volume across every price bucket touched by
    [low, high] (equal split). More accurate than assigning all volume to mid.
    """
    if work.is_empty() or not {"high", "low", "volume"}.issubset(work.columns):
        return None, None, None

    tail = work.tail(lookback) if lookback is not None else work
    if tail.height < 5:
        return None, None, None

    price_min = _as_optional_float(tail["low"].cast(pl.Float64, strict=False).min())
    price_max = _as_optional_float(tail["high"].cast(pl.Float64, strict=False).max())
    if price_min is None or price_max is None or price_max < price_min:
        return None, None, None
    if price_max == price_min:
        # Degenerate flat window: all volume at one price — POC/VAH/VAL collapse to it.
        return price_max, price_max, price_min

    bucket_count = max(1, int(buckets))
    bucket_size = (price_max - price_min) / bucket_count

    bars = tail.select(
        pl.col("high").cast(pl.Float64, strict=False).alias("hi"),
        pl.col("low").cast(pl.Float64, strict=False).alias("lo"),
        pl.col("volume").cast(pl.Float64, strict=False).fill_null(0.0).alias("vol"),
    ).filter(
        (pl.col("vol") > 0)
        & pl.col("hi").is_not_null()
        & pl.col("lo").is_not_null()
        & (pl.col("hi") > 0)
        & (pl.col("lo") > 0)
    )
    if bars.is_empty():
        # No traded volume in the window — a POC fabricated at the range low is not a
        # level anyone traded. Canonical VP is undefined without volume.
        return None, None, None

    hist = (
        bars.with_columns(
            pl.min_horizontal("lo", "hi").alias("price_lo"),
            pl.max_horizontal("lo", "hi").alias("price_hi"),
        )
        .with_columns(
            pl.col("price_lo")
            .sub(price_min)
            .truediv(bucket_size)
            .floor()
            .cast(pl.Int32)
            .alias("b_lo_raw"),
            pl.col("price_hi")
            .sub(price_min)
            .truediv(bucket_size)
            .floor()
            .cast(pl.Int32)
            .alias("b_hi_raw"),
        )
        .with_columns(
            pl.max_horizontal(
                pl.lit(0), pl.min_horizontal(pl.lit(bucket_count - 1), pl.col("b_lo_raw"))
            ).alias("b_lo"),
            pl.max_horizontal(
                pl.lit(0), pl.min_horizontal(pl.lit(bucket_count - 1), pl.col("b_hi_raw"))
            ).alias("b_hi"),
        )
        .with_columns(
            (pl.col("vol") / (pl.col("b_hi") - pl.col("b_lo") + 1)).alias("share"),
        )
        .with_columns(pl.int_ranges(pl.col("b_lo"), pl.col("b_hi") + 1).alias("b"))
        .explode("b", empty_as_null=True)  # keep pre-Polars-2.0 behavior (default flips to False)
        .group_by("b")
        .agg(pl.col("share").sum().alias("v"))
    )
    if hist.is_empty():
        return None, None, None

    total_volume = float(hist["v"].sum())
    if total_volume <= 0.0:
        return None, None, None

    vol_by_bucket = dict(zip(hist["b"].to_list(), hist["v"].to_list(), strict=True))
    # POC = max-volume bucket. Exact ties are COMMON here (one dominating bar equal-split
    # across N buckets produces N identical values) and `sort().head(1)` resolved them by
    # group_by hash order — the POC of the same bars flipped between runs. Canonical
    # Market Profile tie rule: of the tied buckets, take the one closest to the centre of
    # the range (lower bucket on a residual tie), deterministically.
    max_v = max(vol_by_bucket.values())
    tied = [b for b, v in vol_by_bucket.items() if v == max_v]
    center = (bucket_count - 1) / 2.0
    poc_bucket = min(tied, key=lambda b: (abs(b - center), b))
    poc = float(price_min + (poc_bucket + 0.5) * bucket_size)

    target_volume = total_volume * max(0.5, min(value_area_pct, 0.95))
    accumulated = float(vol_by_bucket[poc_bucket])
    included = {poc_bucket}
    left = poc_bucket - 1
    right = poc_bucket + 1
    while accumulated < target_volume and (left >= 0 or right < bucket_count):
        left_vol = vol_by_bucket.get(left, 0.0) if left >= 0 else 0.0
        right_vol = vol_by_bucket.get(right, 0.0) if right < bucket_count else 0.0
        if right_vol >= left_vol and right < bucket_count:
            accumulated += right_vol
            included.add(right)
            right += 1
        elif left >= 0:
            accumulated += left_vol
            included.add(left)
            left -= 1
        else:
            break

    val = float(price_min + min(included) * bucket_size)
    vah = float(price_min + (max(included) + 1) * bucket_size)
    return poc, vah, val


def classify_poc_direction(
    work: pl.DataFrame,
    poc: float | None,
    vah: float | None,
    val: float | None,
    *,
    lookback: int | None = None,
) -> str | None:
    """POC exit direction: break above VAH → long level; below VAL → short level."""
    if poc is None or vah is None or val is None or work.is_empty() or "close" not in work.columns:
        return None
    tail = work.tail(lookback) if lookback is not None else work
    if tail.height < 3:
        return None
    close = _as_optional_float(tail["close"].cast(pl.Float64, strict=False).tail(1).item())
    if close is None:
        return None
    if close > vah:
        return "long"
    if close < val:
        return "short"
    return None


def volume_profile_with_direction(
    work: pl.DataFrame,
    *,
    lookback: int | None = None,
    buckets: int = 20,
    value_area_pct: float = 0.70,
) -> tuple[float | None, float | None, float | None, str | None]:
    """POC/VAH/VAL plus methodology exit-direction classification."""
    poc, vah, val = volume_profile_levels(
        work, lookback=lookback, buckets=buckets, value_area_pct=value_area_pct
    )
    direction = classify_poc_direction(work, poc, vah, val, lookback=lookback)
    return poc, vah, val, direction


def _touches_boundary(
    hi: float,
    lo: float,
    boundary: float,
    *,
    tolerance_pct: float,
) -> bool:
    if boundary <= 0:
        return False
    tol = boundary * tolerance_pct
    return abs(hi - boundary) <= tol or abs(lo - boundary) <= tol


def count_range_touches_from_bars(
    bars: list[tuple[float, float]],
    *,
    range_high: float,
    range_low: float,
    tolerance_pct: float = 0.003,
) -> tuple[int, int, int]:
    """Count bar touches within tolerance_pct of range_high / range_low."""
    high_touches = 0
    low_touches = 0
    for hi, lo in bars:
        if hi <= 0 or lo <= 0:
            continue
        if _touches_boundary(hi, lo, range_high, tolerance_pct=tolerance_pct):
            high_touches += 1
        if _touches_boundary(hi, lo, range_low, tolerance_pct=tolerance_pct):
            low_touches += 1
    return high_touches, low_touches, high_touches + low_touches


def count_range_touches(
    work: pl.DataFrame,
    *,
    range_high: float,
    range_low: float,
    tolerance_pct: float = 0.003,
    lookback: int | None = None,
) -> tuple[int, int, int]:
    """Count OHLC bar touches within tolerance_pct of range boundaries (Phase 4C)."""
    if work.is_empty() or not {"high", "low"}.issubset(work.columns):
        return 0, 0, 0
    if range_high <= 0 or range_low <= 0:
        return 0, 0, 0

    tail = work.tail(lookback) if lookback is not None else work
    bars: list[tuple[float, float]] = []
    for h, l in zip(
        tail["high"].cast(pl.Float64, strict=False).to_list(),
        tail["low"].cast(pl.Float64, strict=False).to_list(),
        strict=False,
    ):
        hi = _as_optional_float(h)
        lo = _as_optional_float(l)
        if hi is not None and lo is not None and hi > 0 and lo > 0:
            bars.append((hi, lo))
    return count_range_touches_from_bars(
        bars,
        range_high=range_high,
        range_low=range_low,
        tolerance_pct=tolerance_pct,
    )


__all__ = [
    "VP_BUCKETS_DEFAULT",
    "VP_LOOKBACK_15M",
    "VP_LOOKBACK_1H",
    "VP_VALUE_AREA_PCT",
    "classify_poc_direction",
    "count_range_touches",
    "count_range_touches_from_bars",
    "volume_profile_levels",
    "volume_profile_with_direction",
]
