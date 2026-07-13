"""Invalidation conditions builder — per-scenario "what kills this trade" logic.

Uses structural levels and volume context to generate concrete invalidation rules:
- Level breach: if price closes below/above a key level, scenario is invalid
- Volume confirmation: if a close through the level is backed by high volume, reject
- Opposite event: if a conflicting event fires, scenario is invalid
"""

from __future__ import annotations

from typing import Any


def build_invalidation(
    *,
    direction: str,
    entry_lo: float,
    entry_hi: float,
    stop: float,
    catalyst_level: float,
    zone: dict[str, Any] | None = None,
    swing_highs: list[float] | None = None,
    swing_lows: list[float] | None = None,
    entry_tf: str = "15m",
) -> list[dict[str, str]]:
    """Build invalidation conditions for a scenario.

    Returns a list of {"condition": str, "reason": str} dicts.
    """
    conditions: list[dict[str, str]] = []

    if direction == "long":
        # Primary: entry zone structural low breach
        invalidation_level = entry_lo * 0.995  # 0.5% below entry band lo
        if stop and stop < entry_lo:
            invalidation_level = stop

        conditions.append({
            "condition": f"{entry_tf} close below {_fmt_price(invalidation_level)}",
            "reason": "нарушение структурного уровня входа",
        })

        # (Removed) A "nearest swing-low breach" secondary was dead code: it read
        # swing_lows, but every caller populates swing_highs for a long / swing_lows
        # for a short (TP-direction swings from _extract_swing_levels, i.e. AHEAD of
        # entry), so the long branch always saw None — and even the intended data
        # (a swing LOW *below* entry) is never produced. See PRIZRAK-1. Params kept
        # for call-site stability; wire a real stop-direction swing source before
        # reinstating.

        # Tertiary: volume confirmation of rejection
        conditions.append({
            "condition": f"volume > 1.5× среднего на {entry_tf} red close",
            "reason": "объёмное подтверждение отказа",
        })

        # Zone-based: if price re-enters and closes below zone mid
        if zone:
            zone_mid = (zone.get("lo", 0) + zone.get("hi", 0)) / 2
            if zone_mid > 0 and zone_mid < entry_lo:
                conditions.append({
                    "condition": f"цена ушла ниже середины зоны накопления ({_fmt_price(zone_mid)})",
                    "reason": "возврат в зону — накопление не сработало",
                })

    else:  # short
        invalidation_level = entry_hi * 1.005
        if stop and stop > entry_hi:
            invalidation_level = stop

        conditions.append({
            "condition": f"{entry_tf} close above {_fmt_price(invalidation_level)}",
            "reason": "нарушение структурного уровня входа",
        })

        # (Removed) symmetric dead "nearest swing-high breach" — see the long
        # branch note above (PRIZRAK-1).

        conditions.append({
            "condition": f"volume > 1.5× среднего на {entry_tf} green close",
            "reason": "объёмное подтверждение отказа",
        })

        if zone:
            zone_mid = (zone.get("lo", 0) + zone.get("hi", 0)) / 2
            if zone_mid > entry_hi:
                conditions.append({
                    "condition": f"цена выше середины зоны накопления ({_fmt_price(zone_mid)})",
                    "reason": "возврат в зону — распределение не сработало",
                })

    return conditions


def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"
