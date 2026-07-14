"""Confluence scoring of a limit/добор zone across already-computed maps.

The project's leverage isn't a better single microstructure metric — it's whether
an interest zone (where a pending limit sits, «вход по факту касания») is
corroborated by MULTIPLE independent maps at once. This fuses the maps we already
compute — volume profile (POC/HVN/naked POC), liquidation magnets, order-book
walls/absorption, funding regime — into a conviction score for the zone. Zero new
network: pure read over the row's `market` + `maps` dicts (SLICE 1b of
MAPS_RESEARCH_UPGRADE §7.4).
"""
from __future__ import annotations

from typing import Any

_FUNDING_CROWDED_LONG = 0.0003   # positive funding → longs pay → crowded long
_FUNDING_CROWDED_SHORT = -0.0001  # negative funding → shorts pay → crowded short


def _prices(items: Any, *keys: str) -> list[float]:
    out: list[float] = []
    for it in items or []:
        if isinstance(it, dict):
            for k in keys:
                v = it.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    out.append(float(v))
                    break
        elif isinstance(it, (int, float)) and it > 0:
            out.append(float(it))
    return out


def _near(level: float, lo: float, hi: float, price: float, *, tol_pct: float = 0.4) -> bool:
    """Inside the zone, or within max(half-zone-width, tol_pct of price) of its center."""
    if lo <= level <= hi:
        return True
    center = (lo + hi) / 2.0
    tol = abs(hi - lo) / 2.0
    if price > 0:
        tol = max(tol, price * tol_pct / 100.0)
    return abs(level - center) <= tol


def score_zone_confluence(
    *,
    lo: float,
    hi: float,
    side: str,
    market: dict[str, Any] | None,
    maps: dict[str, Any] | None,
    price: float,
) -> dict[str, Any]:
    """Return {score, factors, label} — how many independent maps corroborate the zone.

    ``side`` is 'long' (support below price) or 'short' (resistance above). Only
    same-side evidence counts (a long-liq magnet corroborates a support zone, a
    short-squeeze magnet a resistance zone).
    """
    market = market or {}
    maps = maps or {}
    _vp = maps.get("volume_profile")
    vp: dict[str, Any] = _vp if isinstance(_vp, dict) else {}
    _ob = maps.get("orderbook")
    ob: dict[str, Any] = _ob if isinstance(_ob, dict) else {}
    factors: list[str] = []

    # Volume profile — POC / naked POC / HVN acceptance nodes.
    poc = vp.get("poc")
    if isinstance(poc, (int, float)) and _near(float(poc), lo, hi, price):
        factors.append("POC")
    naked = vp.get("naked_poc")
    if isinstance(naked, (int, float)) and _near(float(naked), lo, hi, price):
        factors.append("naked POC")
    if any(_near(h, lo, hi, price) for h in _prices(vp.get("hvn_nodes"), "price")):
        factors.append("HVN")

    # Liquidation magnet on the zone's OWN side (long-liq below / short-squeeze above).
    mag = market.get("liq_heatmap_nearest_long" if side == "long" else "liq_heatmap_nearest_short")
    clusters = _prices(market.get("liq_heatmap_clusters"), "price")
    if side == "long":
        clusters = [c for c in clusters if c <= price] if price > 0 else clusters
    else:
        clusters = [c for c in clusters if c >= price] if price > 0 else clusters
    if (isinstance(mag, (int, float)) and _near(float(mag), lo, hi, price)) or any(
        _near(c, lo, hi, price) for c in clusters
    ):
        factors.append("магнит ликв.")

    # Order-book sticky wall / cluster / absorption at the zone.
    walls = (
        _prices(ob.get("sticky_walls"), "price")
        + _prices(ob.get("wall_clusters"), "price", "price_center")
        + _prices(ob.get("absorption_zones"), "price", "price_center")
    )
    if any(_near(w, lo, hi, price) for w in walls):
        factors.append("стена")

    # Funding regime aligned with the counter-move the zone implies.
    funding = market.get("funding")
    if isinstance(funding, (int, float)):
        if side == "long" and funding < _FUNDING_CROWDED_SHORT:
            factors.append("funding (шорты перегреты)")
        elif side == "short" and funding > _FUNDING_CROWDED_LONG:
            factors.append("funding (лонги перегреты)")

    score = len(factors)
    label = "сильный" if score >= 3 else "средний" if score == 2 else "слабый" if score == 1 else ""
    return {"score": score, "factors": tuple(factors), "label": label}


__all__ = ["score_zone_confluence"]
