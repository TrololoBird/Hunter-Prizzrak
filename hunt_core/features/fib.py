"""Fibonacci anchors and retracement levels (§H)."""
from __future__ import annotations

from hunt_core.levels.levels import fib_retracement_levels

FIB_RATIOS: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618)


def leg_fib_levels(high: float, low: float, *, direction: str = "up") -> dict[str, float]:
    """Retracement/extension levels for a price leg."""
    base = fib_retracement_levels(high, low)
    if direction == "down":
        return {k: v for k, v in base.items()}
    return base


__all__ = ["FIB_RATIOS", "fib_retracement_levels", "leg_fib_levels"]
