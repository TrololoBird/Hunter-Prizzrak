"""Hunter live streams — CCXT Pro watch* (liquidations, trades, OHLCV, mark)."""
from __future__ import annotations



import asyncio
import collections
import structlog
import os
import time
from dataclasses import dataclass, field
from typing import Any

import ccxt

from hunt_core.errors import defensive_exc_types
from hunt_core.market.factory import close_exchange_async, create_pro_secondary_swap
from hunt_core.market.client import HuntCcxtClient
from hunt_core.market.cross import configured_secondary_exchanges, funding_rest_poll_venues
from hunt_core.maps.liquidation import build_liquidation_map, heatmap_to_market_dict
from hunt_core.maps.config import load_maps_config
from hunt_core.market.ccxt_guard import (
    ccxt_ws_method_available,
    exchange_funding_ws_capable,
    is_ccxt_rate_limited,
    liquidation_ws_mode,
)
from hunt_core.market.symbols import (
    to_binance_symbol,
    to_ccxt_symbol,
    try_binance_id_from_ccxt,
    try_resolve_linear_usdt_swap,
)
from hunt_core.params.store import orderflow_use_nq, ws_thresholds

LOG = structlog.get_logger("hunt_core.market.streams")
_MAX_SYMBOL_STREAMS = 100
_LIQ_BUFFER_MAX = 8_000
# The primary venue's liquidation tape lives in _force_order_buffer, which
# liquidation_buffers() exposes under this exact key. Secondary venues get their own
# buffers — never both, or their events get double-counted (see _record_liquidation).
_PRIMARY_LIQ_VENUE = "binance"
_AGG_BUFFER_MAX = 2_000
_WS_WATCH_TIMEOUT_S: float = 120.0
_KLINE_INTERVAL = "1m"
_KLINE_5M_INTERVAL = "5m"
_KLINE_15M_INTERVAL = "15m"


def _kline_ws_5m_enabled() -> bool:
    return os.getenv("HUNT_KLINE_WS_5M", "1").strip().lower() in {"1", "true", "yes", "on"}


def _kline_ws_15m_enabled() -> bool:
    return os.getenv("HUNT_KLINE_WS_15M", "1").strip().lower() in {"1", "true", "yes", "on"}


def _kline_grace_sec() -> float:
    return float(ws_thresholds().get("kline_grace_sec", 1.5))


async def _join_cancelled_task(task: asyncio.Task[Any]) -> None:
    """Await a cancelled stream task; log unexpected shutdown errors."""
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception as exc:
        LOG.warning("stream_task_shutdown_error | task=%s error=%s", task.get_name(), exc)


def _liquidation_event_count(
    buffer: collections.deque[tuple[int, str, str, float, float]],
    *,
    symbol: str | None,
    window_seconds: int,
) -> int:
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    count = 0
    for ts_ms, sym, _side, qty, _price in buffer:
        if ts_ms < cutoff_ms or qty <= 0.0:
            continue
        if symbol is not None and sym != symbol:
            continue
        count += 1
    return count


def _liquidation_rollups(
    buffer: collections.deque[tuple[int, str, str, float, float]],
    *,
    symbol: str | None,
    window_seconds: int,
) -> dict[str, float] | None:
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    long_notional = short_notional = 0.0
    for ts_ms, sym, side, qty, price in buffer:
        if ts_ms < cutoff_ms:
            continue
        if symbol is not None and sym != symbol:
            continue
        try:
            qty_val = float(qty)
            price_val = float(price)
        except (TypeError, ValueError) as liq_parse_exc:
            LOG.debug("liquidation_parse_error | sym=%s error=%s", symbol, liq_parse_exc)
            continue
        if qty_val <= 0.0:
            continue
        notional = qty_val * price_val if price_val > 0.0 else qty_val
        if side == "BUY":
            short_notional += notional
        else:
            long_notional += notional
    total = long_notional + short_notional
    if total <= 0.0:
        return None
    return {
        "liquidation_long_notional": long_notional,
        "liquidation_short_notional": short_notional,
        "liquidation_total_notional": total,
        "liquidation_score": round(short_notional / total, 4),
    }


@dataclass
class _ClosedKlineBar:
    open_ms: int
    o: float
    h: float
    l: float
    c: float
    v: float
    received_ms: int


@dataclass
class _AggPoint:
    ts_ms: int
    qty: float
    qty_full: float
    is_buy: bool
    price: float = 0.0


_TOP_BOOK_DEPTH_LEVELS = 20


def _taker_is_buy(trade: dict[str, Any], info: dict[str, Any]) -> bool:
    """Resolve the aggressor (taker) side of a trade — the ground truth crypto
    gives directly, so no tick-rule inference is needed.

    Prefers CCXT's normalized ``side`` (present on parsed aggTrades). If it is
    absent, falls back to Binance's raw aggTrade maker flag ``m`` (``m=true`` ⇒
    the BUYER is the maker ⇒ the taker is the SELLER). Without this fallback a
    side-less payload silently defaulted to sell, quietly biasing CVD/footprint/
    delta toward sell. The forceOrder path already reads its own ``S`` fallback,
    so this mirrors that discipline for the trade path.
    """
    side = str(trade.get("side") or "").lower()
    if side:
        return side == "buy"
    if "m" in info:
        return not bool(info["m"])
    return False


def _attach_task_guard(task: asyncio.Task[Any]) -> None:
    """Retrieve task exceptions so asyncio does not log 'Future exception was never retrieved'."""

    def _done(t: asyncio.Task[Any]) -> None:
        if t.cancelled():
            return
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        if HuntCcxtStreams._ws_transport_fatal(exc):
            LOG.debug("hunt_ccxt_task_ws_drop | task=%s error=%s", t.get_name(), exc)
        else:
            LOG.warning("hunt_ccxt_task_failed | task=%s error=%s", t.get_name(), exc)

    task.add_done_callback(_done)


@dataclass
class HuntCcxtStreams:
    """CCXT watch* background tasks — multiplexed streams via ccxt.pro watch_*_for_symbols."""

    client: HuntCcxtClient
    _symbols: set[str] = field(default_factory=set)
    _force_order_buffer: collections.deque[tuple[int, str, str, float, float]] = field(
        default_factory=lambda: collections.deque(maxlen=_LIQ_BUFFER_MAX)
    )
    _liq_buffers_by_venue: dict[str, collections.deque[tuple[int, str, str, float, float]]] = field(
        default_factory=dict
    )
    _agg_points: dict[str, collections.deque[_AggPoint]] = field(default_factory=dict)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _connected: bool = False
    _symbols_dirty: bool = False
    _ccxt_symbols_cache: list[str] = field(default_factory=list)
    _ccxt_symbols_cache_dirty: bool = field(default=True)
    _kline_closed_open_ms: dict[str, int] = field(default_factory=dict)
    _kline_waiting: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _kline_ready: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _last_kline_open_ms: dict[str, int] = field(default_factory=dict)
    _kline_closed_open_ms_5m: dict[str, int] = field(default_factory=dict)
    _kline_waiting_5m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _kline_ready_5m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _last_kline_open_ms_5m: dict[str, int] = field(default_factory=dict)
    _kline_closed_open_ms_15m: dict[str, int] = field(default_factory=dict)
    _kline_waiting_15m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _kline_ready_15m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _last_kline_open_ms_15m: dict[str, int] = field(default_factory=dict)
    kline_ws_enabled: bool = True
    mark_price_enabled: bool = True
    _mark_state: dict[str, tuple[int, float, float, float]] = field(default_factory=dict)
    _last_msg_ms: int = 0
    # live book/ticker/funding data from new multiplexed streams
    _live_books: dict[str, dict[str, Any]] = field(default_factory=dict)
    _live_tickers: dict[str, dict[str, float]] = field(default_factory=dict)
    _live_bbo: dict[str, dict[str, float]] = field(default_factory=dict)
    _live_funding: dict[str, dict[str, float]] = field(default_factory=dict)
    _post_reconnect_quiet_until: float = 0.0
    # cross-exchange funding: {exchange_name: {binance_symbol: {rate, mark, index}}}
    _live_funding_by_exchange: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    _secondary_pro_clients: dict[str, Any] = field(default_factory=dict)
    _secondary_funding_disabled: set[str] = field(default_factory=set)
    _pro_ex: Any | None = field(default=None, repr=False)
    _pro_specs: list[tuple[str, Any]] = field(default_factory=list)
    _reset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_pro_reset: float = 0.0
    _reconnect_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _reconnect_count: int = 0

    def ws_health_metrics(self) -> dict[str, Any]:
        """Expose WS transport health for cycle heartbeat / integrity checks."""
        import time

        now_ms = int(time.time() * 1000)
        age_s: float | None = None
        if self._last_msg_ms > 0:
            age_s = max(0.0, (now_ms - self._last_msg_ms) / 1000.0)
        stale_symbols = 0
        for sym, tick in self._live_tickers.items():
            ts = int(tick.get("ts_ms") or 0)
            if ts <= 0 or (now_ms - ts) / 1000.0 > 120.0:
                stale_symbols += 1
        return {
            "reconnect_count": self._reconnect_count,
            "last_msg_age_s": age_s,
            "stale_symbol_count": stale_symbols,
            "connected": bool(self._connected),
        }

    def _schedule_pro_reconnect(self) -> None:
        """Schedule Pro reconnect off the failing watch task (never self-await)."""
        task = self._reconnect_task
        if task is not None and not task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_binance_pro(),
            name="hunt_ccxt_pro_reconnect",
        )

    @property
    def kline_5m_enabled(self) -> bool:
        return self.kline_ws_enabled and _kline_ws_5m_enabled()

    @property
    def kline_15m_enabled(self) -> bool:
        return self.kline_ws_enabled and _kline_ws_15m_enabled()

    @property
    def cross_ws_connected(self) -> bool:
        """True when secondary-exchange funding WS tasks are active."""
        import os

        enabled = os.getenv("HUNT_CROSS_WS", "").strip().lower() in {"1", "true", "yes"}
        if not enabled or not self._connected:
            return False
        active = [
            name
            for name in self._secondary_pro_clients
            if name not in self._secondary_funding_disabled
        ]
        return bool(active)

    def set_symbols(self, symbols: list[str], *, priority: list[str] | None = None) -> None:
        from hunt_core.data.universe import PINNED_SYMBOLS
        from hunt_core.market.symbol_gate import gate_symbol_list

        ex = self.client.exchange
        symbols = gate_symbol_list(symbols, exchange=ex, label="ws_active")
        priority = gate_symbol_list(list(priority or []), exchange=ex, label="ws_priority")

        ordered: list[str] = []
        seen: set[str] = set()
        for raw in list(priority or []) + list(PINNED_SYMBOLS) + list(symbols):
            sym = to_binance_symbol(raw)
            if sym and sym not in seen:
                seen.add(sym)
                ordered.append(sym)
        trimmed = ordered[:_MAX_SYMBOL_STREAMS]
        new_set = set(trimmed)
        if new_set != self._symbols:
            self._symbols = new_set
            self._symbols_dirty = True
            self._ccxt_symbols_cache_dirty = True

    @staticmethod
    def _ws_has(ex: Any, method: str) -> bool:
        return ccxt_ws_method_available(ex, method)

    @staticmethod
    def _ws_binance_id(ex: Any, raw: str) -> str | None:
        """Map a WS payload symbol → Binance market id, or None when unmappable.

        Best-effort by contract: every caller here iterates a BROADCAST payload
        (``!markPrice@arr`` & co.) that carries contracts outside our loaded
        market set, and every call site already guards on a falsy result. The
        strict ``from_ccxt_symbol`` raised ``BadSymbol`` instead of returning
        None, which aborted the whole batch loop mid-iteration: the USDⓈ-M
        ``!markPrice@arr`` array ends with ~55 COIN-M contracts
        (BTCUSD_PERP, …), so every batch raised at index ~683/738 and burned
        the handler's 1 s backoff. The tracked symbols sort ahead of the bad
        tail so no live data was lost — but nothing guaranteed that ordering.
        """
        raw_sym = str(raw or "").strip()
        if not raw_sym:
            return None
        return try_binance_id_from_ccxt(raw_sym, exchange=ex)

    def _ws_ex(self) -> Any:
        if self._pro_ex is None:
            msg = "HuntCcxtStreams.start() must be called before watch loops"
            raise RuntimeError(msg)
        return self._pro_ex

    @staticmethod
    def _ws_transport_fatal(exc: BaseException) -> bool:
        text = repr(exc)
        name = type(exc).__name__
        if is_ccxt_rate_limited(exc):
            return True
        if isinstance(exc, TimeoutError):
            return True
        return (
            "1006" in text
            or "4004" in text
            or name in {"NetworkError", "RequestTimeout", "ExchangeNotAvailable", "ChecksumError"}
            or "ConnectionClosed" in name
            or "ChecksumError" in text
        )

    @staticmethod
    def _close_stale_public_ws_client(ex: Any, stream_idx: str | None, *, label: str) -> None:
        """Evict and close the /public/ws/{stream_idx} client (fire-and-forget).

        Called when symbol rotation caused watch_trades_for_symbols /
        watch_order_book_for_symbols to open a NEW WS connection (because the
        streamHash includes all symbol names, so a different symbol set → different
        URL).  The previous connection has futures=0 (nobody awaiting) but Binance
        still sends data to it, eventually terminating it with 1006.  We pre-empt
        that by closing it ourselves as soon as the active URL changes.
        """
        if stream_idx is None:
            return
        target = f"/public/ws/{stream_idx}"
        for url in list(ex.clients.keys()):
            if url.endswith(target):
                cli = ex.clients.pop(url, None)
                if cli is not None:
                    asyncio.ensure_future(cli.close())
                    LOG.info("ws_stale_closed | label=%s url=...%s", label, url[-30:])

    def _spawn_pro_tasks(self, specs: list[tuple[str, Any]]) -> list[asyncio.Task[None]]:
        tasks: list[asyncio.Task[None]] = []
        for name, fn in specs:
            task = asyncio.create_task(fn(), name=name)
            _attach_task_guard(task)
            tasks.append(task)
        return tasks

    @staticmethod
    async def _dispose_secondary_pro_ex(ex: Any | None, *, label: str) -> None:
        """Close a secondary Pro client that never reached ``_secondary_pro_clients``."""
        if ex is not None:
            await close_exchange_async(ex, label=label)

    async def _reconnect_binance_pro(self) -> None:
        """Cancel all Binance Pro watch tasks, reset client, respawn (CCXT wiki pattern)."""
        async with self._reset_lock:
            now = time.monotonic()
            if now - self._last_pro_reset < 8.0:
                return
            self._last_pro_reset = now
            if not self._pro_specs:
                return
            LOG.info("hunt_ccxt_pro_reconnect_start")
            pro_tasks = [t for t in self._tasks if t.get_name() != "hunt_ccxt_funding_cross"]
            for task in pro_tasks:
                task.cancel()
            for task in pro_tasks:
                await _join_cancelled_task(task)
            self._tasks = [t for t in self._tasks if t.get_name() == "hunt_ccxt_funding_cross"]
            try:
                self._pro_ex = await self.client.reset_pro_exchange()
            except Exception as re_exc:
                LOG.warning("hunt_ccxt_pro_reconnect_failed | error=%s", re_exc)
                return
            self._tasks.extend(self._spawn_pro_tasks(self._pro_specs))
            # Drain stale kline-close backlog; REST/cache need ~30s after transport reset.
            self._kline_ready.clear()
            self._kline_ready_5m.clear()
            self._kline_ready_15m.clear()
            self._kline_waiting.clear()
            self._kline_waiting_5m.clear()
            self._kline_waiting_15m.clear()
            self._post_reconnect_quiet_until = time.monotonic() + 45.0
            self._reconnect_count += 1
            LOG.info(
                "hunt_ccxt_pro_reconnected | tasks=%s",
                [t.get_name() for t in self._tasks if t.get_name() != "hunt_ccxt_funding_cross"],
            )

    async def _on_ws_loop_error(self, label: str, exc: BaseException) -> None:
        if self._ws_transport_fatal(exc):
            LOG.debug("hunt_ccxt_%s_error | %s", label, repr(exc))
        else:
            LOG.warning("hunt_ccxt_%s_error | %s", label, repr(exc))
        if is_ccxt_rate_limited(exc):
            self.client.rest_gate.record_error(exc, context=f"ws:{label}")
            return
        if self._ws_transport_fatal(exc):
            # Suppress cascade reconnects during post-reconnect quiet window.
            # _reconnect_task guard already blocks if task is running; this guard
            # blocks the frequent 1006s that fire right after a completed reconnect.
            if time.monotonic() < self._post_reconnect_quiet_until:
                return
            LOG.info("hunt_ccxt_reconnect_trigger | label=%s exc=%.120s", label, repr(exc))
            try:
                ex = self._ws_ex()
                for url, cli in ex.clients.items():
                    LOG.debug(
                        "ws_sub_diag | label=%s url=...%s subs=%d futures=%d",
                        label, url[-40:], len(cli.subscriptions), len(cli.futures),
                    )
            except Exception as diag_exc:
                LOG.debug("ws_sub_diag_error | label=%s error=%s", label, diag_exc)
            self._schedule_pro_reconnect()
            # Suspend until the reconnect task cancels us.  Without this sleep the
            # calling task's while-loop would immediately call watch_*() again on the
            # dying exchange, causing CCXT Pro to open a new WS connection before
            # old.close() runs.  Binance then sees duplicate subscriptions from the
            # same IP and closes one with 1006, creating the next reconnect cycle.
            await asyncio.sleep(300.0)
            return
        await asyncio.sleep(2.0)

    def liquidation_rollups(self, symbol: str, *, window_seconds: int = 300) -> dict[str, float] | None:
        return _liquidation_rollups(
            self._force_order_buffer,
            symbol=to_binance_symbol(symbol),
            window_seconds=window_seconds,
        )

    def liquidation_events(self, symbol: str, *, window_seconds: int = 300) -> int:
        return _liquidation_event_count(
            self._force_order_buffer,
            symbol=to_binance_symbol(symbol),
            window_seconds=window_seconds,
        )

    def agg_trade_buy_ratio(
        self,
        symbol: str,
        *,
        window_seconds: int = 60,
        use_nq: bool | None = None,
    ) -> float | None:
        """Taker buy share in window (0–1), not signed delta — §3 rename."""
        sym = to_binance_symbol(symbol)
        if use_nq is None:
            use_nq = orderflow_use_nq(sym)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        buy = sell = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff:
                continue
            vol = pt.qty if use_nq else pt.qty_full
            if pt.is_buy:
                buy += vol
            else:
                sell += vol
        total = buy + sell
        if total <= 0:
            return None
        return round(buy / total, 3)

    def agg_rpi_skew(self, symbol: str, *, window_seconds: int = 60) -> float | None:
        sym = to_binance_symbol(symbol)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        nq_sum = q_sum = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff:
                continue
            nq_sum += pt.qty
            q_sum += pt.qty_full
        if q_sum <= 0:
            return None
        return round(max(0.0, (q_sum - nq_sum) / q_sum), 4)

    def ws_cvd(self, symbol: str, *, window_seconds: int = 60, use_nq: bool | None = None) -> float | None:
        """Rolling signed volume delta (buy qty − sell qty) from watch_trades."""
        sym = to_binance_symbol(symbol)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        if use_nq is None:
            use_nq = orderflow_use_nq(sym)
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        cvd = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff:
                continue
            vol = pt.qty if use_nq else pt.qty_full
            cvd += vol if pt.is_buy else -vol
        return round(cvd, 6)

    def ws_price_change_pct(self, symbol: str, *, window_seconds: int = 60) -> float | None:
        """Trade-price change across a rolling WS window (for CVD divergence)."""
        sym = to_binance_symbol(symbol)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        first_px = last_px = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff or pt.price <= 0:
                continue
            if first_px <= 0:
                first_px = pt.price
            last_px = pt.price
        if first_px <= 0 or last_px <= 0:
            return None
        return round((last_px - first_px) / first_px * 100.0, 4)

    def live_book(self, symbol: str) -> dict[str, Any] | None:
        """Latest L2 order book snapshot from watch_order_book_for_symbols."""
        return self._live_books.get(to_binance_symbol(symbol))

    def live_ticker(self, symbol: str, *, max_age_s: float | None = None) -> dict[str, float] | None:
        """Latest 24h ticker from watch_tickers; None if missing or stale."""
        entry = self._live_tickers.get(to_binance_symbol(symbol))
        if not entry:
            return None
        if max_age_s is not None:
            ts_ms = float(entry.get("ts_ms") or 0)
            if ts_ms <= 0 or (time.time() * 1000 - ts_ms) > max_age_s * 1000:
                return None
        return entry

    def live_bbo(self, symbol: str, *, max_age_s: float | None = None) -> dict[str, float] | None:
        """Top-of-book bid/ask — served from L1 order-book depth (watchOrderBookForSymbols).

        BBO WS (watchBidsAsks) was removed: per-symbol bookTicker subscriptions for equity
        tokens (TSLAUSDT, MSFTUSDT etc.) are rejected by Binance with close-code 4004 outside
        US market hours, causing 3-second connection churn that destabilises other streams.
        The depth stream already carries L1 bid/ask at equal or better freshness.

        ``max_age_s`` age-gates the entry (mirrors ``live_ticker``): when the depth
        stream stalls, the last book must NOT be served as a fresh executable price —
        entries/stops/targets would be geometry off a dead quote.
        """
        sym = to_binance_symbol(symbol)
        book = self._live_books.get(sym)
        if book and max_age_s is not None:
            ts_ms = float(book.get("ts_ms") or 0)
            if ts_ms <= 0 or (time.time() * 1000 - ts_ms) > max_age_s * 1000:
                return None
        if book and book.get("bid") and book.get("ask"):
            bid = float(book["bid"])
            ask = float(book["ask"])
            spread_pct = (ask - bid) / bid * 100.0 if bid > 0 else 0.0
            return {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 5)}
        return None

    def live_funding(
        self, symbol: str, *, max_age_s: float | None = None
    ) -> dict[str, float] | None:
        """Latest funding/mark from watch_funding_rates; age-gated when max_age_s given.

        The markPrice here feeds the price oracle, so a stalled mark stream must not
        pass a dead mark off as live (same rationale as live_ticker/live_bbo).
        """
        entry = self._live_funding.get(to_binance_symbol(symbol))
        if entry and max_age_s is not None:
            ts_ms = float(entry.get("ts_ms") or 0)
            if ts_ms <= 0 or (time.time() * 1000 - ts_ms) > max_age_s * 1000:
                return None
        return entry

    def live_funding_cross(self, symbol: str) -> dict[str, dict[str, float]]:
        """Latest funding rates from secondary exchanges keyed by exchange name."""
        sym = to_binance_symbol(symbol)
        return {
            ex: data[sym]
            for ex, data in self._live_funding_by_exchange.items()
            if sym in data
        }

    def liquidation_buffers(self) -> dict[str, collections.deque[tuple[int, str, str, float, float]]]:
        """Per-venue liquidation ring buffers (binance + cross WS when enabled)."""
        return {"binance": self._force_order_buffer, **self._liq_buffers_by_venue}

    def trade_buffer(self, symbol: str) -> collections.deque[_AggPoint]:
        return self._agg_points.get(to_binance_symbol(symbol), collections.deque())

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = to_binance_symbol(symbol)
        liq = self.liquidation_rollups(sym, window_seconds=300)
        liq_60 = self.liquidation_rollups(sym, window_seconds=60)
        fresh = self._last_msg_ms > 0 and time.time() * 1000 - self._last_msg_ms < 20_000
        book = self._live_books.get(sym) or {}
        ticker = self._live_tickers.get(sym) or {}
        funding = self._live_funding.get(sym) or {}
        mark_px = funding.get("markPrice")
        last_px = ticker.get("last")
        if mark_px is None and book.get("bid") and book.get("ask"):
            mark_px = (float(book["bid"]) + float(book["ask"])) / 2.0
        elif mark_px is None and last_px:
            mark_px = last_px
        heatmap = None
        liq_map = None
        bracket_tiers = None
        maps_cfg = load_maps_config()
        if mark_px is not None:
            try:
                bracket_tiers = self.client.get_cached_leverage_tiers(sym)
                liq_buffers = {"binance": self._force_order_buffer, **self._liq_buffers_by_venue}
                liq_map = build_liquidation_map(
                    liq_buffers,
                    symbol=sym,
                    current_price=float(mark_px),
                    cfg=maps_cfg,
                    bracket_tiers=bracket_tiers,
                )
                heatmap = liq_map.heatmap if liq_map else None
            except (TypeError, ValueError) as liq_map_exc:
                LOG.debug("snapshot_liquidation_map_error | sym=%s error=%s", sym, liq_map_exc)
                heatmap = None
                liq_map = None
                bracket_tiers = None
        liq_source = "binance_brackets" if bracket_tiers else "default"
        if liq_map:
            liq_fields = liq_map.to_dict()
        else:
            liq_fields = heatmap_to_market_dict(heatmap, prospective_source=liq_source)
        ratio_60 = self.agg_trade_buy_ratio(sym, window_seconds=60)
        ratio_30 = self.agg_trade_buy_ratio(sym, window_seconds=30)
        return {
            "ws_routed_market": True,
            "ws_base_url": "ccxt.pro",
            "ws_connected": self._connected and fresh,
            "cross_ws_connected": self.cross_ws_connected,
            "ws_socket_open": self._connected,
            "ws_last_msg_age_s": (
                round((time.time() * 1000 - self._last_msg_ms) / 1000.0, 1)
                if self._last_msg_ms
                else None
            ),
            "liq_events_5m": self.liquidation_events(sym, window_seconds=300),
            "liq_events_1m": self.liquidation_events(sym, window_seconds=60),
            "liquidation_score_5m": (liq or {}).get("liquidation_score"),
            "liquidation_score_1m": (liq_60 or {}).get("liquidation_score"),
            "liquidation_long_notional_5m": (liq or {}).get("liquidation_long_notional"),
            "liquidation_short_notional_5m": (liq or {}).get("liquidation_short_notional"),
            "agg_trade_buy_ratio_60s": ratio_60,
            "agg_trade_buy_ratio_30s": ratio_30,
            # Legacy keys (buy share, not signed delta)
            "agg_trade_delta_60s": ratio_60,
            "agg_trade_delta_30s": ratio_30,
            "agg_trade_source": "ccxt_watch_trades",
            "agg_rpi_skew_60s": self.agg_rpi_skew(sym, window_seconds=60),
            "ws_cvd_1m": self.ws_cvd(sym, window_seconds=60),
            "ws_cvd_5m": self.ws_cvd(sym, window_seconds=300),
            "ws_price_chg_1m": self.ws_price_change_pct(sym, window_seconds=60),
            "ws_price_chg_5m": self.ws_price_change_pct(sym, window_seconds=300),
            f"kline_{_KLINE_INTERVAL}_last_close_ms": self._kline_closed_open_ms.get(sym),
            "kline_ws_interval": _KLINE_INTERVAL,
            # live book microstructure
            "live_bid": book.get("bid"),
            "live_ask": book.get("ask"),
            "live_depth_imbalance": book.get("depth_imbalance"),
            "ws_depth_imbalance": book.get("ws_depth_imbalance"),
            "live_microprice_bias": book.get("microprice_bias"),
            # live ticker
            "live_quote_volume": ticker.get("quoteVolume"),
            "live_price_change_pct": ticker.get("percentage"),
            "live_last_price": last_px,
            # live funding
            "live_funding_rate": funding.get("fundingRate"),
            "live_mark_price": funding.get("markPrice"),
            # Age of the mark, so the price oracle can refuse a stale snapshot mark
            # instead of serving it as live (resolve_live_price).
            "live_mark_ts_ms": funding.get("ts_ms"),
            "live_index_price": funding.get("indexPrice"),
            **(self.mark_snapshot(sym) or {}),
            **liq_fields,
        }

    def _promote_kline_grace(self, *, interval: str = _KLINE_INTERVAL) -> None:
        now_ms = int(time.time() * 1000)
        grace_ms = int(_kline_grace_sec() * 1000)
        if interval == _KLINE_5M_INTERVAL:
            waiting, ready, closed_ms = (
                self._kline_waiting_5m,
                self._kline_ready_5m,
                self._kline_closed_open_ms_5m,
            )
        elif interval == _KLINE_15M_INTERVAL:
            waiting, ready, closed_ms = (
                self._kline_waiting_15m,
                self._kline_ready_15m,
                self._kline_closed_open_ms_15m,
            )
        else:
            waiting, ready, closed_ms = (
                self._kline_waiting,
                self._kline_ready,
                self._kline_closed_open_ms,
            )
        for sym, bar in list(waiting.items()):
            if now_ms - bar.received_ms < grace_ms:
                continue
            prev = closed_ms.get(sym)
            if prev != bar.open_ms:
                closed_ms[sym] = bar.open_ms
            ready[sym] = bar
            waiting.pop(sym, None)

    def pop_kline_close_triggers(self) -> set[str]:
        if time.monotonic() < self._post_reconnect_quiet_until:
            self._kline_ready.clear()
            if self.kline_5m_enabled:
                self._kline_ready_5m.clear()
            if self.kline_15m_enabled:
                self._kline_ready_15m.clear()
            return set()
        self._promote_kline_grace()
        if self.kline_5m_enabled:
            self._promote_kline_grace(interval=_KLINE_5M_INTERVAL)
        if self.kline_15m_enabled:
            self._promote_kline_grace(interval=_KLINE_15M_INTERVAL)
        return set(self._kline_ready)

    def consume_kline_close_triggers(self, symbols: set[str] | frozenset[str]) -> None:
        for sym in symbols:
            sym_n = to_binance_symbol(str(sym))
            self._kline_ready.pop(sym_n, None)
            if self.kline_5m_enabled:
                self._kline_ready_5m.pop(sym_n, None)
            if self.kline_15m_enabled:
                self._kline_ready_15m.pop(sym_n, None)

    def _bar_overlay(self, bar: _ClosedKlineBar, *, interval: str) -> dict[str, Any]:
        body = abs(bar.c - bar.o)
        full = max(bar.h - bar.l, 1e-12)
        upper_wick = bar.h - max(bar.o, bar.c)
        lower_wick = min(bar.o, bar.c) - bar.l
        return {
            "close": round(bar.c, 6),
            "closed_bar": True,
            "ws_open_ms": bar.open_ms,
            "ws_grace_s": _kline_grace_sec(),
            "ws_interval": interval,
            "candle": {
                "open": round(bar.o, 6),
                "high": round(bar.h, 6),
                "low": round(bar.l, 6),
                "close": round(bar.c, 6),
                "upper_wick_ratio": round(upper_wick / full, 3),
                "lower_wick_ratio": round(lower_wick / full, 3),
                "body_ratio": round(body / full, 3),
                "bearish": bar.c < bar.o,
                "bullish": bar.c > bar.o,
            },
        }

    def closed_kline_overlay(
        self,
        symbol: str,
        *,
        interval: str = _KLINE_INTERVAL,
    ) -> dict[str, Any] | None:
        if interval == _KLINE_5M_INTERVAL and not self.kline_5m_enabled:
            return None
        if interval == _KLINE_15M_INTERVAL and not self.kline_15m_enabled:
            return None
        self._promote_kline_grace(interval=interval)
        sym = to_binance_symbol(symbol)
        if interval == _KLINE_5M_INTERVAL:
            ready = self._kline_ready_5m
        elif interval == _KLINE_15M_INTERVAL:
            ready = self._kline_ready_15m
        else:
            ready = self._kline_ready
        bar = ready.get(sym)
        if bar is None:
            return None
        return self._bar_overlay(bar, interval=interval)

    def closed_5m_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol, interval=_KLINE_5M_INTERVAL)

    def closed_15m_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol, interval=_KLINE_15M_INTERVAL)

    def closed_1m_kline_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol, interval=_KLINE_INTERVAL)

    def mark_snapshot(self, symbol: str, *, max_age_s: float = 10.0) -> dict[str, float | None] | None:
        """Age-gated mark/index/funding from the markPrice stream.

        Units contract (pinned in tests/test_ws_rest_consistency.py):
        funding_live is a FRACTION (like REST fundingRate), basis_bps_live is
        BASIS POINTS. Missing index/funding must surface as None, never as a
        fabricated 0.0 that would clobber a real REST value downstream (I-6).
        """
        rec = self._mark_state.get(to_binance_symbol(symbol))
        if rec is None:
            return None
        ts_ms, mark, index, funding = rec
        if time.time() * 1000 - ts_ms > max_age_s * 1000:
            return None
        return {
            "mark_live": mark,
            "index_live": index if index > 0 else None,
            # funding==0.0 here means "field absent in the raw message" (parsed via
            # `or 0`); real funding is never exactly 0 at this precision — same
            # convention as the `if funding != 0` guard in _watch_mark_prices.
            "funding_live": funding if funding != 0 else None,
            "basis_bps_live": (
                round((mark - index) / index * 10_000, 2) if index > 0 else None
            ),
        }

    def _touch(self) -> None:
        self._last_msg_ms = int(time.time() * 1000)

    def _record_liquidation(self, item: dict[str, Any], *, exchange: Any, venue: str = "binance") -> None:
        # CCXT's unified Liquidation structure has NO `amount` key (it carries
        # `contracts`/`contractSize`/`baseValue` — see base/exchange.py
        # safe_liquidation). The old `item["amount"] or info["q"]` chain therefore
        # resolved via the Binance-RAW `q` only: bybit/okx yielded qty=0.0 and were
        # dropped by the `qty > 0` guard below, so Bybit — the only FULL-fidelity
        # tape (_VENUE_LIQ_COMPLETENESS) — never produced a single event.
        # Size/side/price parsing lives in hunt_core.maps.liquidation (unit-tested
        # without a WS); imported locally to match _record_liquidation's existing
        # local-import style.
        from hunt_core.maps.liquidation import (
            liq_contract_size,
            liq_contract_units,
            liq_price,
            normalize_liq_side,
        )

        _raw_info = item.get("info")
        info: dict[str, Any] = _raw_info if isinstance(_raw_info, dict) else {}
        ccxt_sym = str(item.get("symbol") or info.get("s") or "")
        sym = self._ws_binance_id(exchange, ccxt_sym)
        # Unknown side => SKIP. Never default to long: ccxt's bybit parser emits the
        # literal "s" (safe_string_lower(liq, 'side', 'S') treats 'S' as a DEFAULT
        # VALUE, not a second key), which the old `else -> long` branch booked as a
        # long-liquidation for every bybit SHORT liquidation.
        side_norm = normalize_liq_side(item, info)
        contracts = liq_contract_units(item, info)
        price_val = liq_price(item, info)
        if side_norm is None or contracts is None:
            LOG.debug(
                "liquidation_unparsed | venue=%s sym=%s side=%s contracts=%s",
                venue, sym, side_norm, contracts,
            )
            return
        # contracts -> base units. OKX quotes size in CONTRACTS (sz=13 @ 0.01);
        # bybit linear / binance USDⓈ-M are contractSize=1, so this is a no-op there.
        market: dict[str, Any] | None = None
        if not isinstance(item.get("contractSize"), (int, float)) and ccxt_sym:
            try:
                market = exchange.market(ccxt_sym)
            except Exception as mkt_exc:  # unknown/unloaded market -> multiplier 1.0
                LOG.debug("liquidation_market_lookup_failed | sym=%s error=%s", ccxt_sym, mkt_exc)
        side = side_norm
        qty = contracts * liq_contract_size(item, market)
        price = price_val if price_val is not None else 0.0
        ts_ms = int(item.get("timestamp") or info.get("T") or time.time() * 1000)
        if sym and qty > 0:
            ev = (ts_ms, sym, side, qty, price)
            # _force_order_buffer IS the "binance" buffer (liquidation_buffers() returns
            # it under that key). Appending EVERY venue's events to it meant each
            # secondary liquidation was counted TWICE downstream — once as "binance",
            # once under its own venue — inflating realized counts and cluster notional,
            # and mislabelling provenance (a bybit=full event booked as binance=capped_1s).
            # Only the primary venue's tape belongs here.
            if venue == _PRIMARY_LIQ_VENUE:
                self._force_order_buffer.append(ev)
            else:
                buf = self._liq_buffers_by_venue.setdefault(
                    venue,
                    collections.deque(maxlen=_LIQ_BUFFER_MAX),
                )
                buf.append(ev)
            try:
                from hunt_core.maps.engine import get_map_store

                get_map_store().record_liquidation(
                    sym, venue=venue, ts_ms=ts_ms, side=side, qty=qty, price=price
                )
            except Exception as map_exc:
                LOG.debug("liquidation_map_store_error | sym=%s venue=%s error=%s", sym, venue, map_exc)

    def _record_trade(self, sym: str, trade: dict[str, Any]) -> None:
        _raw_info = trade.get("info")
        info: dict[str, Any] = _raw_info if isinstance(_raw_info, dict) else {}
        qty_full = float(trade.get("amount") or info.get("q") or 0)
        nq_raw = info.get("nq")
        qty_nq = float(nq_raw) if nq_raw is not None else qty_full
        qty = qty_nq if qty_nq > 0 else qty_full
        ts_ms = int(trade.get("timestamp") or info.get("T") or time.time() * 1000)
        px = float(trade.get("price") or info.get("p") or 0)
        is_buy = _taker_is_buy(trade, info)
        if qty <= 0 and qty_full <= 0:
            return
        buf = self._agg_points.setdefault(sym, collections.deque(maxlen=_AGG_BUFFER_MAX))
        buf.append(
            _AggPoint(
                ts_ms=ts_ms,
                qty=qty,
                qty_full=qty_full if qty_full > 0 else qty,
                is_buy=is_buy,
                price=px,
            )
        )

    def _on_closed_kline(self, sym: str, candle: list[Any], *, interval: str = _KLINE_INTERVAL) -> None:
        try:
            open_ms = int(candle[0])
            o, h, low, c, v = (
                float(candle[1]),
                float(candle[2]),
                float(candle[3]),
                float(candle[4]),
                float(candle[5]),
            )
        except (TypeError, ValueError, IndexError) as kline_parse_exc:
            LOG.debug("on_closed_kline_parse_error | sym=%s error=%s candle=%s", sym, kline_parse_exc, candle[:6] if isinstance(candle, list) else candle)
            return
        if open_ms <= 0 or c <= 0:
            return
        bar = _ClosedKlineBar(
            open_ms=open_ms,
            o=o,
            h=h,
            l=low,
            c=c,
            v=v,
            received_ms=int(time.time() * 1000),
        )
        if interval == _KLINE_5M_INTERVAL:
            self._kline_waiting_5m[sym] = bar
        elif interval == _KLINE_15M_INTERVAL:
            self._kline_waiting_15m[sym] = bar
        else:
            self._kline_waiting[sym] = bar

    def _on_ohlcv_update(
        self,
        sym: str,
        ohlcv: list[list[Any]],
        *,
        interval: str = _KLINE_INTERVAL,
    ) -> None:
        if not ohlcv:
            return
        latest_open = int(ohlcv[-1][0])
        if interval == _KLINE_5M_INTERVAL:
            last_open = self._last_kline_open_ms_5m
        elif interval == _KLINE_15M_INTERVAL:
            last_open = self._last_kline_open_ms_15m
        else:
            last_open = self._last_kline_open_ms
        prev_open = last_open.get(sym)
        if prev_open is not None and latest_open != prev_open and len(ohlcv) >= 2:
            self._on_closed_kline(sym, ohlcv[-2], interval=interval)
        last_open[sym] = latest_open
        try:
            from hunt_core.data.frame_cache import get_frame_cache

            ex = self._pro_ex
            if ex is not None:
                get_frame_cache().update_ohlcv(sym, interval, ohlcv, exchange=ex)
        except Exception:
            LOG.debug("frame_cache_ws_update_skipped", exc_info=True)

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        await self.client.load_markets()
        self._pro_ex = await self.client.acquire_pro_exchange()
        ex = self._pro_ex
        specs: list[tuple[str, Any]] = []
        liq_mode = liquidation_ws_mode(ex)
        if liq_mode == "mux":
            specs.append(("hunt_ccxt_liq", self._watch_liquidations_mux))
        elif liq_mode == "per_symbol":
            specs.append(("hunt_ccxt_liq", self._watch_liquidations_symbol))
        if self.mark_price_enabled and self._ws_has(ex, "watchMarkPrices"):
            specs.append(("hunt_ccxt_mark", self._watch_mark_prices))
        if self._ws_has(ex, "watchTradesForSymbols"):
            specs.append(("hunt_ccxt_trades", self._watch_trades_mux))
        if self.kline_ws_enabled and (
            self._ws_has(ex, "watchOHLCVForSymbols") or self._ws_has(ex, "watchOHLCV")
        ):
            specs.append(("hunt_ccxt_kline", self._watch_ohlcv_mux))
            if self.kline_5m_enabled:
                specs.append(("hunt_ccxt_kline_5m", self._watch_ohlcv_5m_mux))
            if self.kline_15m_enabled:
                specs.append(("hunt_ccxt_kline_15m", self._watch_ohlcv_15m_mux))
        elif self.kline_ws_enabled:
            self.kline_ws_enabled = False
            LOG.info("hunt_ccxt_kline_disabled | reason=exchange_has_no_watch_ohlcv")
        if self._ws_has(ex, "watchOrderBookForSymbols"):
            specs.append(("hunt_ccxt_book", self._watch_order_book_mux))
        if self._ws_has(ex, "watchTickers"):
            specs.append(("hunt_ccxt_tickers", self._watch_tickers_mux))
        # watchBidsAsks (bookTicker) removed — per-symbol exotic subscriptions cause
        # 4004 close-code churn; bid/ask served from depth stream via live_bbo().
        if self._ws_has(ex, "watchFundingRates"):
            specs.append(("hunt_ccxt_funding", self._watch_funding_rates_mux))
        cross_ws = os.getenv("HUNT_CROSS_WS", "").strip().lower() in {"1", "true", "yes"}
        if cross_ws:
            specs.append(("hunt_ccxt_funding_cross", self._watch_secondary_funding_mux))
            if funding_rest_poll_venues():
                specs.append(("hunt_ccxt_funding_rest", self._watch_secondary_funding_rest_mux))
            if os.getenv("HUNT_MAPS_LIQ_CROSS", "1").strip().lower() in {"1", "true", "yes"}:
                specs.append(("hunt_ccxt_liq_cross", self._watch_secondary_liquidations_mux))
        else:
            LOG.info("hunt_cross_ws_disabled | hint=set HUNT_CROSS_WS=1 for Bybit/OKX WS")
        if not specs:
            LOG.warning("hunt_ccxt_streams_no_capabilities | ws_plane=binance_future")
        self._pro_specs = [(n, fn) for n, fn in specs if n != "hunt_ccxt_funding_cross"]
        self._tasks = self._spawn_pro_tasks(self._pro_specs)
        cross = [(n, fn) for n, fn in specs if n == "hunt_ccxt_funding_cross"]
        self._tasks.extend(self._spawn_pro_tasks(cross))
        self._connected = True
        LOG.info("hunt_ccxt_streams_started | tasks=%s", [n for n, _ in specs])

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            await _join_cancelled_task(task)
        self._tasks.clear()
        # Let cancelled mux tasks finish disposing in-flight secondary clients.
        await asyncio.sleep(0.25)
        for name, ex in list(self._secondary_pro_clients.items()):
            await close_exchange_async(ex, label=f"secondary_pro:{name}")
        self._secondary_pro_clients.clear()
        # Pro client owned by HuntCcxtClient — closed in plane.aclose → client.close().
        self._pro_ex = None
        self._connected = False
        await asyncio.sleep(0.35)

    async def _watch_liquidations_mux(self) -> None:
        """All-symbol liquidations via watch_liquidations_for_symbols."""
        ex = self._ws_ex()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                items = await asyncio.wait_for(ex.watch_liquidations_for_symbols(syms), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                batch = items if isinstance(items, list) else [items]
                for item in batch:
                    if isinstance(item, dict):
                        self._record_liquidation(item, exchange=ex)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                # Liquidation stream may reject exotic symbols (4004).
                # Liquidations are supplementary — do not reconnect exchange.
                if self._ws_transport_fatal(exc):
                    LOG.debug("hunt_ccxt_liq_ws_fail | %s", repr(exc)[:100])
                    await asyncio.sleep(3.0)
                else:
                    await self._on_ws_loop_error("liq_mux", exc)

    async def _watch_liquidations_symbol(self) -> None:
        """Fallback: round-robin watch_liquidations(symbol) per subscribed symbol."""
        ex = self._ws_ex()
        idx = 0
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            ccxt_sym = syms[idx % len(syms)]
            idx += 1
            try:
                items = await asyncio.wait_for(ex.watch_liquidations(ccxt_sym), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                batch = items if isinstance(items, list) else [items]
                for item in batch:
                    if isinstance(item, dict):
                        self._record_liquidation(item, exchange=ex)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if self._ws_transport_fatal(exc):
                    LOG.debug("hunt_ccxt_liq_sym_ws_fail | sym=%s %s", ccxt_sym, repr(exc)[:100])
                    await asyncio.sleep(3.0)
                else:
                    await self._on_ws_loop_error("liq_symbol", exc)

    async def _watch_mark_prices(self) -> None:
        if not self.mark_price_enabled:
            return
        ex = self._ws_ex()
        while not self._stop.is_set():
            try:
                prices = await asyncio.wait_for(ex.watch_mark_prices(), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                now_ms = int(time.time() * 1000)
                items = list(prices.values()) if isinstance(prices, dict) else (prices or [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    if not sym or sym not in self._symbols:
                        continue
                    mark = float(item.get("markPrice") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    # (removed: info["ap"] parsing — Binance markPriceUpdate carries
                    # e/E/s/p/i/P/r/T only, so "ap" was always absent and the whole
                    # mark_ap_live/basis_ap_bps branch downstream was dead, audit G.)
                    if mark > 0:
                        self._mark_state[sym] = (now_ms, mark, index, funding)
                        prev = dict(self._live_funding.get(sym) or {})
                        prev["markPrice"] = mark
                        if index > 0:
                            prev["indexPrice"] = index
                        if funding != 0:
                            prev["fundingRate"] = funding
                        prev["ts_ms"] = int(time.time() * 1000)  # age-gate stamp
                        self._live_funding[sym] = prev
                        self.client.update_basis_from_websocket(sym, mark, index if index > 0 else None)
            except asyncio.CancelledError:
                break
            except ccxt.BadSymbol as exc:
                LOG.debug("hunt_ccxt_mark_skip | delisted=%s", exc)
                await asyncio.sleep(1.0)
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("mark", exc)

    def _ccxt_symbols(self) -> list[str]:
        if not self._ccxt_symbols_cache_dirty and self._ccxt_symbols_cache:
            return self._ccxt_symbols_cache
        ex = self._ws_ex()
        self._ccxt_symbols_cache = [to_ccxt_symbol(s, exchange=ex) for s in sorted(self._symbols)]
        self._ccxt_symbols_cache_dirty = False
        return self._ccxt_symbols_cache

    async def _watch_trades_mux(self) -> None:
        """Single multiplexed trades stream for all symbols via watch_trades_for_symbols."""
        ex = self._ws_ex()
        _subscribed: frozenset[str] = frozenset()
        _active_stream_idx: str | None = None  # current live trades URL stream index
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            syms_set = frozenset(syms)
            _subscribed = syms_set
            # Sort for stable streamHash: same symbol SET → same URL on every rotation,
            # preventing watch_trades_for_symbols from opening a new WS connection merely
            # because the list order changed.
            sorted_syms = sorted(syms_set)
            try:
                trades = await asyncio.wait_for(ex.watch_trades_for_symbols(sorted_syms), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                # Detect URL change: symbol set rotated → CCXT opened new /public/ws/N.
                # Close the previous stale connection so Binance doesn't 1006 it later.
                stream_hash = "multipleTrades::" + ",".join(sorted_syms)
                new_idx = (getattr(ex, "options", {}) or {}).get("streamBySubscriptionsHash", {}).get(stream_hash)
                if new_idx is not None and new_idx != _active_stream_idx:
                    self._close_stale_public_ws_client(ex, _active_stream_idx, label="trades")
                    _active_stream_idx = new_idx
                for trade in trades if isinstance(trades, list) else [trades]:
                    if not isinstance(trade, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(trade.get("symbol") or ""))
                    if sym:
                        self._record_trade(sym, trade)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if self._ws_transport_fatal(exc):
                    _subscribed = frozenset()
                    _active_stream_idx = None
                await self._on_ws_loop_error("trades", exc)

    @staticmethod
    async def _unwatch_removed_ohlcv(ex: Any, removed: frozenset[str], interval: str) -> None:
        """Send UNSUBSCRIBE for symbols that left the watchlist on the multipleOHLCV stream.
        Prevents subscription count from growing past Binance's 200/stream limit which
        causes 1006 closes after ~20 minutes of symbol rotation.
        """
        LOG.debug("kline_unwatch_attempt | interval=%s count=%d syms=%s", interval, len(removed), sorted(removed)[:5])
        try:
            await ex.un_watch_ohlcv_for_symbols([(s, interval) for s in removed])
            LOG.debug("kline_unwatch_done | interval=%s count=%d", interval, len(removed))
        except asyncio.CancelledError:
            raise
        except Exception as ue:
            LOG.warning("kline_unwatch_err | interval=%s error=%s", interval, ue)

    async def _watch_ohlcv_mux(self) -> None:
        """Single multiplexed OHLCV stream for all symbols via watch_ohlcv_for_symbols."""
        if not self.kline_ws_enabled:
            return
        ex = self._ws_ex()
        _subscribed: frozenset[str] = frozenset()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            syms_set = frozenset(syms)
            removed = _subscribed - syms_set
            if removed:
                await self._unwatch_removed_ohlcv(ex, removed, _KLINE_INTERVAL)
            _subscribed = syms_set
            try:
                if self._ws_has(ex, "watchOHLCVForSymbols"):
                    pairs = [(s, _KLINE_INTERVAL) for s in syms]
                    result = await asyncio.wait_for(ex.watch_ohlcv_for_symbols(pairs), timeout=_WS_WATCH_TIMEOUT_S)
                    self._touch()
                    if isinstance(result, dict):
                        for ccxt_sym, tf_map in result.items():
                            sym = self._ws_binance_id(ex, str(ccxt_sym))
                            if not sym or not isinstance(tf_map, dict):
                                continue
                            ohlcv = tf_map.get(_KLINE_INTERVAL)
                            if isinstance(ohlcv, list):
                                self._on_ohlcv_update(sym, ohlcv)
                else:
                    sym = syms[0]
                    ohlcv = await asyncio.wait_for(ex.watch_ohlcv(sym, _KLINE_INTERVAL), timeout=_WS_WATCH_TIMEOUT_S)
                    self._touch()
                    bin_sym = self._ws_binance_id(ex, sym)
                    if isinstance(ohlcv, list) and bin_sym:
                        self._on_ohlcv_update(bin_sym, ohlcv)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if self._ws_transport_fatal(exc):
                    _subscribed = frozenset()  # WS gone; reset subscription tracking
                await self._on_ws_loop_error("kline_ws", exc)

    async def _watch_ohlcv_5m_mux(self) -> None:
        """Multiplexed 5m OHLCV — overlays REST ``5m_closed`` on confirm path."""
        if not self.kline_5m_enabled:
            return
        ex = self._ws_ex()
        _subscribed: frozenset[str] = frozenset()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            syms_set = frozenset(syms)
            removed = _subscribed - syms_set
            if removed:
                await self._unwatch_removed_ohlcv(ex, removed, _KLINE_5M_INTERVAL)
            _subscribed = syms_set
            try:
                if self._ws_has(ex, "watchOHLCVForSymbols"):
                    pairs = [(s, _KLINE_5M_INTERVAL) for s in syms]
                    result = await asyncio.wait_for(ex.watch_ohlcv_for_symbols(pairs), timeout=_WS_WATCH_TIMEOUT_S)
                    self._touch()
                    if isinstance(result, dict):
                        for ccxt_sym, tf_map in result.items():
                            sym = self._ws_binance_id(ex, str(ccxt_sym))
                            if not sym or not isinstance(tf_map, dict):
                                continue
                            ohlcv = tf_map.get(_KLINE_5M_INTERVAL)
                            if isinstance(ohlcv, list):
                                self._on_ohlcv_update(sym, ohlcv, interval=_KLINE_5M_INTERVAL)
                else:
                    sym = syms[0]
                    ohlcv = await asyncio.wait_for(ex.watch_ohlcv(sym, _KLINE_5M_INTERVAL), timeout=_WS_WATCH_TIMEOUT_S)
                    self._touch()
                    bin_sym = self._ws_binance_id(ex, sym)
                    if isinstance(ohlcv, list) and bin_sym:
                        self._on_ohlcv_update(bin_sym, ohlcv, interval=_KLINE_5M_INTERVAL)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if self._ws_transport_fatal(exc):
                    _subscribed = frozenset()
                await self._on_ws_loop_error("kline_ws_5m", exc)

    async def _watch_ohlcv_15m_mux(self) -> None:
        """Multiplexed 15m OHLCV — overlays REST ``15m_closed`` on confirm path."""
        if not self.kline_15m_enabled:
            return
        ex = self._ws_ex()
        _subscribed: frozenset[str] = frozenset()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            syms_set = frozenset(syms)
            removed = _subscribed - syms_set
            if removed:
                await self._unwatch_removed_ohlcv(ex, removed, _KLINE_15M_INTERVAL)
            _subscribed = syms_set
            try:
                if self._ws_has(ex, "watchOHLCVForSymbols"):
                    pairs = [(s, _KLINE_15M_INTERVAL) for s in syms]
                    result = await asyncio.wait_for(ex.watch_ohlcv_for_symbols(pairs), timeout=_WS_WATCH_TIMEOUT_S)
                    self._touch()
                    if isinstance(result, dict):
                        for ccxt_sym, tf_map in result.items():
                            sym = self._ws_binance_id(ex, str(ccxt_sym))
                            if not sym or not isinstance(tf_map, dict):
                                continue
                            ohlcv = tf_map.get(_KLINE_15M_INTERVAL)
                            if isinstance(ohlcv, list):
                                self._on_ohlcv_update(sym, ohlcv, interval=_KLINE_15M_INTERVAL)
                else:
                    sym = syms[0]
                    ohlcv = await asyncio.wait_for(ex.watch_ohlcv(sym, _KLINE_15M_INTERVAL), timeout=_WS_WATCH_TIMEOUT_S)
                    self._touch()
                    bin_sym = self._ws_binance_id(ex, sym)
                    if isinstance(ohlcv, list) and bin_sym:
                        self._on_ohlcv_update(bin_sym, ohlcv, interval=_KLINE_15M_INTERVAL)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if self._ws_transport_fatal(exc):
                    _subscribed = frozenset()
                await self._on_ws_loop_error("kline_ws_15m", exc)

    async def _watch_order_book_mux(self) -> None:
        """Live L2 order book via watch_order_book_for_symbols → depth imbalance + microprice."""
        from hunt_core.market.client import (
            depth_imbalance_from_book,
            depth_imbalance_from_levels,
            microprice_bias_from_book,
        )

        ex = self._ws_ex()
        _subscribed: frozenset[str] = frozenset()
        _active_stream_idx: str | None = None  # current live book URL stream index
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            syms_set = frozenset(syms)
            _subscribed = syms_set
            # Sort for stable streamHash: same symbol SET → same URL on every rotation.
            sorted_syms = sorted(syms_set)
            try:
                # limit=_TOP_BOOK_DEPTH_LEVELS keeps the REST seed snapshot at
                # weight 2 (vs 20 at the ccxt default 1000) — see factory option
                # watchOrderBookLimit. We never use deeper levels anyway.
                book = await asyncio.wait_for(ex.watch_order_book_for_symbols(sorted_syms, _TOP_BOOK_DEPTH_LEVELS), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                # Detect URL change: symbol set rotated → CCXT opened new /public/ws/N.
                # Close the previous stale connection so Binance doesn't 1006 it later.
                stream_hash = "multipleOrderbook::" + ",".join(sorted_syms)
                new_idx = (getattr(ex, "options", {}) or {}).get("streamBySubscriptionsHash", {}).get(stream_hash)
                if new_idx is not None and new_idx != _active_stream_idx:
                    self._close_stale_public_ws_client(ex, _active_stream_idx, label="book")
                    _active_stream_idx = new_idx
                if not isinstance(book, dict):
                    continue
                sym = self._ws_binance_id(ex, str(book.get("symbol") or ""))
                if not sym:
                    continue
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                bid_p = float(bids[0][0]) if bids else None
                ask_p = float(asks[0][0]) if asks else None
                bid_q = float(bids[0][1]) if bids else None
                ask_q = float(asks[0][1]) if asks else None
                di_l1 = depth_imbalance_from_book(bid_qty=bid_q, ask_qty=ask_q, delta_ratio=None)
                di_top20 = depth_imbalance_from_levels(bids, asks, top_n=_TOP_BOOK_DEPTH_LEVELS)
                mp = microprice_bias_from_book(bid=bid_p, ask=ask_p, bid_qty=bid_q, ask_qty=ask_q, delta_ratio=None)
                self._live_books[sym] = {
                    "bid": bid_p,
                    "ask": ask_p,
                    "bid_qty": bid_q,
                    "ask_qty": ask_q,
                    "bids": bids,
                    "asks": asks,
                    "depth_imbalance": di_l1,
                    "ws_depth_imbalance": di_top20,
                    "microprice_bias": mp,
                    # Stamped so readers can age-gate: a stalled depth stream must not
                    # serve its last book as a fresh executable price (see live_bbo).
                    "ts_ms": int(time.time() * 1000),
                }
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                # CCXT Pro order-book checksum mismatch (PORTAL etc.) — reconnect path.
                exc_name = exc.__class__.__name__
                if exc_name == "ChecksumError":
                    LOG.debug("hunt_ccxt_book_checksum_reset | error=%s", exc)
                    await self._on_ws_loop_error("book_checksum", exc)
                    continue
                # Binance closes the shared WS connection when it rejects exotic symbol
                # subscriptions (@miniTicker etc.).  Book rides the same connection and
                # gets a 1006; klines recover via CCXT Pro's internal retry, so the
                # exchange-wide reconnect only adds rate-limit pressure without benefit.
                # Watchdog (300 s no-data) catches genuine network outages.
                if self._ws_transport_fatal(exc):
                    _subscribed = frozenset()  # WS gone; reset subscription tracking
                    _active_stream_idx = None
                    LOG.debug("hunt_ccxt_book_ws_fail | %s", repr(exc)[:100])
                    await asyncio.sleep(3.0)
                else:
                    await self._on_ws_loop_error("book", exc)

    async def _watch_bids_asks_mux(self) -> None:
        """BBO spread via watch_bids_asks (lighter than full watch_tickers)."""
        ex = self._ws_ex()
        if not self._ws_has(ex, "watchBidsAsks"):
            return
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                items = await asyncio.wait_for(ex.watch_bids_asks(syms), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                batch = items.values() if isinstance(items, dict) else [items]
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    bid = float(item.get("bid") or 0)
                    ask = float(item.get("ask") or 0)
                    if not sym or bid <= 0 or ask <= 0:
                        continue
                    spread_pct = (ask - bid) / bid * 100.0 if bid > 0 else 0.0
                    self._live_bbo[sym] = {
                        "bid": bid,
                        "ask": ask,
                        "spread_pct": round(spread_pct, 5),
                    }
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                # BBO bookTicker subscription may fail (4004) for some symbols.
                # Do not reconnect exchange — kline/trades/mark are unaffected.
                if self._ws_transport_fatal(exc):
                    LOG.debug("hunt_ccxt_bbo_ws_fail | %s", repr(exc)[:100])
                    await asyncio.sleep(3.0)
                else:
                    await self._on_ws_loop_error("bbo", exc)

    async def _watch_tickers_mux(self) -> None:
        """Rolling 24h stats via REST poll every 60 s (gated through client REST gate).

        watch_tickers(syms) uses per-symbol <sym>@miniTicker subscriptions.  Equity-linked
        tokens (TSLAUSDT, MSFTUSDT, SPCXUSDT, CBRSUSDT) are rejected by Binance with
        close-code 4004 outside US market hours, creating a 3-second reconnect churn that
        destabilises kline/mark/book/trades streams sharing the same IP.  24h stats
        (volume, change%, high/low) need no sub-minute freshness — REST at 60 s is correct.
        """
        while not self._stop.is_set():
            syms = self._symbols
            if not syms:
                await asyncio.sleep(0.5)
                continue
            now_ms = int(time.time() * 1000)
            try:
                tickers = await self.client.fetch_ticker_24h()
                # Deliberately NO self._touch() here: this is a 60s REST poll, and
                # _touch() is the WS-PUSH liveness clock (_last_msg_ms). Touching it
                # from REST pinned ws_last_msg_age_s to ≤60s even when every WS push
                # stream was dead, masking a genuine push blackout from the
                # data-plane audit (and keeping `fresh`/ws_connected falsely alive).
                # WS pushes touch the clock at their own handlers; REST must not.
                for item in tickers:
                    if not isinstance(item, dict):
                        continue
                    sym = str(item.get("symbol") or "")
                    if not sym or sym not in syms:
                        continue
                    self._live_tickers[sym] = {
                        "last": float(item.get("last_price") or 0),
                        "quoteVolume": float(item.get("quote_volume") or 0),
                        "percentage": float(item.get("price_change_percent") or 0),
                        "high": float(item.get("high_price") or 0),
                        "low": float(item.get("low_price") or 0),
                        "ts_ms": now_ms,
                    }
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if is_ccxt_rate_limited(exc):
                    self.client.rest_gate.record_error(exc, context="tickers_rest")
                    LOG.warning("hunt_ccxt_tickers_rest_rate_limited | %s", repr(exc)[:120])
                    await asyncio.sleep(30.0)
                else:
                    LOG.warning("hunt_ccxt_tickers_rest_err | %s", repr(exc)[:120])
                    await asyncio.sleep(15.0)

    async def _watch_funding_rates_mux(self) -> None:
        """Live funding/mark/index prices via watch_funding_rates."""
        ex = self._ws_ex()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                rates = await asyncio.wait_for(ex.watch_funding_rates(syms), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                items = rates.values() if isinstance(rates, dict) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    if not sym:
                        continue
                    mark = float(item.get("markPrice") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    prev = dict(self._live_funding.get(sym) or {})
                    if mark > 0:
                        prev["markPrice"] = mark
                    if index > 0:
                        prev["indexPrice"] = index
                    if funding != 0:
                        prev["fundingRate"] = funding
                    if prev:
                        prev["ts_ms"] = int(time.time() * 1000)  # age-gate stamp
                        self._live_funding[sym] = prev
                    if mark > 0 and index > 0:
                        self.client.update_basis_from_websocket(sym, mark, index)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                if self._funding_ws_permanent(exc):
                    LOG.info(
                        "hunt_ccxt_funding_disabled | reason=not_supported error=%s",
                        exc,
                    )
                    return
                await self._on_ws_loop_error("funding", exc)

    @staticmethod
    def _funding_ws_permanent(exc: BaseException) -> bool:
        text = str(exc).lower()
        name = type(exc).__name__
        return name in {"NotSupported", "NotImplemented"} or "not supported" in text

    async def _reset_secondary_pro(self, name: str) -> None:
        ex = self._secondary_pro_clients.pop(name, None)
        if ex is not None:
            await close_exchange_async(ex, label=f"secondary_pro_reset:{name}")
        try:
            fresh = create_pro_secondary_swap(
                name,
                proxy_url=self.client._proxy_url,
                trust_env=self.client._trust_env,
                timeout_ms=self.client._timeout_ms,
            )
            await fresh.load_markets()
            self._secondary_pro_clients[name] = fresh
            LOG.info("secondary_funding_ws_reset | exchange=%s", name)
        except Exception as exc:
            LOG.warning("secondary_funding_ws_reset_failed | exchange=%s error=%s", name, exc)

    async def _watch_one_secondary_funding(self, name: str) -> None:
        """Continuous watch_funding_rates loop for one secondary exchange."""
        ex = self._secondary_pro_clients.get(name)
        if ex is None or not self._ws_has(ex, "watchFundingRates"):
            LOG.debug(
                "secondary_funding_ws_skipped | exchange=%s reason=watchFundingRates_unsupported",
                name,
            )
            return
        backoff_s = 5.0
        while not self._stop.is_set():
            if name in self._secondary_funding_disabled:
                return
            ex = self._secondary_pro_clients.get(name)
            if ex is None:
                return
            syms_bin = list(self._symbols)
            if not syms_bin:
                await asyncio.sleep(2.0)
                continue
            ccxt_syms = [
                resolved
                for s in syms_bin
                if (resolved := try_resolve_linear_usdt_swap(s, exchange=ex))
            ]
            if not ccxt_syms:
                await asyncio.sleep(2.0)
                continue
            try:
                rates = await asyncio.wait_for(ex.watch_funding_rates(ccxt_syms), timeout=_WS_WATCH_TIMEOUT_S)
                items = rates.values() if isinstance(rates, dict) else []
                bucket = self._live_funding_by_exchange.setdefault(name, {})
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    if not sym:
                        continue
                    mark = float(item.get("markPrice") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    bucket[sym] = {"markPrice": mark, "indexPrice": index, "fundingRate": funding}
                backoff_s = 5.0
            except asyncio.CancelledError:
                return
            except asyncio.TimeoutError:
                # No funding-rate push in 120 s — normal (OKX pushes only on change,
                # funding period is 8 h).  WS is alive; just wait for the next event.
                continue
            except Exception as exc:
                if self._funding_ws_permanent(exc):
                    self._secondary_funding_disabled.add(name)
                    LOG.warning(
                        "secondary_funding_ws_disabled | exchange=%s error=%s",
                        name,
                        exc,
                    )
                    return
                if self._ws_transport_fatal(exc):
                    LOG.info(
                        "secondary_funding_ws_error | exchange=%s error=%.200s",
                        name,
                        repr(exc),
                    )
                    await self._reset_secondary_pro(name)
                    backoff_s = 5.0
                    await asyncio.sleep(2.0)
                    continue
                LOG.warning("secondary_funding_ws_error | exchange=%s error=%s", name, exc)
                await asyncio.sleep(min(60.0, backoff_s))
                backoff_s = min(60.0, backoff_s * 1.5)

    async def _watch_secondary_funding_mux(self) -> None:
        """Spawn per-exchange WS funding tasks for secondary venues with CCXT support."""
        tasks: list[asyncio.Task[None]] = []
        for name in configured_secondary_exchanges():
            if not exchange_funding_ws_capable(name):
                LOG.info(
                    "secondary_funding_rest_plane | exchange=%s ws=skip rest_poll=ok",
                    name,
                )
                continue
            ex_id = name
            ex: Any | None = None
            try:
                await asyncio.sleep(1.5)
                ex = create_pro_secondary_swap(
                    ex_id,
                    proxy_url=self.client._proxy_url,
                    trust_env=self.client._trust_env,
                    timeout_ms=self.client._timeout_ms,
                )
                await ex.load_markets()
                usdt_swap = sum(
                    1
                    for m in ex.markets.values()
                    if isinstance(m, dict)
                    and str(m.get("settle") or "").upper() == "USDT"
                    and str(m.get("type") or "") in {"swap", "future"}
                )
                if usdt_swap <= 0:
                    LOG.warning("secondary_no_usdt_swap_markets | exchange=%s", name)
                    await self._dispose_secondary_pro_ex(ex, label=f"secondary_pro_skip:{name}")
                    ex = None
                    continue
                if not self._ws_has(ex, "watchFundingRates"):
                    LOG.debug(
                        "secondary_funding_ws_skipped | exchange=%s reason=watchFundingRates_unsupported",
                        name,
                    )
                    await self._dispose_secondary_pro_ex(ex, label=f"secondary_pro_skip:{name}")
                    ex = None
                    continue
                self._secondary_pro_clients[name] = ex
                ex = None
                task = asyncio.create_task(
                    self._watch_one_secondary_funding(name),
                    name=f"hunt_ccxt_funding_{name}",
                )
                _attach_task_guard(task)
                tasks.append(task)
                LOG.info("secondary_funding_ws_started | exchange=%s", name)
            except asyncio.CancelledError:
                await self._dispose_secondary_pro_ex(ex, label=f"secondary_pro_cancel:{name}")
                for t in tasks:
                    t.cancel()
                raise
            except Exception as exc:
                LOG.debug("secondary_funding_ws_init_failed | exchange=%s error=%s", name, exc)
                await self._dispose_secondary_pro_ex(ex, label=f"secondary_pro_init:{name}")
                ex = None
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def _watch_secondary_funding_rest_mux(self) -> None:
        """REST funding poll for venues without Pro ``watchFundingRates`` (Bybit/Bitget)."""
        venues = funding_rest_poll_venues()
        if not venues:
            return
        interval_s = max(30.0, float(os.getenv("HUNT_CROSS_FUNDING_REST_S", "60")))
        LOG.info(
            "secondary_funding_rest_started | exchanges=%s interval_s=%s",
            ",".join(venues),
            interval_s,
        )
        while not self._stop.is_set():
            syms_bin = list(self._symbols)
            if not syms_bin:
                await asyncio.sleep(2.0)
                continue
            for name in venues:
                bucket = self._live_funding_by_exchange.setdefault(name, {})
                for sym in syms_bin[:_MAX_SYMBOL_STREAMS]:
                    if self._stop.is_set():
                        return
                    try:
                        rate = await self.client.fetch_secondary_funding_rate(name, sym)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOG.debug(
                            "secondary_funding_rest_error | exchange=%s symbol=%s error=%s",
                            name,
                            sym,
                            exc,
                        )
                        continue
                    if rate is None:
                        continue
                    bucket[sym] = {
                        "fundingRate": float(rate),
                        "markPrice": 0.0,
                        "indexPrice": 0.0,
                    }
            await asyncio.sleep(interval_s)

    async def _watch_one_secondary_liquidations(self, name: str) -> None:
        """Real liquidation events from Bybit / OKX — same mode taxonomy as primary."""
        ex = self._secondary_pro_clients.get(name)
        if ex is None:
            return
        mode = liquidation_ws_mode(ex)
        if mode == "skip":
            LOG.info(
                "secondary_liq_ws_skipped | exchange=%s reason=no_watchLiquidations",
                name,
            )
            return
        sym_idx = 0
        while not self._stop.is_set():
            ex = self._secondary_pro_clients.get(name)
            if ex is None:
                return
            syms_bin = list(self._symbols)
            if not syms_bin:
                await asyncio.sleep(2.0)
                continue
            ccxt_syms = [
                resolved
                for s in syms_bin
                if (resolved := try_resolve_linear_usdt_swap(s, exchange=ex))
            ]
            if not ccxt_syms:
                await asyncio.sleep(2.0)
                continue
            try:
                if mode == "mux":
                    items = await asyncio.wait_for(ex.watch_liquidations_for_symbols(ccxt_syms), timeout=_WS_WATCH_TIMEOUT_S)
                else:
                    ccxt_sym = ccxt_syms[sym_idx % len(ccxt_syms)]
                    sym_idx += 1
                    items = await asyncio.wait_for(ex.watch_liquidations(ccxt_sym), timeout=_WS_WATCH_TIMEOUT_S)
                self._touch()
                batch = items if isinstance(items, list) else [items]
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    if sym and sym in self._symbols:
                        self._record_liquidation(item, exchange=ex, venue=name)
            except asyncio.CancelledError:
                return
            except asyncio.TimeoutError:
                # No liquidation event in 120 s — normal for low-volume symbols.
                # WS connection is alive; just continue to the next symbol/iteration.
                continue
            except defensive_exc_types(Exception) as exc:
                # Secondary (bybit/okx) WS failures must NOT trigger Binance reconnect.
                # Reset only the affected secondary exchange.
                if self._ws_transport_fatal(exc):
                    LOG.info("hunt_ccxt_secondary_liq_ws_fail | exchange=%s %s", name, repr(exc)[:200])
                    await self._reset_secondary_pro(name)
                    await asyncio.sleep(3.0)
                else:
                    LOG.warning("hunt_ccxt_secondary_liq_err | exchange=%s %s", name, repr(exc)[:100])

    async def _watch_secondary_liquidations_mux(self) -> None:
        """Spawn per-exchange liquidation WS for every configured secondary that supports
        it. Bitget included: if its CCXT Pro build lacks watchLiquidations,
        ``_watch_one_secondary_liquidations`` self-skips with a log — so listing it here
        is safe and captures real Bitget liquidations when the method is available."""
        liq_venues = ("bybit", "okx", "bitget")
        tasks: list[asyncio.Task[None]] = []
        for name in configured_secondary_exchanges():
            if name not in liq_venues:
                continue
            ex = self._secondary_pro_clients.get(name)
            if ex is None:
                try:
                    await asyncio.sleep(3.0)
                    ex = self._secondary_pro_clients.get(name)
                except asyncio.CancelledError:
                    raise
            created_local: Any | None = None
            if ex is None:
                try:
                    created_local = create_pro_secondary_swap(
                        name,
                        proxy_url=self.client._proxy_url,
                        trust_env=self.client._trust_env,
                        timeout_ms=self.client._timeout_ms,
                    )
                    await created_local.load_markets()
                    self._secondary_pro_clients[name] = created_local
                    created_local = None
                except asyncio.CancelledError:
                    await self._dispose_secondary_pro_ex(
                        created_local, label=f"secondary_liq_cancel:{name}"
                    )
                    for t in tasks:
                        t.cancel()
                    raise
                except Exception as exc:
                    LOG.warning("secondary_liq_ws_init_failed | exchange=%s error=%s", name, exc)
                    await self._dispose_secondary_pro_ex(
                        created_local, label=f"secondary_liq_init:{name}"
                    )
                    continue
            task = asyncio.create_task(
                self._watch_one_secondary_liquidations(name),
                name=f"hunt_ccxt_liq_{name}",
            )
            _attach_task_guard(task)
            tasks.append(task)
            LOG.info("secondary_liq_ws_started | exchange=%s", name)
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise
