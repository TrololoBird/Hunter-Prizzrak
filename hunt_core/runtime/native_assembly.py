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


def _to_unified(symbol: str) -> str:
    """Compact ``BTCUSDT`` → ccxt-unified ``BTC/USDT:USDT`` for engine lookups (idempotent).

    The engine tracks UNIFIED ccxt symbols; the deep/analyst loop and the probe iterate COMPACT ids
    (``PINNED_SYMBOLS`` = ``BTCUSDT``). Passing a compact id straight to ``rt.view`` finds no planes,
    so every symbol comes back falsely ``not_ready`` — the root cause of the deep lane producing
    nothing live (``assemble_analyst_tick`` passed the compact id unchanged). Normalising here makes
    EVERY caller correct regardless of the id form it holds, and is idempotent for already-unified ids.
    """
    s = symbol.upper()
    if "/" in s or ":" in s:
        return s
    base = s[:-4] if s.endswith("USDT") else s
    return f"{base}/USDT:USDT"


# Per-symbol cache of the raw OI-hist rows. The 1h open-interest history is recomputed by Binance on
# a ~5-min cadence (engine/params.py), so refetching it on every 60s tick returns duplicates and burns
# the tight /futures/data budget — a live 20-min run showed that per-tick volume tripping Binance -1003
# IP bans. Cache the rows for ``_OI_BARS_TTL_S`` and re-join them to the fresh 1h frame each tick (the
# join is cheap; only the REST call is throttled). Fail-loud absent stays absent (not cached).
_OI_BARS_TTL_S = 300.0
_OI_BARS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _fetch_oi_bars(exchange: Any, symbol: str, view: MarketView) -> list[dict[str, Any]] | None:
    """1h open-interest history (48 bars) as-of-joined to the 1h kline frame, or ``None`` fail-loud.

    The raw OI rows are cached per symbol for ``_OI_BARS_TTL_S`` (they change on Binance's ~5-min
    cadence); the as-of join to the live 1h frame runs every call. This throttles the /futures/data
    REST volume that a live run showed was tripping -1003 IP bans.
    """
    h1 = view.klines.h1
    if h1 is None:
        return None
    now = time.monotonic()
    cached = _OI_BARS_CACHE.get(symbol)
    if cached is not None and now - cached[0] < _OI_BARS_TTL_S:
        rows: list[dict[str, Any]] | None = cached[1]
    else:
        rows = await rest.poll_futures_data(
            exchange,
            "fapiDataGetOpenInterestHist",
            {"symbol": _binance_id(symbol), "period": "1h", "limit": 48},
        )
        if rows:
            _OI_BARS_CACHE[symbol] = (now, rows)  # cache only real data (fail-loud absent isn't cached)
    if not rows:
        return None
    bars = oi_bars_from_frames(rows, h1)
    return bars or None


async def assemble_native_analyst(
    rt: MarketRuntime, symbol: str, *, store: MapTimeSeriesStore
) -> NativeAnalystView | None:
    """Compose the full typed native view for ``symbol``, or ``None`` if no price (no fabricated view)."""
    symbol = _to_unified(symbol)  # engine tracks unified ids — normalize so compact callers resolve
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
