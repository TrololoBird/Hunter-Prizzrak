"""Pure order-book math — depth imbalance, microprice, wall clustering.

Extracted verbatim from ``hunt_core.market.client`` (S0 of the native-module
rewrite, ADR-0004). No transport, no I/O — pure functions over book levels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "depth_imbalance_from_levels",
    "depth_imbalance_from_book",
    "microprice_bias_from_book",
    "WallCluster",
    "detect_wall_clusters",
    "depth_imbalance_by_zone",
    "top_depth_walls",
    "normalize_depth_levels",
    "depth_snapshot_from_book",
    "aggregate_cross_exchange_walls",
    "wall_cluster_to_dict",
]


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def depth_imbalance_from_levels(
    bids: list[Any] | tuple[Any, ...] | None,
    asks: list[Any] | tuple[Any, ...] | None,
    *,
    top_n: int = 20,
) -> float | None:
    """Depth imbalance from top-N book levels using notional (price × qty)."""
    bid_notional = 0.0
    ask_notional = 0.0
    for row in (bids or [])[:top_n]:
        try:
            bid_notional += float(row[0]) * float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    for row in (asks or [])[:top_n]:
        try:
            ask_notional += float(row[0]) * float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    return depth_imbalance_from_book(bid_qty=bid_notional, ask_qty=ask_notional, delta_ratio=None)


def depth_imbalance_from_book(
    *, bid_qty: float | None, ask_qty: float | None, delta_ratio: float | None
) -> float | None:
    """Return top-of-book depth imbalance, falling back to signed trade flow."""
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total = bid_qty + ask_qty
        if total > 0.0:
            return round(_clamp((bid_qty - ask_qty) / total), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


def microprice_bias_from_book(
    *,
    bid: float | None,
    ask: float | None,
    bid_qty: float | None = None,
    ask_qty: float | None = None,
    delta_ratio: float | None,
) -> float | None:
    """Return signed microprice bias from L1 book, falling back to trade flow."""
    if bid is None or ask is None or bid <= 0 or (ask <= 0):
        return None
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0 or spread <= 0:
        return None
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total_qty = bid_qty + ask_qty
        if total_qty > 0.0:
            microprice = (ask * bid_qty + bid * ask_qty) / total_qty
            half_spread = spread / 2.0
            if half_spread > 0.0:
                return round(_clamp((microprice - mid) / half_spread), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


_TOP_BOOK_WALL_LEVELS = 5


@dataclass(frozen=True, slots=True)
class WallCluster:
    price_center: float
    total_notional: float
    significance_pct: float
    level_count: int
    side: str
    distance_pct: float
    book_depth_pctile: float = 0.0


def _book_depth_percentile(notional: float, book_notionals: list[float]) -> float:
    """Relative significance as percentile rank within visible book depth."""
    if notional <= 0 or not book_notionals:
        return 0.0
    pool = sorted(n for n in book_notionals if n > 0)
    if not pool:
        return 0.0
    below = sum(1 for n in pool if n <= notional)
    return round(100.0 * below / len(pool), 1)


def detect_wall_clusters(
    levels: list[tuple[float, float]],
    *,
    current_price: float,
    daily_volume: float,
    side: str,
    cluster_tolerance_pct: float = 0.3,
    min_significance_pct: float = 0.5,
    min_book_depth_pctile: float = 85.0,
) -> list[WallCluster]:
    """Group adjacent book levels into wall clusters ranked by distance from price."""
    if current_price <= 0 or not levels:
        return []
    tol = current_price * cluster_tolerance_pct / 100.0
    sorted_levels = sorted(
        ((float(p), float(q)) for p, q in levels if float(p) > 0 and float(q) > 0),
        key=lambda x: x[0],
    )
    level_notionals = [p * q for p, q in sorted_levels]
    clusters: list[WallCluster] = []
    group: list[tuple[float, float]] = []
    anchor = 0.0

    def _flush() -> None:
        if not group:
            return
        total = sum(p * q for p, q in group)
        qty_sum = sum(q for _p, q in group)
        center = sum(p * q for p, q in group) / max(qty_sum, 1e-12)
        sig = (total / daily_volume * 100.0) if daily_volume > 0 else 0.0
        depth_pctile = _book_depth_percentile(total, level_notionals)
        dist = abs(center - current_price) / current_price * 100.0
        if sig >= min_significance_pct or depth_pctile >= min_book_depth_pctile:
            clusters.append(
                WallCluster(
                    price_center=round(center, 6),
                    total_notional=round(total, 2),
                    significance_pct=round(sig, 3),
                    level_count=len(group),
                    side=side,
                    distance_pct=round(dist, 3),
                    book_depth_pctile=depth_pctile,
                )
            )

    for price, qty in sorted_levels:
        if not group:
            group = [(price, qty)]
            anchor = price
            continue
        if abs(price - anchor) <= tol:
            group.append((price, qty))
        else:
            _flush()
            group = [(price, qty)]
            anchor = price
    _flush()
    return sorted(clusters, key=lambda c: c.distance_pct)


def depth_imbalance_by_zone(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    current_price: float,
    zones_pct: list[float] | None = None,
) -> dict[str, float]:
    """Proximity-weighted imbalance (-1..1) within each distance band from mid."""
    if current_price <= 0:
        return {}
    zones = zones_pct or [0.5, 1.0, 2.0, 5.0]
    out: dict[str, float] = {}
    for z in zones:
        band = current_price * z / 100.0
        lo = current_price - band
        hi = current_price + band
        decay_k = 2.0 / max(z, 0.001)
        bid_n = sum(
            p * q * math.exp(-decay_k * abs(current_price - p) / current_price * 100)
            for p, q in bids if lo <= p <= current_price
        )
        ask_n = sum(
            p * q * math.exp(-decay_k * abs(p - current_price) / current_price * 100)
            for p, q in asks if current_price <= p <= hi
        )
        total = bid_n + ask_n
        key = f"imb_{z:g}pct"
        out[key] = round((bid_n - ask_n) / total, 4) if total > 0 else 0.0
    return out


def top_depth_walls(
    levels: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> list[dict[str, float]]:
    """Top bid/ask levels ranked by notional (price × qty)."""
    ranked = sorted(
        (
            {
                "price": float(price),
                "qty": float(qty),
                "notional_usd": round(float(price) * float(qty), 2),
            }
            for price, qty in levels
            if float(price) > 0 and float(qty) > 0
        ),
        key=lambda row: row["notional_usd"],
        reverse=True,
    )
    return ranked[: max(1, int(top_n))]


def normalize_depth_levels(
    raw: Any,
    *,
    side: str = "",
) -> list[tuple[float, float]]:
    """Accept ccxt [[p,q],…] or list of {price, qty} dicts.

    When *side* is ``"bid"`` the result is sorted price-descending (best
    bid first).  When ``"ask"`` — price-ascending (best ask first).
    Without *side* the original order is preserved (CCXT default is
    already correct).
    """
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((float(item[0]), float(item[1])))
        elif isinstance(item, dict):
            p = item.get("price")
            q = item.get("qty")
            if p is not None and q is not None:
                out.append((float(p), float(q)))
    if side == "bid":
        out.sort(key=lambda x: x[0], reverse=True)
    elif side == "ask":
        out.sort(key=lambda x: x[0])
    return out


def depth_snapshot_from_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any]:
    """Build hunt depth snapshot with ranked walls.

    Best bid/ask are derived from price-sorted data (bid descending, ask
    ascending) — independent of the notional-ranked ``top_depth_walls``
    output used for wall display.
    """
    if not bids or not asks:
        return {
            "bid_price": None,
            "ask_price": None,
            "bid_qty": None,
            "ask_qty": None,
            "bid_levels": [],
            "ask_levels": [],
        }
    best_bid = max(p for p, _q in bids)
    best_ask = min(p for p, _q in asks)
    return {
        "bid_price": best_bid,
        "ask_price": best_ask,
        "bid_qty": round(sum(q for _p, q in bids), 4),
        "ask_qty": round(sum(q for _p, q in asks), 4),
        "bid_levels": top_depth_walls(bids, top_n=top_n),
        "ask_levels": top_depth_walls(asks, top_n=top_n),
    }


def aggregate_cross_exchange_walls(
    per_exchange: dict[str, dict[str, Any]],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any]:
    """Merge venue depth snapshots — aggregate same-price buckets across venues."""
    bid_pool: list[dict[str, Any]] = []
    ask_pool: list[dict[str, Any]] = []
    venues: list[str] = []
    for ex, snap in per_exchange.items():
        if not isinstance(snap, dict) or snap.get("bid_price") is None:
            continue
        venues.append(ex)
        for side, pool in (("bid", bid_pool), ("ask", ask_pool)):
            key = f"{side}_levels"
            for lvl in snap.get(key) or []:
                if isinstance(lvl, dict):
                    row = dict(lvl)
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    row = {
                        "price": float(lvl[0]),
                        "qty": float(lvl[1]),
                        "notional_usd": round(float(lvl[0]) * float(lvl[1]), 2),
                    }
                else:
                    continue
                row["exchange"] = ex
                pool.append(row)

    def _merge_pool(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[float, dict[str, Any]] = {}
        for row in pool:
            price = float(row.get("price") or 0)
            if price <= 0:
                continue
            bucket = round(price, 4)
            acc = buckets.setdefault(
                bucket,
                {
                    "price": bucket,
                    "qty": 0.0,
                    "notional_usd": 0.0,
                    "venues": set(),
                },
            )
            acc["qty"] += float(row.get("qty") or 0)
            acc["notional_usd"] += float(row.get("notional_usd") or 0)
            acc["venues"].add(str(row.get("exchange") or ""))
        merged: list[dict[str, Any]] = []
        for bucket, acc in buckets.items():
            merged.append(
                {
                    "price": bucket,
                    "qty": round(acc["qty"], 4),
                    "notional_usd": round(acc["notional_usd"], 2),
                    "venues": sorted(v for v in acc["venues"] if v),
                    "venue_count": len(acc["venues"]),
                }
            )
        return sorted(merged, key=lambda r: float(r.get("notional_usd") or 0), reverse=True)[:top_n]

    bid_levels = _merge_pool(bid_pool)
    ask_levels = _merge_pool(ask_pool)

    total_bid = sum(
        float(lvl.get("notional_usd") or 0)
        for snap in per_exchange.values()
        if isinstance(snap, dict)
        for lvl in (snap.get("bid_levels") or [])
        if isinstance(lvl, dict)
    )
    total_ask = sum(
        float(lvl.get("notional_usd") or 0)
        for snap in per_exchange.values()
        if isinstance(snap, dict)
        for lvl in (snap.get("ask_levels") or [])
        if isinstance(lvl, dict)
    )
    imb = None
    if total_bid + total_ask > 0:
        imb = round((total_bid - total_ask) / (total_bid + total_ask), 4)

    return {
        "venues": venues,
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "depth_imbalance": imb,
        "bid_depth_usd_total": round(total_bid, 2),
        "ask_depth_usd_total": round(total_ask, 2),
        "source": "cross_exchange",
    }


def wall_cluster_to_dict(cluster: WallCluster) -> dict[str, Any]:
    """Serialize a wall cluster for market/snapshot payloads."""
    return {
        "price_center": cluster.price_center,
        "total_notional": cluster.total_notional,
        "significance_pct": cluster.significance_pct,
        "level_count": cluster.level_count,
        "side": cluster.side,
        "distance_pct": cluster.distance_pct,
        "book_depth_pctile": cluster.book_depth_pctile,
    }
