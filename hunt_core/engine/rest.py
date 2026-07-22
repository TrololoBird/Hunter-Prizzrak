"""Thin ccxt-native REST — three jobs only, each fail-loud + logged (ADR-0002 §6.2, §11.E).

1. **seed** — warm-up history so a plane is never empty before WS fills it.
2. **reseed** — refetch after a detected gap / watchdog trip (same call as seed).
3. **poll positioning** — the un-streamable ``/futures/data/*`` stats on their 5-min native cadence.

All calls go through ccxt's built-in weighted throttler (no custom governor). Nothing here
fabricates a value: a missing/failed datum returns ``None`` and is logged, never a substituted ``0``.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

import structlog

from hunt_core.engine.freshness import Bar

# --- /futures/data ban backoff (Binance error -1003) ---------------------------------------------
# Binance IP-bans callers who exceed the /futures/data/* budget, and a call that RETRIES against a
# banned endpoint gets the ban EXTENDED — worse, a rotated egress IP gets banned too. So on a -1003
# we parse the "banned until <epoch-ms>" and PAUSE every /futures/data call until then: the plane goes
# fail-loud absent (never fabricated) and, crucially, we stop hammering the endpoint into a longer or
# wider ban. Module-level so it is shared across all pollers/symbols on the one egress. (Native ban
# surfacing — the capability the legacy ccxt_guard/weight_registry used to provide.)
_BAN_UNTIL_MS: float = 0.0
_BAN_RE = re.compile(r"banned until (\d+)")
_DEFAULT_BAN_MS = 120_000.0  # fallback pause when a -1003 carries no parseable timestamp


def futures_data_banned_until_ms() -> float:
    """Epoch-ms until which ``/futures/data`` polling is paused by a Binance -1003 ban (0 = clear)."""
    return _BAN_UNTIL_MS

LOG = structlog.get_logger(__name__)


def _market_id(exchange: Any, symbol: str) -> str | None:
    """Exchange market id (e.g. ``'BTCUSDT'``) for a unified symbol, or ``None`` if unknown/unloaded."""
    try:
        return str(exchange.market(symbol)["id"])
    except Exception:  # noqa: BLE001
        return None


async def fetch_klines_full(
    exchange: Any,
    symbol: str,
    timeframe: str,
    *,
    limit: int,
    market_id: str | None = None,
    now_ms: int | None = None,
) -> list[Bar]:
    """Full-fidelity CLOSED klines via Binance ``fapiPublicGetKlines`` (public implicit method).

    ccxt's unified ``fetch_ohlcv``/``watch_ohlcv`` normalize a kline to 6 columns, **dropping**
    ``taker_buy_base_volume`` / ``quote_volume`` / ``num_trades``. The raw ``/fapi/v1/klines`` endpoint
    returns the full 12-element row, so the engine's kline planes carry the REAL taker-buy volume that
    the orderflow CVD / ``delta_ratio`` features depend on — never a zero-fill (no fabrication, I-6).

    Drops the forming last bar (I-5). Fail-loud ``[]`` on failure / a non-Binance client (no
    ``fapiPublicGetKlines``) / an unresolved market id. Returned rows are
    ``[open_ms, o, h, l, c, v, close_ms, quote_vol, num_trades, taker_base, taker_quote]`` — the exact
    shape ``toolkit.ohlcv.ccxt_ohlcv_to_frame`` reads when ``len(row) >= 11``.
    """
    fn = getattr(exchange, "fapiPublicGetKlines", None)
    if fn is None:
        return []
    mid = market_id or _market_id(exchange, symbol)
    if mid is None:
        return []
    try:
        rows = await fn({"symbol": mid, "interval": timeframe, "limit": int(limit)})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_fetch_klines_full_failed", symbol=symbol, tf=timeframe, err=str(exc))
        return []
    out: list[Bar] = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 11:
            continue
        try:
            out.append([float(r[i]) for i in range(11)])
        except (TypeError, ValueError):
            continue
    if not out:
        return []
    interval_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return [b for b in out if int(b[0]) + interval_ms <= now]  # closed-only (I-5)


async def _fetch_ohlcv_raw(
    exchange: Any,
    symbol: str,
    timeframe: str,
    *,
    limit: int,
    since: int | None = None,
    params: dict[str, Any] | None = None,
) -> list[Bar] | None:
    """Core ccxt ``fetch_ohlcv`` → ascending float bars-list, or ``None`` on failure (logged).

    ``None`` (not ``[]``) distinguishes a failed call from a genuine empty window, so callers keep
    a plane ``absent`` rather than fabricating a frame.
    """
    try:
        rows = await exchange.fetch_ohlcv(
            symbol, timeframe, since=since, limit=limit, params=params or {}
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_fetch_ohlcv_failed", symbol=symbol, tf=timeframe, err=str(exc))
        return None
    return [[float(x) for x in r] for r in (rows or [])]


async def seed_ohlcv(exchange: Any, symbol: str, timeframe: str, *, limit: int) -> list[Bar]:
    """Fetch closed history via REST to warm a kline plane (drops the forming last bar, I-5).

    Returns ``[]`` on failure (logged) — the caller keeps the plane ``absent`` and the snapshot
    reports ``NotReady``, never a fabricated frame.
    """
    # Binance clamps a single fetch_ohlcv to 1000 bars (and a wide since→until returns only the first
    # 1000) — so for deep history route through ccxt's deterministic paginator; a normal ≤1000 seed
    # stays a single call.
    extra = {"paginate": True} if limit > 1000 else {}
    bars = await _fetch_ohlcv_raw(exchange, symbol, timeframe, limit=limit, params=extra)
    if bars is None:
        return []
    return bars[:-1] if bars else []


async def fetch_ohlcv_series(
    exchange: Any, symbol: str, timeframe: str, *, limit: int, price: str | None = None
) -> list[Bar]:
    """Closed OHLCV series, optionally the mark/index/premium candle stream (``price='mark'`` …).

    Same shape as :func:`seed_ohlcv` (drops the forming bar, I-5). For mark/index/premiumIndex the
    OHLC is meaningful but ``volume[5]`` is 0/meaningless (data-catalog). Fail-loud ``[]``.
    """
    params: dict[str, Any] = {}
    if price is not None:
        params["price"] = price
    if limit > 1000:
        params["paginate"] = True
    bars = await _fetch_ohlcv_raw(exchange, symbol, timeframe, limit=limit, params=params)
    if bars is None:
        return []
    return bars[:-1] if bars else []


async def fetch_ohlcv_between(
    exchange: Any, symbol: str, timeframe: str, *, start_ms: int, end_ms: int
) -> list[Bar]:
    """Windowed CLOSED OHLCV in ``[start_ms, end_ms]`` (reconcile / path-backfill). Fail-loud ``[]``.

    Filters to fully-closed bars (``open + interval ≤ min(end_ms, now)``) so a forming bar at the
    window edge is never served as data (I-5). ``end_ms`` maps to ccxt's ``until`` (Binance
    ``endTime``); ``start_ms`` to ``since``.
    """
    interval_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    bars = await _fetch_ohlcv_raw(
        exchange, symbol, timeframe, since=int(start_ms), limit=1500, params={"until": int(end_ms)}
    )
    if not bars:
        return []
    cutoff = min(int(end_ms), int(time.time() * 1000))
    return [b for b in bars if int(b[0]) + interval_ms <= cutoff]


async def fetch_funding_history(exchange: Any, symbol: str, *, limit: int = 16) -> list[dict[str, Any]]:
    """Settled funding records via ``fetch_funding_rate_history`` (``fundingRate``/``timestamp`` each).

    Raw ccxt records, oldest→newest, for the derived funding stats (z-score/trend/extreme) that move
    to ``features/`` at cutover. Fail-loud ``[]`` (a failed fetch is not an empty history).
    """
    try:
        rows = await exchange.fetch_funding_rate_history(symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_funding_history_failed", symbol=symbol, err=str(exc))
        return []
    return [r for r in (rows or []) if isinstance(r, dict)]


async def fetch_futures_data_series(
    exchange: Any, method: str, req_params: dict[str, Any], key: str
) -> list[float]:
    """A ``/futures/data/*`` statistic as a ``list[float]`` (oldest→newest) for ``key``.

    Backs the OI / global-L-S series (``fapiDataGetOpenInterestHist`` → ``sumOpenInterest``;
    ``fapiDataGetGlobalLongShortAccountRatio`` → ``longShortRatio``). Fail-loud: an absent series or a
    non-finite/garbage entry is skipped, never fabricated (``[]`` on total failure).
    """
    rows = await poll_futures_data(exchange, method, req_params)
    if not rows:
        return []
    out: list[float] = []
    for row in rows:
        raw = row.get(key) if isinstance(row, dict) else None
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append(value)
    return out


async def fetch_all_tickers(exchange: Any) -> dict[str, dict[str, Any]]:
    """All-symbol 24h tickers via REST ``fetch_tickers`` (the scanner funnel ranks the whole universe).

    The streamed per-symbol ticker planes only cover the TRACKED universe; the scanner has to rank
    every perp, so this is the one genuinely universe-wide REST batch (weight ~40, periodic). Fail-loud
    ``{}`` on failure — never a partial/fabricated map.
    """
    try:
        tickers = await exchange.fetch_tickers()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("engine_fetch_all_tickers_failed", venue=getattr(exchange, "id", "?"), err=str(exc))
        return {}
    return {s: t for s, t in (tickers or {}).items() if isinstance(t, dict)}


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
    global _BAN_UNTIL_MS
    now_ms = time.time() * 1000.0
    if now_ms < _BAN_UNTIL_MS:
        return None  # paused during an active -1003 ban — hitting the endpoint would extend it
    fn = getattr(exchange, method, None)
    if fn is None:
        LOG.warning("engine_futures_data_unsupported", method=method)
        return None
    try:
        rows = await fn(dict(req_params))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        match = _BAN_RE.search(msg)
        if match or "-1003" in msg:
            until = float(match.group(1)) if match else now_ms + _DEFAULT_BAN_MS
            _BAN_UNTIL_MS = max(_BAN_UNTIL_MS, until)
            LOG.warning(
                "engine_futures_data_banned",
                method=method,
                banned_until_ms=int(until),
                pause_s=round(max(0.0, until - now_ms) / 1000.0, 1),
            )
        else:
            LOG.warning("engine_futures_data_failed", method=method, params=req_params, err=msg)
        return None
    return list(rows) if isinstance(rows, list) else None


__all__ = [
    "seed_ohlcv",
    "fetch_klines_full",
    "fetch_ohlcv_series",
    "fetch_ohlcv_between",
    "fetch_funding_history",
    "fetch_futures_data_series",
    "fetch_all_tickers",
    "poll_open_interest",
    "poll_funding_rates",
    "poll_long_short_ratio",
    "poll_futures_data",
    "futures_data_banned_until_ms",
]
