"""Доминация (BTC.D/TOTAL3) as continuous confluence — not a binary gate.

Призрак's own BTC review explicitly folded dominance into the read: "график доминации
USD идёт вниз, крипта идёт вверх" (dominance down = crypto up, and vice versa). The
existing ``deep.pipeline.macro_data.fetch_macro_data()`` already fetches
``btc_d_change_24h``/``total3_change_24h`` for the old macro filter's binary veto gate
(``run_macro_filter``) — this module reinterprets the SAME already-fetched snapshot as
a bounded directional multiplier instead, no new fetch.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig


def dominance_confluence(
    *,
    direction: str,
    btc_d_change_24h: float | None,
    total3_change_24h: float | None,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Bounded multiplier in [0.85, 1.15]. BTC.D falling / TOTAL3 rising = bullish for
    crypto broadly (курс: "доминация вниз, крипта вверх"); neutral inside the band.
    """
    cfg = cfg or PrizrakConfig.load()
    want_up = direction == "long"
    mult = 1.0
    evidence: list[str] = []
    band = cfg.dominance_neutral_band_pct

    if btc_d_change_24h is not None and abs(btc_d_change_24h) > band:
        dominance_falling = btc_d_change_24h < 0
        if dominance_falling == want_up:
            mult += 0.08
            evidence.append(f"btc_d_change_24h={btc_d_change_24h:+.2f}% supports")
        else:
            mult -= 0.08
            evidence.append(f"btc_d_change_24h={btc_d_change_24h:+.2f}% against")

    if total3_change_24h is not None and abs(total3_change_24h) > band:
        total3_rising = total3_change_24h > 0
        if total3_rising == want_up:
            mult += 0.07
            evidence.append(f"total3_change_24h={total3_change_24h:+.2f}% supports")
        else:
            mult -= 0.07
            evidence.append(f"total3_change_24h={total3_change_24h:+.2f}% against")

    mult = max(0.85, min(1.15, mult))
    return {"multiplier": round(mult, 3), "evidence": evidence}


__all__ = ["dominance_confluence"]
