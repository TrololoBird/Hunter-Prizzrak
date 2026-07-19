"""Native maps feeder (ADR-0004 S4) — ``build_map_bundle(view) → MapBundle``.

Re-sources the three (unchanged) map builders from a :class:`MarketView` instead of the row-dict:
book/trades/liq/derivs come off the view; the map-owned time-series (book history + per-venue liq
deques) stay in :class:`MapTimeSeriesStore`, now fed from the view read-through rather than a WS
callback. Fail-loud: an absent datum is ``None`` (never a fabricated 0). ``bracket_tiers`` is
deliberately **not** passed (no synthetic leverage ladder — the liq map derives magnets from real
forceOrder events + OI); ``cross_walls``/``cross_vp`` come from the cross-microstructure build (added
next) — ``None`` here is a not-yet-wired input on the additive path, not a shipped degradation.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from hunt_core.maps.config import MapsConfig, load_maps_config
from hunt_core.maps.engine import MapBundle, MapTimeSeriesStore
from hunt_core.maps.liquidation import LiqEvent, build_liquidation_map
from hunt_core.maps.orderbook import build_orderbook_map
from hunt_core.maps.volume_profile import build_volume_profile_map
from hunt_core.view.models import MarketView

_VP_TF_FIELD: tuple[tuple[str, str], ...] = (
    ("15m", "m15"), ("1h", "h1"), ("4h", "h4"), ("1d", "d1"), ("1w", "w1")
)


class MapTrade(NamedTuple):
    """Normalized live trade — the map footprint/CVD reads ``ts_ms``/``price``/``qty``/``is_buy``."""

    ts_ms: int
    price: float
    qty: float
    is_buy: bool


def _to_map_trades(trades: list[dict[str, Any]] | None) -> list[MapTrade]:
    """ccxt unified trade dicts → ``MapTrade`` tuples (fail-loud skip, never a fabricated side/qty)."""
    out: list[MapTrade] = []
    for tr in trades or []:
        ts, px, amt, side = tr.get("timestamp"), tr.get("price"), tr.get("amount"), tr.get("side")
        if ts is None or px is None or amt is None or side not in ("buy", "sell"):
            continue
        out.append(MapTrade(int(ts), float(px), float(amt), side == "buy"))
    return out


def _ccxt_liq_to_event(ev: dict[str, Any], *, symbol: str, contract_size: float | None) -> LiqEvent | None:
    """ccxt liquidation dict → ``LiqEvent`` (ts, SYMBOL, SIDE, base-qty, price); base-qty = contracts×size.

    Notional is computed from base units so ``qty×price`` is correct cross-venue (never trusts the
    payload's baseValue/quoteValue). Fail-loud ``None`` on any missing/unsizable field.
    """
    ts, side, contracts, price = ev.get("timestamp"), ev.get("side"), ev.get("contracts"), ev.get("price")
    if ts is None or side not in ("buy", "sell") or contracts is None or price is None:
        return None
    raw_cs = ev.get("contractSize")
    cs = float(raw_cs) if raw_cs not in (None, 0) else contract_size
    if cs is None or cs <= 0:
        return None
    qty_base = float(contracts) * cs
    if qty_base <= 0 or float(price) <= 0:
        return None
    return (int(ts), symbol, side.upper(), qty_base, float(price))


def _ingest_liq(
    store: MapTimeSeriesStore,
    symbol: str,
    cross_liq: dict[str, list[dict[str, Any]] | None] | None,
    contract_sizes: dict[str, float | None] | None,
) -> None:
    """Accumulate per-venue read-through liquidation events into the map-owned rolling deques."""
    for venue, evs in (cross_liq or {}).items():
        if not evs:  # None (stale/absent) or [] — nothing to add
            continue
        buf = store.liq_buffer(symbol, venue)
        last = buf[-1][0] if buf else -1
        for ev in evs:
            le = _ccxt_liq_to_event(ev, symbol=symbol, contract_size=(contract_sizes or {}).get(venue))
            if le is None:
                continue
            if le[0] > last or le not in buf:  # newer, or a same-ms event not already held
                buf.append(le)


def build_map_bundle(
    view: MarketView,
    *,
    store: MapTimeSeriesStore,
    trades: list[dict[str, Any]] | None = None,
    cross_liq: dict[str, list[dict[str, Any]] | None] | None = None,
    contract_sizes: dict[str, float | None] | None = None,
    oi_bars: list[dict[str, Any]] | None = None,
    oi_z: float | None = None,
    cross_walls: dict[str, Any] | None = None,
    cross_vp: dict[str, Any] | None = None,
    cfg: MapsConfig | None = None,
) -> MapBundle | None:
    """Assemble the three maps for ``view`` from the engine read-through, or ``None`` if disabled/no price."""
    cfg = cfg or load_maps_config()
    price = view.last_price
    if not cfg.enabled or price <= 0:
        return None
    store.touch_symbol(view.symbol)

    bids = list(view.book.bids or ())
    asks = list(view.book.asks or ())
    top_n = cfg.book_deep_top_n
    if bids or asks:
        store.sample_book(view.symbol, {"bids": bids[:top_n], "asks": asks[:top_n]})

    map_trades = _to_map_trades(trades)
    _ingest_liq(store, view.symbol, cross_liq, contract_sizes)
    buffers = store.liq_buffers(view.symbol)

    orderbook = build_orderbook_map(
        symbol=view.symbol,
        current_price=price,
        bids=bids or None,
        asks=asks or None,
        cross_walls=cross_walls,
        trades=map_trades,
        daily_volume=view.quote_volume_24h or 0.0,
        book_history=store.book_history(view.symbol),
        price_change_pct=view.orderflow.price_chg_1m,
        deep_bids=bids or None,  # engine book is already deep (ORDER_BOOK_LIMIT=1000)
        deep_asks=asks or None,
        cfg=cfg,
    )
    liq_map = build_liquidation_map(
        buffers,
        symbol=view.symbol,
        current_price=price,
        cfg=cfg,
        bracket_tiers=None,  # NO synthetic ladder — magnets come from real forceOrder events + OI
        oi_bars=oi_bars,
        global_ls_ratio=view.derivs.global_ls_5m,
        oi_usd=view.derivs.oi,
        funding_rate=view.derivs.funding,
        top_ls_ratio=view.derivs.top_ls_acct_5m,
        basis_pct=view.derivs.basis,
    )
    vp_frames = {
        tf: frame for tf, field in _VP_TF_FIELD if (frame := getattr(view.klines, field)) is not None
    }
    vp_map = build_volume_profile_map(
        symbol=view.symbol, current_price=price, frames=vp_frames, cross_vp=cross_vp, cfg=cfg
    )
    return MapBundle(
        symbol=view.symbol,
        ts_ms=view.now_ms,
        orderbook=orderbook,
        liquidation=liq_map,
        volume_profile=vp_map,
        extra={
            "oi_z": oi_z,
            "funding": view.derivs.funding,
            "basis": view.derivs.basis,
            "ws_cvd": view.orderflow.cvd_5m,
        },
    )


__all__ = ["MapTrade", "build_map_bundle"]
