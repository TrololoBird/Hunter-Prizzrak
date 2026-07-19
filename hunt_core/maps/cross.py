"""Cross-venue book-wall aggregation (ADR-0004 S4) — maps-layer, pure, no transport.

Takes the per-venue depth snapshots from :meth:`MultiEngine.cross_orderbook` and merges them into the
``cross_walls`` dict that ``build_orderbook_map`` consumes. Ports the aggregation half of the deleted
``market/cross.py::fetch_cross_exchange_book_walls`` (the fetch half is now the engine method):
time-alignment (drop venues whose snapshot is skewed from the primary), wall aggregation, depth-bin
merge, and an honest ``fetched_at`` anchored to the primary's own snapshot time.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from hunt_core.maps.config import MapsConfig, load_maps_config
from hunt_core.maps.orderbook import merge_full_depth_bins
from hunt_core.toolkit.book_math import aggregate_cross_exchange_walls

LOG = structlog.get_logger("hunt_core.maps.cross")

_PRIMARY = "binance"
_CROSS_BOOK_STALE_MS = 750.0
_EMPTY: dict[str, Any] = {"venues": [], "bid_levels": [], "ask_levels": [], "source": "cross_exchange"}


def _reference_mid(snap: dict[str, Any] | None) -> float:
    """Mid of a venue snapshot (best-bid fallback) — the depth-bin grid centres on this."""
    if not isinstance(snap, dict):
        return 0.0
    bid = float(snap.get("bid_price") or 0.0)
    asks = snap.get("asks") or []
    ask = 0.0
    if asks:
        first = asks[0]
        try:
            ask = float(first.get("price", 0.0)) if isinstance(first, dict) else float(first[0])
        except (TypeError, ValueError, IndexError, KeyError):
            ask = 0.0
    if bid > 0 and ask > bid:
        return (bid + ask) / 2.0
    return bid


def _stale_venues_by_alignment(stamped: dict[str, float], *, primary: str, stale_ms: float) -> list[str]:
    """Secondary venues whose snapshot is >``stale_ms`` from the primary's clock (primary never dropped)."""
    if len(stamped) < 2:
        return []
    ref = stamped.get(primary, max(stamped.values()))
    return [ex for ex, ts in stamped.items() if ex != primary and abs(ref - ts) > stale_ms]


def _anchor_fetched_at_ms(stamped: dict[str, float], kept: list[str], *, primary: str) -> float | None:
    """Fetch-time to stamp on the merged book: primary's own time, else the oldest kept, else ``None``."""
    if primary in stamped:
        return stamped[primary]
    kept_stamps = [stamped[ex] for ex in kept if ex in stamped]
    return min(kept_stamps) if kept_stamps else None


def aggregate_cross_walls(
    per_ex: dict[str, dict[str, Any]], *, cfg: MapsConfig | None = None
) -> dict[str, Any]:
    """Merge per-venue depth snapshots into a time-aligned cross-book wall dict (empty if none usable)."""
    cfg = cfg or load_maps_config()
    per_ex = {ex: s for ex, s in per_ex.items() if isinstance(s, dict) and s.get("bid_price")}
    if not per_ex:
        return dict(_EMPTY)
    stamped = {
        ex: float(s["fetched_at_ms"])
        for ex, s in per_ex.items()
        if isinstance(s.get("fetched_at_ms"), (int, float))
    }
    excluded = _stale_venues_by_alignment(stamped, primary=_PRIMARY, stale_ms=_CROSS_BOOK_STALE_MS)
    for ex in excluded:
        per_ex.pop(ex, None)
    if not per_ex:
        return dict(_EMPTY)

    merged = aggregate_cross_exchange_walls(per_ex)
    if excluded:
        merged["stale_venues_excluded"] = excluded
    anchor = _anchor_fetched_at_ms(stamped, list(per_ex.keys()), primary=_PRIMARY)
    merged["fetched_at"] = (
        datetime.fromtimestamp(anchor / 1000.0, tz=UTC).isoformat()
        if anchor is not None
        else datetime.now(UTC).isoformat()
    )
    price = _reference_mid(per_ex.get(_PRIMARY))
    if price <= 0:
        for snap in per_ex.values():
            price = _reference_mid(snap)
            if price > 0:
                break
    if price > 0:
        try:
            merged["depth_bins"] = merge_full_depth_bins(
                per_ex, current_price=price, n_buckets=cfg.n_buckets, price_range_pct=cfg.price_range_pct
            )
        except Exception:  # noqa: BLE001 — depth-bin merge is best-effort telemetry
            LOG.debug("cross_depth_bins_merge_failed", exc_info=True)
    merged["per_exchange"] = {
        ex: {
            "bid_levels": snap.get("bid_levels") or [],
            "ask_levels": snap.get("ask_levels") or [],
            "bids": snap.get("bids") or [],
            "asks": snap.get("asks") or [],
            "depth_imbalance": snap.get("depth_imbalance"),
        }
        for ex, snap in per_ex.items()
    }
    return merged


__all__ = ["aggregate_cross_walls"]
