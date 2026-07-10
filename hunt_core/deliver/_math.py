"""Shared delivery math helpers — percentage and entry-edge calculations."""
from __future__ import annotations

from typing import Any


def pct_str(entry: float, target: float | None, direction: str) -> str:
    """Move % from worst-fill entry edge to target."""
    if not entry or not target:
        return ""
    if direction == "short":
        pct = (entry - float(target)) / entry * 100.0
    else:
        pct = (float(target) - entry) / entry * 100.0
    return f"{pct:+.1f}%"


def risk_pct_str(entry: float, stop: float | None, direction: str) -> str:
    """Downside % from worst-fill entry to stop."""
    if not entry or stop is None:
        return ""
    if direction == "short":
        pct = (float(stop) - entry) / entry * 100.0
    else:
        pct = (entry - float(stop)) / entry * 100.0
    return f"{pct:+.1f}%"


def worst_entry_from_setup(setup: dict[str, Any], *, direction: str, price: float) -> float:
    from hunt_core.contract import worst_entry_edge

    edge = worst_entry_edge(setup, direction=direction)
    if edge is not None and edge > 0:
        return edge
    return price
