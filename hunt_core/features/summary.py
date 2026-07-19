"""Typed per-TF summaries (ADR-0004 S3) — :class:`TfSummary` from a prepared frame.

Reuses the existing ``tf_snapshot`` computation (all its indicator/divergence/candle logic) and
projects it onto the frozen typed :class:`TfSummary` — parity-preserving (same numbers as today), so
this is a data-*shape* change, not a behaviour change. ``extra="forbid"`` means only declared fields
survive; an absent frame yields ``None`` (fail-loud, I-6). With full-fidelity klines (real taker), the
CVD / ``delta_ratio`` fields now carry real values rather than the old zero-fill.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.features.models import CandleShape, TfSummary
from hunt_core.features.snapshot import tf_snapshot

_TF_FIELDS = frozenset(TfSummary.model_fields) - {"candle"}
_CANDLE_FIELDS = frozenset(CandleShape.model_fields)


def tf_summary(
    df: pl.DataFrame | None,
    *,
    rsi_trendline: bool = False,
    hidden_stoch_div: bool = False,
    candle_patterns: bool = False,
) -> TfSummary | None:
    """Typed newest-closed-bar summary of a prepared frame, or ``None`` if the frame is absent/empty."""
    if df is None or df.is_empty():
        return None
    snap = tf_snapshot(
        df,
        rsi_trendline=rsi_trendline,
        hidden_stoch_div=hidden_stoch_div,
        candle_patterns=candle_patterns,
    )
    if snap.get("status") == "empty":
        return None
    data: dict[str, Any] = {}
    for key in _TF_FIELDS:
        val = snap.get(key)
        if val is None:
            continue
        # tf_snapshot rounds to floats; coerce the two declared-int fields so strict validation holds.
        if key in ("supertrend_dir", "close_time_ms"):
            data[key] = int(val)
        elif isinstance(val, bool):
            data[key] = val
        elif isinstance(val, (int, float)):
            data[key] = float(val)
        else:
            data[key] = val
    candle = snap.get("candle")
    if isinstance(candle, dict):
        data["candle"] = CandleShape(
            **{k: candle[k] for k in _CANDLE_FIELDS if candle.get(k) is not None}
        )
    return TfSummary(**data)


__all__ = ["tf_summary"]
