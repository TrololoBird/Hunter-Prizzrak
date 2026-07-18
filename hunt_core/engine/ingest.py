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

import ccxt
import structlog

from hunt_core.engine import freshness, params
from hunt_core.engine.state import PlaneStamp, Source, SymbolState

LOG = structlog.get_logger(__name__)


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
        # Universe-wide native streams (one subscription each), capability-gated on `has`.
        self._spawn("*:marks", self._step_marks(self._symbols))
        if self._ex.has.get("watchBidsAsks"):
            self._spawn("*:bidsasks", self._step_bidsasks(self._symbols))
        if self._ex.has.get("watchTickers"):
            self._spawn("*:tickers", self._step_tickers(self._symbols))
        if self._ex.has.get("watchLiquidationsForSymbols"):
            self._spawn("*:liquidations", self._step_liquidations(self._symbols))

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
        # Branch on ccxt's typed hierarchy (isinstance, not string names — subsumes every subclass):
        # ChecksumError → book gap, ccxt re-seeds itself, re-loop; DDoS/RateLimit → LONG backoff (a
        # short retry extends the 418 ban); NetworkError → transient short jittered backoff;
        # ExchangeError (non-network: bad symbol / not-supported / bug) → don't retry-storm.
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                attempt = 0
                self.last_frame_ms[key] = _now_ms()
            except asyncio.CancelledError:
                raise
            except ccxt.ChecksumError:
                LOG.debug("engine_ws_checksum_reseed", stream=key)  # expected; ccxt re-seeds the book
            except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as exc:
                LOG.error("engine_ws_rate_limited", stream=key, delay_s=params.RATE_LIMIT_BACKOFF_S, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except ccxt.NetworkError as exc:
                attempt += 1
                delay = backoff_delay_s(attempt)
                LOG.warning("engine_ws_reconnect", stream=key, attempt=attempt, delay_s=round(delay, 2), err=str(exc))
                await asyncio.sleep(delay)
            except ccxt.ExchangeError as exc:
                LOG.error("engine_ws_exchange_error", stream=key, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 — unknown → treat as transient
                attempt += 1
                delay = backoff_delay_s(attempt)
                LOG.warning("engine_ws_unknown_error", stream=key, attempt=attempt, delay_s=round(delay, 2), err=str(exc))
                await asyncio.sleep(delay)

    # --- per-stream steps: each `await watch_*` DRIVES updates + reconnect; the DATA stays in
    # ccxt's own caches (read-through at snapshot) — we only stamp freshness. OHLCV is the sole
    # exception: ccxt's WS cache lacks deep history, so we merge its recent closed bars into the
    # REST-seeded frame (freqtrade "REST truth, WS fresh tail" — a minimal append, not a 2nd cache).

    def _step_ohlcv(self, symbol: str, tf: str) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.fresh_kline_s(self._ex.parse_timeframe(tf)) * 1000.0)  # native tf parse

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

    def _step_bidsasks(self, symbols: list[str]) -> Callable[[], Awaitable[None]]:
        wanted = set(symbols)
        bound_ms = int(params.FRESH_BBO_S * 1000.0)

        async def step() -> None:
            # Universe-wide !bookTicker@arr — the native best bid/ask stream (lighter than the book).
            bbos = await self._ex.watch_bids_asks()
            now = _now_ms()
            for sym, ba in bbos.items():
                if sym in wanted:
                    self.state_for(sym).put_value(
                        "bbo", ba, PlaneStamp(Source.WS, now, int(ba.get("timestamp") or now), bound_ms)
                    )

        return step

    def _step_tickers(self, symbols: list[str]) -> Callable[[], Awaitable[None]]:
        wanted = set(symbols)
        bound_ms = int(params.FRESH_TICKER_S * 1000.0)

        async def step() -> None:
            # Universe-wide !miniTicker@arr; value-backed (small dict, carries 24h volume/quoteVolume).
            tickers = await self._ex.watch_tickers()
            now = _now_ms()
            for sym, tk in tickers.items():
                if sym in wanted:
                    self.state_for(sym).put_value(
                        "ticker", tk, PlaneStamp(Source.WS, now, int(tk.get("timestamp") or now), bound_ms)
                    )

        return step

    def _step_liquidations(self, symbols: list[str]) -> Callable[[], Awaitable[None]]:
        wanted = set(symbols)
        # Event-driven (!forceOrder): silence ≠ stale (no liquidation is normal). Data is read through
        # `exchange.liquidations[symbol]` at snapshot time; here we stamp only when one arrives.
        bound_ms = int(params.NO_MESSAGE_WATCHDOG_S * 1000.0)

        async def step() -> None:
            liqs = await self._ex.watch_liquidations_for_symbols(symbols)
            now = _now_ms()
            for liq in liqs if isinstance(liqs, list) else []:
                sym = liq.get("symbol") if isinstance(liq, dict) else None
                if sym in wanted:
                    ev = int(liq.get("timestamp") or now) if isinstance(liq, dict) else now
                    self.state_for(sym).stamp_only("liq", PlaneStamp(Source.WS, now, ev, bound_ms))

        return step

    async def close(self) -> None:
        """Stop all watch loops and close the client (un_watch is implicit on close)."""
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()
