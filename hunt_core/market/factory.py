"""Factory for hunt CCXT market plane + CCXT helpers + OHLCV frames."""
from __future__ import annotations



import asyncio
import structlog
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import ccxt
import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro

from hunt_core.market.network import is_socks_proxy
from hunt_core.market.symbols import to_ccxt_symbol

if TYPE_CHECKING:
    from hunt_core.market.client import HuntCcxtClient
    from hunt_core.market.spot import HuntCcxtSpotCompanion
    from hunt_core.market.streams import HuntCcxtStreams

LOG = structlog.get_logger("hunt_core.market.factory")
_PROBE_TIMEOUT_MS = 20_000

# The DNS-cached ccxt session moved to hunt_core/engine/dns.py (the engine owns its transport
# concerns and must not depend on this doomed market/ layer). factory.py uses both below; other
# consumers (telegram.py, the canary test) import straight from engine.dns.
from hunt_core.engine.dns import _CachedDnsSessionMixin, dns_cached_class  # noqa: E402

BINANCE_EXCHANGE_ID = "binance"
FUTURES_DEFAULT_TYPE = "future"
SPOT_DEFAULT_TYPE = "spot"


def build_network_config(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 45_000,
    default_type: str = FUTURES_DEFAULT_TYPE,
    pro: bool = False,
) -> dict[str, Any]:
    """Base kwargs for ``ccxt.binance`` / ``ccxt.pro.binance`` (public endpoints only)."""
    options: dict[str, Any] = {
        "defaultType": default_type,
        "adjustForTimeDifference": True,
        # ccxt.pro seeds watch_order_book with a REST /fapi/v1/depth snapshot.
        # Its default watchOrderBookLimit=1000 costs weight 20 PER SYMBOL and
        # re-fires for every symbol on each WS reconnect — 135 symbols × 20 =
        # 2700 weight in seconds → 429 → 418 IP ban. We only consume the top
        # _TOP_BOOK_DEPTH_LEVELS (20), and limit≤50 costs weight 2, so cap the
        # snapshot at 20 (10× less weight, identical usable data).
        "watchOrderBookLimit": 20,
        **(
            {"fetchMarkets": ["linear"]}
            if default_type == FUTURES_DEFAULT_TYPE
            else {}
        ),
    }
    if pro:
        options["tradesLimit"] = 500
        options["OHLCVLimit"] = 200
    config: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": timeout_ms,
        "options": options,
    }
    if pro:
        # CCXT Pro manual: delta-only updates reduce hot-path work in watch loops.
        config["newUpdates"] = True
        config["streaming"] = {
            # Binance recommendation: client-side keepalive every 3min.
            # 30s was too aggressive — event-loop saturation during startup
            # caused ping/pong misses, making CCXT Pro self-close every ~79s.
            "keepAlive": 180_000,
            "maxPingPongMisses": 3.0,
        }
    if proxy_url:
        config["aiohttp_proxy"] = proxy_url
        if is_socks_proxy(proxy_url):
            config["wsSocksProxy"] = proxy_url
        else:
            config["wssProxy"] = proxy_url
    if trust_env:
        config["aiohttp_trust_env"] = True
    return config


class HuntAsyncBinanceFutures(_CachedDnsSessionMixin, ccxt_async.binance):  # type: ignore[misc]
    """ccxt.async_support binance with the DNS-cached session (see the mixin)."""


class HuntAsyncBinanceSpot(_CachedDnsSessionMixin, ccxt_async.binance):  # type: ignore[misc]
    """Spot-configured async binance with the DNS-cached session."""


def create_async_binance_future(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 45_000,
) -> ccxt_async.binance:
    return HuntAsyncBinanceFutures(
        build_network_config(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
            default_type=FUTURES_DEFAULT_TYPE,
        )
    )


def extend_parsed_ws_kline(stored_row: list[Any], kline: dict[str, Any]) -> None:
    """Extend ccxt's parsed 6-element WS kline row with the raw payload fields.

    Binance's kline payload carries T (close time), q (quote volume), n (num
    trades), V (taker buy base) and Q (taker buy quote); ccxt's parser keeps
    only [t,o,h,l,c,v]. taker_buy_base_volume is load-bearing for orderflow
    features (delta = 2·taker_buy − volume), so the row is extended IN PLACE to
    the same 11-element layout as raw REST klines (see ccxt_ohlcv_to_frame).
    """
    try:
        ext = [
            int(kline.get("T") or 0),
            float(kline.get("q") or 0.0),
            int(kline.get("n") or 0),
            float(kline.get("V") or 0.0),
            float(kline.get("Q") or 0.0),
        ]
    except (TypeError, ValueError):
        return
    if len(stored_row) == 6:
        stored_row.extend(ext)
    elif len(stored_row) >= 11:
        stored_row[6:11] = ext


class HuntProBinanceFutures(_CachedDnsSessionMixin, ccxtpro.binance):  # type: ignore[misc]
    """ccxt.pro binance that retains the kline fields the stock parser drops.

    Also DNS-cached: ccxt.pro derives from the async Exchange and uses the same
    aiohttp session for its REST half and for the WS handshake.
    """

    def handle_ohlcv(self, client: Any, message: Any) -> None:
        super().handle_ohlcv(client, message)
        try:
            if not isinstance(message, dict) or message.get("e") != "kline":
                return  # mark/index klines carry no taker fields
            kline = message.get("k") or {}
            t = kline.get("t")
            market_id = kline.get("s")
            tf = self.find_timeframe(kline.get("i"))
            if t is None or not market_id or not tf:
                return
            symbol = self.safe_symbol(market_id, None, None, "contract")
            stored = (self.ohlcvs.get(symbol) or {}).get(tf)
            if stored is None or not len(stored):
                return
            # Look the row up via the cache's hashmap, not just stored[-1]: a
            # reordered final update for candle T arriving after T+1's first
            # update would otherwise skip the extension, pairing final OHLC
            # with stale intrabar taker/quote values.
            row_obj: Any | None = None
            hashmap = getattr(stored, "hashmap", None)
            if isinstance(hashmap, dict):
                row_obj = hashmap.get(t)
            if row_obj is None:
                last = stored[-1]
                if isinstance(last, list) and last and last[0] == t:
                    row_obj = last
            if isinstance(row_obj, list) and row_obj and row_obj[0] == t:
                extend_parsed_ws_kline(row_obj, kline)
        except Exception:  # noqa: BLE001 — extension is best-effort, never break the stream
            LOG.debug("ws_kline_extend_failed", exc_info=True)


def create_pro_binance_future(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 45_000,
) -> ccxtpro.binance:
    return HuntProBinanceFutures(
        build_network_config(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
            default_type=FUTURES_DEFAULT_TYPE,
            pro=True,
        )
    )


def create_async_binance_spot(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 12_000,
) -> ccxt_async.binance:
    return HuntAsyncBinanceSpot(
        build_network_config(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
            default_type=SPOT_DEFAULT_TYPE,
        )
    )


def create_async_secondary_swap(
    exchange_id: str,
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 45_000,
) -> ccxt_async.Exchange:
    cls = dns_cached_class(getattr(ccxt_async, exchange_id))
    return cls(
        build_network_config(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
            default_type="swap",
        )
    )


def create_pro_secondary_swap(
    exchange_id: str,
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 45_000,
) -> Any:
    cls = dns_cached_class(getattr(ccxtpro, exchange_id))
    config = build_network_config(
        proxy_url=proxy_url,
        trust_env=trust_env,
        timeout_ms=timeout_ms,
        default_type="swap",
        pro=True,
    )
    # Bybit requires ping every 20 s; OKX every 30 s — both drop connection at ~60 s idle.
    # The primary Binance client uses 180 s (Binance is tolerant; shorter caused misses).
    # Secondary exchanges need a shorter keepAlive to survive.
    config["streaming"]["keepAlive"] = 20_000
    return cls(config)


def create_sync_binance_future(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_ms: int = 45_000,
) -> ccxt.binance:
    config = build_network_config(
        proxy_url=proxy_url,
        trust_env=trust_env,
        timeout_ms=timeout_ms,
        default_type=FUTURES_DEFAULT_TYPE,
    )
    # Sync ccxt uses `requests`, not aiohttp — the aiohttp_* keys are inert here.
    config.pop("aiohttp_proxy", None)
    config.pop("aiohttp_trust_env", None)
    config.pop("wsSocksProxy", None)
    config.pop("wssProxy", None)
    ex = ccxt.binance(config)
    # Route the sync client through the same proxy: without this, offline scripts
    # (calibration/reconcile/tg_backtest) silently egress DIRECT even when a proxy
    # was supplied — the aiohttp_* keys above never applied to the requests session.
    if proxy_url:
        if is_socks_proxy(proxy_url):
            ex.socksProxy = proxy_url
        else:
            ex.httpsProxy = proxy_url
    return ex


def close_exchange_sync(ex: Any, *, label: str) -> None:
    close_fn = getattr(ex, "close", None)
    if not callable(close_fn):
        return
    try:
        close_fn()
    except Exception as exc:
        LOG.warning("exchange_close_failed | label=%s error=%s", label, exc)


async def close_exchange_async(ex: Any, *, label: str) -> None:
    close_fn = getattr(ex, "close", None)
    if not callable(close_fn):
        return
    try:
        result = close_fn()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        LOG.warning("exchange_close_failed | label=%s error=%s", label, exc)


def fetch_klines_sync(
    symbol: str,
    interval: str,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int = 1500,
    max_pages: int = 10,
    proxy_url: str | None = None,
    trust_env: bool = True,
) -> list[list[Any]]:
    """Paginated sync OHLCV for offline scripts (calibration, reconcile, tg_backtest)."""
    ex = create_sync_binance_future(proxy_url=proxy_url, trust_env=trust_env)
    try:
        ex.load_markets()
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        out: list[list[Any]] = []
        cursor = since_ms
        page_limit = min(1500, max(1, int(limit)))
        for _ in range(max_pages):
            batch = ex.fetch_ohlcv(
                ccxt_sym,
                interval,
                since=cursor,
                limit=page_limit if cursor is not None else min(1500, max(1, int(limit))),
            )
            if not batch:
                break
            if until_ms is not None:
                batch = [row for row in batch if int(row[0]) <= until_ms]
            out.extend(batch)
            if until_ms is not None and batch and int(batch[-1][0]) >= until_ms:
                break
            if len(batch) < page_limit:
                break
            if cursor is None:
                break
            cursor = int(batch[-1][0]) + 1
        return out
    finally:
        close_exchange_sync(ex, label="sync_binance_future")


async def fetch_klines_async(
    client: "HuntCcxtClient",
    symbol: str,
    interval: str,
    *,
    since_ms: int,
    limit: int = 1500,
) -> list[list[Any]]:
    return await client.fetch_ohlcv_list(
        symbol,
        interval,
        since=max(0, int(since_ms)),
        limit=min(1500, max(1, int(limit))),
    )


# --- OHLCV transforms extracted to hunt_core/toolkit/ohlcv.py (ADR-0004 S0) ---
from hunt_core.toolkit.ohlcv import (  # noqa: F401,E402
    ccxt_ohlcv_to_frame,
    drop_unclosed_ohlcv_tail,
    finalize_kline_frame,
    interval_to_seconds,
    min_1m_bars_for_resample,
    resample_ohlcv_from_1m,
)


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class HuntMarketPlane:
    client: "HuntCcxtClient"
    streams: "HuntCcxtStreams"
    spot: "HuntCcxtSpotCompanion"

    async def aclose(self) -> None:
        """Release CCXT REST + Pro WS resources (probes, one-shot scripts)."""
        try:
            await self.streams.stop()
        except Exception:
            LOG.debug("market_plane_streams_stop_failed", exc_info=True)
        try:
            await self.client.close()
        except Exception:
            LOG.debug("market_plane_client_close_failed", exc_info=True)
        try:
            await self.spot.close()
        except Exception:
            LOG.debug("market_plane_spot_close_failed", exc_info=True)
        # Yield so aiohttp/CCXT Pro sessions finish teardown (avoids Unclosed client session).
        await asyncio.sleep(1.5)

    async def close(self) -> None:
        """Alias for ``aclose()`` — matches probe/smoke call sites."""
        await self.aclose()


async def _create_plane_once(
    *,
    proxy_url: str | None = None,
    trust_env: bool,
) -> HuntMarketPlane:
    from hunt_core.market.client import HuntCcxtClient
    from hunt_core.market.spot import HuntCcxtSpotCompanion
    from hunt_core.market.streams import HuntCcxtStreams

    client = HuntCcxtClient(
        proxy_url=proxy_url,
        trust_env=trust_env,
        timeout_ms=_PROBE_TIMEOUT_MS,
    )
    try:
        await client.load_markets()
    except Exception:
        await client.close()
        raise
    streams = HuntCcxtStreams(client=client)
    client.set_streams_reconnect(streams._reconnect_binance_pro)
    return HuntMarketPlane(
        client=client,
        streams=streams,
        spot=HuntCcxtSpotCompanion(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=12_000,
        ),
    )


async def create_hunt_market_plane(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
) -> HuntMarketPlane:
    return await _create_plane_once(proxy_url=proxy_url, trust_env=trust_env)


async def create_hunt_market_plane_from_settings(settings: Any) -> HuntMarketPlane:
    """Create the market plane on a DIRECT Binance connection.

    Branch A: the rotating proxy pool / discovery / failover was removed after an
    empirical probe showed Binance USDⓈ-M is reachable directly and stably from the
    deploy host. No proxy is attached; a transient DNS/network blip at startup is
    handled by the caller's bounded retry in the watch loop.
    """
    return await _create_plane_once(proxy_url=None, trust_env=False)
