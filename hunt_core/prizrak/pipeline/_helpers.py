from __future__ import annotations



def safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def safe_float_opt(val: object, default: float | None = None) -> float | None:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default
