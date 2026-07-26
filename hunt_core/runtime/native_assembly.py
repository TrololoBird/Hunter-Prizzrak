"""Native per-symbol assembly (ADR-0004 S8/S10) — the composition that replaces ``snapshot_symbol``.

Ties the from-scratch producers together for one tracked symbol: ``MarketView`` (engine read-through)
→ ``FeaturePanel`` (features) + ``MapBundle`` (maps, with real cross-DOM + OI bars) → ``PrizrakOutput``
(prizrak). This is the ~40-line native path the 851-line row-dict builder collapses to; the main tick
and the deep/analyst loop call this instead of building a row. Fully typed, fail-loud, no fabrication.
"""
from __future__ import annotations

import time
from typing import Any, NamedTuple

import polars as pl
import structlog

from hunt_core.engine import rest
from hunt_core.features.build import compute_features
from hunt_core.features.models import FeaturePanel
from hunt_core.maps.cross import aggregate_cross_walls
from hunt_core.maps.engine import MapBundle, MapTimeSeriesStore
from hunt_core.maps.feed import build_map_bundle
from hunt_core.engine.funding_stats import funding_trend, funding_zscore
from hunt_core.engine.oi_stats import oi_change, oi_series
from hunt_core.toolkit.robust_stats import robust_z
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
# 24 bars of 1h-period OI history = a 24h move. Not a round number picked for looks (I-7): the
# consumer `classify_oi_regime` bands OI at ±15%, and a 2026-07-26 measurement over 8 majors put the
# hour-over-hour move at ≤0.33% and the 24h move at ≤2.54% — only the latter is on the band's order
# of magnitude. The fetched series is 48 bars, so this window always has its baseline.
_OI_CHANGE_WINDOW_BARS = 24

# Funding history TTL. NOT a "reasonable value" (I-7): Binance settles funding every **8h**, so the
# derived z-score/trend can only change when a new record settles. A 3600s TTL therefore cannot miss
# a settlement by more than 1h — an 8× margin — while cutting the fetch to one call per symbol per
# hour. `fetch_funding_rate_history` is a normal REST endpoint, NOT `/futures/data`, so it is outside
# the IP-window that produced the -1003 bans; the limit of 16 records spans ~5 days, enough for both
# `funding_zscore(min_records=6)` and `funding_trend(window=4)`.
_FUNDING_TTL_S = 3600.0
_FUNDING_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _fetch_oi_bars(
    exchange: Any, symbol: str, view: MarketView
) -> tuple[list[dict[str, Any]] | None, float | None, float | None]:
    """1h OI history (48 bars) joined to the 1h frame, plus the OI %-change over that window.

    The raw OI rows are cached per symbol for ``_OI_BARS_TTL_S`` (they change on Binance's ~5-min
    cadence); the as-of join to the live 1h frame runs every call. This throttles the /futures/data
    REST volume that a live run showed was tripping -1003 IP bans.

    The %-change is derived from the SAME already-fetched rows via ``engine/oi_stats.py``, so
    closing the ``oi_change_pct`` gap costs **zero** extra requests. That producer was built
    2026-07-19 (`f040786`, ADR-0004 S8) and sat unconsumed until 2026-07-26 — meanwhile
    ``oi_regime_from_row`` resolved every regime to ``"unknown"`` for want of this one number.

    Returns:
        ``(bars, oi_change_pct, oi_z)`` — each independently ``None`` when its input is absent.
    """
    h1 = view.klines.h1
    if h1 is None:
        return None, None, None
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
        return None, None, None
    # ×100: `oi_change` returns a FRACTION, the field is named `_pct`. This project has already
    # shipped a fraction under a percent name once (`buffer_pct` → negative stop price), so the
    # conversion is explicit and local. window=24 over 1h-period rows = a 24h move, the only
    # pairing that can reach `classify_oi_regime`'s ±15% band (measurement in `oi_stats.oi_change`).
    raw = oi_change(oi_series(rows), window=_OI_CHANGE_WINDOW_BARS)
    oi_change_pct = raw * 100.0 if raw is not None else None
    # z-скор той же серии — бесплатно, строки уже скачаны. `features/build.py` держал
    # `deriv_oi_z=None` с пометкой «needs OI-history z-score refresher (tracked)»; refresher
    # существует с 2026-07-19 (`engine/oi_stats.py::oi_series`), а `_fetch_oi_bars` тянет 48
    # часовых баров с 2026-07-22. `robust_z` отдаёт None при нехватке точек и на константной
    # серии — то есть «мерить нечего», а не сфабрикованный 0.0 (I-6).
    oi_z = robust_z(pl.Series("oi", oi_series(rows)))
    bars = oi_bars_from_frames(rows, h1)
    return (bars or None), oi_change_pct, oi_z


def _price_change_pct(view: MarketView, *, window: int = _OI_CHANGE_WINDOW_BARS) -> float | None:
    """Close-to-close %-change over the SAME ``window`` of 1h bars the OI change uses, or ``None``.

    Pairing matters: ``classify_oi_regime`` reads OI-move against price-move, and comparing a 24h OI
    change to, say, a 1h price change would label ordinary drift as a squeeze. Both sides therefore
    come from the same window and the same 1h cadence.

    Frames are closed-only post-finalize, so ``-1`` IS the newest closed bar (I-5) — no ``-2``
    "safety" offset, which would serve a stale bar.
    """
    h1 = view.klines.h1
    if h1 is None or "close" not in h1.columns or h1.height < window + 1:
        return None
    closes = h1.get_column("close")
    last = closes[-1]
    prev = closes[-1 - window]
    if last is None or prev is None or float(prev) <= 0:
        return None
    return (float(last) / float(prev) - 1.0) * 100.0


async def _funding_stats(exchange: Any, symbol: str) -> tuple[float | None, str | None]:
    """``(funding_zscore, funding_trend)`` over settled history, or ``(None, None)`` fail-loud.

    ``view/build.py`` deliberately leaves both fields ``None`` — they need funding *history*, not a
    per-tick plane — and marks them "deferred to features/". The deferral was never collected: the
    pure producer ``engine/funding_stats.py`` shipped 2026-07-18 (`08ae584`, ADR-0003 E4a) and stayed
    unconsumed, so ``features/feature_engine.py`` derived ``funding_velocity`` from a field that was
    ALWAYS ``None``. Fetched here rather than in ``features/`` because this is the async layer that
    may do REST; the statistics themselves stay pure. Wired 2026-07-26.
    """
    now = time.monotonic()
    cached = _FUNDING_CACHE.get(symbol)
    if cached is not None and now - cached[0] < _FUNDING_TTL_S:
        records: list[dict[str, Any]] = cached[1]
    else:
        records = await rest.fetch_funding_history(exchange, symbol, limit=16)
        if records:  # fail-loud: an empty fetch is not an empty history, so it is not cached
            _FUNDING_CACHE[symbol] = (now, records)
    if not records:
        return None, None
    return funding_zscore(records), funding_trend(records)


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

    # Collect the funding-history deferral `view/build.py` declares. The view is rebuilt per call
    # (`MarketRuntime.view` → `build_market_view`), so copying it mutates no shared state; the
    # frozen/strict models are honoured by passing already-correct types to `model_copy`.
    fz, ftrend = await _funding_stats(ex, symbol)
    if fz is not None or ftrend is not None:
        view = view.model_copy(
            update={"derivs": view.derivs.model_copy(
                update={"funding_zscore": fz, "funding_trend": ftrend}
            )}
        )

    panel = compute_features(view)

    trades = list((getattr(ex, "trades", {}) or {}).get(symbol) or [])
    cross_liq = rt.multi.cross_liquidations(symbol)
    contract_sizes: dict[str, float | None] = {"binance": eng.contract_size(symbol)}
    oi_bars, oi_change_pct, oi_z = await _fetch_oi_bars(ex, symbol, view)
    cross_walls = aggregate_cross_walls(await rt.multi.cross_orderbook(symbol))

    maps = build_map_bundle(
        view,
        store=store,
        trades=trades,
        cross_liq=cross_liq,
        contract_sizes=contract_sizes,
        oi_bars=oi_bars,
        oi_z=oi_z,  # измеренный здесь; panel.factors.deriv_oi_z структурно None
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
    # Fusion is display/journal-only (no emission gate reads it). `oi_change_pct` is now supplied
    # from the OI rows this function already fetched (follow-up #38 closed 2026-07-26 — the typed
    # producer `engine/oi_stats.py` had existed since 2026-07-19 unconsumed, so every OI regime
    # resolved to "unknown"). lifecycle/structure still have no typed producer → None, checks stay
    # inert rather than fabricated (I-6).
    fusion = compute_manipulation_fusion_native(
        view,
        panel,
        maps,
        session=session,
        oi_change_pct=oi_change_pct,
        price_change_pct=_price_change_pct(view),
    )
    # contract_weekly is the fallback source for underlyings the venue lists no spot market for
    # (Binance's tokenized XAU/XAG perps) — without it those symbols lose the macro horizon entirely.
    # It must be the RAW weekly klines, not `panel.frames.w1`: `_prepare_frame` trims the indicator
    # warm-up (BTC 360 weeks → 161 rows), so a young listing prepares to ZERO — XAG's 29 weeks and
    # XAU's 33 both did. A level ladder reads swing pivots and needs no EMA200 runway.
    spot_ladder = await spot_weekly_ladder_native(
        symbol, price=view.last_price, spot=rt.spot, contract_weekly=view.klines.w1
    )
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
