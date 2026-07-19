"""Native scanner detection-frame feed — the MANIPULATIONS data-read, on the engine (ADR-0004 S7).

The scanner (Module 2) scans the NON-tracked watch tail, so it reads on-demand REST through the
engine's OWN ccxt client (:attr:`Engine.exchange`) and its :mod:`hunt_core.engine.rest` helpers —
exactly the dynamic tail the engine was designed to serve on demand (see ``Engine.exchange``'s
docstring). This is the native replacement for ``HuntCcxtClient.fetch_ohlcv_list_cached`` +
``fetch_funding_rate_history``: no legacy client, no row-dict.

**Caching is bar-close-driven, not time-TTL.** A kline frame is reused iff no new bar has closed
since it was fetched (``last_open + 2·interval > now`` — the last *closed* bar opened at ``o`` and
closed at ``o+interval``; the next closes at ``o+2·interval``, so no fresher closed bar exists to
fetch before then). A due bar forces a refetch. This is strictly better than the old interval-aware
TTL and eliminates *by construction* the HTF-cache-TTL-drift bug family — a cache whose TTL outlived
the staleness reject, which shipped repeated pinned-universe blackouts (memory:
``stale-htf-cache-trap``, ``pinned-4h-stale-blackout``, ``blackout-root-cause-htf-cache-ttl-drift``).
The staleness bound below is exactly ``2·interval`` per TF, i.e. identical to the reuse horizon, so a
reused frame can never be one the freshness gate would reject.

Invariants: closed-only (I-5 — ``rest.seed_ohlcv`` drops the forming bar); fail-loud (I-6 — a frame
still older than its staleness bound after a refetch, or an unparseable funding record, yields
``None``/is skipped, never a fabricated bar or ``0.0`` rate).

Two implementations share one :class:`ScannerFeed` interface so the coexistence flag can pick either
without the delivery path knowing which: :class:`EngineScannerFeed` (native, the cutover target) and
:class:`LegacyScannerFeed` (the doomed ``HuntCcxtClient`` behind the same interface, byte-identical to
the pre-cutover path, deleted with the transport at S11).
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Protocol

import structlog

from hunt_core.engine import rest
from hunt_core.engine.freshness import Bar

LOG = structlog.get_logger(__name__)

# ── Scanner detection-frame config (Module 2) ────────────────────────────────────────────────
# The TF ladder the manipulation scales read (macro 1d/4h · meso 4h/1h · micro 1h/15m, plus 1w
# context and 5m confirm), the per-TF history depth, and the per-TF staleness bound. Method config
# grounded on advance_manipulation_scales' scale ladder — NOT deployment knobs (see the delivery
# module's calibration-surface note). Each staleness bound is exactly 2·interval (see module doc).
# 1w is the macro context of the (1w, 1d, 4h) ladder: the author's biggest manipulations
# («восходящий канал начиная с февраля», ESPORTS 160%, BSB 250% «в среднесроке») are DAILY-scale
# structures, and a 1d meso needs a weekly frame above it.
_TIMEFRAMES: tuple[str, ...] = ("1w", "1d", "4h", "1h", "15m", "5m")
_LOOKBACK_BY_TF: dict[str, int] = {"1w": 160, "1d": 220, "4h": 120, "1h": 120, "15m": 700, "5m": 1000}
# Per-TF staleness bound — the LEGACY path's table (kept byte-identical, deleted with it at S11). The
# ENGINE path does NOT read this: it DERIVES its bound as 2·interval at the reject site, so the reuse
# horizon and staleness bound are the SAME expression and cannot drift apart. Each entry here equals
# 2·interval (pinned by tests/test_scanner_feed.py) — that identity is what makes the two paths agree.
_MAX_STALE_MS_BY_TF: dict[str, int] = {
    "1w": 604800_000 * 2,   # 2 weeks
    "1d": 86400_000 * 2,    # 2 days
    "4h": 14400_000 * 2,    # 8 hours
    "1h": 3600_000 * 2,     # 2 hours
    "15m": 900_000 * 2,     # 30 minutes
    "5m": 300_000 * 2,      # 10 minutes
}
# Bar duration per TF (ms) — the legacy path drops the forming candle with this map (the engine
# path derives it natively from ccxt's ``parse_timeframe`` instead, no hardcoded copy).
_INTERVAL_MS: dict[str, int] = {
    "1w": 604800_000, "1d": 86400_000, "4h": 14400_000, "1h": 3600_000, "15m": 900_000, "5m": 300_000,
}
_FUNDING_LIMIT = 10
# Funding settles every 8h; the legacy client cached its history ~900s. The engine path keeps the
# same modest TTL — funding is a low-cadence conviction dop-factor, not a bar-close-driven frame.
_FUNDING_TTL_S = 900.0
_DEFAULT_PARALLELISM = 10

# Public type aliases for the delivery consumer.
OHLCVByTF = dict[str, list[Bar]]
FundingCtx = dict[str, float]


def _funding_ctx_from(rates: list[float]) -> FundingCtx | None:
    """``{"rate": last, "peak": max}`` for the short conviction/timing signal, or ``None`` if empty.

    Elevated positive funding = crowded longs = squeeze fuel; a rollover from the peak times the
    "основной слив" (see ``_funding_short_signal``). Fail-loud: an empty rate list yields ``None``.
    """
    if not rates:
        return None
    return {"rate": rates[-1], "peak": max(rates)}


class ScannerFeed(Protocol):
    """Detection-frame source the manipulation delivery consumes (engine-native or legacy client)."""

    async def detection_data(
        self, symbol: str, *, now_ms: float
    ) -> tuple[str, OHLCVByTF, FundingCtx | None]:
        """Return ``(symbol, {tf: closed_bars}, funding_ctx | None)`` for one symbol.

        Only fresh, non-empty frames appear in the dict; a stale/absent TF is simply omitted (I-6).
        """
        ...


class EngineScannerFeed:
    """On-demand REST detection frames for the non-tracked scanner tail, via the engine's client.

    Holds a bar-close-driven kline cache and a small funding TTL cache across scan cycles (the
    scanner re-runs every ~300s over the watch universe). Reads only ``engine.exchange`` +
    ``hunt_core.engine.rest`` — never the tracked WS planes (the watch tail is deliberately not
    streamed) and never the legacy client.
    """

    def __init__(
        self,
        engine: Any,
        *,
        timeframes: tuple[str, ...] = _TIMEFRAMES,
        parallelism: int = _DEFAULT_PARALLELISM,
    ) -> None:
        self._engine = engine
        self._timeframes = timeframes
        self._sem = asyncio.Semaphore(parallelism)
        self._frames: dict[tuple[str, str], list[Bar]] = {}
        self._funding: dict[str, tuple[float, FundingCtx | None]] = {}

    @property
    def _exchange(self) -> Any:
        return self._engine.exchange

    async def detection_data(
        self, symbol: str, *, now_ms: float
    ) -> tuple[str, OHLCVByTF, FundingCtx | None]:
        async with self._sem:
            frames = await asyncio.gather(
                *(self._frame(symbol, tf, now_ms=now_ms) for tf in self._timeframes)
            )
            funding = await self._funding_for(symbol)
        ohlcv_by_tf: OHLCVByTF = {tf: bars for tf, bars in zip(self._timeframes, frames) if bars}
        return symbol, ohlcv_by_tf, funding

    async def _frame(self, symbol: str, tf: str, *, now_ms: float) -> list[Bar] | None:
        """One closed-only kline frame for ``(symbol, tf)`` — cached until a new bar is due, else None.

        Reuse horizon and staleness bound are the same ``2·interval``, so a cache hit is fresh by
        construction; a miss refetches and applies the explicit staleness reject (fail-loud None).
        """
        interval_ms = int(self._exchange.parse_timeframe(tf) * 1000)
        key = (symbol, tf)
        cached = self._frames.get(key)
        if cached and int(cached[-1][0]) + 2 * interval_ms > now_ms:
            return cached  # no new bar has closed since the last fetch — reuse (never stale, see doc)
        bars = await rest.seed_ohlcv(self._exchange, symbol, tf, limit=_LOOKBACK_BY_TF[tf])
        if not bars:
            return None  # fetch failed / empty window — нет данных, keep no stale entry cached (I-6)
        # Derive the bound from the same interval as the reuse horizon (2·interval), NOT a parallel
        # table — so "reuse ⟹ fresh" is true BY CONSTRUCTION for any TF, present or future, and can
        # never drift back into the HTF-cache-TTL-drift blackout family (the whole point of S7).
        if now_ms - int(bars[-1][0]) > 2 * interval_ms:
            return None  # even the freshest closed bar is too old — never serve a stale frame (I-6)
        self._frames[key] = bars
        return bars

    async def _funding_for(self, symbol: str) -> FundingCtx | None:
        cached = self._funding.get(symbol)
        if cached is not None and time.monotonic() - cached[0] < _FUNDING_TTL_S:
            return cached[1]
        hist = await rest.fetch_funding_history(self._exchange, symbol, limit=_FUNDING_LIMIT)
        rates: list[float] = []
        for record in hist:
            raw = record.get("fundingRate")
            try:
                value = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue  # unparseable record — skip it, never fabricate a 0.0 rate (I-6)
            if math.isfinite(value):
                rates.append(value)
        ctx = _funding_ctx_from(rates)
        self._funding[symbol] = (time.monotonic(), ctx)
        return ctx


class LegacyScannerFeed:
    """The doomed ``HuntCcxtClient`` behind the :class:`ScannerFeed` interface (deleted at S11).

    Byte-identical to the pre-cutover ``manipulation_delivery._fetch_symbol_data`` path — used when
    the engine coexistence flag is OFF so the default live scanner is unchanged during the cutover.
    """

    def __init__(
        self,
        client: Any,
        *,
        timeframes: tuple[str, ...] = _TIMEFRAMES,
        parallelism: int = _DEFAULT_PARALLELISM,
    ) -> None:
        self._client = client
        self._timeframes = timeframes
        self._sem = asyncio.Semaphore(parallelism)

    async def detection_data(
        self, symbol: str, *, now_ms: float
    ) -> tuple[str, OHLCVByTF, FundingCtx | None]:
        ohlcv_by_tf: OHLCVByTF = {}

        async def _fetch(tf: str) -> tuple[str, list[Bar] | None]:
            try:
                bars = await self._client.fetch_ohlcv_list_cached(
                    symbol, tf, limit=_LOOKBACK_BY_TF[tf]
                )
                if bars and len(bars) >= 2:
                    interval_ms = _INTERVAL_MS.get(tf)
                    if interval_ms is not None and int(bars[-1][0]) + interval_ms > now_ms:
                        bars = bars[:-1]  # drop the still-forming candle (list path, I-5)
                if bars and len(bars) > 0:
                    if now_ms - int(bars[-1][0]) > _MAX_STALE_MS_BY_TF.get(tf, 3600_000):
                        return tf, None
                return tf, bars
            except Exception:
                LOG.debug("manipulation_fetch_failed sym=%s tf=%s", symbol, tf, exc_info=True)
                return tf, None

        async with self._sem:
            tfs = await asyncio.gather(
                *[_fetch(tf) for tf in self._timeframes], return_exceptions=True
            )
            funding = await self._funding_for(symbol)
        for item in tfs:
            if isinstance(item, BaseException):
                continue
            tf, bars = item
            if isinstance(tf, str) and bars:
                ohlcv_by_tf[tf] = bars
        return symbol, ohlcv_by_tf, funding

    async def _funding_for(self, symbol: str) -> FundingCtx | None:
        try:
            hist = await self._client.fetch_funding_rate_history(symbol, limit=_FUNDING_LIMIT)
        except Exception:
            LOG.debug("manipulation_funding_failed sym=%s", symbol, exc_info=True)
            return None
        rates = [float(r.get("fundingRate") or 0.0) for r in (hist or [])]
        return _funding_ctx_from(rates)


__all__ = ["ScannerFeed", "EngineScannerFeed", "LegacyScannerFeed", "OHLCVByTF", "FundingCtx"]
