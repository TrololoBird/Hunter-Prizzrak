"""Engine facade — the ONLY surface strategies call (ADR-0002 §6.2).

:meth:`Engine.start` seeds kline history via REST (so no plane is ever empty), launches the
per-(symbol, stream) WS ingest, the health watchdog, and the ``/futures/data`` poller.
:meth:`Engine.snapshot` returns a freshness-proven :class:`MarketSnapshot`, or one whose
``not_ready`` names exactly which planes are absent/stale. Strategies never touch ccxt.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence

import structlog

from hunt_core.engine import exchanges, params, rest
from hunt_core.engine.health import Watchdog
from hunt_core.engine.ingest import _TF_MS, Ingest
from hunt_core.engine.state import MarketSnapshot, Plane, Source

LOG = structlog.get_logger(__name__)

_DEFAULT_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h")


def _last_float(rows: list[dict[str, object]] | None, key: str) -> float | None:
    """Parse ``key`` from the newest ``/futures/data`` row as a finite float, else ``None``.

    Fail-loud: an absent row, missing key, non-numeric, or NaN/inf yields ``None`` (no data) — never
    a fabricated substitute.
    """
    if not rows:
        return None
    raw = rows[-1].get(key)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


class Engine:
    """A ccxt.pro-native, freshness-proven market-data engine for one venue (Binance USDⓈ-M)."""

    def __init__(self, symbols: Sequence[str], timeframes: Sequence[str] = _DEFAULT_TFS) -> None:
        self._symbols = list(symbols)
        self._timeframes = tuple(timeframes)
        self._ingest = Ingest(exchanges.make_binance)
        self._watchdog: Watchdog | None = None
        self._bg: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self._ingest.exchange.load_markets()
        await self._seed()
        self._ingest.start(self._symbols, self._timeframes)
        self._watchdog = Watchdog(
            self._ingest.last_frame_ms,
            on_silent=self._ingest.reconnect,
            on_rotate=self._ingest.reconnect,
        )
        self._bg.append(asyncio.create_task(self._watchdog.run(), name="engine_watchdog"))
        self._bg.append(asyncio.create_task(self._poll_positioning(), name="engine_positioning"))
        LOG.info("engine_started", symbols=len(self._symbols), timeframes=self._timeframes)

    async def _seed(self) -> None:
        now = int(time.time() * 1000)
        ex = self._ingest.exchange
        for symbol in self._symbols:
            st = self._ingest.state_for(symbol)
            for tf in self._timeframes:
                bars = await rest.seed_ohlcv(ex, symbol, tf, limit=params.OHLCV_LIMIT)
                if bars:
                    bound = int(params.fresh_kline_s(_TF_MS[tf] / 1000.0) * 1000.0)
                    st.put(Plane(f"kline.{tf}", bars, Source.REST_SEED, now, int(bars[-1][0]), bound))

    async def _poll_positioning(self) -> None:
        """Poll the un-streamable ``/futures/data/*`` planes on their 5-min native cadence.

        Every write is a real, fail-loud :class:`Plane`; a missing/unparseable datum is skipped
        (logged in ``rest``), never fabricated.
        """
        bound = int(params.FRESH_FUTURES_DATA_S * 1000.0)
        while True:
            ex = self._ingest.exchange
            for symbol in self._symbols:
                now = int(time.time() * 1000)
                st = self._ingest.state_for(symbol)
                oi = await rest.poll_open_interest(ex, symbol)
                if oi is not None:
                    st.put(Plane("oi", oi, Source.REST_SEED, now, now, bound))
                taker = await rest.poll_futures_data(ex, "fapiDataGetTakerlongshortRatio", symbol)
                ratio = _last_float(taker, "buySellRatio")
                if ratio is not None:
                    st.put(Plane("taker_5m", ratio, Source.REST_SEED, now, now, bound))
                gls = await rest.poll_futures_data(ex, "fapiDataGetGlobalLongShortAccountRatio", symbol)
                lsr = _last_float(gls, "longShortRatio")
                if lsr is not None:
                    st.put(Plane("global_ls_5m", lsr, Source.REST_SEED, now, now, bound))
            await asyncio.sleep(params.FUTURES_DATA_POLL_S)

    def snapshot(self, symbol: str, required: Sequence[str]) -> MarketSnapshot:
        """A consistent, freshness-checked view; ``not_ready`` names any absent/stale required plane."""
        now = int(time.time() * 1000)
        st = self._ingest.states.get(symbol)
        if st is None:
            return MarketSnapshot(symbol, now, {}, (f"{symbol}: not tracked",))
        return st.snapshot(now, tuple(required))

    async def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.stop()
        for task in self._bg:
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._ingest.close()
