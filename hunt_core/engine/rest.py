"""Thin ccxt-native REST — three jobs only, each fail-loud + logged (ADR-0002 §6.2, §11.E).

1. **seed** — warm-up history so a plane is never empty before WS fills it.
2. **reseed** — refetch after a detected gap / watchdog trip (same call as seed).
3. **poll positioning** — the un-streamable ``/futures/data/*`` stats on their 5-min native cadence.

All calls go through ccxt's built-in weighted throttler (no custom governor). Nothing here
fabricates a value: a missing/failed datum returns ``None`` and is logged, never a substituted ``0``.
"""
from __future__ import annotations

import math
from typing import Any

import structlog

from hunt_core.engine.freshness import Bar

LOG = structlog.get_logger(__name__)


async def seed_ohlcv(exchange: Any, symbol: str, timeframe: str, *, limit: int) -> list[Bar]:
    """Fetch closed history via REST to warm a kline plane (drops the forming last bar, I-5).

    Returns ``[]`` on failure (logged) — the caller keeps the plane ``absent`` and the snapshot
    reports ``NotReady``, never a fabricated frame.
    """
    # Binance clamps a single fetch_ohlcv to 1000 bars (and a wide since→until returns only the first
    # 1000) — so for deep history route through ccxt's deterministic paginator; a normal ≤1000 seed
    # stays a single call.
    extra = {"paginate": True} if limit > 1000 else {}
    try:
        rows = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit, params=extra)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_seed_ohlcv_failed", symbol=symbol, tf=timeframe, err=str(exc))
        return []
    bars = [list(map(float, r)) for r in (rows or [])]
    return bars[:-1] if bars else []


async def poll_open_interest(exchange: Any, symbol: str) -> float | None:
    """Current open interest (``/fapi/v1/openInterest``, weight 1). ``None`` on failure — no fake 0."""
    try:
        oi = await exchange.fetch_open_interest(symbol)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_poll_oi_failed", symbol=symbol, err=str(exc))
        return None
    value = oi.get("openInterestAmount") if isinstance(oi, dict) else None
    return float(value) if value is not None else None


async def poll_funding_rates(exchange: Any, symbols: list[str]) -> dict[str, float]:
    """Unified cross-venue funding via ``fetch_funding_rates`` (all venues support it).

    Filters to symbols the venue actually lists, parses ``fundingRate`` fail-loud (a missing/garbage
    rate is skipped, never fabricated). Returns ``{unified_symbol: rate}``.
    """
    markets = getattr(exchange, "markets", None) or {}
    wanted = [s for s in symbols if s in markets]
    if not wanted:
        return {}
    try:
        data = await exchange.fetch_funding_rates(wanted)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_poll_funding_failed", venue=getattr(exchange, "id", "?"), err=str(exc))
        return {}
    out: dict[str, float] = {}
    for sym, fr in (data or {}).items():
        rate = fr.get("fundingRate") if isinstance(fr, dict) else None
        if rate is None:
            continue
        try:
            out[sym] = float(rate)
        except (TypeError, ValueError):
            continue
    return out


async def poll_long_short_ratio(
    exchange: Any, symbol: str, *, timeframe: str = "1h"
) -> float | None:
    """Latest global long/short **account** ratio via unified ``fetchLongShortRatioHistory``.

    Portable across all four venues (Binance maps it to ``globalLongShortAccountRatio``). Returns the
    newest record's ``longShortRatio`` as a finite float, or ``None`` fail-loud (venue lacks the
    method / empty history / unparseable) — never a fabricated ``1.0``. The ``has`` guard is silent
    (unsupported-by-design is not a failure; the caller gates which venues it polls at start).

    ``timeframe='1h'`` is deliberate: it is the ONLY period all four venues actually serve — Bybit
    returns an empty history for ``5m``/``15m`` (no sub-hour retention), Bitget errors on ``1d``
    (measured live). A shorter default silently starved Bybit to ``None``. The account ratio barely
    moves across granularities (Binance ``5m`` 1.495 vs ``1h`` 1.4994), so 1h is apples-to-apples
    with the primary engine's ``global_ls_5m`` plane for a divergence signal.
    """
    if not getattr(exchange, "has", {}).get("fetchLongShortRatioHistory"):
        return None
    try:
        rows = await exchange.fetch_long_short_ratio_history(symbol, timeframe, limit=30)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_poll_lsr_failed", venue=getattr(exchange, "id", "?"), symbol=symbol, err=str(exc))
        return None
    if not rows or not isinstance(rows[-1], dict):
        return None
    try:
        value = float(rows[-1].get("longShortRatio"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


async def poll_liquidations(
    exchange: Any, symbol: str, *, limit: int = 100
) -> list[dict[str, Any]] | None:
    """Recent public liquidation events via unified ``fetchLiquidations`` (OKX/Bybit; not Bitget).

    Binance has **no** public REST liquidation endpoint (its liquidations come from the WS
    ``!forceOrder`` stream), so this is capability-gated on ``has['fetchLiquidations']``. Returns the
    raw liquidation structures (notional is computed by the caller via
    :func:`hunt_core.engine.liquidations.liquidation_notional` — the payload's ``baseValue``/
    ``quoteValue`` are unreliable), or ``None`` fail-loud when unsupported/failed. The ``has`` guard
    is silent (unsupported-by-design is not a failure; the caller gates venues at start).
    """
    if not getattr(exchange, "has", {}).get("fetchLiquidations"):
        return None
    try:
        rows = await exchange.fetch_liquidations(symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_poll_liq_failed", venue=getattr(exchange, "id", "?"), symbol=symbol, err=str(exc))
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


async def poll_futures_data(
    exchange: Any, method: str, req_params: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Fetch one ``/futures/data/*`` statistic via ccxt's implicit endpoint (5-min native cadence).

    ``method`` is a ccxt implicit method name (``fapiDataGetOpenInterestHist``,
    ``fapiDataGetTopLongShortAccountRatio``, ``fapiDataGetBasis``, …); ``req_params`` is the exact
    query the caller builds (endpoints differ — OI/LS use ``symbol``, basis uses ``pair`` +
    ``contractType``). Capability-gated: an absent method / failed call yields a loud skip, never a
    fabricated series.
    """
    fn = getattr(exchange, method, None)
    if fn is None:
        LOG.warning("engine_futures_data_unsupported", method=method)
        return None
    try:
        rows = await fn(dict(req_params))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_futures_data_failed", method=method, params=req_params, err=str(exc))
        return None
    return list(rows) if isinstance(rows, list) else None


__all__ = [
    "seed_ohlcv",
    "poll_open_interest",
    "poll_funding_rates",
    "poll_long_short_ratio",
    "poll_liquidations",
    "poll_futures_data",
]
