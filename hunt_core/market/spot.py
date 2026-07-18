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
    spot_quote_volume_24h: float | None
    fetched_at: float
    # Spot taker aggression over a recent aggTrades window: net (buy−sell) notional and
    # buy share — a first-order read of which side is actively hitting the spot book
    # («крупняк на споте покупает на зонах» — Prizrak). Fetched ONLY on the deep/pinned
    # path (with_taker_flow); None on the universe tick, which must not pay a per-symbol
    # fetch_trades call. None = «нет данных», never a fabricated 0.0 (invariant I-6).
    spot_taker_delta_usd: float | None = None
    spot_taker_buy_ratio: float | None = None


# Weekly spot OHLCV cache TTL. A closed 1w candle changes once per week; 6h keeps
# the ladder fresh across a long-running process without re-charging the spot
# weight budget on every deep tick.
_WEEKLY_OHLCV_TTL_S = 6 * 3600.0


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
        # Weekly spot OHLCV (full-history HTF ladder): sym -> (bars, fetched_at).
        self._weekly_cache: dict[str, tuple[list[list[float]], float]] = {}
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
    def _taker_flow(trades: list[dict[str, Any]] | None) -> tuple[float | None, float | None]:
        """Spot taker buy/sell notional from public aggTrades → (net_delta_usd, buy_ratio).

        CCXT's ``trade['side']`` is the TAKER side: 'buy' = taker lifted the ask (aggressive
        buy), 'sell' = taker hit the bid. Net delta = buy − sell notional; buy_ratio = buy /
        (buy+sell). Returns ``(None, None)`` when no usable trade — an empty/garbage window is
        «нет данных», never a fabricated ``0.0`` that would read as «perfect balance» (I-6).
        """
        buy = 0.0
        sell = 0.0
        used = 0
        for t in trades or []:
            if not isinstance(t, dict):
                continue
            side = t.get("side")
            if side not in ("buy", "sell"):
                continue
            cost = t.get("cost")
            if cost is None:
                px, amt = t.get("price"), t.get("amount")
                if px is None or amt is None:
                    continue
                try:
                    cost = float(px) * float(amt)
                except (TypeError, ValueError):
                    continue
            try:
                cost_f = float(cost)
            except (TypeError, ValueError):
                continue
            if side == "buy":
                buy += cost_f
            else:
                sell += cost_f
            used += 1
        if used == 0:
            return None, None
        total = buy + sell
        ratio = buy / total if total > 0 else None
        return buy - sell, ratio

    @staticmethod
    def _lead_return_1m(ohlcv: list[list]) -> float | None:
        """Spot 1m lead/lag return, in percent.

        Deliberately reads the FORMING 1m bar (``ohlcv[-1]``): this is a live
        lead/lag probe — "is spot moving ahead of the perp right now" — and a
        closed-bar read would be up to a minute stale, which is the whole signal.
        It is therefore CONTEXT, not a signal input: it repaints within the
        minute and must never gate an emission or be recorded as a bar fact.
        (The weekly ladder in ``fetch_weekly_ohlcv`` is the opposite case and
        does drop its unclosed tail.)
        """
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
    def _spot_reference_price(ticker: dict[str, Any], last: float) -> float:
        """Spot MID when the book is quoted, else last trade.

        The basis is a spot-vs-perp comparison, so both legs must be the same
        price type: comparing a spot LAST TRADE against a futures MID prices in
        half the spot spread. On an illiquid spot market that noise is larger
        than the basis itself, so «перп дороже/дешевле спота» flips sign on which
        side the last print landed. fetch_ticker already carries bid/ask — no
        extra weight.
        """
        try:
            bid = float(ticker.get("bid") or 0.0)
            ask = float(ticker.get("ask") or 0.0)
        except (TypeError, ValueError):
            return last
        if bid > 0.0 and ask > 0.0 and ask >= bid:
            return (bid + ask) / 2.0
        return last

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
        with_taker_flow: bool = False,
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
            # Basis leg: mid-vs-mid (see _spot_reference_price). spot_price stays
            # the last trade — it is the reported spot print, not a basis input.
            spot_ref = self._spot_reference_price(ticker, spot_price)
            # 24h spot quote volume (USDT) — spot-vs-perp volume divergence context:
            # a perp pump with no spot participation is a derivatives-only move.
            # 0.0 is valid data (dead spot market), so only absence maps to None.
            _qv = ticker.get("quoteVolume")
            try:
                spot_qv = float(_qv) if _qv is not None else None
            except (TypeError, ValueError):
                spot_qv = None
            ohlcv = await self._spot_fetch(
                lambda: self._ex.fetch_ohlcv(ccxt_sym, "1m", limit=2),
                context=f"spot_ohlcv:{sym}",
                weight=1,
                method="fetchOHLCV",
            )
            lead = self._lead_return_1m(ohlcv)
            spread = self._spread_bps(spot_ref, futures_mid)
            # Taker flow — deep/pinned path only. A failure here must degrade to «нет
            # данных» for THIS field, not null the whole metrics object (spread/volume are
            # independently useful), so it has its own try; rate-limit still propagates.
            taker_delta: float | None = None
            taker_ratio: float | None = None
            if with_taker_flow:
                try:
                    trades = await self._spot_fetch(
                        lambda: self._ex.fetch_trades(ccxt_sym, limit=1000),
                        context=f"spot_trades:{sym}",
                        weight=4,
                        method="fetchTrades",
                    )
                    taker_delta, taker_ratio = self._taker_flow(trades)
                except ccxt.BaseError as exc:
                    if is_ccxt_rate_limited(exc):
                        raise
                    LOG.debug("spot_taker_flow_failed", symbol=sym, error=str(exc))
                except DEFENSIVE_EXC as exc:
                    LOG.debug("spot_taker_flow_failed", symbol=sym, error=str(exc))
            return SpotMetrics(
                symbol=sym,
                spot_price=spot_price,
                spot_lead_return_1m=lead,
                spot_futures_spread_bps=spread,
                spot_quote_volume_24h=spot_qv,
                fetched_at=time.monotonic(),
                spot_taker_delta_usd=taker_delta,
                spot_taker_buy_ratio=taker_ratio,
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
        with_taker_flow: bool = False,
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
                    with_taker_flow=with_taker_flow,
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
        if metrics.spot_quote_volume_24h is not None:
            payload["spot_quote_volume_24h"] = metrics.spot_quote_volume_24h
        if metrics.spot_taker_delta_usd is not None:
            payload["spot_taker_delta_usd"] = metrics.spot_taker_delta_usd
        if metrics.spot_taker_buy_ratio is not None:
            payload["spot_taker_buy_ratio"] = metrics.spot_taker_buy_ratio
        return payload

    async def fetch_weekly_ohlcv(
        self,
        symbol: str,
        *,
        limit: int = 520,
        max_age_seconds: float = _WEEKLY_OHLCV_TTL_S,
    ) -> list[list[float]] | None:
        """Full-history weekly spot OHLCV for the HTF ladder (cached, lazy).

        Prizrak draws macro zones on the *spot weekly chart with full history*
        (prizrak_pol_matic разбор: недельный MATICUSD спот → macro-зоны); the
        futures window is too short for those levels. ``limit=520`` covers ~10
        years — the whole listed life of any Binance spot market in one call
        (Binance spot klines weight 2). Returns None on any failure; cached per
        symbol for ``max_age_seconds``.
        """
        sym = to_binance_symbol(symbol)
        if not sym:
            return None
        cached = self._weekly_cache.get(sym)
        if cached is not None and time.monotonic() - cached[1] <= max_age_seconds:
            return cached[0]
        try:
            await self._ensure_markets()
            ccxt_sym = to_ccxt_symbol(sym, exchange=self._ex)
            ohlcv = await self._spot_fetch(
                lambda: self._ex.fetch_ohlcv(ccxt_sym, "1w", limit=limit),
                context=f"spot_ohlcv_1w:{sym}",
                weight=2,
                method="fetchOHLCV",
            )
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.debug("spot_weekly_fetch_failed", symbol=sym, error=str(exc))
            return None
        except DEFENSIVE_EXC as exc:
            LOG.debug("spot_weekly_fetch_failed", symbol=sym, error=str(exc))
            return None
        if not ohlcv:
            return None
        # No-lookahead: drop the in-progress week so ladder pivots never form on
        # (or get confirmed by) an unclosed candle and repaint intra-week.
        from hunt_core.market.factory import drop_unclosed_ohlcv_tail

        ohlcv = drop_unclosed_ohlcv_tail(list(ohlcv), "1w", exchange=self._ex)
        bars = [list(map(float, r[:6])) for r in ohlcv if r and len(r) >= 5]
        if not bars:
            return None
        async with self._lock:
            self._weekly_cache[sym] = (bars, time.monotonic())
        return bars

    def cache_size(self) -> int:
        return len(self._cache)
