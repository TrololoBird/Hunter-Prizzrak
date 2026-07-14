"""Shared helpers for the surviving Deep-delivery infra (signal_queue.py, activation.py).

Trimmed (2026-07): ``conviction``, ``dominant_side``, ``direction_bias``,
``trend_scores_from_snap``, ``information_value_from_z``, ``coverage_ratio``,
``atr_from_row``, ``pct_move``, ``rr_ratio`` were removed — all exclusively fed the
deleted L0-L5 engine (``core.py``/``blender.py``/``plan.py``/``levels.py``, all gone).
Confirmed zero external callers by grep before removal, not assumed.
"""
from __future__ import annotations

# safe_float is the SAME finite-coercion helper the structure pipeline uses; both live
# inside prizrak/ and pipeline/_helpers is a leaf (no prizrak imports), so re-exporting it
# here removes the byte-identical duplicate the audit flagged (G-27) without an import cycle.
# The cross-boundary copies (market/, scanner/, data/) stay separate on purpose: a spine
# util everyone imports would couple the two strategies through it for a 6-line function.
from hunt_core.prizrak.pipeline._helpers import safe_float


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


__all__ = ["clamp01", "safe_float"]
