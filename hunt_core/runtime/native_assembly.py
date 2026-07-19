"""Native per-symbol assembly (ADR-0004 S8/S10) — the composition that replaces ``snapshot_symbol``.

Ties the from-scratch producers together for one tracked symbol: ``MarketView`` (engine read-through)
→ ``FeaturePanel`` (features) + ``MapBundle`` (maps, with real cross-DOM + OI bars) → ``PrizrakOutput``
(prizrak). This is the ~40-line native path the 851-line row-dict builder collapses to; the main tick
and the deep/analyst loop call this instead of building a row. Fully typed, fail-loud, no fabrication.
"""
from __future__ import annotations

from typing import Any, NamedTuple

import structlog

from hunt_core.engine import rest
from hunt_core.features.build import compute_features
from hunt_core.features.models import FeaturePanel
from hunt_core.maps.cross import aggregate_cross_walls
from hunt_core.maps.engine import MapBundle, MapTimeSeriesStore
from hunt_core.maps.feed import build_map_bundle
from hunt_core.maps.oi import oi_bars_from_frames
from hunt_core.prizrak.assemble import assemble_prizrak
from hunt_core.prizrak.models import PrizrakOutput
from hunt_core.view.models import MarketView
from hunt_core.view.runtime import MarketRuntime

LOG = structlog.get_logger("hunt.runtime.native_assembly")


class NativeAnalystView(NamedTuple):
    """The full typed native output for one symbol — replaces the ``dict[str, Any]`` row."""

    view: MarketView
    features: FeaturePanel
    maps: MapBundle | None
    prizrak: PrizrakOutput


def _binance_id(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "")


async def _fetch_oi_bars(exchange: Any, symbol: str, view: MarketView) -> list[dict[str, Any]] | None:
    """1h open-interest history (48 bars) as-of-joined to the 1h kline frame, or ``None`` fail-loud."""
    h1 = view.klines.h1
    if h1 is None:
        return None
    rows = await rest.poll_futures_data(
        exchange, "fapiDataGetOpenInterestHist", {"symbol": _binance_id(symbol), "period": "1h", "limit": 48}
    )
    if not rows:
        return None
    bars = oi_bars_from_frames(rows, h1)
    return bars or None


async def assemble_native_analyst(
    rt: MarketRuntime, symbol: str, *, store: MapTimeSeriesStore
) -> NativeAnalystView | None:
    """Compose the full typed native view for ``symbol``, or ``None`` if no price (no fabricated view)."""
    view = rt.view(symbol)
    if view is None:
        return None
    eng = rt.multi.primary
    ex = eng.exchange

    panel = compute_features(view)

    trades = list((getattr(ex, "trades", {}) or {}).get(symbol) or [])
    cross_liq = rt.multi.cross_liquidations(symbol)
    contract_sizes: dict[str, float | None] = {"binance": eng.contract_size(symbol)}
    oi_bars = await _fetch_oi_bars(ex, symbol, view)
    cross_walls = aggregate_cross_walls(await rt.multi.cross_orderbook(symbol))

    maps = build_map_bundle(
        view,
        store=store,
        trades=trades,
        cross_liq=cross_liq,
        contract_sizes=contract_sizes,
        oi_bars=oi_bars,
        oi_z=panel.factors.deriv_oi_z,
        cross_walls=cross_walls,
    )
    prizrak = assemble_prizrak(view, maps)
    return NativeAnalystView(view=view, features=panel, maps=maps, prizrak=prizrak)


__all__ = ["NativeAnalystView", "assemble_native_analyst"]
