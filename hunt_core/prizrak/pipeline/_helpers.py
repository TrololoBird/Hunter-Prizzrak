"""Numeric coercion helpers for the prizrak structure pipeline."""
from __future__ import annotations

import math
from typing import Any


def safe_float(val: Any, default: float = 0.0) -> float:
    """Coerce to a FINITE float, else ``default``.

    The isfinite check is the point: every other safe_float in the codebase rejects
    inf/nan, and this one — the one feeding _detect_structure — did not. A single broken
    bar (an inf high, a nan close) therefore flowed straight into the level structure and
    poisoned every downstream comparison it touched, silently. Non-finite is not a number
    you can put a stop behind.
    """
    if val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def safe_float_opt(val: Any, default: float | None = None) -> float | None:
    """Same, but ``default`` (typically None) also stands in for a non-finite value."""
    if val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default
