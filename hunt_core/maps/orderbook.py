"""Map 1 — Orderbook heatmap: walls, sticky/iceberg/absorption/spoof/void + footprint."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from hunt_core.maps.config import MapsConfig
from hunt_core.market.client import (
    WallCluster,
    aggregate_cross_exchange_walls,
    depth_imbalance_by_zone,
    depth_snapshot_from_book,
    detect_wall_clusters,
    normalize_depth_levels,
    wall_cluster_to_dict,
)


@dataclass
class FootprintBin:
    price_center: float
    buy_volume: float
    sell_volume: float
    delta: float
    cvd_contrib: float


@dataclass
class OrderbookMap:
    symbol: str
    current_price: float
    bid_walls: list[dict[str, Any]] = field(default_factory=list)
    ask_walls: list[dict[str, Any]] = field(default_factory=list)
    wall_clusters: list[dict[str, Any]] = field(default_factory=list)
    sticky_walls: list[dict[str, Any]] = field(default_factory=list)
    iceberg_levels: list[dict[str, Any]] = field(default_factory=list)
    absorption_zones: list[dict[str, Any]] = field(default_factory=list)
    spoof_flags: list[dict[str, Any]] = field(default_factory=list)
    liquidity_voids: list[dict[str, Any]] = field(default_factory=list)
    zone_imbalance: dict[str, float] = field(default_factory=dict)
    footprint_bins: list[dict[str, Any]] = field(default_factory=list)
    stacked_imbalance: str | None = None
    cvd_divergence: str | None = None
    depth_heatmap_max: float = 0.0
    depth_heatmap_matrix: list[dict[str, Any]] = field(default_factory=list)
    venues: list[str] = field(default_factory=list)
    source: str = "ws"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "bid_walls": self.bid_walls,
            "ask_walls": self.ask_walls,
            "wall_clusters": self.wall_clusters,
            "sticky_walls": self.sticky_walls,
            "iceberg_levels": self.iceberg_levels,
            "absorption_zones": self.absorption_zones,
            "spoof_flags": self.spoof_flags,
            "liquidity_voids": self.liquidity_voids,
            "zone_imbalance": self.zone_imbalance,
            "footprint_bins": self.footprint_bins,
            "stacked_imbalance": self.stacked_imbalance,
            "cvd_divergence": self.cvd_divergence,
            "depth_heatmap_max": self.depth_heatmap_max,
            "depth_heatmap_matrix": self.depth_heatmap_matrix,
            "venues": self.venues,
            "source": self.source,
        }


def _trade_footprint(
    trades: deque[Any] | list[Any],
    *,
    current_price: float,
    n_buckets: int,
    price_range_pct: float,
    window_seconds: int = 60,
) -> list[FootprintBin]:
    if current_price <= 0 or not trades:
        return []
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    span = current_price * price_range_pct / 100.0
    lo = current_price - span
    hi = current_price + span
    bucket_size = (hi - lo) / max(1, n_buckets)
    bins: dict[int, dict[str, float]] = {}
    for pt in trades:
        ts_ms = getattr(pt, "ts_ms", None) or (pt[0] if isinstance(pt, (list, tuple)) else 0)
        if int(ts_ms) < cutoff_ms:
            continue
        px = float(getattr(pt, "price", None) or (pt[4] if isinstance(pt, (list, tuple)) and len(pt) > 4 else 0))
        qty = float(getattr(pt, "qty", None) or (pt[1] if isinstance(pt, (list, tuple)) and len(pt) > 1 else 0))
        is_buy = bool(getattr(pt, "is_buy", None))
        if isinstance(pt, (list, tuple)) and len(pt) > 3:
            is_buy = str(pt[3]).lower() == "buy" if isinstance(pt[3], str) else is_buy
        if px <= 0 or qty <= 0 or px < lo or px > hi:
            continue
        b = max(0, min(n_buckets - 1, int((px - lo) / bucket_size)))
        row = bins.setdefault(b, {"buy": 0.0, "sell": 0.0})
        if is_buy:
            row["buy"] += qty * px
        else:
            row["sell"] += qty * px
    out: list[FootprintBin] = []
    for b, row in sorted(bins.items()):
        center = lo + (b + 0.5) * bucket_size
        delta = row["buy"] - row["sell"]
        out.append(
            FootprintBin(
                price_center=round(center, 6),
                buy_volume=round(row["buy"], 2),
                sell_volume=round(row["sell"], 2),
                delta=round(delta, 2),
                cvd_contrib=round(delta, 2),
            )
        )
    return out


def _detect_sticky_walls(
    history: deque[dict[str, Any]],
    *,
    current_price: float,
    min_samples: int,
    tolerance_pct: float = 0.15,
) -> list[dict[str, Any]]:
    if len(history) < min_samples or current_price <= 0:
        return []
    tol = current_price * tolerance_pct / 100.0
    price_counts: dict[tuple[str, float], int] = {}
    notionals: dict[tuple[str, float], float] = {}
    for snap in history:
        for side in ("bid", "ask"):
            for lvl in snap.get(f"{side}_levels") or []:
                if not isinstance(lvl, dict):
                    continue
                px = float(lvl.get("price") or 0)
                if px <= 0:
                    continue
                key_round = round(px / tol) * tol if tol > 0 else px
                key = (side, key_round)
                price_counts[key] = price_counts.get(key, 0) + 1
                notionals[key] = max(notionals.get(key, 0.0), float(lvl.get("notional_usd") or 0))
    sticky: list[dict[str, Any]] = []
    for (side, px), count in price_counts.items():
        if count >= min_samples:
            sticky.append(
                {
                    "side": side,
                    "price": round(px, 6),
                    "samples": count,
                    "notional_usd": round(notionals.get((side, px), 0.0), 2),
                    "distance_pct": round(abs(px - current_price) / current_price * 100.0, 3),
                }
            )
    return sorted(sticky, key=lambda x: x["distance_pct"])[:6]


def _detect_voids(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    current_price: float,
    n_buckets: int,
    price_range_pct: float,
    void_pctile: float,
) -> list[dict[str, Any]]:
    if current_price <= 0:
        return []
    span = current_price * price_range_pct / 100.0
    lo = current_price - span
    bucket_size = (2.0 * span) / max(1, n_buckets)
    depths: list[float] = []
    bucket_depth: dict[int, float] = {}
    for price, qty in bids + asks:
        if price < lo or price > current_price + span:
            continue
        b = max(0, min(n_buckets - 1, int((price - lo) / bucket_size)))
        notional = price * qty
        bucket_depth[b] = bucket_depth.get(b, 0.0) + notional
        depths.append(notional)
    if not depths:
        return []
    depths.sort()
    cutoff_idx = max(0, int(len(depths) * void_pctile / 100.0) - 1)
    threshold = depths[cutoff_idx] if depths else 0.0
    voids: list[dict[str, Any]] = []
    for b, depth in bucket_depth.items():
        if depth <= threshold:
            center = lo + (b + 0.5) * bucket_size
            voids.append(
                {
                    "price_center": round(center, 6),
                    "depth_usd": round(depth, 2),
                    "distance_pct": round(abs(center - current_price) / current_price * 100.0, 3),
                }
            )
    return sorted(voids, key=lambda x: x["distance_pct"])[:5]


def _stacked_imbalance(bins: list[FootprintBin], *, min_run: int = 3) -> str | None:
    if len(bins) < min_run:
        return None
    deltas = [b.delta for b in bins]
    run_buy = run_sell = 0
    for d in deltas:
        if d > 0:
            run_buy += 1
            run_sell = 0
        elif d < 0:
            run_sell += 1
            run_buy = 0
        else:
            run_buy = run_sell = 0
        if run_buy >= min_run:
            return "buy_stack"
        if run_sell >= min_run:
            return "sell_stack"
    return None


def _depth_heatmap_matrix(
    history: deque[dict[str, Any]],
    *,
    current_price: float,
    n_buckets: int,
    price_range_pct: float,
) -> list[dict[str, Any]]:
    """Price-bucket × time samples of resting notional from book history."""
    if current_price <= 0 or len(history) < 2:
        return []
    span = current_price * price_range_pct / 100.0
    lo = current_price - span
    bucket_size = (2.0 * span) / max(1, n_buckets)
    matrix: list[dict[str, Any]] = []
    for sample_idx, snap in enumerate(list(history)[-min(len(history), 12) :]):
        acc: dict[int, float] = {}
        for side in ("bid", "ask"):
            for lvl in snap.get(f"{side}_levels") or []:
                if not isinstance(lvl, dict):
                    continue
                px = float(lvl.get("price") or 0)
                notional = float(lvl.get("notional_usd") or 0)
                if px <= 0 or notional <= 0 or px < lo or px > current_price + span:
                    continue
                b = max(0, min(n_buckets - 1, int((px - lo) / bucket_size)))
                acc[b] = acc.get(b, 0.0) + notional
        for b, depth in sorted(acc.items(), key=lambda kv: kv[1], reverse=True)[:6]:
            center = lo + (b + 0.5) * bucket_size
            matrix.append(
                {
                    "sample": sample_idx,
                    "price_center": round(center, 6),
                    "depth_usd": round(depth, 2),
                    "intensity": round(depth / max(acc.values()), 4),
                }
            )
    return matrix


def _detect_iceberg(
    trades: deque[Any] | list[Any],
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    current_price: float,
    tolerance_pct: float = 0.08,
) -> list[dict[str, Any]]:
    """Repeated fills at a level without proportional displayed size depletion."""
    if current_price <= 0 or not trades:
        return []
    tol = current_price * tolerance_pct / 100.0
    book_by_px: dict[float, tuple[float, str]] = {}
    for px, qty in bids:
        if px > 0:
            book_by_px[round(px / tol) * tol if tol else px] = (px * qty, "bid")
    for px, qty in asks:
        if px > 0:
            book_by_px[round(px / tol) * tol if tol else px] = (px * qty, "ask")
    fill_at: dict[float, float] = {}
    cutoff_ms = int(time.time() * 1000) - 120_000
    for pt in trades:
        ts_ms = int(getattr(pt, "ts_ms", 0) or 0)
        if ts_ms < cutoff_ms:
            continue
        px = float(getattr(pt, "price", 0) or 0)
        qty = float(getattr(pt, "qty", 0) or 0)
        if px <= 0 or qty <= 0:
            continue
        key = round(px / tol) * tol if tol else px
        fill_at[key] = fill_at.get(key, 0.0) + qty * px
    _RATIO_CAP = 50.0
    out: list[dict[str, Any]] = []
    for key, filled in fill_at.items():
        shown = book_by_px.get(key)
        if shown and filled > shown[0] * 1.4:
            # A flat $1 floor on displayed notional lets a near-empty book level
            # (e.g. a few cents shown) blow the ratio up into the tens of thousands —
            # not a meaningful "replenishment" reading. Floor against a fraction of the
            # fill itself instead, and cap the displayed figure so it stays interpretable.
            floor = max(shown[0], filled * 0.02)
            raw_ratio = filled / floor
            out.append(
                {
                    "price": round(key, 6),
                    "side": shown[1],
                    "filled_usd": round(filled, 2),
                    "displayed_usd": round(shown[0], 2),
                    "replenishment_ratio": round(min(raw_ratio, _RATIO_CAP), 2),
                    "replenishment_ratio_capped": raw_ratio > _RATIO_CAP,
                }
            )
    return sorted(out, key=lambda x: x["filled_usd"], reverse=True)[:4]


def _detect_absorption(
    footprint: list[FootprintBin],
    clusters: list[WallCluster],
    *,
    current_price: float,
    price_change_pct: float | None,
) -> list[dict[str, Any]]:
    """Large resting notional + aggressive opposite flow + price stall."""
    if current_price <= 0 or not footprint or not clusters:
        return []
    stall = price_change_pct is not None and abs(float(price_change_pct)) <= 0.12
    out: list[dict[str, Any]] = []
    for cluster in clusters[:4]:
        if cluster.distance_pct > 1.5:
            continue
        near_bins = [
            b
            for b in footprint
            if abs(b.price_center - cluster.price_center) / current_price * 100.0 <= 0.35
        ]
        if not near_bins:
            continue
        delta = sum(b.delta for b in near_bins)
        opposite = (cluster.side == "bid" and delta < 0) or (cluster.side == "ask" and delta > 0)
        if opposite and cluster.total_notional >= 25_000 and (stall or abs(delta) >= 10_000):
            out.append(
                {
                    "price_center": cluster.price_center,
                    "side": cluster.side,
                    "notional_usd": cluster.total_notional,
                    "aggressive_delta_usd": round(delta, 2),
                    "stalled": stall,
                }
            )
    return out


_SPOOF_MIN_PRIOR_SAMPLES = 3  # wall must show up in this many consecutive snapshots before vanishing counts as spoof


def _detect_spoof(
    history: deque[dict[str, Any]],
    current_bids: list[tuple[float, float]],
    current_asks: list[tuple[float, float]],
    *,
    current_price: float,
    tolerance_pct: float = 0.12,
) -> list[dict[str, Any]]:
    """Wall vanishes as price approaches (present across several prior samples, gone now).

    Used to compare only the single immediately-prior snapshot (``history[-2]``) —
    a wall that simply hadn't been re-quoted yet on that one tick (ordinary MM
    order refresh/rebalancing, which happens constantly) read identically to a
    genuine spoof (a wall placed to bait, then pulled right before it would fill).
    Requiring the wall to have actually persisted across several consecutive
    snapshots before its disappearance counts as anything filters out that
    single-tick noise — a real spoof wall sits there to be seen, a
    rebalance-in-progress wall doesn't have a multi-sample history to erase.
    """
    if len(history) < _SPOOF_MIN_PRIOR_SAMPLES + 1 or current_price <= 0:
        return []
    tol = current_price * tolerance_pct / 100.0
    prior_samples = list(history)[-(_SPOOF_MIN_PRIOR_SAMPLES + 1):-1]
    flags: list[dict[str, Any]] = []

    def _levels(side: str, snap: dict[str, Any]) -> dict[float, float]:
        out: dict[float, float] = {}
        for lvl in snap.get(f"{side}_levels") or []:
            if not isinstance(lvl, dict):
                continue
            px = float(lvl.get("price") or 0)
            if px <= 0:
                continue
            key = round(px / tol) * tol if tol else px
            out[key] = float(lvl.get("notional_usd") or 0)
        return out

    cur_bid = {round(p / tol) * tol if tol else p: p * q for p, q in current_bids if p > 0}
    cur_ask = {round(p / tol) * tol if tol else p: p * q for p, q in current_asks if p > 0}
    for side, cur_map in (("bid", cur_bid), ("ask", cur_ask)):
        per_sample = [_levels(side, snap) for snap in prior_samples]
        candidate_prices = set(per_sample[0].keys()) if per_sample else set()
        for px in candidate_prices:
            notionals = [m.get(px, 0.0) for m in per_sample]
            if any(n < 50_000 for n in notionals):
                continue  # must have been a genuinely large wall on EVERY prior sample
            dist = abs(px - current_price) / current_price * 100.0
            if dist > 1.2:
                continue
            prev_n = notionals[-1]  # most recent prior sample, for the reported drop size
            if cur_map.get(px, 0.0) < min(notionals) * 0.25:
                flags.append(
                    {
                        "side": side,
                        "price": round(px, 6),
                        "prior_notional_usd": round(prev_n, 2),
                        "current_notional_usd": round(cur_map.get(px, 0.0), 2),
                        "distance_pct": round(dist, 3),
                        "persisted_samples": len(notionals),
                    }
                )
    return flags[:4]


def _detect_cvd_divergence(
    trades: deque[Any] | list[Any],
    *,
    price_change_pct: float | None,
    window_seconds: int = 60,
) -> str | None:
    if price_change_pct is None or not trades:
        return None
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    cvd = 0.0
    for pt in trades:
        ts_ms = int(getattr(pt, "ts_ms", 0) or 0)
        if ts_ms < cutoff_ms:
            continue
        qty = float(getattr(pt, "qty", 0) or 0)
        px = float(getattr(pt, "price", 0) or 0)
        is_buy = bool(getattr(pt, "is_buy", False))
        if qty <= 0:
            continue
        cvd += qty * px if is_buy else -qty * px
    px_chg = float(price_change_pct)
    if px_chg >= 0.15 and cvd < -5_000:
        return "bearish_div"
    if px_chg <= -0.15 and cvd > 5_000:
        return "bullish_div"
    return None


def merge_full_depth_bins(
    per_exchange: dict[str, dict[str, Any]],
    *,
    current_price: float,
    n_buckets: int,
    price_range_pct: float,
) -> dict[str, Any]:
    """Full-depth price-bin merge across venues (time-aligned snapshot assumed)."""
    if current_price <= 0:
        return {"bid_bins": [], "ask_bins": [], "venues": []}
    span = current_price * price_range_pct / 100.0
    lo = current_price - span
    bucket_size = (2.0 * span) / max(1, n_buckets)
    bid_bins: dict[int, float] = {}
    ask_bins: dict[int, float] = {}
    venues: list[str] = []
    for ex, snap in per_exchange.items():
        if not isinstance(snap, dict):
            continue
        venues.append(ex)
        raw_bids = snap.get("bids") or []
        raw_asks = snap.get("asks") or []
        if not raw_bids and snap.get("bid_levels"):
            raw_bids = [
                (float(x.get("price", 0)), float(x.get("qty", 0)))
                for x in snap.get("bid_levels") or []
                if isinstance(x, dict)
            ]
        if not raw_asks and snap.get("ask_levels"):
            raw_asks = [
                (float(x.get("price", 0)), float(x.get("qty", 0)))
                for x in snap.get("ask_levels") or []
                if isinstance(x, dict)
            ]
        for side, levels, pool in (("bid", raw_bids, bid_bins), ("ask", raw_asks, ask_bins)):
            for item in levels:
                try:
                    if isinstance(item, dict):
                        px, qty = float(item.get("price", 0)), float(item.get("qty", 0))
                    else:
                        px, qty = float(item[0]), float(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if px <= 0 or qty <= 0 or px < lo or px > current_price + span:
                    continue
                b = max(0, min(n_buckets - 1, int((px - lo) / bucket_size)))
                pool[b] = pool.get(b, 0.0) + px * qty
    max_d = max([*bid_bins.values(), *ask_bins.values()], default=1.0) or 1.0

    def _fmt(pool: dict[int, float]) -> list[dict[str, Any]]:
        return [
            {
                "price_center": round(lo + (b + 0.5) * bucket_size, 6),
                "depth_usd": round(v, 2),
                "intensity": round(v / max_d, 4),
            }
            for b, v in sorted(pool.items(), key=lambda kv: kv[1], reverse=True)[:12]
        ]

    return {"bid_bins": _fmt(bid_bins), "ask_bins": _fmt(ask_bins), "venues": venues}


def build_orderbook_map(
    *,
    symbol: str,
    current_price: float,
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    cross_walls: dict[str, Any] | None = None,
    trades: deque[Any] | list[Any] | None = None,
    daily_volume: float = 0.0,
    book_history: deque[dict[str, Any]] | None = None,
    price_change_pct: float | None = None,
    deep_bids: list[tuple[float, float]] | None = None,
    deep_asks: list[tuple[float, float]] | None = None,
    cfg: MapsConfig | None = None,
) -> OrderbookMap | None:
    """Build orderbook heatmap from WS book + optional cross merge + trade footprint."""
    cfg = cfg or MapsConfig()
    if current_price <= 0:
        return None
    top_n = cfg.book_top_n
    if cross_walls and cross_walls.get("bid_levels"):
        bid_walls = list(cross_walls.get("bid_levels") or [])[:top_n]
        ask_walls = list(cross_walls.get("ask_levels") or [])[:top_n]
        venues = list(cross_walls.get("venues") or [])
        source = str(cross_walls.get("source") or "cross_exchange")
        zone_bids = normalize_depth_levels(
            [(float(x.get("price", 0)), float(x.get("qty", 0))) for x in bid_walls if isinstance(x, dict)]
        )
        zone_asks = normalize_depth_levels(
            [(float(x.get("price", 0)), float(x.get("qty", 0))) for x in ask_walls if isinstance(x, dict)]
        )
    elif bids and asks:
        snap = depth_snapshot_from_book(bids, asks, top_n=top_n)
        bid_walls = snap.get("bid_levels") or []
        ask_walls = snap.get("ask_levels") or []
        venues = ["binance"]
        source = "ws"
        zone_bids, zone_asks = bids, asks
    else:
        return None

    if deep_bids:
        zone_bids = normalize_depth_levels(deep_bids) or zone_bids
    if deep_asks:
        zone_asks = normalize_depth_levels(deep_asks) or zone_asks

    hist = book_history or deque()
    clusters: list[WallCluster] = []
    clusters.extend(
        detect_wall_clusters(
            zone_bids,
            current_price=current_price,
            daily_volume=daily_volume,
            side="bid",
        )
    )
    clusters.extend(
        detect_wall_clusters(
            zone_asks,
            current_price=current_price,
            daily_volume=daily_volume,
            side="ask",
        )
    )

    sticky = _detect_sticky_walls(
        book_history or deque(),
        current_price=current_price,
        min_samples=cfg.sticky_min_samples,
    )
    voids = _detect_voids(
        zone_bids,
        zone_asks,
        current_price=current_price,
        n_buckets=cfg.n_buckets,
        price_range_pct=cfg.price_range_pct,
        void_pctile=cfg.void_depth_pctile,
    )
    footprint = _trade_footprint(
        trades or [],
        current_price=current_price,
        n_buckets=cfg.n_buckets,
        price_range_pct=cfg.price_range_pct,
    )
    stacked = _stacked_imbalance(footprint)
    icebergs = _detect_iceberg(trades or [], zone_bids, zone_asks, current_price=current_price)
    absorption = _detect_absorption(footprint, clusters, current_price=current_price, price_change_pct=price_change_pct)
    spoofs = _detect_spoof(hist, zone_bids, zone_asks, current_price=current_price)
    cvd_div = _detect_cvd_divergence(trades or [], price_change_pct=price_change_pct)
    heat_matrix = _depth_heatmap_matrix(
        hist,
        current_price=current_price,
        n_buckets=cfg.n_buckets,
        price_range_pct=cfg.price_range_pct,
    )

    total_depth = sum(p * q for p, q in zone_bids + zone_asks)
    zone_imb = depth_imbalance_by_zone(zone_bids, zone_asks, current_price)

    return OrderbookMap(
        symbol=symbol,
        current_price=current_price,
        bid_walls=bid_walls,
        ask_walls=ask_walls,
        wall_clusters=[wall_cluster_to_dict(c) for c in clusters[:8]],
        sticky_walls=sticky,
        iceberg_levels=icebergs,
        absorption_zones=absorption,
        spoof_flags=spoofs,
        liquidity_voids=voids,
        zone_imbalance=zone_imb,
        footprint_bins=[
            {
                "price": b.price_center,
                "buy": b.buy_volume,
                "sell": b.sell_volume,
                "delta": b.delta,
            }
            for b in footprint[: cfg.n_buckets]
        ],
        stacked_imbalance=stacked,
        cvd_divergence=cvd_div,
        depth_heatmap_max=round(total_depth, 2),
        depth_heatmap_matrix=heat_matrix,
        venues=venues,
        source=source,
    )


def derive_ob_accumulation_features(
    ob: OrderbookMap,
    *,
    current_price: float,
) -> dict[str, Any]:
    """Orderbook accumulation — sticky bid support, bid absorption, voids/thin asks above."""
    if current_price <= 0:
        return {}
    out: dict[str, Any] = {}
    bid_abs = sum(1 for z in ob.absorption_zones if z.get("side") == "bid")
    sticky_bid = sum(1 for w in ob.sticky_walls if w.get("side") == "bid")
    if bid_abs >= 1 and sticky_bid >= 1:
        out["map_accum_bid_absorption"] = True
    elif bid_abs >= 2 or sticky_bid >= 2:
        out["map_accum_bid_absorption"] = True

    voids_above = [
        v
        for v in ob.liquidity_voids
        if v.get("price_center") is not None and float(v["price_center"]) > current_price
    ]
    if voids_above:
        nearest = min(voids_above, key=lambda v: float(v.get("distance_pct") or 99.0))
        out["map_void_above"] = nearest.get("price_center")
        out["map_void_above_pct"] = nearest.get("distance_pct")

    imb = ob.zone_imbalance.get("imb_1.0pct") if ob.zone_imbalance else None
    bid_notional = sum(float(w.get("notional_usd") or 0) for w in ob.bid_walls[:4])
    ask_notional = sum(float(w.get("notional_usd") or 0) for w in ob.ask_walls[:4])
    ask_thin = False
    if imb is not None and float(imb) >= 0.12:
        ask_thin = True
    if bid_notional > 0 and ask_notional > 0 and bid_notional / ask_notional >= 1.35:
        ask_thin = True
    upper_void = any(
        v.get("price_center") is not None
        and float(v["price_center"]) > current_price
        and float(v.get("distance_pct") or 99) <= 2.5
        for v in ob.liquidity_voids
    )
    if ask_thin or upper_void:
        out["map_ask_thinning"] = True
    return out


def merge_cross_books(per_exchange: dict[str, dict[str, Any]], *, top_n: int = 5) -> dict[str, Any]:
    """Thin wrapper around aggregate_cross_exchange_walls for maps callers."""
    return aggregate_cross_exchange_walls(per_exchange, top_n=top_n)
