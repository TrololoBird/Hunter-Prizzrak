"""Factory for hunt CCXT market plane + CCXT helpers + OHLCV frames."""
from __future__ import annotations



import asyncio
import structlog
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import ccxt
import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro
import polars as pl

from hunt_core import clock
from hunt_core.market.network import is_socks_proxy
from hunt_core.market.symbols import to_ccxt_symbol

if TYPE_CHECKING:
    from hunt_core.market.client import HuntCcxtClient
    from hunt_core.market.spot import HuntCcxtSpotCompanion
    from hunt_core.market.streams import HuntCcxtStreams

LOG = structlog.get_logger("hunt_core.market.factory")
_PROBE_TIMEOUT_MS = 20_000

# --- DNS: aiohttp's defaults are actively hostile to a long-lived bot -------------
# CCXT builds its TCPConnector as `TCPConnector(ssl=..., loop=..., enable_cleanup_closed
# =True)` and passes neither of these, so both aiohttp defaults apply:
#
#   * ttl_dns_cache=10 — every host is re-resolved every 10 SECONDS. Across the REST
#     plane that is a continuous query stream, and it converts any brief resolver
#     outage straight into request failures instead of riding it out on cache.
#   * resolver=AsyncResolver — aiohttp picks this whenever `aiodns` is installed (it
#     is, transitively). AsyncResolver talks to c-ares directly and reads
#     /etc/resolv.conf, BYPASSING the OS resolver. On macOS the real DNS configuration
#     lives in the system configuration database, which /etc/resolv.conf mirrors only
#     partially — so under a VPN, c-ares can be querying a different (or dead) server
#     than the rest of the machine. ThreadedResolver calls getaddrinfo, i.e. exactly
#     what the OS does, VPN split-DNS included.
#
# 10 min matches Binance's own DNS TTL granularity closely enough while collapsing the
# query rate ~60×; failover still works because aiohttp re-resolves on connect errors.
_DNS_CACHE_TTL_S = 600

BINANCE_EXCHANGE_ID = "binance"
FUTURES_DEFAULT_TYPE = "future"
SPOT_DEFAULT_TYPE = "spot"

_KLINE_FRAME_SCHEMA: dict[str, Any] = {
    "time": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "close_time": pl.Datetime("us", "UTC"),
    "quote_volume": pl.Float64,
    "num_trades": pl.Int64,
    "taker_buy_base_volume": pl.Float64,
    "taker_buy_quote_volume": pl.Float64,
    "open_time": pl.Datetime("us", "UTC"),
}


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


class _CachedDnsSessionMixin:
    """Build CCXT's aiohttp session with a sane DNS cache and the OS resolver.

    CCXT hardcodes its TCPConnector kwargs inside ``open()``, and offers no hook to
    influence them. Passing a pre-built ``session`` through the constructor is not an
    option either: CCXT reads ``own_session = 'session' not in config``, so supplying
    one silently makes CCXT stop closing it and moves session teardown onto us.

    So we seed ``self.session`` here and let ``super().open()`` skip its own creation
    via its ``if self.own_session and self.session is None`` guard — ownership (and
    therefore ``close()``) stays exactly where it was.

    That guard is load-bearing and belongs to a third party, so it is pinned by a
    canary test: if a CCXT upgrade ever drops it, CCXT would build a second, default
    connector, overwrite ours, and the DNS fix would silently revert to the old
    behaviour — the exact failure mode that is invisible in production.
    """

    # Provided by the CCXT Exchange this mixes into (which is untyped for mypy).
    asyncio_loop: Any
    ssl_context: Any
    session: Any
    tcp_connector: Any
    throttler: Any
    own_session: bool
    cafile: Any
    verify: Any
    aiohttp_trust_env: bool

    def open(self) -> None:
        import ssl

        import aiohttp

        if self.own_session and self.session is None:
            if self.asyncio_loop is None:
                self.asyncio_loop = asyncio.get_running_loop()
                self.throttler.loop = self.asyncio_loop
            if self.ssl_context is None:
                self.ssl_context = (
                    ssl.create_default_context(cafile=self.cafile)
                    if self.verify
                    else self.verify
                )
            self.tcp_connector = aiohttp.TCPConnector(
                ssl=self.ssl_context,
                loop=self.asyncio_loop,
                enable_cleanup_closed=True,
                ttl_dns_cache=_DNS_CACHE_TTL_S,
                resolver=aiohttp.ThreadedResolver(),
            )
            self.session = aiohttp.ClientSession(
                loop=self.asyncio_loop,
                connector=self.tcp_connector,
                trust_env=self.aiohttp_trust_env,
            )
        super().open()  # type: ignore[misc]


class HuntAsyncBinanceFutures(_CachedDnsSessionMixin, ccxt_async.binance):  # type: ignore[misc]
    """ccxt.async_support binance with the DNS-cached session (see the mixin)."""


class HuntAsyncBinanceSpot(_CachedDnsSessionMixin, ccxt_async.binance):  # type: ignore[misc]
    """Spot-configured async binance with the DNS-cached session."""


def dns_cached_class(base: type) -> type:
    """Subclass ``base`` with the DNS-cached session mixin, memoised per base class.

    Used for the secondary venues, whose CCXT class is resolved by id at runtime.
    """
    cached = _DNS_CACHED_CLASSES.get(base)
    if cached is None:
        cached = type(f"HuntDnsCached{base.__name__}", (_CachedDnsSessionMixin, base), {})
        _DNS_CACHED_CLASSES[base] = cached
    return cached


_DNS_CACHED_CLASSES: dict[type, type] = {}


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


def interval_to_seconds(interval: str, exchange: Any) -> int:
    if exchange is None:
        raise TypeError("interval_to_seconds requires a CCXT exchange instance")
    parse_tf = getattr(exchange, "parse_timeframe", None)
    if not callable(parse_tf):
        raise TypeError(f"{getattr(exchange, 'id', 'exchange')}: parse_timeframe is not available")
    return int(parse_tf(interval))


def _close_time_ms(open_ms: int, interval: str, exchange: Any) -> int:
    step = interval_to_seconds(interval, exchange) * 1000
    return open_ms + step - 1


def ccxt_ohlcv_to_frame(
    rows: list[list[Any]],
    interval: str,
    *,
    exchange: Any,
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    built: list[dict[str, Any]] = []
    for row in rows:
        if not row or len(row) < 6:
            continue
        try:
            open_ms = int(row[0])
            o, h, low, c, v = (
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        except (TypeError, ValueError, IndexError):
            continue
        if open_ms <= 0 or c <= 0:
            continue
        # Full-fidelity rows (raw fapi klines / extended WS capture) carry
        # [6]=closeTime [7]=quoteVolume [8]=numTrades [9]=takerBuyBase
        # [10]=takerBuyQuote. ccxt's standard 6-element OHLCV drops them, which
        # zeroed taker_buy_base_volume and silently degenerated the orderflow
        # delta features (delta_ratio→0, bar delta→−volume). Zero-fill remains
        # only for genuinely 6-element rows.
        close_ms = _close_time_ms(open_ms, interval, exchange)
        quote_v = 0.0
        trades = 0
        taker_base = 0.0
        taker_quote = 0.0
        if len(row) >= 11:
            try:
                raw_close = int(row[6])
                if raw_close > open_ms:
                    close_ms = raw_close
                quote_v = float(row[7] or 0.0)
                trades = int(row[8] or 0)
                taker_base = float(row[9] or 0.0)
                taker_quote = float(row[10] or 0.0)
            except (TypeError, ValueError, IndexError):
                quote_v, trades, taker_base, taker_quote = 0.0, 0, 0.0, 0.0
        built.append(
            {
                "time": open_ms,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
                "close_time": close_ms,
                "quote_volume": quote_v,
                "num_trades": trades,
                "taker_buy_base_volume": taker_base,
                "taker_buy_quote_volume": taker_quote,
            }
        )
    if not built:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    frame = pl.DataFrame(built)
    return frame.with_columns(
        pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC").alias("time"),
        pl.from_epoch(pl.col("close_time"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("close_time"),
        pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC").alias("open_time"),
    )


def _ohlcv_frame_has_incomplete_tail(
    df: pl.DataFrame,
    timeframe: str,
    *,
    exchange: Any,
) -> bool:
    if df.is_empty():
        return False
    if "close_time" in df.columns:
        last_close = df["close_time"].tail(1).item()
        if isinstance(last_close, datetime):
            return clock.now_utc() <= last_close
    timeframe_seconds = interval_to_seconds(timeframe, exchange)
    last_open = df["time"].tail(1).item()
    if not isinstance(last_open, datetime):
        return False
    return clock.now_utc() < last_open + timedelta(seconds=timeframe_seconds)


def _drop_incomplete_ohlcv_tail(
    df: pl.DataFrame,
    timeframe: str,
    *,
    exchange: Any,
) -> pl.DataFrame:
    if df.is_empty():
        return df
    if "close_time" in df.columns:
        now = clock.now_utc()
        closed = df.filter(pl.col("close_time") < pl.lit(now))
        if closed.height != df.height:
            return closed
    if _ohlcv_frame_has_incomplete_tail(df, timeframe, exchange=exchange):
        return df.head(df.height - 1)
    return df


def drop_unclosed_ohlcv_tail(
    rows: list[list[Any]],
    timeframe: str,
    *,
    exchange: Any,
    now_ms: int | None = None,
) -> list[list[Any]]:
    """Drop the still-forming last kline from a raw list-path OHLCV window.

    The list path (``fetch_ohlcv_list`` / ``fetch_ohlcv_list_cached``) bypasses
    ``finalize_kline_frame``'s incomplete-tail drop, so Binance's in-progress
    candle reaches consumers and repaints (closed-bar discipline violation —
    a mid-bar close beyond a level would count as a confirmed break, then the
    bar can close back). Every detector-facing list consumer must pass its
    window through this before analysis.

    ``rows`` are ascending ``[open_ms, o, h, l, c, v, ...]``; a bar is closed
    once ``open_ms + timeframe_duration <= now``.
    """
    if not rows:
        return rows
    step_ms = interval_to_seconds(timeframe, exchange) * 1000
    now = int(clock.now_utc().timestamp() * 1000) if now_ms is None else int(now_ms)
    if int(rows[-1][0]) + step_ms > now:
        return rows[:-1]
    return rows


_RESAMPLE_FROM_1M_INTERVALS = frozenset({"5m", "15m", "1h", "4h", "1d"})


def min_1m_bars_for_resample(interval: str, target_limit: int, *, exchange: Any) -> int:
    """How many 1m bars are needed to derive ``target_limit`` bars at ``interval``."""
    if interval == "1m":
        return max(1, int(target_limit))
    step_s = interval_to_seconds(interval, exchange)
    bars_per_bucket = max(1, step_s // 60)
    return min(1500, int(target_limit) * bars_per_bucket + bars_per_bucket)


def resample_ohlcv_from_1m(
    df_1m: pl.DataFrame,
    interval: str,
    *,
    exchange: Any,
    limit: int | None = None,
) -> pl.DataFrame:
    """U3: derive higher TF OHLCV from 1m via Polars ``group_by_dynamic`` (MTF-consistent)."""
    if df_1m.is_empty() or interval == "1m" or interval not in _RESAMPLE_FROM_1M_INTERVALS:
        return df_1m
    work = df_1m
    if "open_time" not in work.columns and "time" in work.columns:
        work = work.with_columns(pl.col("time").alias("open_time"))
    if "open_time" not in work.columns:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    if "close_time" in work.columns:
        now = clock.now_utc()
        work = work.filter(pl.col("close_time") < pl.lit(now))
    if work.height < 2:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    step_s = interval_to_seconds(interval, exchange)
    every = f"{step_s}s"
    agg_exprs: list[pl.Expr] = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    if "quote_volume" in work.columns:
        agg_exprs.append(pl.col("quote_volume").sum().alias("quote_volume"))
    else:
        agg_exprs.append(pl.lit(0.0).alias("quote_volume"))
    if "num_trades" in work.columns:
        agg_exprs.append(pl.col("num_trades").sum().alias("num_trades"))
    else:
        agg_exprs.append(pl.lit(0).alias("num_trades"))
    if "taker_buy_base_volume" in work.columns:
        agg_exprs.append(pl.col("taker_buy_base_volume").sum().alias("taker_buy_base_volume"))
    else:
        agg_exprs.append(pl.lit(0.0).alias("taker_buy_base_volume"))
    if "taker_buy_quote_volume" in work.columns:
        agg_exprs.append(pl.col("taker_buy_quote_volume").sum().alias("taker_buy_quote_volume"))
    else:
        agg_exprs.append(pl.lit(0.0).alias("taker_buy_quote_volume"))
    resampled = (
        work.sort("open_time")
        .group_by_dynamic("open_time", every=every, closed="left")
        .agg(agg_exprs)
    )
    if resampled.is_empty():
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    resampled = resampled.with_columns(
        pl.col("open_time").dt.epoch(time_unit="ms").alias("time"),
        (
            pl.col("open_time").dt.epoch(time_unit="ms")
            + pl.lit(step_s * 1000 - 1)
        ).alias("close_time"),
    )
    if limit is not None and resampled.height > int(limit):
        resampled = resampled.tail(int(limit))
    return finalize_kline_frame(resampled, interval, exchange=exchange)


def finalize_kline_frame(frame: pl.DataFrame, interval: str, *, exchange: Any) -> pl.DataFrame:
    return _drop_incomplete_ohlcv_tail(frame, interval, exchange=exchange)


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
