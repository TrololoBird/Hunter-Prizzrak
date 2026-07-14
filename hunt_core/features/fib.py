"""Fibonacci anchors and retracement levels (§H)."""
from __future__ import annotations

from hunt_core.levels.levels import fib_retracement_levels

FIB_RATIOS: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618)


def leg_fib_levels(high: float, low: float, *, direction: str = "up") -> dict[str, float]:
    """Retracement/extension levels for a price leg.

    ``up``   — leg ran low→high: retracements pull price DOWN into the leg from the
               high; extensions sit ABOVE the high (continuation up).
    ``down`` — leg ran high→low: retracements bounce UP from the low; extensions sit
               BELOW the low (continuation down).

    The ``down`` branch previously returned the up-leg dict verbatim, so ``direction``
    was a no-op: retracement levels were mirrored (a down-leg 38.2% retrace is the
    up-leg 61.8% level) and extension targets sat above the high instead of below the
    low — nonsensical for the short setups that are the only live callers.
    """
    if direction != "down":
        return fib_retracement_levels(high, low)
    leg = high - low
    return {
        "ext_1272": round(low - leg * 0.272, 6),
        "ext_1618": round(low - leg * 0.618, 6),
        "ret_236": round(low + leg * 0.236, 6),
        "ret_382": round(low + leg * 0.382, 6),
        "ret_50": round(low + leg * 0.5, 6),
        "ret_618": round(low + leg * 0.618, 6),
    }


__all__ = ["FIB_RATIOS", "fib_retracement_levels", "leg_fib_levels"]
