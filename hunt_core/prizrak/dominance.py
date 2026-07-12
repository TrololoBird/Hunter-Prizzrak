"""Доминация (BTC.D/TOTAL3) as a continuous confluence доп-фактор — not a binary gate.

Prizrak's own reads fold dominance into direction: «график доминации USD идёт вниз, крипта
идёт вверх» (dominance down = crypto up), and the POL/MATIC video uses TOTAL3/Others reaching
a level as an entry-reaction confirmation. This module turns the 24h dominance change into a
bounded directional multiplier. The 24h change is produced off the tick plane by
``dominance_source`` (CoinGecko free ``/global`` + rolling snapshot cache); when the factor is
disabled or has no data it reads neutral (1.0), so the live path is untouched.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig


def dominance_confluence(
    *,
    direction: str,
    btc_d_change_24h: float | None,
    total3_change_24h: float | None,
    stable_cd_change_24h: float | None = None,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Bounded multiplier in [0.85, 1.15]. BTC.D falling / TOTAL3 rising = bullish for crypto
    broadly (курс: «доминация вниз, крипта вверх»); rising STABLE.C.D = risk-off (money to
    stables) = bearish. Neutral inside the band.
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

    # STABLE.C.D (Prizrak «график Стейблов, как сейчас его использую»): rising stablecoin
    # dominance = risk-off = supports SHORT / opposes LONG.
    if stable_cd_change_24h is not None and abs(stable_cd_change_24h) > band:
        stable_falling = stable_cd_change_24h < 0  # risk-on
        if stable_falling == want_up:
            mult += 0.05
            evidence.append(f"stable_cd_change_24h={stable_cd_change_24h:+.2f}pp supports")
        else:
            mult -= 0.05
            evidence.append(f"stable_cd_change_24h={stable_cd_change_24h:+.2f}pp against")

    mult = max(0.85, min(1.15, mult))
    return {"multiplier": round(mult, 3), "evidence": evidence}


def compute_dominance_factor(
    changes: dict[str, float] | None,
    *,
    direction: str,
    cfg: PrizrakConfig,
) -> dict[str, Any]:
    """Gated wrapper mirroring ``compute_marketcap_factor``: neutral (1.0) unless the factor
    is explicitly enabled AND 24h dominance changes are available.

    ``changes`` = ``{btc_d_change_24h, total3_change_24h}`` from
    ``dominance_source.read_cached_changes_24h()`` (or ``None``).
    """
    if not getattr(cfg, "dominance_enabled", False):
        return {"multiplier": 1.0, "evidence": ["dominance_disabled"]}
    if not changes:
        return {"multiplier": 1.0, "evidence": ["dominance_unavailable"]}
    out = dominance_confluence(
        direction=direction,
        btc_d_change_24h=changes.get("btc_d_change_24h"),
        total3_change_24h=changes.get("total3_change_24h"),
        stable_cd_change_24h=changes.get("stable_cd_change_24h"),
        cfg=cfg,
    )
    if not out.get("evidence"):
        out["evidence"] = ["dominance_neutral"]
    return out


__all__ = ["dominance_confluence", "compute_dominance_factor"]
