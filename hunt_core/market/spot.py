"""Spot companion for hunt — CCXT binance spot (public)."""
from __future__ import annotations



import asyncio
import time
from dataclasses import dataclass
from typing import Any

import ccxt.async_support as ccxt
import structlog

from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.market.ccxt_guard import ccxt_method_available, is_ccxt_rate_limited
from hunt_core.market.ccxt_rest import create_spot_rest_gate
from hunt_core.market.factory import close_exchange_async, create_async_binance_spot
from hunt_core.market.symbols import to_binance_symbol, to_ccxt_symbol

LOG = structlog.get_logger("hunt_core.market.spot")


@dataclass(frozen=True, slots=True)
class SpotMetrics:
    symbol: str
    spot_price: float
    spot_lead_return_1m: float | None
    spot_futures_spread_bps: float | None
    fetched_at: float


class HuntCcxtSpotCompanion:
    """Drop-in for ``SpotCompanionService`` — spot lead-lag vs futures mid."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        trust_env: bool = True,
        timeout_ms: int = 12_000,
    ) -> None:
        self._ex: ccxt.binance = create_async_binance_spot(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
        )
        # Own spot budgets — spot's 6000/min weight counter is separate from
        # fapi's 2400/min; never charge or floor the futures budget (F2).
        self._rest_gate = create_spot_rest_gate()
        self._cache: dict[str, SpotMetrics] = {}
        self._lock = asyncio.Lock()
        self._markets_loaded = False

    async def close(self) -> None:
        await close_exchange_async(self._ex, label="binance_spot")

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await self._ex.load_markets()
            self._markets_loaded = True

    async def _spot_fetch(
        self,
        factory: Any,
        *,
        context: str,
        weight: int,
        method: str,
    ) -> Any:
        if not ccxt_method_available(self._ex, method):
            raise ccxt.NotSupported(f"{method} unavailable on {self._ex.id}")
        await self._rest_gate.acquire_binance_weight(weight=weight, label=context)
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self._rest_gate.record_error(exc, context=context)
            raise
        self._rest_gate.sync_weight_from_exchange(self._ex)
        return result

    @staticmethod
    def _lead_return_1m(ohlcv: list[list]) -> float | None:
        if len(ohlcv) < 2:
            return None
        try:
            prev_close = float(ohlcv[-2][4])
            last_close = float(ohlcv[-1][4])
        except (IndexError, TypeError, ValueError):
            return None
        if prev_close <= 0.0:
            return None
        return (last_close - prev_close) / prev_close * 100.0

    @staticmethod
    def _spread_bps(spot_price: float, futures_mid: float | None) -> float | None:
        if futures_mid is None or spot_price <= 0.0 or futures_mid <= 0.0:
            return None
        return (futures_mid - spot_price) / spot_price * 10_000.0

    async def fetch_symbol_metrics(
        self,
        symbol: str,
        *,
        futures_mid: float | None = None,
    ) -> SpotMetrics | None:
        sym = to_binance_symbol(symbol)
        if not sym:
            return None
        try:
            await self._ensure_markets()
            ccxt_sym = to_ccxt_symbol(sym, exchange=self._ex)
            ticker = await self._spot_fetch(
                lambda: self._ex.fetch_ticker(ccxt_sym),
                context=f"spot_ticker:{sym}",
                weight=1,
                method="fetchTicker",
            )
            spot_price = float(ticker.get("last") or 0.0)
            if spot_price <= 0.0:
                return None
            ohlcv = await self._spot_fetch(
                lambda: self._ex.fetch_ohlcv(ccxt_sym, "1m", limit=2),
                context=f"spot_ohlcv:{sym}",
                weight=1,
                method="fetchOHLCV",
            )
            lead = self._lead_return_1m(ohlcv)
            spread = self._spread_bps(spot_price, futures_mid)
            return SpotMetrics(
                symbol=sym,
                spot_price=spot_price,
                spot_lead_return_1m=lead,
                spot_futures_spread_bps=spread,
                fetched_at=time.monotonic(),
            )
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.debug("spot_fetch_failed", symbol=sym, error=str(exc))
            return None
        except DEFENSIVE_EXC as exc:
            LOG.debug("spot_fetch_failed", symbol=sym, error=str(exc))
            return None
        except Exception as exc:
            LOG.debug("spot_fetch_failed", symbol=sym, error=str(exc))
            return None

    async def refresh_symbols(
        self,
        symbols: list[str],
        *,
        futures_mid_by_symbol: dict[str, float | None] | None = None,
        concurrency: int = 6,
    ) -> int:
        mids = futures_mid_by_symbol or {}
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        updated = 0

        async def _one(symbol: str) -> None:
            nonlocal updated
            async with sem:
                metrics = await self.fetch_symbol_metrics(
                    symbol,
                    futures_mid=mids.get(to_binance_symbol(symbol)),
                )
                if metrics is None:
                    return
                async with self._lock:
                    self._cache[metrics.symbol] = metrics
                updated += 1

        await asyncio.gather(
            *[_one(sym) for sym in symbols if str(sym).strip()],
            return_exceptions=True,
        )
        return updated

    def enrichments_for(self, symbol: str, *, max_age_seconds: float = 120.0) -> dict[str, float]:
        sym = to_binance_symbol(symbol)
        metrics = self._cache.get(sym)
        if metrics is None:
            return {}
        if time.monotonic() - metrics.fetched_at > max_age_seconds:
            return {}
        payload: dict[str, float] = {}
        if metrics.spot_lead_return_1m is not None:
            payload["spot_lead_return_1m"] = metrics.spot_lead_return_1m
        if metrics.spot_futures_spread_bps is not None:
            payload["spot_futures_spread_bps"] = metrics.spot_futures_spread_bps
        return payload

    def cache_size(self) -> int:
        return len(self._cache)
