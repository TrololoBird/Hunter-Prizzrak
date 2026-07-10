"""Shared helpers for the surviving Deep-delivery infra (signal_queue.py, activation.py).

Trimmed (2026-07): ``conviction``, ``dominant_side``, ``direction_bias``,
``trend_scores_from_snap``, ``information_value_from_z``, ``coverage_ratio``,
``atr_from_row``, ``pct_move``, ``rr_ratio`` were removed — all exclusively fed the
deleted L0-L5 engine (``core.py``/``blender.py``/``plan.py``/``levels.py``, all gone).
Confirmed zero external callers by grep before removal, not assumed.
"""
from __future__ import annotations

import math
from typing import Any


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


__all__ = ["clamp01", "safe_float"]
