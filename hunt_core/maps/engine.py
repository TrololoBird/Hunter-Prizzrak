"""Maps engine — time-series store, bundle builder, scalar feature derivation."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from hunt_core.maps.config import MapsConfig, load_maps_config
from hunt_core.maps.liquidation import (
    LiqEvent,
    build_liquidation_map,
    calibration_confidence,
)
from hunt_core.maps.orderbook import OrderbookMap, build_orderbook_map, derive_ob_accumulation_features
from hunt_core.maps.volume_profile import VolumeProfileMap, build_volume_profile_map, derive_vp_accumulation_features

LOG = logging.getLogger("hunt_core.maps.engine")


@dataclass
class MapBundle:
    symbol: str
    ts_ms: int
    orderbook: OrderbookMap | None = None
    liquidation: Any | None = None
    volume_profile: VolumeProfileMap | None = None
    # Raw cross-signal context (oi_z, funding, basis, ws_cvd) used for accumulation/squeeze fusion.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts_ms": self.ts_ms,
            "orderbook": self.orderbook.to_dict() if self.orderbook else None,
            "liquidation": self.liquidation.to_dict() if self.liquidation else None,
            "volume_profile": self.volume_profile.to_dict() if self.volume_profile else None,
            "extra": dict(self.extra),
        }


class MapTimeSeriesStore:
    """Per-symbol ring buffers for book samples, liq events, VP snapshots."""

    def __init__(self, cfg: MapsConfig | None = None) -> None:
        self.cfg = cfg or load_maps_config()
        self._book_history: dict[str, deque[dict[str, Any]]] = {}
        self._liq_by_venue: dict[str, dict[str, deque[LiqEvent]]] = {}
        self._vp_snapshots: dict[str, deque[dict[str, Any]]] = {}
        self._last_book_sample: dict[str, float] = {}
        self._oi_bars_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._liq_estimate_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._symbol_touch_order: deque[str] = deque()
        self._lock = threading.Lock()
        self._map_lake_buf: list[str] = []

    def touch_symbol(self, symbol: str) -> None:
        """Track symbol access; evict oldest when over max_symbols."""
        sym = symbol.upper()
        with self._lock:
            if sym in self._symbol_touch_order:
                try:
                    self._symbol_touch_order.remove(sym)
                except ValueError:
                    pass
            self._symbol_touch_order.append(sym)
            while len(self._symbol_touch_order) > self.cfg.max_symbols:
                old = self._symbol_touch_order.popleft()
                self._evict_symbol_unlocked(old)

    def _evict_symbol_unlocked(self, symbol: str) -> None:
        sym = symbol.upper()
        self._book_history.pop(sym, None)
        self._liq_by_venue.pop(sym, None)
        self._vp_snapshots.pop(sym, None)
        self._last_book_sample.pop(sym, None)
        self._oi_bars_cache.pop(sym, None)
        self._liq_estimate_cache.pop(sym, None)
        try:
            self._symbol_touch_order.remove(sym)
        except ValueError:
            pass

    def evict_symbol(self, symbol: str) -> None:
        sym = symbol.upper()
        with self._lock:
            self._evict_symbol_unlocked(sym)

    def cache_oi_bars(self, symbol: str, bars: list[dict[str, Any]], *, ttl_s: float = 300.0) -> None:
        if bars:
            self._oi_bars_cache[symbol.upper()] = (time.monotonic() + ttl_s, bars)

    def get_cached_oi_bars(self, symbol: str) -> list[dict[str, Any]] | None:
        sym = symbol.upper()
        row = self._oi_bars_cache.get(sym)
        if not row:
            return None
        expires, bars = row
        if time.monotonic() > expires:
            self._oi_bars_cache.pop(sym, None)
            return None
        return bars

    def cache_liq_estimate(self, symbol: str, payload: dict[str, Any], *, ttl_s: float = 300.0) -> None:
        self._liq_estimate_cache[symbol.upper()] = (time.monotonic() + ttl_s, payload)

    def get_cached_liq_estimate(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.upper()
        row = self._liq_estimate_cache.get(sym)
        if not row:
            return None
        expires, payload = row
        if time.monotonic() > expires:
            self._liq_estimate_cache.pop(sym, None)
            return None
        return payload

    def _book_deque(self, symbol: str) -> deque[dict[str, Any]]:
        sym = symbol.upper()
        if sym not in self._book_history:
            self._book_history[sym] = deque(maxlen=self.cfg.retention_samples)
        return self._book_history[sym]

    def liq_buffer(self, symbol: str, venue: str = "binance") -> deque[LiqEvent]:
        sym = symbol.upper()
        with self._lock:
            venue_map = self._liq_by_venue.setdefault(sym, {})
            if venue not in venue_map:
                venue_map[venue] = deque(maxlen=8_000)
            return venue_map[venue]

    def record_liquidation(
        self,
        symbol: str,
        *,
        venue: str,
        ts_ms: int,
        side: str,
        qty: float,
        price: float,
    ) -> None:
        if qty <= 0 or price <= 0:
            return
        buf = self.liq_buffer(symbol, venue)
        buf.append((ts_ms, symbol.upper(), side.upper(), qty, price))

    def sample_book(self, symbol: str, snap: dict[str, Any]) -> None:
        sym = symbol.upper()
        now = time.monotonic()
        last = self._last_book_sample.get(sym, 0.0)
        if now - last < self.cfg.book_sample_interval_s:
            return
        self._last_book_sample[sym] = now
        self._book_deque(sym).append(snap)

    def book_history(self, symbol: str) -> deque[dict[str, Any]]:
        return self._book_deque(symbol.upper())

    def liq_buffers(self, symbol: str) -> dict[str, deque[LiqEvent]]:
        return dict(self._liq_by_venue.get(symbol.upper(), {}))

    def record_vp_snapshot(self, symbol: str, snap: dict[str, Any]) -> None:
        sym = symbol.upper()
        with self._lock:
            if sym not in self._vp_snapshots:
                self._vp_snapshots[sym] = deque(maxlen=self.cfg.retention_samples)
            self._vp_snapshots[sym].append(snap)

    def enqueue_lake(self, bundle: MapBundle) -> None:
        self._map_lake_buf.append(json.dumps(bundle.to_dict(), default=str))

    def flush_lake(self, path: Path) -> int:
        if not self._map_lake_buf:
            return 0
        from hunt_core.data.jsonl_io import append_jsonl_lines

        lines = list(self._map_lake_buf)
        self._map_lake_buf.clear()
        path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(path, lines)
        return len(lines)


# Module-level singleton for tick path
_STORE: MapTimeSeriesStore | None = None


def get_map_store(cfg: MapsConfig | None = None) -> MapTimeSeriesStore:
    global _STORE
    if _STORE is None:
        _STORE = MapTimeSeriesStore(cfg or load_maps_config())
    return _STORE


def build_map_bundle(
    *,
    symbol: str,
    current_price: float,
    ws_snap: dict[str, Any] | None = None,
    book_walls: dict[str, Any] | None = None,
    live_book: dict[str, Any] | None = None,
    trades: deque[Any] | list[Any] | None = None,
    liq_buffers: dict[str, deque[LiqEvent]] | None = None,
    frames: dict[str, pl.DataFrame] | None = None,
    cross_vp: dict[str, Any] | None = None,
    bracket_tiers: list[dict[str, Any]] | None = None,
    oi_bars: list[dict[str, Any]] | None = None,
    global_ls_ratio: float | None = None,
    oi_usd: float | None = None,
    daily_volume: float = 0.0,
    price_change_pct: float | None = None,
    deep_bids: list[tuple[float, float]] | None = None,
    deep_asks: list[tuple[float, float]] | None = None,
    funding_rate: float | None = None,
    top_ls_ratio: float | None = None,
    basis_pct: float | None = None,
    oi_z: float | None = None,
    ws_cvd: float | None = None,
    store: MapTimeSeriesStore | None = None,
    cfg: MapsConfig | None = None,
) -> MapBundle | None:
    """Build all three maps for a symbol tick (in-memory, no network)."""
    cfg = cfg or load_maps_config()
    if not cfg.enabled or current_price <= 0:
        return None
    store = store or get_map_store(cfg)
    store.touch_symbol(symbol)
    ts_ms = int(time.time() * 1000)

    if oi_bars is None:
        oi_bars = store.get_cached_oi_bars(symbol)
    if oi_bars is not None and not isinstance(oi_bars, list):
        oi_bars = None
    if bracket_tiers is not None and not isinstance(bracket_tiers, list):
        bracket_tiers = None

    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    if live_book:
        bids = [(float(live_book["bid"]), float(live_book.get("bid_qty") or 0))] if live_book.get("bid") else []
        asks = [(float(live_book["ask"]), float(live_book.get("ask_qty") or 0))] if live_book.get("ask") else []
        raw_bids = live_book.get("bids")
        raw_asks = live_book.get("asks")
        if isinstance(raw_bids, list) and raw_bids:
            bids = [(float(x[0]), float(x[1])) for x in raw_bids if len(x) >= 2]
        if isinstance(raw_asks, list) and raw_asks:
            asks = [(float(x[0]), float(x[1])) for x in raw_asks if len(x) >= 2]

    if bids or asks:
        # Sample DEEP levels into the history (book_deep_top_n, default 50), not just the
        # top 10. Sticky-wall detection runs off this history, and on a liquid symbol the
        # top 10 levels sit within a few bps of price — so a wall 1.5-3% out was never
        # even recorded, and WO#6's "show the deep defended level" had nothing to show.
        deep_n = max(10, int(cfg.book_deep_top_n))
        store.sample_book(
            symbol,
            {
                "bid_levels": [
                    {"price": p, "qty": q, "notional_usd": p * q} for p, q in bids[:deep_n]
                ],
                "ask_levels": [
                    {"price": p, "qty": q, "notional_usd": p * q} for p, q in asks[:deep_n]
                ],
            },
        )

    orderbook = None
    try:
        orderbook = build_orderbook_map(
            symbol=symbol,
            current_price=current_price,
            bids=bids or None,
            asks=asks or None,
            cross_walls=book_walls,
            trades=trades,
            daily_volume=daily_volume,
            book_history=store.book_history(symbol),
            price_change_pct=price_change_pct,
            deep_bids=deep_bids,
            deep_asks=deep_asks,
            cfg=cfg,
        )
    except Exception as exc:
        LOG.warning("orderbook_map_failed | symbol=%s error=%s", symbol, exc)

    buffers = liq_buffers or store.liq_buffers(symbol)
    if ws_snap and not buffers:
        buffers = {"binance": store.liq_buffer(symbol, "binance")}

    liq_map = None
    try:
        liq_map = build_liquidation_map(
            buffers,
            symbol=symbol.upper(),
            current_price=current_price,
            cfg=cfg,
            bracket_tiers=bracket_tiers,
            oi_bars=oi_bars,
            global_ls_ratio=global_ls_ratio,
            oi_usd=oi_usd,
            funding_rate=funding_rate,
            top_ls_ratio=top_ls_ratio,
            basis_pct=basis_pct,
        )
        if liq_map and liq_map.forward_zones and liq_map.realized_zones:
            conf = calibration_confidence(liq_map.forward_zones, liq_map.realized_zones)
            hm = liq_map.heatmap
            from hunt_core.maps.liquidation import LiquidationHeatmap

            liq_map.heatmap = LiquidationHeatmap(
                clusters=hm.clusters,
                density_zones=hm.density_zones,
                nearest_long_liquidation=hm.nearest_long_liquidation,
                nearest_short_liquidation=hm.nearest_short_liquidation,
                cascade_risk_direction=hm.cascade_risk_direction,
                total_long_at_risk=hm.total_long_at_risk,
                total_short_at_risk=hm.total_short_at_risk,
                forward_confidence=conf,
                venues=hm.venues,
                realized_event_count=hm.realized_event_count,
            )
    except Exception as exc:
        LOG.warning("liquidation_map_failed | symbol=%s error=%s", symbol, exc)

    vp_map = None
    if frames is not None and len(frames) > 0:
        try:
            vp_map = build_volume_profile_map(
                symbol=symbol,
                current_price=current_price,
                frames=frames,
                cross_vp=cross_vp,
                cfg=cfg,
            )
            if vp_map:
                store.record_vp_snapshot(symbol, vp_map.to_dict())
        except Exception as exc:
            LOG.warning("volume_profile_map_failed | symbol=%s error=%s", symbol, exc)

    if orderbook is None and liq_map is None and vp_map is None:
        return None

    extra: dict[str, Any] = {}
    if oi_z is not None:
        extra["map_oi_z"] = round(float(oi_z), 4)
    if funding_rate is not None:
        extra["map_funding_rate"] = float(funding_rate)
    if basis_pct is not None:
        extra["map_basis_pct"] = round(float(basis_pct), 4)
    if ws_cvd is not None:
        extra["map_ws_cvd"] = float(ws_cvd)

    bundle = MapBundle(
        symbol=symbol.upper(),
        ts_ms=ts_ms,
        orderbook=orderbook,
        liquidation=liq_map,
        volume_profile=vp_map,
        extra=extra,
    )
    try:
        store.enqueue_lake(bundle)
    except Exception as exc:
        LOG.debug("map_lake_enqueue_skipped | symbol=%s error=%s", symbol, exc)
    return bundle


def derive_map_features(
    bundle: MapBundle | None,
    *,
    current_price: float,
    cfg: MapsConfig | None = None,
) -> dict[str, Any]:
    """Scalar features for market dict + scoring/confluence."""
    if bundle is None or current_price <= 0:
        return {}
    cfg = cfg or load_maps_config()
    out: dict[str, Any] = {"maps_ts_ms": bundle.ts_ms}

    if bundle.orderbook:
        ob = bundle.orderbook
        out["map_book_imbalance_1pct"] = ob.zone_imbalance.get("imb_1pct")
        out["map_sticky_wall_count"] = len(ob.sticky_walls)
        out["map_void_count"] = len(ob.liquidity_voids)
        out["map_stacked_imbalance"] = ob.stacked_imbalance
        out["map_wall_cluster_count"] = len(ob.wall_clusters)
        if ob.sticky_walls:
            nearest = min(ob.sticky_walls, key=lambda w: w.get("distance_pct", 99))
            out["map_nearest_sticky_wall_pct"] = nearest.get("distance_pct")
            out["map_nearest_sticky_side"] = nearest.get("side")
        out["map_iceberg_count"] = len(ob.iceberg_levels)
        out["map_absorption_count"] = len(ob.absorption_zones)
        out["map_spoof_count"] = len(ob.spoof_flags)
        out["map_cvd_divergence"] = ob.cvd_divergence
        if ob.footprint_bins:
            delta_sum = sum(float(b.get("delta") or 0) for b in ob.footprint_bins if isinstance(b, dict))
            total = sum(abs(float(b.get("delta") or 0)) for b in ob.footprint_bins if isinstance(b, dict))
            if total > 0:
                out["map_footprint_delta"] = round(delta_sum / total, 4)
        out.update(derive_ob_accumulation_features(ob, current_price=current_price))

    if bundle.liquidation:
        liq = bundle.liquidation
        hm_dict = liq.to_dict() if hasattr(liq, "to_dict") else {}
        out.update({k: v for k, v in hm_dict.items() if k.startswith("liq_")})
        # NOT `or 1.0`: a genuine 0.0 confidence must stay 0.0, not invert to full
        # confidence. Default to 1.0 only when the key is truly absent.
        _conf_raw = hm_dict.get("liq_forward_confidence")
        conf = float(_conf_raw) if _conf_raw is not None else 1.0
        fwd_weight = conf * cfg.forward_blend_ratio
        out["liq_forward_weight"] = round(fwd_weight, 3)
        if liq.magnet_pull_long is not None:
            out["liq_magnet_pull_long_pct"] = liq.magnet_pull_long
        if liq.magnet_pull_short is not None:
            out["liq_magnet_pull_short_pct"] = liq.magnet_pull_short

    if bundle.volume_profile:
        vp = bundle.volume_profile
        out["map_vp_poc"] = vp.primary_poc
        out["map_vp_vah"] = vp.primary_vah
        out["map_vp_val"] = vp.primary_val
        for prof in vp.profiles or []:
            if prof.naked_poc:
                out[f"map_naked_poc_{prof.period}"] = prof.naked_poc
            if prof.poc_migrated:
                out[f"map_poc_migration_{prof.period}"] = prof.poc_migrated
        out.update(derive_vp_accumulation_features(vp, current_price=current_price))

    # Cross-signal context (oi_z, funding, basis, ws_cvd) threaded from the tick.
    out.update(bundle.extra)

    # Accumulation fusion (pre-pump): VP coil + bid absorption + ask thinning +
    # bullish CVD divergence + rising OI. 0-1; the leading "is this coiling for a pump" score.
    acc: list[float] = []
    vp_acc = out.get("map_vp_accumulation")
    if vp_acc is not None:
        acc.append(min(1.0, max(0.0, float(vp_acc))))
    if out.get("map_accum_bid_absorption"):
        acc.append(1.0)
    if out.get("map_ask_thinning"):
        acc.append(0.7)
    if out.get("map_cvd_divergence") == "bullish_div":
        acc.append(0.8)
    oi_z = out.get("map_oi_z")
    if oi_z is not None and float(oi_z) > 0:
        acc.append(min(1.0, float(oi_z) / 2.0))
    if acc:
        out["map_accumulation_score"] = round(sum(acc) / len(acc), 3)

    return out


def apply_map_bundle_to_row(row: dict[str, Any], bundle: MapBundle | None) -> None:
    """Merge map bundle into tick row (market + top-level maps key)."""
    if bundle is None:
        return
    price = float(row.get("price") or 0)
    features = derive_map_features(bundle, current_price=price)
    market = row.setdefault("market", {})
    if isinstance(market, dict):
        market.update(features)
    row["maps"] = bundle.to_dict()
    if bundle.orderbook and not row.get("book_walls"):
        row["book_walls"] = {
            "bid_levels": bundle.orderbook.bid_walls,
            "ask_levels": bundle.orderbook.ask_walls,
            "venues": bundle.orderbook.venues,
            "depth_imbalance": bundle.orderbook.zone_imbalance.get("imb_1pct"),
            "source": bundle.orderbook.source,
        }
