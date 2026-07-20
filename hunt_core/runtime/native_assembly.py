"""Native per-symbol assembly (ADR-0004 S8/S10) — the composition that replaces ``snapshot_symbol``.

Ties the from-scratch producers together for one tracked symbol: ``MarketView`` (engine read-through)
→ ``FeaturePanel`` (features) + ``MapBundle`` (maps, with real cross-DOM + OI bars) → ``PrizrakOutput``
(prizrak). This is the ~40-line native path the 851-line row-dict builder collapses to; the main tick
and the deep/analyst loop call this instead of building a row. Fully typed, fail-loud, no fabrication.
"""
from __future__ import annotations

import time
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
from hunt_core.prizrak.structural_forecast_native import (
    build_structural_down_forecast_native,
    build_structural_up_forecast_native,
)
from hunt_core.runtime.native_producers import (
    cross_walls_fetched_at_ms,
    freshness_native,
    session_stats_native,
    spot_weekly_ladder_native,
)
from hunt_core.toolkit.manipulation_fusion_native import compute_manipulation_fusion_native
from hunt_core.view.models import MarketView
from hunt_core.view.runtime import MarketRuntime

LOG = structlog.get_logger("hunt.runtime.native_assembly")


class NativeAnalystView(NamedTuple):
    """The full typed native output for one symbol — replaces the ``dict[str, Any]`` row.

    ``view``/``features``/``maps``/``prizrak`` are the four core typed handles; the remaining fields
    are the deep-tick enrichment side-channels that ``analyst_assembly`` used to stamp onto the row
    (all natively derived, fail-loud): the structural forecasts, the manipulation-fusion assessment
    (display/journal-only), the weekly-spot ladder, the intraday session stats, and the freshness
    stamp. ``btc_context`` / ``microstructure_by_direction`` are deliberately absent — dead telemetry.
    """

    view: MarketView
    features: FeaturePanel
    maps: MapBundle | None
    prizrak: PrizrakOutput
    forecasts: dict[str, dict[str, Any] | None]
    fusion: dict[str, Any]
    spot_ladder: dict[str, Any] | None
    session: dict[str, float | int | None] | None
    freshness: dict[str, Any]


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

    # ── Deep-tick enrichment side-channels (native, fail-loud) ──────────────────────────────
    # These replace the analyst_assembly row stamps; each reads only typed handles.
    session = session_stats_native(panel.frames.m1, last_price=view.last_price)
    forecasts: dict[str, dict[str, Any] | None] = {
        "structural_up": build_structural_up_forecast_native(view, maps),
        "structural_down": build_structural_down_forecast_native(view, maps, session=session),
    }
    # Fusion is display/journal-only (no emission gate reads it). lifecycle/structure and OI-%change
    # have no typed producer yet (tracked follow-up #38) → passed None, checks inert (not fabricated).
    fusion = compute_manipulation_fusion_native(view, panel, maps, session=session)
    spot_ladder = await spot_weekly_ladder_native(symbol, price=view.last_price, spot=rt.spot)
    freshness = freshness_native(
        now_ms=int(time.time() * 1000),
        tick_ts_ms=int(view.now_ms),
        dom_fetched_at_ms=cross_walls_fetched_at_ms(cross_walls),
    )
    return NativeAnalystView(
        view=view,
        features=panel,
        maps=maps,
        prizrak=prizrak,
        forecasts=forecasts,
        fusion=fusion,
        spot_ladder=spot_ladder,
        session=session,
        freshness=freshness,
    )


__all__ = ["NativeAnalystView", "assemble_native_analyst"]
