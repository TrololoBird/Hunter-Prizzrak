"""Watch supervisor — one asyncio task per (symbol, stream) (ADR-0002 §6.2).

ccxt.pro owns subscribe / reconnect / exponential *pong* handling; the loop adds two things it does
NOT do: (1) **jittered exponential backoff** on failure — ccxt.pro re-subscribes on the next
``watch_*`` call, so a bare ``except: continue`` becomes a hot reconnect loop that trips Binance's
300-new-connections/5min ban; (2) a per-stream **last-frame clock** for the health watchdog. Every
frame is stamped into :class:`SymbolState` as a fail-loud :class:`Plane`; the forming candle is
dropped (I-5).
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from hunt_core.engine import freshness, params
from hunt_core.engine.state import PlaneStamp, Source, SymbolState

LOG = structlog.get_logger(__name__)

_TF_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}


def backoff_delay_s(attempt: int) -> float:
    """python-binance jittered exponential backoff, capped (§11.C).

    ``attempt`` starts at 1 on the first failure. Delay ∈ ``[1, cap+1]`` with jitter, so a fleet of
    reconnecting streams never thunders the connect endpoint in lockstep.
    """
    ceil = min(params.BACKOFF_CAP_S, float(2 ** min(attempt, 30)) - 1.0)
    return random.random() * max(0.0, ceil) + 1.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class Ingest:
    """Owns one venue's ccxt.pro client, the per-symbol :class:`SymbolState`, and the watch tasks."""

    def __init__(self, make_exchange: Callable[[], Any]) -> None:
        self._make_exchange = make_exchange
        self._ex = make_exchange()
        self.states: dict[str, SymbolState] = {}
        self.last_frame_ms: dict[str, int] = {}  # identity stable across reconnect (watchdog holds it)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._symbols: list[str] = []
        self._timeframes: tuple[str, ...] = ()

    @property
    def exchange(self) -> Any:
        return self._ex

    def state_for(self, symbol: str) -> SymbolState:
        return self.states.setdefault(symbol, SymbolState(symbol))

    def start(self, symbols: list[str], timeframes: tuple[str, ...]) -> None:
        """Spawn per-(symbol, stream) watch tasks + a universe-wide mark/funding task."""
        self._symbols = list(symbols)
        self._timeframes = tuple(timeframes)
        for symbol in self._symbols:
            self.state_for(symbol)
            for tf in self._timeframes:
                self._spawn(f"{symbol}:ohlcv.{tf}", self._step_ohlcv(symbol, tf))
            self._spawn(f"{symbol}:book", self._step_book(symbol))
            self._spawn(f"{symbol}:trades", self._step_trades(symbol))
        self._spawn("*:marks", self._step_marks(self._symbols))

    async def reconnect(self) -> None:
        """Force a clean reconnect: cancel loops, drop the frozen client, respawn on a fresh one.

        Invoked by the health watchdog when the whole feed goes silent (ccxt reports ``errors=0``).
        The ``last_frame_ms`` dict identity is preserved so the watchdog keeps observing.
        """
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self._ex.close()
        self.last_frame_ms.clear()
        self._ex = self._make_exchange()
        self.start(self._symbols, self._timeframes)

    def _spawn(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._stream_loop(key, step), name=f"engine_ws:{key}")
        task.add_done_callback(self._tasks.remove)
        self._tasks.append(task)

    async def _stream_loop(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                attempt = 0
                self.last_frame_ms[key] = _now_ms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — ccxt.pro surfaces NetworkError/ChecksumError here
                attempt += 1
                delay = backoff_delay_s(attempt)
                LOG.warning(
                    "engine_ws_reconnect", stream=key, attempt=attempt, delay_s=round(delay, 2), err=str(exc)
                )
                await asyncio.sleep(delay)

    # --- per-stream steps: each `await watch_*` DRIVES updates + reconnect; the DATA stays in
    # ccxt's own caches (read-through at snapshot) — we only stamp freshness. OHLCV is the sole
    # exception: ccxt's WS cache lacks deep history, so we merge its recent closed bars into the
    # REST-seeded frame (freqtrade "REST truth, WS fresh tail" — a minimal append, not a 2nd cache).

    def _step_ohlcv(self, symbol: str, tf: str) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.fresh_kline_s(_TF_MS[tf] / 1000.0) * 1000.0)

        async def step() -> None:
            await self._ex.watch_ohlcv(symbol, tf)  # trigger + reconnect; return is a delta (newUpdates)
            cache = ((getattr(self._ex, "ohlcvs", {}) or {}).get(symbol) or {}).get(tf) or []
            closed = freshness.closed_bars(list(cache))  # ccxt's recent CLOSED bars (drop forming, I-5)
            if not closed:
                return
            self.state_for(symbol).merge_frame(
                f"kline.{tf}",
                [[float(x) for x in bar] for bar in closed],
                PlaneStamp(Source.WS, _now_ms(), int(closed[-1][0]), bound_ms),
            )

        return step

    def _step_book(self, symbol: str) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.FRESH_DEPTH_S * 1000.0)

        async def step() -> None:
            # ccxt.pro maintains the book natively (REST snapshot + nonce-validated diffs, auto re-seed
            # on a gap via ChecksumError → caught by _stream_loop). We store nothing — the book is read
            # through `exchange.orderbooks[symbol]` at snapshot time; here we only stamp freshness.
            ob = await self._ex.watch_order_book(symbol, params.ORDER_BOOK_LIMIT)
            now = _now_ms()
            self.state_for(symbol).stamp_only(
                "book", PlaneStamp(Source.WS, now, int(ob.get("timestamp") or now), bound_ms)
            )

        return step

    def _step_trades(self, symbol: str) -> Callable[[], Awaitable[None]]:
        # Event-driven: silence ≠ stale (a quiet symbol has no trades); the transport watchdog catches
        # a dead socket. Data is read through `exchange.trades[symbol]` at snapshot time.
        bound_ms = int(params.NO_MESSAGE_WATCHDOG_S * 1000.0)

        async def step() -> None:
            await self._ex.watch_trades(symbol)  # drive ccxt's trades cache; stamp freshness only
            now = _now_ms()
            self.state_for(symbol).stamp_only("trades", PlaneStamp(Source.WS, now, now, bound_ms))

        return step

    def _step_marks(self, symbols: list[str]) -> Callable[[], Awaitable[None]]:
        wanted = set(symbols)
        bound_ms = int(params.FRESH_MARK_S * 1000.0)
        fund_bound_ms = int(params.FRESH_FUNDING_S * 1000.0)

        async def step() -> None:
            # One universe-wide subscription; `r` = funding rate → funding from WS, never REST-polled.
            # mark/funding are small scalars ccxt doesn't cache per-symbol usefully, so value-backed.
            marks = await self._ex.watch_mark_prices()
            now = _now_ms()
            for sym, mk in marks.items():
                if sym not in wanted:
                    continue
                st = self.state_for(sym)
                st.put_value("mark", mk, PlaneStamp(Source.WS, now, int(mk.get("timestamp") or now), bound_ms))
                rate = (mk.get("info") or {}).get("r")
                if rate is None:
                    continue
                try:
                    fval = float(rate)
                except (TypeError, ValueError):
                    continue
                st.put_value("funding", fval, PlaneStamp(Source.WS, now, now, fund_bound_ms))

        return step

    async def close(self) -> None:
        """Stop all watch loops and close the client (un_watch is implicit on close)."""
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()
