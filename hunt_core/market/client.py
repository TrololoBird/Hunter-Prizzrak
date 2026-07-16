"""Hunter REST market client — CCXT ``binance`` + defaultType future (public only)."""
from __future__ import annotations



import asyncio
import structlog
import math
import time
from datetime import timedelta
from typing import Any

import ccxt.async_support as ccxt
import polars as pl

from hunt_core import clock
from hunt_core.errors import finite_float_or_none
from hunt_core.domain.schemas import AggTradeSnapshot, SymbolMeta
from hunt_core.market.ccxt_guard import ccxt_method_available, is_ccxt_rate_limited
from hunt_core.market.ccxt_rest import HuntCcxtRestGate
from hunt_core.market.weight_registry import depth_weight, klines_weight, weight_for_context
from hunt_core.market.factory import (
    close_exchange_async,
    create_async_binance_future,
    create_async_secondary_swap,
    create_pro_binance_future,
)
from hunt_core.market.network import mask_proxy_url
from hunt_core.market.tick_registry import register_ticks_from_markets
from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
from hunt_core.market.symbols import (
    is_linear_usdt_swap_market,
    underlying_type_of,
    resolve_linear_usdt_swap,
    to_binance_symbol,
    to_ccxt_symbol,
    try_binance_id_from_ccxt,
)

LOG = structlog.get_logger("hunt_core.market.client")
_CACHE_TTL: dict[str, int] = {
    "klines_1m": 25,
    "klines_3m": 120,
    "klines_5m": 45,
    "klines_15m": 900,
    "klines_1h": 3900,
    "klines_4h": 14400,
    "klines_1d": 3600,
    "open_interest": 600,
    "open_interest_change": 600,
    "metric_series": 240,
    "long_short_ratio": 600,
    "funding_rate": 300,
    "funding_history": 1800,
    "funding_info": 3600,
    # basis (futures premium) is slow-moving ENRICHMENT — it is NOT read by the
    # manipulation detector at all. Yet its fapiDataGetBasis REST calls are the
    # dominant rate-limit sink: a burst of ~universe basis refetches trips Binance
    # 429/418 (2026-07-11: a basis 429 extended the GLOBAL rest-gate pause, which
    # starved the REST-only 4h-kline refresh → 93% klines.4h.stale → universe
    # blackout). Widen the TTL 4× so basis refetches far less often (and its OHLCV
    # fallback covers the gaps), cutting the 429 pressure that halts the critical
    # klines plane. Override: cache is per-process, so this just paces REST.
    "basis": 7200,
    "book_ticker": 5,
    "order_book_depth": 5,
    "ticker_24h": 15,
    "exchange_info": 3600,
    "taker_ratio": 1200,
    "leverage_tiers": 3600,
    "secondary_funding": 600,
    "secondary_oi": 600,
    "agg_trades": 10,
}
# A blank secondary result (venue timeout / NotSupported) is cached only briefly —
# see HuntCcxtClient._secondary_ttl. Caching "no data" for the full 600s turned one
# transient error into a 10-minute venue blackout on the cross card.
_SECONDARY_NEGATIVE_CACHE_TTL_S = 20.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


class HuntCcxtClient:
    """CCXT REST + lazy Pro client for hunt runtime and offline scripts."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        trust_env: bool = True,
        timeout_ms: int = 45_000,
    ) -> None:
        self._proxy_url = proxy_url
        self._trust_env = trust_env
        self._timeout_ms = timeout_ms
        self._ex: ccxt.binance = create_async_binance_future(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
        )
        self._pro_ex: Any | None = None
        self._pro_lock = asyncio.Lock()
        self._markets_loaded = False
        self._klines_cache: dict[tuple[str, str, int], tuple[float, pl.DataFrame]] = {}
        self._klines_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._ohlcv_list_cache: dict[tuple[str, str, int], tuple[float, list[list[Any]]]] = {}
        self._ohlcv_list_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._ticker_24h_cache: tuple[float, list[dict[str, float | str]]] | None = None
        self._exchange_info_cache: tuple[float, list[SymbolMeta]] | None = None
        self._open_interest_cache: dict[str, tuple[float, float]] = {}
        self._open_interest_change_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._long_short_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._top_position_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._global_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._taker_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._agg_trade_cache: dict[tuple[str, int], tuple[float, AggTradeSnapshot]] = {}
        self._funding_rate_cache: dict[str, tuple[float, float]] = {}
        self._funding_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._funding_info_all_cache: tuple[float, dict[str, dict[str, float | int]]] | None = None
        self._premium_index_all_cache: tuple[float, dict[str, dict[str, float]]] | None = None
        self._basis_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._basis_stats_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._basis_api_unsupported: set[str] = set()
        self._oi_series_cache: dict[tuple[str, str, int], tuple[float, Any]] = {}
        self._gls_series_cache: dict[tuple[str, str, int], tuple[float, list[float]]] = {}
        self._order_book_depth_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._leverage_tiers_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._leverage_tiers_skip_logged = False
        self._secondary_funding_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._secondary_oi_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._secondary_clients: dict[str, ccxt.Exchange] = {}
        self._secondary_failed: set[str] = set()
        self._secondary_lock = asyncio.Lock()
        from hunt_core.market.cross import configured_secondary_exchanges
        # HUNT_CROSS_EXCHANGES override; ids reused verbatim as ccxt exchange ids.
        self._secondary_exchange_ids: dict[str, str] = {
            name: name for name in configured_secondary_exchanges()
        }
        self._rest_gate = HuntCcxtRestGate()
        self._streams_reconnect: Any | None = None

    def set_streams_reconnect(self, callback: Any | None) -> None:
        """Store the streams' own reconnect callback (used by the WS backoff path)."""
        self._streams_reconnect = callback

    @property
    def rest_gate(self) -> HuntCcxtRestGate:
        return self._rest_gate

    @property
    def ccxt_guard(self) -> Any:
        return self._rest_gate.guard

    async def await_rate_limit_pause(self, *, cap_s: float = 120.0) -> float:
        return await self._rest_gate.await_pause(cap_s=cap_s)

    async def _rest_call(
        self,
        factory: Any,
        *,
        context: str,
        weight: int = 5,
    ) -> Any:
        return await self._rest_gate.invoke(
            self._ex,
            factory,
            context=context,
            weight=weight,
        )

    async def _direct_binance_fetch(
        self,
        factory: Any,
        *,
        context: str,
        weight: int = 5,
        method: str | None = None,
    ) -> Any:
        """Gated direct ``_ex.fetch_*`` with CCXT error recording."""
        if method and not self._ccxt_has(self._ex, method):
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

    async def _fapi_call(
        self,
        factory: Any,
        *,
        context: str,
    ) -> Any:
        return await self._rest_gate.invoke_fapi(
            self._ex,
            factory,
            context=context,
        )


    @classmethod
    def from_settings(cls, settings: Any) -> HuntCcxtClient:
        net = getattr(settings, "network", settings)
        return cls(
            proxy_url=getattr(net, "proxy_url", None),
            trust_env=getattr(net, "trust_env", True),
        )

    @property
    def exchange(self) -> ccxt.binance:
        return self._ex

    def used_weight_1m(self) -> int | None:
        """Latest Binance ``X-MBX-USED-WEIGHT-1M`` header value (per-IP weight used
        in the trailing minute). Binance reports the cumulative per-IP weight on
        every fapi response, so the REST instance's last header reflects the whole
        host budget (incl. ccxt.pro order-book snapshots). Hard cap is 2400/min.
        """
        try:
            hdrs = getattr(self._ex, "last_response_headers", None) or {}
        except AttributeError:
            return None
        raw = hdrs.get("x-mbx-used-weight-1m") or hdrs.get("X-MBX-USED-WEIGHT-1M")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _share_markets_to(self, target: ccxt.binance) -> None:
        """Reuse bootstrapped REST markets on Pro/secondary CCXT instances."""
        if not self._markets_loaded or not self._ex.markets:
            return
        target.set_markets(list(self._ex.markets.values()))

    async def acquire_pro_exchange(self) -> Any:
        """Lazy CCXT Pro ``binance`` (future) — shared with ``HuntCcxtStreams``."""
        async with self._pro_lock:
            if self._pro_ex is None:
                self._pro_ex = create_pro_binance_future(
                    proxy_url=self._proxy_url,
                    trust_env=self._trust_env,
                    timeout_ms=self._timeout_ms,
                )
                if self._markets_loaded:
                    self._share_markets_to(self._pro_ex)
                else:
                    await self._pro_ex.load_markets()
            return self._pro_ex

    async def reset_pro_exchange(self) -> Any:
        """Close and recreate Pro client after WS transport failure (CCXT wiki pattern)."""
        async with self._pro_lock:
            if self._pro_ex is not None:
                await close_exchange_async(self._pro_ex, label="binance_pro_reset")
                self._pro_ex = None
            self._pro_ex = create_pro_binance_future(
                proxy_url=self._proxy_url,
                trust_env=self._trust_env,
                timeout_ms=self._timeout_ms,
            )
            if self._markets_loaded:
                self._share_markets_to(self._pro_ex)
            else:
                await self._pro_ex.load_markets()
            return self._pro_ex

    async def _sync_time_offset(self) -> None:
        """Anchor the wall clock to Binance server time (local OS clock may drift).

        A constant offset is enough; failure is non-fatal (offset stays 0 = local
        clock). See hunt_core.clock for why cross-clock paths need this.
        """
        try:
            server_ms = float(await self._ex.fetch_time())
        except Exception as exc:  # noqa: BLE001 — never let a time probe block startup
            LOG.warning("hunt_clock_sync_failed | err=%s", type(exc).__name__)
            return
        local_ms = time.time() * 1000.0
        offset = server_ms - local_ms
        clock.set_offset_ms(offset)
        if abs(offset) >= 60_000.0:
            LOG.warning("hunt_clock_skew | offset_h=%.2f (local OS clock off)", offset / 3_600_000.0)
        else:
            LOG.info("hunt_clock_synced | offset_ms=%.0f", offset)

    async def load_markets(self) -> None:
        if self._markets_loaded:
            return
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                await self._ex.load_markets()
                self._markets_loaded = True
                self._register_tick_sizes()
                await self._sync_time_offset()
                return
            except ccxt.BaseError as exc:
                last_exc = exc
                self._rest_gate.record_error(exc, context="load_markets")
                if is_ccxt_rate_limited(exc):
                    await self._rest_gate.await_pause(cap_s=60.0)
                elif attempt < 2:
                    await asyncio.sleep(2**attempt)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        LOG.warning(
            "ccxt_load_markets_failed | proxy=%s err=%s — ccxt implicit bootstrap",
            self._proxy_url or "direct",
            type(last_exc).__name__ if last_exc else "unknown",
        )
        await self._bootstrap_markets_via_ccxt_implicit()
        self._markets_loaded = True
        self._register_tick_sizes()
        await self._sync_time_offset()

    def _register_tick_sizes(self) -> None:
        """Feed per-symbol price ticks to the global tick registry (public metadata)."""
        try:
            register_ticks_from_markets((self._ex.markets or {}).values())
        except Exception:
            LOG.warning("tick_registry_populate_failed", exc_info=True)

    async def _bootstrap_markets_via_ccxt_implicit(self) -> None:
        """CCXT implicit ``fapiPublicGetExchangeInfo`` when ``load_markets`` fails."""

        fetcher = getattr(self._ex, "fapipublicGetExchangeinfo", None)
        if not callable(fetcher):
            fetcher = getattr(self._ex, "fapiPublicGetExchangeInfo", None)
        if not callable(fetcher):
            raise ccxt.NotSupported("fapiPublicGetExchangeInfo unavailable")
        payload = await self._rest_call(
            fetcher,
            context="bootstrap_exchange_info",
            weight=10,
        )
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list) or not symbols:
            raise RuntimeError("ccxt_exchange_info_empty")
        markets = self._ex.parse_markets(symbols)
        self._ex.set_markets(markets)
        LOG.info(
            "hunt_markets_bootstrapped | via=ccxt_implicit n=%d proxy=%s",
            len(markets),
            mask_proxy_url(self._proxy_url) if self._proxy_url else "direct",
        )

    async def close(self) -> None:
        for name, ex in list(self._secondary_clients.items()):
            await close_exchange_async(ex, label=f"secondary_rest:{name}")
        self._secondary_clients.clear()
        self._secondary_failed.clear()
        if self._pro_ex is not None:
            await close_exchange_async(self._pro_ex, label="binance_pro")
            self._pro_ex = None
        await close_exchange_async(self._ex, label="binance_rest")
        await asyncio.sleep(0.35)

    def _ccxt_sym(self, symbol: str) -> str:
        return to_ccxt_symbol(symbol, exchange=self._ex)

    def _bin_sym(self, symbol: str) -> str:
        return to_binance_symbol(symbol)

    @staticmethod
    def _cache_fresh(entry: tuple[float, Any] | None, ttl: float) -> bool:
        return entry is not None and (time.monotonic() - entry[0]) < ttl

    @staticmethod
    def _cache_age_s(entry: tuple[float, Any] | None) -> float | None:
        if entry is None:
            return None
        return max(0.0, time.monotonic() - float(entry[0]))

    def snapshot_rest_cache_ages(self, symbol: str) -> dict[str, float]:
        """Monotonic ages (seconds) for REST enrichment caches — P0 data-plane audit."""
        sym = self._bin_sym(symbol)
        ages: dict[str, float] = {}

        def _put(key: str, entry: tuple[float, Any] | None) -> None:
            age = self._cache_age_s(entry)
            if age is not None:
                ages[key] = round(age, 2)

        _put("oi", self._open_interest_cache.get(sym))
        _put("oi_chg_5m", self._open_interest_change_cache.get((sym, "5m")))
        _put("oi_chg_1h", self._open_interest_change_cache.get((sym, "1h")))
        _put("ls_5m", self._long_short_ratio_cache.get((sym, "5m")))
        _put("ls_1h", self._long_short_ratio_cache.get((sym, "1h")))
        _put("top_ls_5m", self._top_position_ls_ratio_cache.get((sym, "5m")))
        _put("top_ls_1h", self._top_position_ls_ratio_cache.get((sym, "1h")))
        _put("global_ls_5m", self._global_ls_ratio_cache.get((sym, "5m")))
        _put("global_ls_1h", self._global_ls_ratio_cache.get((sym, "1h")))
        _put("taker_5m", self._taker_ratio_cache.get((sym, "5m")))
        _put("taker_15m", self._taker_ratio_cache.get((sym, "15m")))
        _put("taker_1h", self._taker_ratio_cache.get((sym, "1h")))
        _put("funding", self._funding_rate_cache.get(sym))
        _put("book_depth", self._order_book_depth_cache.get((sym, 100)))
        _put("basis_5m", self._basis_cache.get((sym, "5m")))
        return ages

    @staticmethod
    def _ccxt_has(exchange: ccxt.Exchange, method: str) -> bool:
        return ccxt_method_available(exchange, method)

    def _fapi_market_id(self, symbol: str) -> str:
        market = self._ex.market(self._ccxt_sym(symbol))
        return str(market.get("id") or self._bin_sym(symbol))

    @staticmethod
    def _fapi_latest_ratio(payload: Any, *keys: str) -> float | None:
        if not payload:
            return None
        rows = payload if isinstance(payload, list) else [payload]
        if not rows:
            return None
        item = rows[-1] if isinstance(rows[-1], dict) else {}
        for key in keys:
            value = finite_float_or_none(item.get(key))
            if value is not None and value > 0:
                return value
        return None

    async def _fetch_fapi_metric(
        self,
        symbol: str,
        *,
        period: str,
        fetcher: Any,
        ratio_keys: tuple[str, ...],
        cache: dict[tuple[str, str], tuple[float, float]],
        ttl_key: str,
    ) -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL[ttl_key]):
            return cached[1]  # type: ignore[index]
        if not callable(fetcher):
            LOG.debug("fapi_metric_unavailable | symbol=%s period=%s", sym, period)
            return None
        try:
            await self.load_markets()
            payload = await self._fapi_call(
                lambda: fetcher(
                    {"symbol": self._fapi_market_id(sym), "period": period, "limit": 1}
                ),
                context=f"{ttl_key}:{sym}:{period}",
            )
            value = self._fapi_latest_ratio(payload, *ratio_keys)
            if value is not None and value > 0:
                cache[cache_key] = (now, value)
                return value
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning(
                "fapi_metric_failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
        except Exception as exc:
            LOG.warning(
                "fapi_metric_failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
        return None

    async def fetch_status(self) -> dict[str, Any] | None:
        """Exchange operational status (server time, maintenance windows, etc)."""
        try:
            return await self._direct_binance_fetch(
                lambda: self._ex.fetch_status(),
                context="exchange_status",
                weight=1,
                method="fetchStatus",
            )
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning("fetch_status failed | error=%s", exc)
        except Exception as exc:
            LOG.warning("fetch_status failed | error=%s", exc)
        return None

    async def fetch_bids_asks(
        self, symbols: list[str]
    ) -> dict[str, dict[str, float]]:
        """Best bid/ask for multiple symbols via REST (WS fallback / warmup)."""
        if not symbols:
            return {}
        syms = [self._ccxt_sym(s) for s in symbols]
        await self.load_markets()
        try:
            result = await self._direct_binance_fetch(
                lambda: self._ex.fetch_bids_asks(syms),
                context=f"bids_asks:{len(syms)}syms",
                weight=2,
                method="fetchBidsAsks",
            )
            out: dict[str, dict[str, float]] = {}
            for ccxt_sym, bbo in (result or {}).items():
                if not isinstance(bbo, dict):
                    continue
                bid = float(bbo.get("bid") or 0)
                ask = float(bbo.get("ask") or 0)
                if bid > 0 and ask > 0:
                    sym = self._bin_sym(ccxt_sym)
                    out[sym] = {"bid": bid, "ask": ask}
            return out
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning("fetch_bids_asks failed | error=%s", exc)
        except Exception as exc:
            LOG.warning("fetch_bids_asks failed | error=%s", exc)
        return {}

    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        now = time.monotonic()
        if self._cache_fresh(self._exchange_info_cache, _CACHE_TTL["exchange_info"]):
            assert self._exchange_info_cache is not None
            return self._exchange_info_cache[1]
        await self.load_markets()
        rows: list[SymbolMeta] = []
        for market in self._ex.markets.values():
            info = market.get("info") if isinstance(market, dict) else None
            info = info if isinstance(info, dict) else {}
            rows.append(
                SymbolMeta(
                    symbol=str(market.get("id") or info.get("symbol") or ""),
                    base_asset=str(market.get("base") or info.get("baseAsset") or ""),
                    quote_asset=str(market.get("quote") or info.get("quoteAsset") or ""),
                    contract_type=str(info.get("contractType") or ""),
                    status=str(info.get("status") or ""),
                    onboard_date_ms=int(info.get("onboardDate") or 0),
                )
            )
        self._exchange_info_cache = (now, rows)
        return rows

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        now = time.monotonic()
        if self._cache_fresh(self._ticker_24h_cache, _CACHE_TTL["ticker_24h"]):
            assert self._ticker_24h_cache is not None
            return self._ticker_24h_cache[1]
        await self.load_markets()
        try:
            tickers = await self._rest_call(
                lambda: self._ex.fetch_tickers(),
                context="ticker_24h",
                weight=40,
            )
        except Exception:
            LOG.warning("fetch_ticker_24h_failed | serving stale cache")
            if self._ticker_24h_cache is not None:
                return self._ticker_24h_cache[1]
            raise
        rows: list[dict[str, float | str]] = []
        for ccxt_sym, item in tickers.items():
            if not is_linear_usdt_swap_market(self._ex.markets.get(ccxt_sym)):
                continue
            sym = try_binance_id_from_ccxt(ccxt_sym, exchange=self._ex)
            if not sym:
                continue
            last_price = _safe_float(item.get("last"))
            quote_volume = _safe_float(item.get("quoteVolume"))
            if not sym or last_price <= 0 or quote_volume <= 0:
                continue
            row: dict[str, float | str] = {
                "symbol": sym,
                "last_price": last_price,
                "price_change_percent": _safe_float(item.get("percentage")),
                "quote_volume": quote_volume,
                "trade_count": _safe_float(item.get("info", {}).get("count")),
                # Asset class from exchangeInfo so the scanner gate can drop tokenized
                # equities/commodities (COIN = real crypto). Kept on the row — not
                # filtered here — so /signal and pinned metals still get their ticker.
                "underlying_type": underlying_type_of(self._ex.markets.get(ccxt_sym)),
            }
            high = _safe_float(item.get("high"))
            low = _safe_float(item.get("low"))
            if high > 0:
                row["high_price"] = high
            if low > 0:
                row["low_price"] = low
            rows.append(row)
        if rows:
            self._ticker_24h_cache = (now, rows)
        elif self._ticker_24h_cache is not None:
            # Binance returned empty/all-filtered tickers — keep last good data.
            return self._ticker_24h_cache[1]
        return rows

    async def _fetch_raw_klines(
        self,
        symbol: str,
        interval: str,
        *,
        since: int | None = None,
        limit: int = 500,
        qos_context: str | None = None,
    ) -> list[list[Any]]:
        """Raw ``/fapi/v1/klines`` — FULL 12-element rows, numerics typed.

        ccxt's ``fetch_ohlcv`` truncates to [t,o,h,l,c,v], zeroing the taker/
        quote fields that orderflow features depend on (delta_ratio degenerated
        to 0 = «всё продажи»). The raw public endpoint keeps them: [6]=closeTime
        [7]=quoteVol [8]=numTrades [9]=takerBuyBase [10]=takerBuyQuote.
        """
        await self.load_markets()
        capped = min(1500, max(1, int(limit)))
        params: dict[str, Any] = {
            "symbol": self._bin_sym(symbol),
            "interval": interval,
            "limit": capped,
        }
        if since is not None:
            params["startTime"] = max(0, int(since))
        base_ctx = f"ohlcv.{interval}"
        raw = await self._rest_call(
            lambda: self._ex.fapiPublicGetKlines(params),
            context=f"{qos_context}:{base_ctx}" if qos_context else base_ctx,
            weight=klines_weight(capped),
        )
        typed: list[list[Any]] = []
        for r in raw or []:
            if not r or len(r) < 6:
                continue
            try:
                row: list[Any] = [
                    int(r[0]),
                    float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                ]
                if len(r) >= 11:
                    row.extend([int(r[6]), float(r[7]), int(r[8]), float(r[9]), float(r[10])])
                typed.append(row)
            except (TypeError, ValueError):
                continue
        return typed

    async def fetch_ohlcv_list(
        self,
        symbol: str,
        interval: str,
        *,
        since: int | None = None,
        limit: int = 500,
        qos_context: str | None = None,
    ) -> list[list[Any]]:
        """``qos_context`` tags the gate context with the caller's QoS family
        (e.g. ``"path_backfill"``) so background loops yield under scarcity."""
        return await self._fetch_raw_klines(
            symbol, interval, since=since, limit=limit, qos_context=qos_context
        )

    async def fetch_ohlcv_list_cached(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> list[list[Any]]:
        """Interval-aware cached list OHLCV — the hot-path variant.

        The scanner and pinned analyst re-scan every cycle; without caching each
        cycle re-fetched 5 TFs × N symbols via weight-10 klines, the dominant REST
        sink that earned the 418 IP ban. TTLs are per-interval (see ``_CACHE_TTL``
        ``klines_*``): a 1d bar is stable for an hour, only 5m needs frequent refresh.
        A single-flight lock coalesces concurrent scanners onto one fetch."""
        key = (self._bin_sym(symbol), interval, int(limit))
        ttl = float(_CACHE_TTL.get(f"klines_{interval}", 60))
        now = time.monotonic()
        cached = self._ohlcv_list_cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        lock = self._ohlcv_list_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._ohlcv_list_cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < ttl:
                return cached[1]
            rows = await self.fetch_ohlcv_list(symbol, interval, limit=limit)
            self._ohlcv_list_cache[key] = (time.monotonic(), rows)
            return rows

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        rows = await self._fetch_raw_klines(symbol, interval, limit=limit)
        frame = finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )
        return frame

    async def fetch_klines_between(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1500,
    ) -> pl.DataFrame:
        rows = await self._fetch_raw_klines(
            symbol, interval, since=max(0, int(start_time_ms)), limit=limit
        )
        end_ms = max(0, int(end_time_ms))
        trimmed = [r for r in rows if r and int(r[0]) <= end_ms]
        return ccxt_ohlcv_to_frame(trimmed, interval, exchange=self._ex)

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        key = (self._bin_sym(symbol), interval, int(limit))
        ttl = float(_CACHE_TTL.get(f"klines_{interval}", 60))
        now = time.monotonic()
        cached = self._klines_cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        lock = self._klines_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._klines_cache.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
            frame = await self.fetch_klines(symbol, interval, limit=limit)
            self._klines_cache[key] = (time.monotonic(), frame)
            return frame

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> pl.DataFrame | None:
        key = (self._bin_sym(symbol), interval, int(limit))
        cached = self._klines_cache.get(key)
        if cached is None:
            return None
        ttl = max_age_s if max_age_s is not None else float(_CACHE_TTL.get(f"klines_{interval}", 60))
        if time.monotonic() - cached[0] > ttl:
            return None
        return cached[1]

    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> dict[str, float | None]:
        sym = self._bin_sym(symbol)
        depth_limit = min(100, max(5, int(limit)))
        key = (sym, depth_limit)
        now = time.monotonic()
        cached = self._order_book_depth_cache.get(key)
        if self._cache_fresh(cached, _CACHE_TTL["order_book_depth"]):
            return dict(cached[1])  # type: ignore[index]
        await self.load_markets()
        try:
            ob = await self._direct_binance_fetch(
                lambda: self._ex.fetch_order_book(self._ccxt_sym(sym), limit=depth_limit),
                context=f"order_book:{sym}",
                weight=depth_weight(depth_limit),
                method="fetchOrderBook",
            )
        except ccxt.BaseError:
            raise
        except Exception as exc:
            LOG.warning("fetch_order_book failed | symbol=%s error=%s", sym, exc)
            raise
        bids = [(float(row[0]), float(row[1])) for row in (ob.get("bids") or []) if row]
        asks = [(float(row[0]), float(row[1])) for row in (ob.get("asks") or []) if row]
        if not bids or not asks:
            return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}
        snapshot: dict[str, Any] = depth_snapshot_from_book(bids, asks)
        snapshot["bids"] = bids
        snapshot["asks"] = asks
        snapshot["exchange"] = "binance"
        self._order_book_depth_cache[key] = (now, snapshot)
        return snapshot

    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        depth = await self.fetch_order_book_depth_snapshot(symbol, limit=5)
        if depth.get("bid_price"):
            return depth
        return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}

    async def fetch_open_interest(self, symbol: str) -> float | None:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._open_interest_cache.get(sym)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        try:
            payload = await self._direct_binance_fetch(
                lambda: self._ex.fetch_open_interest(self._ccxt_sym(sym)),
                context=f"open_interest:{sym}",
                weight=1,
                method="fetchOpenInterest",
            )
            value = _safe_float(payload.get("openInterestAmount") or payload.get("info", {}).get("openInterest"))
            if value > 0:
                self._open_interest_cache[sym] = (now, value)
                return value
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning("fetch_open_interest failed | symbol=%s error=%s", sym, exc)
        except Exception as exc:
            LOG.warning("fetch_open_interest failed | symbol=%s error=%s", sym, exc)
        return None

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._open_interest_change_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest_change"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            if not self._ccxt_has(self._ex, "fetchOpenInterestHistory"):
                raise ccxt.NotSupported(f"fetchOpenInterestHistory unavailable on {self._ex.id}")
            # fetchOpenInterestHistory -> Binance GET /futures/data/openInterestHist,
            # a /futures/data/* endpoint under the SEPARATE 1000-req/5min IP limit --
            # NOT the general 2400-weight/1min budget. Was routed through
            # _direct_binance_fetch's acquire_binance_weight(weight=1), which has zero
            # awareness of that narrow window, so this call was effectively unpaced
            # against the limit that actually governs it (root cause of an observed
            # 418 IP ban despite the general weight budget never being near capacity).
            payload = await self._fapi_call(
                lambda: self._ex.fetch_open_interest_history(
                    self._ccxt_sym(sym), timeframe=period, limit=2
                ),
                context=f"oi_change:{sym}:{period}",
            )
            series = [float(item["openInterestAmount"]) for item in payload
                      if item.get("openInterestAmount") is not None]
            if len(series) < 2 or series[-2] <= 0:
                return None
            change = series[-1] / series[-2] - 1.0
            self._open_interest_change_cache[cache_key] = (now, change)
            return change
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning("oi_change failed | symbol=%s period=%s error=%s", sym, period, exc)
        except Exception as exc:
            LOG.warning("oi_change failed | symbol=%s period=%s error=%s", sym, period, exc)
        return None

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Top trader long/short account ratio (topLongShortAccountRatio)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetTopLongShortAccountRatio,
            ratio_keys=("longShortRatio", "long_short_ratio"),
            cache=self._long_short_ratio_cache,
            ttl_key="long_short_ratio",
        )

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Top trader long/short position ratio (topLongShortPositionRatio)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetTopLongShortPositionRatio,
            ratio_keys=("longShortRatio", "long_short_ratio"),
            cache=self._top_position_ls_ratio_cache,
            ttl_key="long_short_ratio",
        )

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Global long/short account ratio (globalLongShortAccountRatio)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetGlobalLongShortAccountRatio,
            ratio_keys=("longShortRatio", "long_short_ratio"),
            cache=self._global_ls_ratio_cache,
            ttl_key="long_short_ratio",
        )

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Taker buy/sell volume ratio via fapi takerlongshortRatio (CCXT implicit)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetTakerlongshortRatio,
            ratio_keys=("buySellRatio", "buy_sell_ratio"),
            cache=self._taker_ratio_cache,
            ttl_key="taker_ratio",
        )

    async def fetch_oi_history_raw(
        self, symbol: str, *, period: str = "1h", limit: int = 48
    ) -> list[dict[str, Any]]:
        """Raw CCXT open-interest history rows (timestamp + openInterestAmount)."""
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit), "raw")
        cached = self._oi_series_cache.get(cache_key)  # type: ignore[arg-type]
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        payload = await self._fapi_call(
            lambda: self._ex.fetch_open_interest_history(
                self._ccxt_sym(sym), timeframe=period, limit=int(limit)
            ),
            context=f"oi_history_raw:{sym}:{period}",
        )
        rows = [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []
        if rows:
            self._oi_series_cache[cache_key] = (time.monotonic(), rows)  # type: ignore[index]
        return rows

    async def fetch_oi_bars_for_maps(
        self, symbol: str, *, period: str = "1h", limit: int = 48
    ) -> list[dict[str, Any]]:
        """OI deltas aligned to OHLCV for entry-anchored liquidation forward map."""
        from hunt_core.maps.oi import oi_bars_from_frames, oi_bars_from_scalar_series

        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit), "map_bars")
        cached = self._oi_series_cache.get(cache_key)  # type: ignore[arg-type]
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            raw = await self.fetch_oi_history_raw(sym, period=period, limit=limit)
            klines = await self.fetch_klines(sym, period, limit=limit + 5)
            bars = oi_bars_from_frames(raw, klines)
            if not bars and raw:
                scalars = [
                    float(x.get("openInterestAmount") or 0)
                    for x in raw
                    if x.get("openInterestAmount") is not None
                ]
                bars = oi_bars_from_scalar_series(scalars, klines)
            if bars:
                self._oi_series_cache[cache_key] = (time.monotonic(), bars)  # type: ignore[index]
            return bars
        except Exception as exc:
            LOG.warning("fetch_oi_bars_for_maps failed | symbol=%s error=%s", sym, exc)
            return []

    async def fetch_open_interest_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._oi_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            if not self._ccxt_has(self._ex, "fetchOpenInterestHistory"):
                raise ccxt.NotSupported(f"fetchOpenInterestHistory unavailable on {self._ex.id}")
            # /futures/data/openInterestHist -- see fetch_open_interest_change for why
            # this must go through the narrow fapi_data window, not general weight.
            payload = await self._fapi_call(
                lambda: self._ex.fetch_open_interest_history(
                    self._ccxt_sym(sym), timeframe=period, limit=int(limit)
                ),
                context=f"oi_series:{sym}:{period}",
            )
            series = [float(item["openInterestAmount"]) for item in payload
                      if item.get("openInterestAmount") is not None]
            if series:
                self._oi_series_cache[cache_key] = (time.monotonic(), series)
            return series
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning(
                "fetch_open_interest_history_series failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            raise
        except Exception as exc:
            LOG.warning(
                "fetch_open_interest_history_series failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            raise

    async def fetch_global_ls_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._gls_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            if not self._ccxt_has(self._ex, "fetchLongShortRatioHistory"):
                raise ccxt.NotSupported(f"fetchLongShortRatioHistory unavailable on {self._ex.id}")
            # /futures/data/globalLongShortAccountRatio -- see fetch_open_interest_change
            # for why this must go through the narrow fapi_data window, not general weight.
            payload = await self._fapi_call(
                lambda: self._ex.fetch_long_short_ratio_history(
                    self._ccxt_sym(sym), timeframe=period, limit=int(limit)
                ),
                context=f"gls_series:{sym}:{period}",
            )
            series = [float(item["longShortRatio"]) for item in payload
                      if item.get("longShortRatio") is not None]
            if series:
                self._gls_series_cache[cache_key] = (time.monotonic(), series)
            return series
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning(
                "fetch_long_short_ratio_history failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            raise
        except Exception as exc:
            LOG.warning(
                "fetch_long_short_ratio_history failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            raise

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._funding_rate_cache.get(sym)
        if self._cache_fresh(cached, _CACHE_TTL["funding_rate"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        try:
            payload = await self._direct_binance_fetch(
                lambda: self._ex.fetch_funding_rate(self._ccxt_sym(sym)),
                context=f"funding_rate:{sym}",
                weight=1,
                method="fetchFundingRate",
            )
            value = payload.get("fundingRate")
            if value is not None:
                rate = float(value)
                self._funding_rate_cache[sym] = (now, rate)
                return rate
        except ccxt.BaseError as exc:
            if is_ccxt_rate_limited(exc):
                raise
            LOG.warning("fetch_funding_rate failed | symbol=%s error=%s", sym, exc)
        except Exception as exc:
            LOG.warning("fetch_funding_rate failed | symbol=%s error=%s", sym, exc)
        return None

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._funding_history_cache.get(sym)
        if cached is not None and now - cached[0] < 900:
            return cached[1]
        await self.load_markets()
        try:
            payload = await self._direct_binance_fetch(
                lambda: self._ex.fetch_funding_rate_history(self._ccxt_sym(sym), limit=limit),
                context=f"funding_hist:{sym}",
                weight=1,
                method="fetchFundingRateHistory",
            )
            rows: list[dict[str, Any]] = []
            for item in payload:
                rows.append(
                    {
                        "fundingTime": int(item.get("timestamp") or 0),
                        "fundingRate": float(item.get("fundingRate") or 0.0),
                        "markPrice": float(item.get("markPrice") or item.get("info", {}).get("markPrice") or 0.0),
                    }
                )
            rows.sort(key=lambda r: r["fundingTime"])
            self._funding_history_cache[sym] = (now, rows)
            return rows
        except Exception as exc:
            LOG.warning("fetch_funding_rate_history failed | symbol=%s error=%s", sym, exc)
            raise

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        if self._cache_fresh(self._premium_index_all_cache, 30):
            assert self._premium_index_all_cache is not None
            return self._premium_index_all_cache[1]
        if not self._ccxt_has(self._ex, "fetchFundingRates"):
            raise ccxt.NotSupported(
                f"fetchFundingRates unavailable on {self._ex.id}"
            )
        await self.load_markets()
        funding = await self._rest_call(
            lambda: self._ex.fetch_funding_rates(),
            context="premium_index_all",
            weight=10,
        )
        rows: dict[str, dict[str, float]] = {}
        for ccxt_sym, item in funding.items():
            if not is_linear_usdt_swap_market(self._ex.markets.get(ccxt_sym)):
                continue
            resolved = try_binance_id_from_ccxt(ccxt_sym, exchange=self._ex)
            if not resolved:
                continue
            sym = self._bin_sym(resolved)
            mark = _safe_float(item.get("markPrice"))
            index = _safe_float(item.get("indexPrice"))
            if not sym or mark <= 0:
                continue
            rows[sym] = {
                "mark_price": mark,
                "index_price": index,
                "last_funding_rate": _safe_float(item.get("fundingRate")),
            }
        self._premium_index_all_cache = (now, rows)
        return rows

    async def fetch_funding_info_all(self) -> dict[str, dict[str, float | int]]:
        now = time.monotonic()
        if self._cache_fresh(self._funding_info_all_cache, _CACHE_TTL["funding_info"]):
            assert self._funding_info_all_cache is not None
            return self._funding_info_all_cache[1]
        await self.load_markets()
        if not self._ccxt_has(self._ex, "fetchFundingIntervals"):
            raise ccxt.NotSupported(
                f"fetchFundingIntervals unavailable on {self._ex.id}"
            )
        intervals = await self._rest_call(
            lambda: self._ex.fetch_funding_intervals(),
            context="funding_info_all",
            weight=10,
        )
        rows: dict[str, dict[str, float | int]] = {}
        for ccxt_sym, item in intervals.items():
            if not is_linear_usdt_swap_market(self._ex.markets.get(ccxt_sym)):
                continue
            info = item.get("info") if isinstance(item, dict) else None
            info = info if isinstance(info, dict) else {}
            resolved = try_binance_id_from_ccxt(ccxt_sym, exchange=self._ex)
            if not resolved:
                continue
            sym = self._bin_sym(resolved)
            rows[sym] = {
                "funding_interval_hours": int(info.get("fundingIntervalHours") or 8),
                "cap": _safe_float(info.get("adjustedFundingRateCap")),
                "floor": _safe_float(info.get("adjustedFundingRateFloor")),
            }
        self._funding_info_all_cache = (now, rows)
        return rows

    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._basis_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["basis"]):
            return cached[1]  # type: ignore[index]
        if sym in self._basis_api_unsupported:
            return await self._fetch_basis_fallback(symbol, period=period)
        if not callable(getattr(self._ex, "fapiDataGetBasis", None)):
            self._basis_api_unsupported.add(sym)
            return await self._fetch_basis_fallback(symbol, period=period)
        try:
            await self.load_markets()
            payload = await self._fapi_call(
                lambda: self._ex.fapiDataGetBasis(
                    {
                        "pair": sym,
                        "contractType": "PERPETUAL",
                        "period": period,
                        "limit": limit,
                    }
                ),
                context=f"basis:{sym}:{period}",
            )
            basis_series: list[float] = []
            for row in payload if isinstance(payload, list) else []:
                futures_price = _safe_float(row.get("futuresPrice"))
                index_price = _safe_float(row.get("indexPrice"))
                if index_price <= 0:
                    continue
                basis_series.append((futures_price - index_price) / index_price * 100.0)
            if not basis_series:
                return await self._fetch_basis_fallback(symbol, period=period)
            s = pl.Series("basis", basis_series)
            basis_pct = float(s[-1])
            premium_slope = float(s[-1] - s[-2]) if len(basis_series) >= 2 else None
            premium_zscore = None
            if len(basis_series) >= 3:
                _std = s.std(ddof=0)
                std = float(_std.total_seconds()) if isinstance(_std, timedelta) else float(_std) if _std is not None else 0.0
                if std > 0:
                    premium_zscore = float((s[-1] - s.mean()) / std)
            self._basis_cache[cache_key] = (now, basis_pct)
            self._basis_stats_cache[cache_key] = (
                now,
                {
                    "latest_basis_pct": basis_pct,
                    "premium_slope_5m": premium_slope,
                    "premium_zscore_5m": premium_zscore,
                    "mark_index_spread_bps": basis_pct * 100.0,
                },
            )
            return basis_pct
        except ccxt.BaseError as exc:
            self._rest_gate.record_error(exc, context=f"basis:{sym}")
            err = str(exc)
            if "-4104" in err or "Invalid contract type" in err:
                self._basis_api_unsupported.add(sym)
                LOG.debug(
                    "fetch_basis_perpetual_unsupported | symbol=%s period=%s",
                    sym,
                    period,
                )
                return await self._fetch_basis_fallback(symbol, period=period)
            if is_ccxt_rate_limited(exc):
                LOG.warning(
                    "fetch_basis_rate_limited | symbol=%s period=%s error=%s",
                    sym,
                    period,
                    exc,
                )
                raise
            LOG.warning(
                "fetch_basis_failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            return await self._fetch_basis_fallback(symbol, period=period)
        except Exception as exc:
            LOG.warning(
                "fetch_basis_failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            return await self._fetch_basis_fallback(symbol, period=period)

    async def _fetch_basis_fallback(self, symbol: str, *, period: str) -> float | None:
        """Mark/index OHLCV basis for symbols without PERPETUAL fapiDataGetBasis."""
        stats = await self.fetch_basis_from_ohlcv(symbol, interval=period, limit=48)
        latest = stats.get("latest_basis_pct")
        return float(latest) if latest is not None else None

    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
        sym = self._bin_sym(symbol)
        cache_key = (sym, int(limit))
        cached = self._agg_trade_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["agg_trades"]):
            return cached[1]  # type: ignore[index]
        now = time.monotonic()
        await self.load_markets()
        trades = await self._direct_binance_fetch(
            lambda: self._ex.fetch_trades(
                self._ccxt_sym(sym), limit=min(1000, max(1, limit))
            ),
            context=f"agg_trades:{sym}",
            weight=weight_for_context("agg_trades"),  # spec weight 20 (was undercharged 5)
            method="fetchTrades",
        )
        buy_qty = sell_qty = 0.0
        for trade in trades:
            qty = _safe_float(trade.get("amount"))
            side = str(trade.get("side") or "").lower()
            if side == "buy":
                buy_qty += qty
            else:
                sell_qty += qty
        total = buy_qty + sell_qty
        delta_ratio = (buy_qty - sell_qty) / total if total > 0 else None
        snapshot = AggTradeSnapshot(
            symbol=sym,
            trade_count=len(trades),
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            delta_ratio=delta_ratio,
        )
        self._agg_trade_cache[cache_key] = (now, snapshot)
        return snapshot

    def get_cached_open_interest(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        cached = self._open_interest_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._open_interest_change_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_oi_series(
        self,
        symbol: str,
        *,
        period: str = "5m",
        limit: int = 48,
        max_age_s: float = 1800.0,
    ) -> list[float] | None:
        cache_key = (self._bin_sym(symbol), period, int(limit))
        cached = self._oi_series_cache.get(cache_key)
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return list(cached[1])

    def get_cached_gls_series(
        self,
        symbol: str,
        *,
        period: str = "5m",
        limit: int = 48,
        max_age_s: float = 1800.0,
    ) -> list[float] | None:
        cache_key = (self._bin_sym(symbol), period, int(limit))
        cached = self._gls_series_cache.get(cache_key)
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return list(cached[1])

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._long_short_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_top_position_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._top_position_ls_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._global_ls_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._taker_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    async def fetch_leverage_tiers(self, symbol: str) -> list[dict[str, Any]] | None:
        """Not available on public-only Hunt — Binance ``leverageBracket`` requires signed auth.

        Liquidation heatmap falls back to ``liquidation_heatmap._DEFAULT_LEVERAGE_TIERS``.
        """
        if not self._leverage_tiers_skip_logged:
            self._leverage_tiers_skip_logged = True
            LOG.info(
                "leverage_tiers_skipped_public_only | symbol=%s — "
                "CCXT fetchLeverageTiers needs apiKey; using default liq tiers",
                self._bin_sym(symbol),
            )
        return None

    def get_cached_leverage_tiers(
        self, symbol: str, *, max_age_s: float | None = None
    ) -> list[dict[str, Any]] | None:
        cached = self._leverage_tiers_cache.get(self._bin_sym(symbol))
        ttl = float(max_age_s if max_age_s is not None else _CACHE_TTL["leverage_tiers"])
        if not self._cache_fresh(cached, ttl):
            return None
        return cached[1]  # type: ignore[index]

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        cached = self._funding_rate_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> str | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        rows = cached[1]
        if len(rows) < 3:
            return None
        s = pl.Series("r", [float(r["fundingRate"]) for r in rows[-4:]])
        diffs = s.diff().drop_nulls()
        ups = int((diffs > 0).sum())
        downs = int((diffs < 0).sum())
        steps = diffs.len()
        if ups >= steps * 0.75:
            return "rising"
        if downs >= steps * 0.75:
            return "falling"
        return "flat"

    def get_cached_funding_rate_zscore(
        self, symbol: str, *, max_cache_age_s: float = 1800.0
    ) -> float | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_cache_age_s:
            return None
        s = pl.Series("rates", [float(r["fundingRate"]) for r in cached[1]]).drop_nans()
        if s.len() < 6:
            return None
        _stdev = s.std(ddof=1)
        stdev = float(_stdev.total_seconds()) if isinstance(_stdev, timedelta) else float(_stdev) if _stdev is not None else 0.0
        if stdev <= 1e-12:
            return 0.0
        return float((s[-1] - s.mean()) / stdev)

    def get_cached_funding_recent_extreme(
        self,
        symbol: str,
        *,
        max_age_hours: float = 48.0,
        max_cache_age_s: float = 1800.0,
    ) -> tuple[float, float] | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_cache_age_s or not cached[1]:
            return None
        now_ms = int(time.time() * 1000)
        max_age_ms = max(0.0, float(max_age_hours)) * 3_600_000.0
        candidates: list[tuple[float, float]] = []
        for row in cached[1]:
            rate = float(row.get("fundingRate") or 0.0)
            funding_time = int(row.get("fundingTime") or 0)
            if funding_time <= 0:
                continue
            age_ms = max(0, now_ms - funding_time)
            if age_ms <= max_age_ms:
                candidates.append((rate, age_ms / 3_600_000.0))
        if not candidates:
            return None
        return max(candidates, key=lambda item: abs(item[0]))

    def get_cached_basis_stats(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> dict[str, float | None] | None:
        cached = self._basis_stats_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return dict(cached[1])

    def update_basis_from_websocket(
        self,
        symbol: str,
        mark_price: float,
        index_price: float | None = None,
        period: str = "5m",
    ) -> dict[str, float | None] | None:
        if index_price is None or index_price <= 0 or mark_price <= 0:
            return None
        basis_pct = (mark_price - index_price) / index_price * 100.0
        now = time.monotonic()
        cache_key = (self._bin_sym(symbol), period)
        prev = self._basis_stats_cache.get(cache_key)
        premium_slope = None
        if prev is not None:
            prev_basis = prev[1].get("latest_basis_pct")
            if prev_basis is not None:
                premium_slope = basis_pct - float(prev_basis)
        stats = {
            "latest_basis_pct": basis_pct,
            "premium_slope_5m": premium_slope,
            # Not recomputed here (WS path only has mark/index price, not the
            # basis history needed for a z-score) -- must NOT carry the prior
            # REST-computed value forward, because doing so under a refreshed
            # cache timestamp made a stale z-score look fresh under the 30min
            # TTL check in get_cached_basis_stats(). Fresh values are supplied
            # by the periodic REST refresh (fetch_basis / fetch_basis_from_ohlcv).
            "premium_zscore_5m": None,
            "mark_index_spread_bps": basis_pct * 100.0,
        }
        self._basis_cache[cache_key] = (now, basis_pct)
        self._basis_stats_cache[cache_key] = (now, stats)
        return stats

    async def fetch_mark_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Mark price OHLCV via ccxt fetch_mark_ohlcv."""
        if not self._ccxt_has(self._ex, "fetchMarkOHLCV"):
            raise ccxt.NotSupported(f"fetchMarkOHLCV unavailable on {self._ex.id}")
        await self.load_markets()
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._direct_binance_fetch(
            lambda: self._ex.fetch_mark_ohlcv(self._ccxt_sym(symbol), interval, limit=limit),
            context=f"mark_ohlcv:{symbol}",
            weight=2,
            method="fetchMarkOHLCV",
        )
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )

    async def fetch_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Index price OHLCV via ccxt fetch_index_ohlcv."""
        if not self._ccxt_has(self._ex, "fetchIndexOHLCV"):
            raise ccxt.NotSupported(f"fetchIndexOHLCV unavailable on {self._ex.id}")
        await self.load_markets()
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._direct_binance_fetch(
            lambda: self._ex.fetch_index_ohlcv(self._ccxt_sym(symbol), interval, limit=limit),
            context=f"index_ohlcv:{symbol}",
            weight=2,
            method="fetchIndexOHLCV",
        )
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )

    async def fetch_premium_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Premium index (basis %) OHLCV via ccxt fetch_premium_index_ohlcv."""
        if not self._ccxt_has(self._ex, "fetchPremiumIndexOHLCV"):
            raise ccxt.NotSupported(
                f"fetchPremiumIndexOHLCV unavailable on {self._ex.id}"
            )
        await self.load_markets()
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._direct_binance_fetch(
            lambda: self._ex.fetch_premium_index_ohlcv(
                self._ccxt_sym(symbol), interval, limit=limit
            ),
            context=f"premium_ohlcv:{symbol}",
            weight=2,
            method="fetchPremiumIndexOHLCV",
        )
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )

    async def fetch_basis_from_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 48
    ) -> dict[str, float | None]:
        """Basis stats computed from mark vs index OHLCV frames via polars."""
        try:
            mark_df, index_df = await asyncio.gather(
                self.fetch_mark_ohlcv(symbol, interval, limit=limit),
                self.fetch_index_ohlcv(symbol, interval, limit=limit),
            )
            if mark_df.is_empty() or index_df.is_empty():
                return {}
            joined = mark_df.select(["time", pl.col("close").alias("mark_close")]).join(
                index_df.select(["time", pl.col("close").alias("index_close")]),
                on="time",
                how="inner",
            )
            if joined.is_empty():
                return {}
            basis = (
                (pl.col("mark_close") - pl.col("index_close")) / pl.col("index_close") * 100.0
            )
            joined = joined.with_columns(basis.alias("basis_pct"))
            s = joined["basis_pct"]
            latest = float(s[-1])
            slope = float(s[-1] - s[-2]) if s.len() >= 2 else None
            _std = s.std(ddof=1)
            std = float(_std.total_seconds()) if isinstance(_std, timedelta) else float(_std) if _std is not None else 0.0
            zscore = float((s[-1] - s.mean()) / std) if std > 0 else None
            cache_key = (self._bin_sym(symbol), interval)
            now = time.monotonic()
            stats = {
                "latest_basis_pct": latest,
                "premium_slope_5m": slope,
                "premium_zscore_5m": zscore,
                "mark_index_spread_bps": latest * 100.0,
            }
            self._basis_cache[cache_key] = (now, latest)
            self._basis_stats_cache[cache_key] = (now, stats)
            return stats
        except Exception as exc:
            LOG.debug("fetch_basis_from_ohlcv failed | symbol=%s error=%s", symbol, exc)
            return {}

    # ── Secondary exchange REST (configurable via HUNT_CROSS_EXCHANGES) ──────

    async def _get_secondary(self, name: str) -> ccxt.Exchange | None:
        """Return cached secondary REST client, or None if init previously failed."""
        if name in self._secondary_failed:
            return None
        if name in self._secondary_clients:
            return self._secondary_clients[name]
        async with self._secondary_lock:
            if name in self._secondary_failed:
                return None
            if name in self._secondary_clients:
                return self._secondary_clients[name]
            ex_id = self._secondary_exchange_ids[name]
            ex: ccxt.Exchange = create_async_secondary_swap(
                ex_id,
                proxy_url=self._proxy_url,
                trust_env=self._trust_env,
                timeout_ms=self._timeout_ms,
            )
            try:
                await ex.load_markets()
                usdt_swap = sum(
                    1
                    for m in ex.markets.values()
                    if isinstance(m, dict)
                    and str(m.get("settle") or "").upper() == "USDT"
                    and str(m.get("type") or "") in {"swap", "future"}
                )
                if usdt_swap <= 0:
                    LOG.warning("secondary_rest_no_usdt_swap | exchange=%s", name)
                    await close_exchange_async(ex, label=f"secondary_rest_skip:{name}")
                    self._secondary_failed.add(name)
                    return None
            except asyncio.CancelledError:
                await close_exchange_async(ex, label=f"secondary_rest_cancel:{name}")
                raise
            except Exception as exc:
                LOG.debug("secondary_load_markets_failed | exchange=%s error=%s", name, exc)
                await close_exchange_async(ex, label=f"secondary_rest_init:{name}")
                self._secondary_failed.add(name)
                return None
            self._secondary_clients[name] = ex
            return ex

    async def _secondary_listing(
        self, exchange_name: str, binance_sym: str
    ) -> tuple[str | None, str]:
        """Resolve a Binance id on a secondary venue, distinguishing WHY it failed.

        ``_secondary_ccxt_symbol`` collapses two very different facts into ``None``:
        the venue does not list the coin, and the venue's client is unavailable
        (``_get_secondary`` failure is permanent via ``_secondary_failed``). The
        card rendered both as «OKX✗», telling the trader OKX had delisted a coin
        it lists fine whenever OKX's client failed at startup.

        Args:
            exchange_name: Configured secondary venue key (e.g. ``"okx"``).
            binance_sym: Binance-style symbol id (e.g. ``"BTCUSDT"``).

        Returns:
            ``(ccxt_symbol_or_None, status)`` where status is ``"listed"``,
            ``"not_listed"``, or ``"unknown"`` (venue unavailable — no claim made).
        """
        ex = await self._get_secondary(exchange_name)
        if ex is None:
            return None, "unknown"
        try:
            return resolve_linear_usdt_swap(binance_sym, exchange=ex), "listed"
        except Exception as exc:
            LOG.debug(
                "secondary_symbol_not_listed | exchange=%s symbol=%s error=%s",
                exchange_name,
                binance_sym,
                exc,
            )
            return None, "not_listed"

    async def _secondary_ccxt_symbol(self, exchange_name: str, binance_sym: str) -> str | None:
        """Resolve Binance id on a secondary venue; None if not listed/unavailable."""
        ccxt_sym, _status = await self._secondary_listing(exchange_name, binance_sym)
        return ccxt_sym

    @staticmethod
    def _secondary_ttl(payload: dict[str, float | None], kind: str) -> float:
        """TTL for a secondary result — full for data, short for a blank.

        A transient venue error used to be cached as ``{None}`` for the full 600s
        TTL, so one timeout blanked that venue's funding/OI on the card for ten
        minutes. Negative results get a short TTL: they still absorb a retry
        stampede, but the venue reappears within seconds of recovering.

        Args:
            payload: The cached per-venue result dict.
            kind: ``_CACHE_TTL`` key for the positive case.

        Returns:
            TTL in seconds.
        """
        if any(v is not None for v in payload.values()):
            return float(_CACHE_TTL[kind])
        return _SECONDARY_NEGATIVE_CACHE_TTL_S

    async def _secondary_call(
        self,
        name: str,
        ex: ccxt.Exchange,
        factory: Any,
        *,
        context: str,
        method: str,
    ) -> Any:
        if not ccxt_method_available(ex, method):
            raise ccxt.NotSupported(f"{method} unavailable on {ex.id}")
        return await self._rest_gate.invoke_secondary(
            name,
            ex,
            factory,
            context=context,
        )

    async def _fetch_secondary_funding(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        """Funding rate AND its funding interval from one secondary venue.

        The rate alone is not comparable across venues: bybit/bitget list symbols
        on 1h/2h/4h funding while Binance is mostly 8h, so CCXT's unified
        ``interval`` field is fetched alongside and carried to the aggregation.

        Args:
            name: Configured secondary venue key.
            ccxt_sym: Venue-resolved unified symbol.

        Returns:
            ``{"fundingRate": rate|None, "interval_hours": hours|None}`` — ``None``
            means unknown, never 0.0 (``0.0`` is a valid, meaningfully flat rate).
        """
        from hunt_core.market.cross import parse_funding_interval_hours

        cache_key = (name, ccxt_sym)
        now = time.monotonic()
        cached = self._secondary_funding_cache.get(cache_key)
        if cached is not None and self._cache_fresh(cached, self._secondary_ttl(cached[1], "secondary_funding")):
            return cached[1]
        try:
            ex = await self._get_secondary(name)
            if ex is None:
                result: dict[str, float | None] = {"fundingRate": None, "interval_hours": None}
            else:
                r = await self._secondary_call(
                    name,
                    ex,
                    lambda: ex.fetch_funding_rate(ccxt_sym),
                    context=f"funding:{ccxt_sym}",
                    method="fetchFundingRate",
                )
                result = {
                    "fundingRate": finite_float_or_none(r.get("fundingRate")),
                    "interval_hours": parse_funding_interval_hours(r.get("interval")),
                }
        except ccxt.NotSupported:
            result = {"fundingRate": None, "interval_hours": None}
        except ccxt.BaseError as exc:
            LOG.debug(
                "secondary_funding_failed | exchange=%s sym=%s error=%s",
                name,
                ccxt_sym,
                exc,
            )
            result = {"fundingRate": None, "interval_hours": None}
        self._secondary_funding_cache[cache_key] = (now, result)
        return result

    async def fetch_secondary_funding_rate(self, exchange_id: str, symbol: str) -> float | None:
        """Public REST funding for cross venues without Pro ``watchFundingRates``."""
        bin_sym = self._bin_sym(symbol)
        ccxt_sym = await self._secondary_ccxt_symbol(exchange_id, bin_sym)
        if ccxt_sym is None:
            return None
        row = await self._fetch_secondary_funding(exchange_id, ccxt_sym)
        fr = row.get("fundingRate")
        return float(fr) if fr is not None else None

    async def _fetch_secondary_oi(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        """Open interest from one secondary venue, in BOTH the units it reports.

        CCXT's ``openInterestValue`` (USD notional) is venue-specific: OKX sets it
        from ``oiUsd``; bybit leaves it ``None`` for LINEAR markets (value is only
        set for inverse) and reports ``openInterestAmount`` in base coins; bitget
        hardcodes ``'openInterestValue': None``. So reading only
        ``openInterestValue`` yields an OKX-only number. ``safe_open_interest``
        rebuilds the dict from a fixed key mapping that has no ``openInterest``
        key at all, so the old ``r.get("openInterest")`` fallback was dead code.

        Args:
            name: Configured secondary venue key.
            ccxt_sym: Venue-resolved unified symbol.

        Returns:
            ``{"oi_usd": usd|None, "oi_base": coins|None}`` — the caller converts
            base-coin OI with that venue's own price rather than dropping it.
        """
        cache_key = (name, ccxt_sym)
        now = time.monotonic()
        cached = self._secondary_oi_cache.get(cache_key)
        if cached is not None and self._cache_fresh(cached, self._secondary_ttl(cached[1], "secondary_oi")):
            return cached[1]
        try:
            ex = await self._get_secondary(name)
            if ex is None:
                result: dict[str, float | None] = {"oi_usd": None, "oi_base": None}
            else:
                r = await self._secondary_call(
                    name,
                    ex,
                    lambda: ex.fetch_open_interest(ccxt_sym),
                    context=f"oi:{ccxt_sym}",
                    method="fetchOpenInterest",
                )
                result = {
                    "oi_usd": finite_float_or_none(r.get("openInterestValue")),
                    "oi_base": finite_float_or_none(r.get("openInterestAmount")),
                }
        except ccxt.NotSupported:
            result = {"oi_usd": None, "oi_base": None}
        except ccxt.BaseError as exc:
            LOG.debug(
                "secondary_oi_failed | exchange=%s sym=%s error=%s",
                name,
                ccxt_sym,
                exc,
            )
            result = {"oi_usd": None, "oi_base": None}
        self._secondary_oi_cache[cache_key] = (now, result)
        return result

    async def _fetch_secondary_ticker(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        """Mark and last price from one secondary venue, kept separate.

        ``mark`` is NOT a CCXT ticker key — the unified key is ``markPrice`` (see
        ``Exchange.safe_ticker``/``Ticker``), so ``t.get("mark")`` was always
        ``None`` and every secondary silently contributed its LAST trade while
        Binance contributed its MARK. The resulting "price divergence" largely
        measured mark-vs-last basis. Both are returned so the caller can compare
        one homogeneous price type.

        Args:
            name: Configured secondary venue key.
            ccxt_sym: Venue-resolved unified symbol.

        Returns:
            ``{"mark_price": price|None, "last_price": price|None}``. ``markPrice``
            is genuinely absent on some venues' ``fetchTicker`` (e.g. OKX) — that
            is reported as ``None``, not backfilled from ``last``.
        """
        try:
            ex = await self._get_secondary(name)
            if ex is None:
                return {"mark_price": None, "last_price": None}
            t = await self._secondary_call(
                name,
                ex,
                lambda: ex.fetch_ticker(ccxt_sym),
                context=f"ticker:{ccxt_sym}",
                method="fetchTicker",
            )
            mark = finite_float_or_none(t.get("markPrice"))
            last = finite_float_or_none(t.get("last"))
            return {
                "mark_price": mark if mark and mark > 0 else None,
                "last_price": last if last and last > 0 else None,
            }
        except ccxt.NotSupported:
            return {"mark_price": None, "last_price": None}
        except ccxt.BaseError as exc:
            LOG.debug(
                "secondary_ticker_failed | exchange=%s sym=%s error=%s",
                name,
                ccxt_sym,
                exc,
            )
            return {"mark_price": None, "last_price": None}

    async def fetch_secondary_tickers(self, name: str) -> list[dict[str, float | str]]:
        """All linear-USDT-swap 24h tickers from one configured secondary venue.

        Rows are normalized to the same shape as :meth:`fetch_ticker_24h`
        (Binance-style ``symbol``/``last_price``/``quote_volume``...). Returns an
        empty list if the venue is unavailable — soft overlay, never fatal.
        """
        ex = await self._get_secondary(name)
        if ex is None:
            return []
        try:
            tickers = await self._secondary_call(
                name,
                ex,
                lambda: ex.fetch_tickers(),
                context="tickers_all",
                method="fetchTickers",
            )
        except ccxt.NotSupported:
            return []
        except ccxt.BaseError as exc:
            LOG.warning("secondary_tickers_failed | exchange=%s error=%s", name, exc)
            return []
        rows: list[dict[str, float | str]] = []
        for ccxt_sym, item in tickers.items():
            market = ex.markets.get(ccxt_sym) if isinstance(ex.markets, dict) else None
            if not is_linear_usdt_swap_market(market):
                continue
            base = str((market or {}).get("base") or "").upper()
            if not base:
                continue
            bin_sym = f"{base}USDT"
            last_price = _safe_float(item.get("last"))
            quote_volume = _safe_float(item.get("quoteVolume"))
            if last_price <= 0 or quote_volume <= 0:
                continue
            row: dict[str, float | str] = {
                "symbol": bin_sym,
                "exchange": name,
                "last_price": last_price,
                "price_change_percent": _safe_float(item.get("percentage")),
                "quote_volume": quote_volume,
            }
            high = _safe_float(item.get("high"))
            low = _safe_float(item.get("low"))
            if high > 0:
                row["high_price"] = high
            if low > 0:
                row["low_price"] = low
            rows.append(row)
        return rows

    async def _binance_funding_interval_hours(self, bin_sym: str) -> float | None:
        """Binance's funding interval for one symbol, or None if unavailable.

        CCXT's ``fetchFundingRate`` on binance leaves ``interval`` unset (the
        premium-index payload has no ``fundingIntervalHours``); the interval comes
        from the separate ``fetchFundingIntervals`` endpoint, which this client
        already caches for the primary plane.

        Args:
            bin_sym: Binance-style symbol id.

        Returns:
            Interval in hours, or ``None`` when the endpoint is unavailable.
        """
        try:
            info_all = await self.fetch_funding_info_all()
        except (ccxt.NotSupported, ccxt.BaseError) as exc:
            LOG.debug("binance_funding_interval_unavailable | symbol=%s error=%s", bin_sym, exc)
            return None
        row = info_all.get(bin_sym)
        if not row:
            return None
        return finite_float_or_none(row.get("funding_interval_hours"))

    async def _binance_oi_usd(self, bin_sym: str, ref_mark: float | None) -> tuple[float | None, float | None]:
        """Binance open interest as (base coins, USD notional).

        Binance's ``fetchOpenInterest`` reports ``openInterestAmount`` in base
        coins and leaves ``openInterestValue`` ``None`` (only the *History*
        endpoint carries ``sumOpenInterestValue``), so USD notional is derived
        with the mark price.

        Args:
            bin_sym: Binance-style symbol id.
            ref_mark: Binance mark price used to convert coins → USD.

        Returns:
            ``(oi_base, oi_usd)``; either element is ``None`` when unknown.
        """
        try:
            oi_base = await self.fetch_open_interest(bin_sym)
        except ccxt.BaseError as exc:
            LOG.debug("binance_cross_oi_failed | symbol=%s error=%s", bin_sym, exc)
            return None, None
        if oi_base is None or oi_base <= 0:
            return None, None
        if ref_mark is None or ref_mark <= 0:
            return oi_base, None
        return oi_base, oi_base * ref_mark

    async def fetch_cross_exchange_snapshot(self, symbol: str) -> dict[str, Any]:
        """Cross-venue funding / OI / price intel for one Binance symbol.

        Every venue is aggregated in comparable units: funding is normalized to a
        per-8h rate before any spread/consensus comparison, OI is converted to USD
        notional per venue (INCLUDING Binance) rather than reading OKX's
        ``openInterestValue`` alone, and price divergence compares a single
        homogeneous price type.

        Args:
            symbol: Binance-style or unified symbol.

        Returns:
            Snapshot dict. Unknowns are ``None``/absent — never 0.0. Keys:
              ``fetched_at_ms``: wall-clock stamp so consumers can age-check it.
              ``funding``: ``{venue: raw_rate}`` per that venue's own interval.
              ``funding_interval_hours``: ``{venue: hours|None}``.
              ``funding_8h``: ``{venue: rate}`` normalized — the comparable map.
              ``funding_unknown_interval``: venues excluded from consensus.
              ``funding_spread`` / ``funding_consensus``: over ``funding_8h``.
              ``oi_base`` / ``oi_usd``: ``{venue: value}``; ``oi_venues`` lists the
              venues actually summed into ``oi_total``; ``oi_total_partial`` is
              True when a listed venue could not be converted (total understates).
              ``mark_price`` / ``last_price``: ``{venue: price}``, unmixed.
              ``price_divergence_pct`` / ``price_divergence_basis``.
              ``listed``: ``{venue: "listed"|"not_listed"|"unknown"}``.
        """
        from hunt_core.market.cross import (
            funding_consensus_from_normalized,
            normalized_funding_map,
            price_divergence_from_map,
        )

        bin_sym = self._bin_sym(symbol)
        await self.load_markets()
        premium_all = await self.fetch_premium_index_all()
        pr = premium_all.get(bin_sym)
        # Absent from the premium index = unknown, NOT a 0.0 funding rate. The old
        # `float(pr.get(...) or 0)` injected a phantom binance=0.0 into the rates
        # list, which forced consensus to "neutral" and inflated the spread.
        ref_funding = finite_float_or_none(pr.get("last_funding_rate")) if pr else None
        ref_mark = finite_float_or_none(pr.get("mark_price")) if pr else None
        if ref_mark is not None and ref_mark <= 0:
            ref_mark = None

        listed: dict[str, str] = {"binance": "listed" if bin_sym in premium_all else "not_listed"}
        funding: dict[str, float] = {}
        intervals: dict[str, float | None] = {}
        oi_usd: dict[str, float] = {}
        oi_base: dict[str, float] = {}
        mark_price: dict[str, float] = {}
        last_price: dict[str, float] = {}
        oi_unconvertible: list[str] = []

        if ref_funding is not None:
            funding["binance"] = ref_funding
            intervals["binance"] = await self._binance_funding_interval_hours(bin_sym)
        if ref_mark is not None:
            mark_price["binance"] = ref_mark

        bin_oi_base, bin_oi_usd = await self._binance_oi_usd(bin_sym, ref_mark)
        if bin_oi_base is not None:
            oi_base["binance"] = bin_oi_base
        if bin_oi_usd is not None:
            oi_usd["binance"] = bin_oi_usd
        elif bin_oi_base is not None:
            oi_unconvertible.append("binance")

        async def _fetch_one_secondary(name: str) -> tuple[str, str, Any]:
            ccxt_sym, status = await self._secondary_listing(name, bin_sym)
            if ccxt_sym is None:
                return name, status, None
            res = await asyncio.gather(
                self._fetch_secondary_funding(name, ccxt_sym),
                self._fetch_secondary_oi(name, ccxt_sym),
                self._fetch_secondary_ticker(name, ccxt_sym),
            )
            return name, status, res

        secondary_results = await asyncio.gather(
            *(_fetch_one_secondary(name) for name in self._secondary_exchange_ids),
            return_exceptions=True,
        )

        for item in secondary_results:
            if isinstance(item, BaseException):
                LOG.warning("cross_exchange_secondary_batch_failed | error=%s", item)
                continue
            name, status, res = item
            listed[name] = status
            if res is None:
                continue
            f_r, oi_r, t_r = res
            fr = finite_float_or_none(f_r.get("fundingRate"))
            if fr is not None:
                funding[name] = fr
                intervals[name] = finite_float_or_none(f_r.get("interval_hours"))
            mp = finite_float_or_none(t_r.get("mark_price"))
            if mp is not None and mp > 0:
                mark_price[name] = mp
            lp = finite_float_or_none(t_r.get("last_price"))
            if lp is not None and lp > 0:
                last_price[name] = lp

            # OI → USD notional. Prefer the venue's own USD figure; otherwise
            # convert base-coin OI with that venue's own price. A venue we cannot
            # convert contributes NOTHING and marks the total partial — it is
            # never silently dropped, and never fabricated.
            venue_usd = finite_float_or_none(oi_r.get("oi_usd"))
            venue_base = finite_float_or_none(oi_r.get("oi_base"))
            if venue_base is not None and venue_base > 0:
                oi_base[name] = venue_base
            if venue_usd is not None and venue_usd > 0:
                oi_usd[name] = venue_usd
            elif venue_base is not None and venue_base > 0:
                px = mark_price.get(name) or last_price.get(name)
                if px:
                    oi_usd[name] = venue_base * px
                else:
                    oi_unconvertible.append(name)

        normalized, unknown_interval = normalized_funding_map(funding, intervals)
        funding_spread, consensus = funding_consensus_from_normalized(normalized)

        oi_total = round(sum(oi_usd.values()), 0) if oi_usd else None
        # Partial = some venue that lists this symbol has OI we could not express
        # in USD, or a venue's availability is unknown. The card must not call a
        # single-venue proxy a "total".
        oi_total_partial = bool(oi_unconvertible) or any(
            status == "unknown" for status in listed.values()
        )

        price_div = price_divergence_from_map(mark_price)
        basis: str | None = "mark"
        if price_div is None:
            price_div = price_divergence_from_map(last_price)
            basis = "last" if price_div is not None else None

        return {
            "symbol": bin_sym,
            "fetched_at_ms": clock.now_ms(),
            "funding": funding,
            "funding_interval_hours": intervals,
            "funding_8h": normalized,
            "funding_unknown_interval": unknown_interval,
            "oi_usd": oi_usd,
            "oi_base": oi_base,
            "oi_venues": sorted(oi_usd),
            "oi_total_partial": oi_total_partial,
            "mark_price": mark_price,
            "last_price": last_price,
            "listed": listed,
            "funding_spread": funding_spread,
            "funding_consensus": consensus,
            "oi_total": oi_total,
            "price_divergence_pct": price_div,
            "price_divergence_basis": basis,
        }


# --- merged from market/book_parsers.py ---



def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def depth_imbalance_from_levels(
    bids: list[Any] | tuple[Any, ...] | None,
    asks: list[Any] | tuple[Any, ...] | None,
    *,
    top_n: int = 20,
) -> float | None:
    """Depth imbalance from top-N book levels using notional (price × qty)."""
    bid_notional = 0.0
    ask_notional = 0.0
    for row in (bids or [])[:top_n]:
        try:
            bid_notional += float(row[0]) * float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    for row in (asks or [])[:top_n]:
        try:
            ask_notional += float(row[0]) * float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    return depth_imbalance_from_book(bid_qty=bid_notional, ask_qty=ask_notional, delta_ratio=None)


def depth_imbalance_from_book(
    *, bid_qty: float | None, ask_qty: float | None, delta_ratio: float | None
) -> float | None:
    """Return top-of-book depth imbalance, falling back to signed trade flow."""
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total = bid_qty + ask_qty
        if total > 0.0:
            return round(_clamp((bid_qty - ask_qty) / total), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


def microprice_bias_from_book(
    *,
    bid: float | None,
    ask: float | None,
    bid_qty: float | None = None,
    ask_qty: float | None = None,
    delta_ratio: float | None,
) -> float | None:
    """Return signed microprice bias from L1 book, falling back to trade flow."""
    if bid is None or ask is None or bid <= 0 or (ask <= 0):
        return None
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0 or spread <= 0:
        return None
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total_qty = bid_qty + ask_qty
        if total_qty > 0.0:
            microprice = (ask * bid_qty + bid * ask_qty) / total_qty
            half_spread = spread / 2.0
            if half_spread > 0.0:
                return round(_clamp((microprice - mid) / half_spread), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)

# --- merged from market/depth_walls.py ---

from dataclasses import dataclass

_TOP_BOOK_WALL_LEVELS = 5


@dataclass(frozen=True, slots=True)
class WallCluster:
    price_center: float
    total_notional: float
    significance_pct: float
    level_count: int
    side: str
    distance_pct: float
    book_depth_pctile: float = 0.0


def _book_depth_percentile(notional: float, book_notionals: list[float]) -> float:
    """Relative significance as percentile rank within visible book depth."""
    if notional <= 0 or not book_notionals:
        return 0.0
    pool = sorted(n for n in book_notionals if n > 0)
    if not pool:
        return 0.0
    below = sum(1 for n in pool if n <= notional)
    return round(100.0 * below / len(pool), 1)


def detect_wall_clusters(
    levels: list[tuple[float, float]],
    *,
    current_price: float,
    daily_volume: float,
    side: str,
    cluster_tolerance_pct: float = 0.3,
    min_significance_pct: float = 0.5,
    min_book_depth_pctile: float = 85.0,
) -> list[WallCluster]:
    """Group adjacent book levels into wall clusters ranked by distance from price."""
    if current_price <= 0 or not levels:
        return []
    tol = current_price * cluster_tolerance_pct / 100.0
    sorted_levels = sorted(
        ((float(p), float(q)) for p, q in levels if float(p) > 0 and float(q) > 0),
        key=lambda x: x[0],
    )
    level_notionals = [p * q for p, q in sorted_levels]
    clusters: list[WallCluster] = []
    group: list[tuple[float, float]] = []
    anchor = 0.0

    def _flush() -> None:
        if not group:
            return
        total = sum(p * q for p, q in group)
        qty_sum = sum(q for _p, q in group)
        center = sum(p * q for p, q in group) / max(qty_sum, 1e-12)
        sig = (total / daily_volume * 100.0) if daily_volume > 0 else 0.0
        depth_pctile = _book_depth_percentile(total, level_notionals)
        dist = abs(center - current_price) / current_price * 100.0
        if sig >= min_significance_pct or depth_pctile >= min_book_depth_pctile:
            clusters.append(
                WallCluster(
                    price_center=round(center, 6),
                    total_notional=round(total, 2),
                    significance_pct=round(sig, 3),
                    level_count=len(group),
                    side=side,
                    distance_pct=round(dist, 3),
                    book_depth_pctile=depth_pctile,
                )
            )

    for price, qty in sorted_levels:
        if not group:
            group = [(price, qty)]
            anchor = price
            continue
        if abs(price - anchor) <= tol:
            group.append((price, qty))
        else:
            _flush()
            group = [(price, qty)]
            anchor = price
    _flush()
    return sorted(clusters, key=lambda c: c.distance_pct)


def depth_imbalance_by_zone(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    current_price: float,
    zones_pct: list[float] | None = None,
) -> dict[str, float]:
    """Proximity-weighted imbalance (-1..1) within each distance band from mid."""
    if current_price <= 0:
        return {}
    zones = zones_pct or [0.5, 1.0, 2.0, 5.0]
    out: dict[str, float] = {}
    for z in zones:
        band = current_price * z / 100.0
        lo = current_price - band
        hi = current_price + band
        decay_k = 2.0 / max(z, 0.001)
        bid_n = sum(
            p * q * math.exp(-decay_k * abs(current_price - p) / current_price * 100)
            for p, q in bids if lo <= p <= current_price
        )
        ask_n = sum(
            p * q * math.exp(-decay_k * abs(p - current_price) / current_price * 100)
            for p, q in asks if current_price <= p <= hi
        )
        total = bid_n + ask_n
        key = f"imb_{z:g}pct"
        out[key] = round((bid_n - ask_n) / total, 4) if total > 0 else 0.0
    return out


def top_depth_walls(
    levels: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> list[dict[str, float]]:
    """Top bid/ask levels ranked by notional (price × qty)."""
    ranked = sorted(
        (
            {
                "price": float(price),
                "qty": float(qty),
                "notional_usd": round(float(price) * float(qty), 2),
            }
            for price, qty in levels
            if float(price) > 0 and float(qty) > 0
        ),
        key=lambda row: row["notional_usd"],
        reverse=True,
    )
    return ranked[: max(1, int(top_n))]


def normalize_depth_levels(
    raw: Any,
    *,
    side: str = "",
) -> list[tuple[float, float]]:
    """Accept ccxt [[p,q],…] or list of {price, qty} dicts.

    When *side* is ``"bid"`` the result is sorted price-descending (best
    bid first).  When ``"ask"`` — price-ascending (best ask first).
    Without *side* the original order is preserved (CCXT default is
    already correct).
    """
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((float(item[0]), float(item[1])))
        elif isinstance(item, dict):
            p = item.get("price")
            q = item.get("qty")
            if p is not None and q is not None:
                out.append((float(p), float(q)))
    if side == "bid":
        out.sort(key=lambda x: x[0], reverse=True)
    elif side == "ask":
        out.sort(key=lambda x: x[0])
    return out


def depth_snapshot_from_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any]:
    """Build hunt depth snapshot with ranked walls.

    Best bid/ask are derived from price-sorted data (bid descending, ask
    ascending) — independent of the notional-ranked ``top_depth_walls``
    output used for wall display.
    """
    if not bids or not asks:
        return {
            "bid_price": None,
            "ask_price": None,
            "bid_qty": None,
            "ask_qty": None,
            "bid_levels": [],
            "ask_levels": [],
        }
    best_bid = max(p for p, _q in bids)
    best_ask = min(p for p, _q in asks)
    return {
        "bid_price": best_bid,
        "ask_price": best_ask,
        "bid_qty": round(sum(q for _p, q in bids), 4),
        "ask_qty": round(sum(q for _p, q in asks), 4),
        "bid_levels": top_depth_walls(bids, top_n=top_n),
        "ask_levels": top_depth_walls(asks, top_n=top_n),
    }


def aggregate_cross_exchange_walls(
    per_exchange: dict[str, dict[str, Any]],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any]:
    """Merge venue depth snapshots — aggregate same-price buckets across venues."""
    bid_pool: list[dict[str, Any]] = []
    ask_pool: list[dict[str, Any]] = []
    venues: list[str] = []
    for ex, snap in per_exchange.items():
        if not isinstance(snap, dict) or snap.get("bid_price") is None:
            continue
        venues.append(ex)
        for side, pool in (("bid", bid_pool), ("ask", ask_pool)):
            key = f"{side}_levels"
            for lvl in snap.get(key) or []:
                if isinstance(lvl, dict):
                    row = dict(lvl)
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    row = {
                        "price": float(lvl[0]),
                        "qty": float(lvl[1]),
                        "notional_usd": round(float(lvl[0]) * float(lvl[1]), 2),
                    }
                else:
                    continue
                row["exchange"] = ex
                pool.append(row)

    def _merge_pool(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[float, dict[str, Any]] = {}
        for row in pool:
            price = float(row.get("price") or 0)
            if price <= 0:
                continue
            bucket = round(price, 4)
            acc = buckets.setdefault(
                bucket,
                {
                    "price": bucket,
                    "qty": 0.0,
                    "notional_usd": 0.0,
                    "venues": set(),
                },
            )
            acc["qty"] += float(row.get("qty") or 0)
            acc["notional_usd"] += float(row.get("notional_usd") or 0)
            acc["venues"].add(str(row.get("exchange") or ""))
        merged: list[dict[str, Any]] = []
        for bucket, acc in buckets.items():
            merged.append(
                {
                    "price": bucket,
                    "qty": round(acc["qty"], 4),
                    "notional_usd": round(acc["notional_usd"], 2),
                    "venues": sorted(v for v in acc["venues"] if v),
                    "venue_count": len(acc["venues"]),
                }
            )
        return sorted(merged, key=lambda r: float(r.get("notional_usd") or 0), reverse=True)[:top_n]

    bid_levels = _merge_pool(bid_pool)
    ask_levels = _merge_pool(ask_pool)

    total_bid = sum(
        float(lvl.get("notional_usd") or 0)
        for snap in per_exchange.values()
        if isinstance(snap, dict)
        for lvl in (snap.get("bid_levels") or [])
        if isinstance(lvl, dict)
    )
    total_ask = sum(
        float(lvl.get("notional_usd") or 0)
        for snap in per_exchange.values()
        if isinstance(snap, dict)
        for lvl in (snap.get("ask_levels") or [])
        if isinstance(lvl, dict)
    )
    imb = None
    if total_bid + total_ask > 0:
        imb = round((total_bid - total_ask) / (total_bid + total_ask), 4)

    return {
        "venues": venues,
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "depth_imbalance": imb,
        "bid_depth_usd_total": round(total_bid, 2),
        "ask_depth_usd_total": round(total_ask, 2),
        "source": "cross_exchange",
    }


def wall_cluster_to_dict(cluster: WallCluster) -> dict[str, Any]:
    """Serialize a wall cluster for market/snapshot payloads."""
    return {
        "price_center": cluster.price_center,
        "total_notional": cluster.total_notional,
        "significance_pct": cluster.significance_pct,
        "level_count": cluster.level_count,
        "side": cluster.side,
        "distance_pct": cluster.distance_pct,
        "book_depth_pctile": cluster.book_depth_pctile,
    }




# --- liquidation heatmap (canonical impl in hunt_core.maps.liquidation) ---



