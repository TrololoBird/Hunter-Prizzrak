"""Ловушки — прокол (wick beyond + return, still a valid level reaction) vs пробой
(closed bodies beyond, level flips side / breaks).

Course definition (словарь трейдера): прокол = цена прошла за уровень и вернулась той
же/следующей 1-2 свечами — counts as a worked reaction, level stays valid. Пробой =
цена прошла и ОСТАЁТСЯ за уровнем, requires close confirmation — level flips to the
opposite side. Reuses ``pp.confirmation_bodies`` for the body-count side of the check.
"""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.pp import confirmation_bodies


def classify_level_touch(
    bars: list[dict[str, float]],
    *,
    level: float,
    side: Literal["short", "long"],
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Classify the most recent touch of ``level``.

    ``side="short"`` means the level is resistance (we're checking a wick/close ABOVE
    it that then returns/holds); ``side="long"`` means support (wick/close BELOW).
    Returns {} if the level hasn't been touched recently at all.
    """
    cfg = cfg or PrizrakConfig.load()
    if not bars:
        return {}

    # Polarity note: traps.py's `side` names the level type (short=resistance watched
    # from below, long=support watched from above). pp.confirmation_bodies' `side`
    # names the BREAK direction (short=counts closes BELOW, long=counts closes ABOVE).
    # A broken resistance (our side="short") means closes ABOVE it -> pass "long".
    body_side: Literal["short", "long"] = "long" if side == "short" else "short"
    bodies = confirmation_bodies(bars, level=level, side=body_side)
    if bodies >= cfg.trap_proboy_min_bodies:
        return {"kind": "proboy", "bodies": bodies, "level": level, "side": side}

    # Прокол check: within the last N bars, did a wick cross the level while that
    # bar's own close stayed back on the original side?
    window = bars[-cfg.trap_prokol_max_bars:]
    for b in window:
        wicked = (b["high"] > level) if side == "short" else (b["low"] < level)
        held = (b["close"] <= level) if side == "short" else (b["close"] >= level)
        if wicked and held:
            return {"kind": "prokol", "bodies": bodies, "level": level, "side": side}

    if bodies > 0:
        return {"kind": "testing", "bodies": bodies, "level": level, "side": side}
    return {}


__all__ = ["classify_level_touch"]
