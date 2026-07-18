"""Spot sibling engine (ADR-0003 E6b) — ccxt.pro Binance spot, for spot-vs-perp enrichments.

Replaces ``HuntCcxtSpotCompanion`` with a push-state WS source: one universe ``watchTickers`` (last/
bid/ask/24h quote-volume) + per-symbol ``watchOHLCV('1m')`` (the live lead/lag probe) + ``watchTrades``
(spot taker aggression). The metrics are computed by the pure :mod:`hunt_core.engine.spot_metrics`
(which reuses :func:`hunt_core.engine.orderflow.taker_flow`). The full-history weekly ladder stays a
lazy cached REST fetch (a 1W candle changes once a week).

Spot is a separate venue with its OWN weight budget — this client never touches the fapi throttler.
Fail-loud: a stale/absent ticker plane yields an empty enrichment dict (нет данных), never a
fabricated value; ``None`` fields are omitted (matching the old ``enrichments_for``).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence

import ccxt
import structlog

from hunt_core.engine import exchanges, params, rest
from hunt_core.engine.freshness import Bar
from hunt_core.engine.ingest import backoff_delay_s
from hunt_core.engine.spot_metrics import (
    lead_return_pct,
    quote_volume_24h,
    spot_reference_price,
    spot_taker_flow,
    spread_bps,
)
from hunt_core.engine.state import PlaneStamp, Source, SymbolState

LOG = structlog.get_logger(__name__)

_WEEKLY_TTL_S = 6 * 3600.0  # a closed 1W candle changes once a week (old companion value)


def _now_ms() -> int:
    return int(time.time() * 1000)


class SpotEngine:
    """Push-state spot data source for spot-vs-perp enrichment (public, own budget)."""

    def __init__(self, symbols: Sequence[str]) -> None:
        self._symbols = list(symbols)
        self._ex = exchanges.make_binance_spot()
        self._states: dict[str, SymbolState] = {}
        self._weekly: dict[str, tuple[list[Bar], float]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def _state(self, symbol: str) -> SymbolState:
        return self._states.setdefault(symbol, SymbolState(symbol))

    async def start(self) -> None:
        await self._ex.load_markets()
        for symbol in self._symbols:
            self._state(symbol)
            self._spawn(f"{symbol}:ohlcv.1m", self._step_ohlcv(symbol))
            self._spawn(f"{symbol}:trades", self._step_trades(symbol))
        if self._ex.has.get("watchTickers"):
            self._spawn("*:tickers", self._step_tickers(self._symbols))
        LOG.info("spot_engine_started", symbols=len(self._symbols))

    def _spawn(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._loop(key, step), name=f"spot_ws:{key}")
        task.add_done_callback(self._tasks.remove)
        self._tasks.append(task)

    async def _loop(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        # Same typed-error discipline as the futures Ingest: ChecksumError re-loops, DDoS/RateLimit
        # long-backs-off (a short retry extends the ban), NetworkError jittered, ExchangeError doesn't
        # retry-storm. Lighter than the futures path (no watchdog): spot is a dop-factor, and ccxt.pro
        # re-subscribes on the next watch_* call so a dropped socket self-heals via this loop.
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except ccxt.ChecksumError:
                continue
            except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as exc:
                LOG.error("spot_ws_rate_limited", stream=key, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except ccxt.NetworkError as exc:
                attempt += 1
                await asyncio.sleep(backoff_delay_s(attempt))
                LOG.warning("spot_ws_reconnect", stream=key, attempt=attempt, err=str(exc))
            except ccxt.ExchangeError as exc:
                LOG.error("spot_ws_exchange_error", stream=key, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 — unknown → transient
                attempt += 1
                await asyncio.sleep(backoff_delay_s(attempt))
                LOG.warning("spot_ws_unknown_error", stream=key, attempt=attempt, err=str(exc))

    def _step_tickers(self, symbols: list[str]) -> Callable[[], Awaitable[None]]:
        wanted = set(symbols)
        bound = int(params.FRESH_TICKER_S * 1000.0)

        async def step() -> None:
            tickers = await self._ex.watch_tickers()
            now = _now_ms()
            for sym, tk in tickers.items():
                if sym in wanted:
                    self._state(sym).put_value(
                        "ticker", tk, PlaneStamp(Source.WS, now, int(tk.get("timestamp") or now), bound)
                    )

        return step

    def _step_ohlcv(self, symbol: str) -> Callable[[], Awaitable[None]]:
        # Lead/lag probe reads the FORMING 1m bar deliberately (spot_metrics.lead_return_pct), so the
        # frame is stored forming-INCLUSIVE (the one place the engine keeps the live tail), bounded to
        # the 1m cadence. Warm-up note: the WS cache starts empty and accumulates, so lead-return is
        # fail-loud None for the first ~1 min after start (needs 2 bars) — acceptable, it's repainting
        # context, never a signal gate — then it stays populated (the cache only grows).
        bound = int(params.fresh_kline_s(60.0) * 1000.0)

        async def step() -> None:
            await self._ex.watch_ohlcv(symbol, "1m")
            cache = ((getattr(self._ex, "ohlcvs", {}) or {}).get(symbol) or {}).get("1m") or []
            if not cache:
                return
            frame = [[float(x) for x in bar] for bar in cache]
            self._state(symbol).seed_frame(
                "spot_1m", frame, PlaneStamp(Source.WS, _now_ms(), int(frame[-1][0]), bound)
            )

        return step

    def _step_trades(self, symbol: str) -> Callable[[], Awaitable[None]]:
        bound = int(params.NO_MESSAGE_WATCHDOG_S * 1000.0)

        async def step() -> None:
            await self._ex.watch_trades(symbol)  # drives ex.trades[symbol]; read-through at query
            self._state(symbol).stamp_only("trades", PlaneStamp(Source.WS, _now_ms(), _now_ms(), bound))

        return step

    # --- consumer surface ---

    def spot_enrichments(self, symbol: str, *, futures_mid: float | None = None) -> dict[str, float]:
        """Spot-vs-perp enrichment dict for ``symbol`` (empty when the ticker plane isn't fresh).

        Mirrors the old ``enrichments_for``: omits any ``None`` field. Taker flow is always included
        when spot trades are streaming (WS makes it free — the old REST path gated it behind a flag).
        """
        st = self._states.get(symbol)
        if st is None:
            return {}
        now = _now_ms()
        stamp = st.stamp_of("ticker")
        if stamp is None or stamp.stale_by(now) is not None:
            return {}  # нет данных — never a fabricated field
        ticker = st.value_of("ticker")
        if not isinstance(ticker, dict):
            return {}
        try:
            spot_price = float(ticker.get("last") or 0.0)
        except (TypeError, ValueError):
            return {}
        if spot_price <= 0.0:
            return {}
        out: dict[str, float] = {}
        spread = spread_bps(spot_reference_price(ticker, spot_price), futures_mid)
        if spread is not None:
            out["spot_futures_spread_bps"] = spread
        qv = quote_volume_24h(ticker)
        if qv is not None:
            out["spot_quote_volume_24h"] = qv
        lead = lead_return_pct(st.frame_of("spot_1m"))
        if lead is not None:
            out["spot_lead_return_1m"] = lead
        tr_stamp = st.stamp_of("trades")
        if tr_stamp is not None and tr_stamp.stale_by(now) is None:
            trades = (getattr(self._ex, "trades", {}) or {}).get(symbol)
            delta, ratio = spot_taker_flow(list(trades) if trades else None)
            if delta is not None:
                out["spot_taker_delta_usd"] = delta
            if ratio is not None:
                out["spot_taker_buy_ratio"] = ratio
        return out

    async def weekly_ohlcv(self, symbol: str, *, limit: int = 520) -> list[Bar] | None:
        """Full-history weekly spot OHLCV for the macro ladder (lazy, cached, closed-only).

        ``limit=520`` ≈ 10 yr (the whole listed life of any Binance spot market) in one call; the
        forming week is dropped (I-5). Cached per symbol for 6h. ``None`` fail-loud on failure.
        """
        cached = self._weekly.get(symbol)
        if cached is not None and time.monotonic() - cached[1] <= _WEEKLY_TTL_S:
            return cached[0]
        bars = await rest.seed_ohlcv(self._ex, symbol, "1w", limit=limit)
        if not bars:
            return None
        self._weekly[symbol] = (bars, time.monotonic())
        return bars

    async def close(self) -> None:
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._ex.close()


__all__ = ["SpotEngine"]
