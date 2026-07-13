"""Stub chart-pattern detection from confirmed swing points (Phase 6A)."""
from __future__ import annotations



import math
from typing import Any

import polars as pl

from hunt_core.features.prepare import _swing_points

_SWING_N = 3
_EMPTY: dict[str, Any] = {"pattern": None, "confidence": 0.0, "direction": None}


def _slice_work(work: pl.DataFrame, lookback: int) -> pl.DataFrame:
    if work.is_empty():
        return work
    n = max(lookback + _SWING_N + 2, 20)
    if work.height <= n:
        return work
    return work.tail(n)


def _swing_prices(
    work: pl.DataFrame,
    mask: pl.Series,
    *,
    price_col: str,
) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    values = mask.to_list()
    prices = work[price_col].to_list()
    for idx, hit in enumerate(values):
        if not hit:
            continue
        px = prices[idx]
        if px is None or not math.isfinite(float(px)):
            continue
        out.append((idx, float(px)))
    return out


def _confirmed_close(frame: pl.DataFrame) -> float:
    """Last fully closed bar close (exclude forming tail)."""
    if frame.is_empty() or "close" not in frame.columns:
        return 0.0
    idx = frame.height - 2 if frame.height >= 2 else frame.height - 1
    if idx < 0:
        return 0.0
    try:
        return float(frame["close"][idx])
    except (TypeError, ValueError, IndexError):
        return 0.0


def _norm_confidence(score: float) -> float:
    return round(max(0.0, min(1.0, score)), 3)


def detect_double_bottom(work: pl.DataFrame, lookback: int = 50) -> dict[str, Any]:
    """Two similar swing lows with an intervening swing high → long bias."""
    if work is None or work.is_empty() or "low" not in work.columns or "high" not in work.columns:
        return dict(_EMPTY)
    frame = _slice_work(work, lookback)
    # _swing_points returns (swing_HIGH mask, swing_LOW mask). The high mask was
    # mis-unpacked into sh_mask (the low mask both times), so `highs` collected
    # prices at swing-LOW indices → mid_highs between two consecutive swing lows was
    # always empty → double_bottom never fired (FEAT-2). One call, correct order.
    sh_mask, sl_mask = _swing_points(frame, n=_SWING_N, include_unconfirmed_tail=False)
    lows = _swing_prices(frame, sl_mask, price_col="low")
    highs = _swing_prices(frame, sh_mask, price_col="high")
    if len(lows) < 2:
        return dict(_EMPTY)

    first_low_idx, first_low = lows[-2]
    second_low_idx, second_low = lows[-1]
    if second_low_idx <= first_low_idx:
        return dict(_EMPTY)

    mid_highs = [px for idx, px in highs if first_low_idx < idx < second_low_idx]
    if not mid_highs:
        return dict(_EMPTY)

    avg_low = (first_low + second_low) / 2.0
    if avg_low <= 0:
        return dict(_EMPTY)
    low_delta_pct = abs(first_low - second_low) / avg_low
    if low_delta_pct > 0.025:
        return dict(_EMPTY)

    max(mid_highs)
    close = _confirmed_close(frame)
    if close <= 0:
        return dict(_EMPTY)
    recovery = (close - avg_low) / avg_low if close > avg_low else 0.0
    conf = _norm_confidence(0.45 + (0.025 - low_delta_pct) / 0.025 * 0.35 + min(recovery, 0.02) / 0.02 * 0.2)
    return {"pattern": "double_bottom", "confidence": conf, "direction": "long"}


def detect_head_and_shoulders(work: pl.DataFrame, lookback: int = 80) -> dict[str, Any]:
    """Three swing highs: shoulders below a central head → short bias."""
    if work is None or work.is_empty() or "high" not in work.columns:
        return dict(_EMPTY)
    frame = _slice_work(work, lookback)
    sh_mask, _ = _swing_points(frame, n=_SWING_N, include_unconfirmed_tail=False)
    highs = _swing_prices(frame, sh_mask, price_col="high")
    if len(highs) < 3:
        return dict(_EMPTY)

    left_idx, left = highs[-3]
    head_idx, head = highs[-2]
    right_idx, right = highs[-1]
    if not (left_idx < head_idx < right_idx):
        return dict(_EMPTY)
    if head <= left or head <= right:
        return dict(_EMPTY)

    shoulder_avg = (left + right) / 2.0
    if head <= 0:
        return dict(_EMPTY)
    shoulder_delta_pct = abs(left - right) / head
    head_prominence = (head - shoulder_avg) / head
    if shoulder_delta_pct > 0.03 or head_prominence < 0.008:
        return dict(_EMPTY)

    conf = _norm_confidence(
        0.4
        + min(head_prominence, 0.03) / 0.03 * 0.35
        + (0.03 - shoulder_delta_pct) / 0.03 * 0.25
    )
    return {"pattern": "head_and_shoulders", "confidence": conf, "direction": "short"}


def chart_pattern_snapshot(work: pl.DataFrame) -> dict[str, Any]:
    """Run HTF pattern detectors and flatten for TF snapshots."""
    db = detect_double_bottom(work)
    hs = detect_head_and_shoulders(work)
    out: dict[str, Any] = {
        "double_bottom": db if db.get("pattern") else None,
        "head_and_shoulders": hs if hs.get("pattern") else None,
    }
    return out


__all__ = [
    "chart_pattern_snapshot",
    "detect_double_bottom",
    "detect_head_and_shoulders",
]
