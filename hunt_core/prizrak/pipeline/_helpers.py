from __future__ import annotations

from typing import Any


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def safe_float_opt(val: Any, default: float | None = None) -> float | None:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default
