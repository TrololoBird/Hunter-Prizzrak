"""REST/WS data ingest (P2 — no detect/analysis imports)."""
from __future__ import annotations

import asyncio
import inspect
import re
import structlog
import time
from datetime import datetime
from typing import Any, Literal, TYPE_CHECKING

import ccxt
import polars as pl

if TYPE_CHECKING:
    from hunt_core.market.client import HuntCcxtClient

LOG = structlog.get_logger("hunt_core.data.collect")
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.errors import DEFENSIVE_EXC, system_breakers
from hunt_core.market import HuntCcxtClient, HuntCcxtStreams
from hunt_core.market.ccxt_guard import is_ccxt_rate_limited
from hunt_core.market.ccxt_rest import RestBanSkip

def kline_limits(minimums: dict[str, int], symbol: str = "") -> dict[str, int]:
    """Hunt watch pulls deeper history than default bot warmup (max 1500 bars)."""
    limits: dict[str, int] = {
        "1m": min(1500, max(1440, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
        "5m": kline_fetch_limit(int(minimums.get("5m", 300)), "5m"),
        "15m": kline_fetch_limit(int(minimums.get("15m", 400)), "15m"),
        "1h": kline_fetch_limit(int(minimums.get("1h", 400)), "1h"),
        "4h": kline_fetch_limit(int(minimums.get("4h", 200)), "4h"),
        # 1d/1w must clear EMA200 warmup (~200 bars) or _prepare_frame drops every
        # row → empty frame → HTF snapshot falls to lite (no EMAs) → trend always
        # "neutral". Only 90 daily bars made the 1d/1w trend structurally dead for
        # every symbol. 260 daily / 220 weekly warms EMA200 for established symbols
        # (verified: 203 daily bars reproduce the real bear stack). Binance caps at
        # 1500 klines/request, so this is a single cheap REST call per TF.
        "1d": 260,
    }
    if symbol.upper() in PINNED_SYMBOLS:
        limits["1w"] = 220  # ~4y weekly — clears EMA200 warmup for MTF structure
    return limits


def probe_kline_limits(minimums: dict[str, int], symbol: str = "") -> dict[str, int]:
    """Shallow kline budget for dev delivery probes — meets minimums, no 1w."""
    limits = kline_limits(minimums, symbol)
    limits["1m"] = min(limits["1m"], max(360, int(minimums.get("5m", 200)) * 2))
    limits.pop("1w", None)
    return limits


async def safe_fetch(
    target: Any,
    *,
    context: str = "",
    client: HuntCcxtClient | None = None,
) -> Any:
    """Await REST via callable factory; 418/429 pause is handled inside client._rest_call."""
    is_factory = callable(target) and not inspect.iscoroutine(target)

    async def _invoke() -> Any:
        if is_factory:
            result = target()
            if asyncio.iscoroutine(result):
                return await result
            return result
        return await target

    try:
        return await _invoke()
    except ccxt.BadSymbol as exc:
        from hunt_core.data.symbol_blacklist import blacklist_symbol
        sym = _extract_symbol_from_context(context)
        if sym:
            blacklist_symbol(sym)
            LOG.warning(
                "safe_fetch_bad_symbol_blacklisted | symbol=%s context=%s error=%s",
                sym, context or type(target).__name__, exc,
            )
        LOG.warning(
            "safe_fetch_bad_symbol | context=%s error=%s",
            context or type(target).__name__, exc,
        )
        return None
    except RestBanSkip as exc:
        # Intentional skip during an active 418 ban (never hit Binance) — NOT a fetch failure,
        # so no breaker record and no ban re-record. Fall back to cached/WS data.
        LOG.debug(
            "safe_fetch_skipped_ip_ban | context=%s %s",
            context or type(target).__name__, exc,
        )
        return None
    except ccxt.BaseError as exc:
        system_breakers().rest.record_failure()
        if client is not None:
            client.rest_gate.record_error(exc, context=context or "safe_fetch")
        if is_ccxt_rate_limited(exc):
            LOG.warning(
                "safe_fetch_ccxt_rate_limited | context=%s error=%s",
                context or type(target).__name__,
                exc,
            )
            raise
        LOG.warning(
            "safe_fetch_ccxt_failed | context=%s error=%s",
            context or type(target).__name__,
            exc,
        )
        return None
    except DEFENSIVE_EXC as exc:
        system_breakers().rest.record_failure()
        LOG.warning(
            "safe_fetch_failed | context=%s error=%s",
            context or type(target).__name__,
            exc,
        )
        return None


_QUOTE_SUFFIXES = ("USDT", "USD", "BTC", "ETH")


def _extract_symbol_from_context(context: str) -> str | None:
    """Extract a raw exchange symbol from a safe_fetch context like 'klines.BTCUSDT.1m'.

    Only clean alphanumeric tokens qualify — a composite token like
    'FUNDING_RATE:BTCUSDT' or 'inwatch_klines' must never become a blacklist
    key that no universe symbol can ever match.
    """
    if not context:
        return None

    for token in re.split(r"[.:/\s]+", str(context).upper()):
        if not token or token in _QUOTE_SUFFIXES:
            continue
        if token.isalnum() and token.endswith(_QUOTE_SUFFIXES):
            return token
    return None



def _book_from_pack(pack: dict[str, Any]) -> dict[str, float | None]:
    depth = pack.get("book_depth")
    if isinstance(depth, dict) and depth.get("bid_price"):
        return depth
    ticker = pack.get("book_ticker")
    return ticker if isinstance(ticker, dict) else {}




def _overlay_ws_market(prepared: Any, ws_snap: dict[str, Any] | None) -> None:
    """Prefer live WS orderflow + mark/ap between REST polls (reports A7/A8)."""
    if not ws_snap:
        return
    ws_delta = ws_snap.get("agg_trade_delta_30s")
    if ws_delta is not None:
        prepared.agg_trade_delta_30s = float(ws_delta)
        prepared.orderflow_source = str(ws_snap.get("agg_trade_source") or "ws_nq")
    if ws_snap.get("funding_live") is not None:
        prepared.funding_rate = float(ws_snap["funding_live"])
    if ws_snap.get("mark_live") is not None:
        prepared.mark_price = float(ws_snap["mark_live"])
    if ws_snap.get("basis_bps_live") is not None:
        # basis_bps_live is in BASIS POINTS; basis_pct is in PERCENT, hence /100.
        bps = float(ws_snap["basis_bps_live"])
        prepared.basis_pct = bps / 100.0
        prepared.mark_index_spread_bps = bps
    # (removed: dead basis_ap_bps branch — WS never produced the key; see
    # features/snapshot.py._overlay_ws_market, audit G.)
    live_di = ws_snap.get("live_depth_imbalance")
    if live_di is not None and ws_snap.get("ws_connected"):
        prepared.depth_imbalance = float(live_di)
        prepared.depth_imbalance_source = "ws_book"
    live_mp = ws_snap.get("live_microprice_bias")
    if live_mp is not None and ws_snap.get("ws_connected"):
        prepared.microprice_bias = float(live_mp)
        prepared.microprice_bias_source = "ws_book"


async def fetch_rest_pack(
    client: HuntCcxtClient,
    symbol: str,
    *,
    tier: SnapshotTier = "full",
    ws_feed: HuntCcxtStreams | None = None,
) -> dict[str, Any]:
    """Fetch public REST enrichment; fast tier keeps dump-onset fields."""
    ws_snap = ws_feed.snapshot(symbol) if ws_feed is not None else None
    specs = rest_pack_specs(
        client,
        symbol,
        tier=tier,
        ws_orderflow_fresh=ws_orderflow_fresh(ws_snap),
    )
    results = await asyncio.gather(*(c for _, c in specs), return_exceptions=True)
    pack: dict[str, Any] = {}
    for (name, _), res in zip(specs, results, strict=True):
        pack[name] = None if isinstance(res, BaseException) else res
    depth = pack.get("book_depth")
    if not isinstance(depth, dict) or not depth.get("bid_price"):
        pack["book_ticker"] = await safe_fetch(client._fetch_book_ticker_rest_detail(symbol))
    try:
        pack["_rest_cache_ages"] = client.snapshot_rest_cache_ages(symbol)
    except Exception:
        pack["_rest_cache_ages"] = {}
    return pack


_fetch_rest_pack = fetch_rest_pack

# --- merged from data/rest_tiers.py ---



SnapshotTier = Literal["full", "fast", "hot", "probe_lite"]

PREMIUM_BATCH_TTL_S = 30.0
FUNDING_BATCH_TTL_S = 30.0
EXCHANGE_INFO_TTL_S = 3600.0
BTC_1H_TTL_S = 900.0

_FAST_FRESH_KLINES = frozenset({"1m", "5m"})
_FAST_CACHE_KLINES = ("15m", "1h", "4h", "1d")
FULL_KLINE_ORDER = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")


def _fetch_error_label(exc: BaseException) -> str:
    return f"fetch_failed:{type(exc).__name__}"


def ws_kline_frame_serves(
    df: Any,
    need: int,
    *,
    interval_ms: int = 60_000,
    max_close_age_s: float = 180.0,
) -> bool:
    """True when the WS-merged cached 1m frame can serve INSTEAD of REST.

    Push-first (ADR-0001 pillar 4): a REST klines call is weight never spent —
    but only when the cache is provably equivalent. Gates:
    - coverage: >= ``need`` bars;
    - continuity: the served tail has NO gaps (every open_time exactly
      ``interval_ms`` after the previous — a WS outage hole forces REST);
    - freshness: the last CLOSED bar's close_time is recent (stale WS → REST);
    - fidelity: taker fields are real, not legacy zero-fill (zeros would
      degenerate the orderflow delta — no-lookahead/quality guard).
    """
    try:
        if df is None or df.is_empty() or df.height < max(1, int(need)):
            return False
        tail = df.tail(max(1, int(need)))
        times = tail["time"].dt.epoch(time_unit="ms")
        diffs = times.diff().drop_nulls()
        if diffs.is_empty() or bool((diffs != interval_ms).any()):
            return False
        last_close = tail["close_time"].tail(1).item()
        if not isinstance(last_close, datetime):
            return False
        from hunt_core import clock

        if (clock.now_utc() - last_close).total_seconds() > max_close_age_s:
            return False
        if "taker_buy_base_volume" in tail.columns:
            vol_sum = float(tail["volume"].sum() or 0.0)
            taker_sum = float(tail["taker_buy_base_volume"].sum() or 0.0)
            if vol_sum > 0.0 and taker_sum <= 0.0:
                return False
        if {"volume", "num_trades"}.issubset(tail.columns):
            # Per-bar zero-fill detector: a REAL bar with volume>0 always has
            # trades>=1 (even an all-sell bar), so volume>0 & trades==0 can only
            # be a legacy zero-filled row — one such bar carries a degenerate
            # delta (−volume) into orderflow features. Aggregate checks miss it.
            zero_filled = tail.filter(
                (pl.col("volume") > 0.0) & (pl.col("num_trades") == 0)
            ).height
            if zero_filled > 0:
                return False
        return True
    except Exception:  # noqa: BLE001 — any doubt → REST
        return False


# HTF timeframes eligible for cache-first serve from the (disk-reloaded or
# REST-seeded) frame cache. Deliberately excludes 15m: 15m frames are WS-merged
# and go through the fidelity-checked 1m/WS path instead.
_HTF_CACHE_SERVE_TFS = frozenset({"1h", "4h", "1d", "1w"})


def htf_cache_frame_serves(
    df: Any,
    interval: str,
    need: int,
    *,
    exchange: Any,
    now_ms: int | None = None,
) -> bool:
    """True when a cached HTF frame can serve INSTEAD of a full REST re-fetch.

    The frame serves only when it is *current* — its newest closed bar is the
    latest one that can exist (``now - newest_close < one TF step``) — so a REST
    call could not return anything newer and skipping it is a pure weight win.
    This is what makes the disk-persisted HTF reload an incremental top-up: a
    restart re-fetches a TF over REST only once its bar actually rolls over,
    instead of re-downloading hundreds of unchanged bars per symbol. Gates:
    - coverage: >= ``need`` bars (structure analysis depth);
    - currency: newest closed bar is the latest possible one.
    """
    try:
        if df is None or exchange is None or df.is_empty() or df.height < max(1, int(need)):
            return False
        from hunt_core.data.frame_cache import _frame_newest_ms
        from hunt_core.market.factory import interval_to_seconds

        newest = _frame_newest_ms(df)
        if newest is None:
            return False
        if now_ms is None:
            from hunt_core import clock

            now_ms = int(clock.now_ms())
        step_ms = interval_to_seconds(interval, exchange) * 1000
        return now_ms - newest < step_ms
    except Exception:  # noqa: BLE001 — any doubt → REST
        return False


async def resolve_kline_map(
    client: HuntCcxtClient,
    symbol: str,
    limits: dict[str, int],
    *,
    tier: SnapshotTier,
    safe_fetch: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch klines with tier-aware budget.

    X8/U3: one fresh 1m REST pull + Polars resample for 5m→1d (all tiers).
    Weekly (1w) still uses a direct cached fetch when requested.

    Returns (kline_map, fetch_errors) — never silent None without a reason key.
    """
    kline_map: dict[str, Any] = {}
    fetch_errors: dict[str, str] = {}

    from hunt_core.data.frame_cache import get_frame_cache
    from hunt_core.market.factory import min_1m_bars_for_resample, resample_ohlcv_from_1m

    exchange = getattr(client, "_ex", None)
    if tier == "full":
        derive_names = tuple(
            n for n in FULL_KLINE_ORDER if n in limits and n not in {"1m", "1w"}
        )
        direct_fetch = tuple(n for n in ("1w",) if n in limits)
    else:
        derive_names = tuple(
            n for n in (*_FAST_FRESH_KLINES, *_FAST_CACHE_KLINES) if n in limits and n != "1m"
        )
        direct_fetch = ()
    need_1m = int(limits.get("1m", 500))
    if exchange is not None:
        for name in derive_names:
            need_1m = max(
                need_1m,
                min_1m_bars_for_resample(name, limits[name], exchange=exchange),
            )
        need_1m = min(1500, need_1m)

    # Push-first (ADR-0001 pillar 4): serve 1m from the WS-merged cache when it
    # is provably equivalent (coverage+continuity+freshness+fidelity) — the
    # whole 5m→1d map derives from this one frame, so a hit removes the largest
    # per-symbol REST weight sink. REST remains for cold start and gaps.
    res_1m: Any = None
    ws_df = get_frame_cache().get_kline_frame(symbol, "1m")
    if ws_df is not None and ws_kline_frame_serves(ws_df, need_1m):
        res_1m = ws_df.tail(need_1m)
    if res_1m is None:
        res_1m = await safe_fetch(
            lambda: client.fetch_klines_cached(symbol, "1m", limit=need_1m),
            context=f"klines.{symbol}.1m",
            client=client,
        )
    if res_1m is None:
        if ws_df is not None and not ws_df.is_empty():
            # Degraded fallback (REST outage): any WS history beats nothing.
            res_1m = ws_df
        else:
            fetch_errors["1m"] = "fetch_failed"

    df_1m = res_1m if isinstance(res_1m, pl.DataFrame) else None
    if df_1m is not None and not df_1m.is_empty():
        kline_map["1m"] = df_1m

    derived_ok = (
        df_1m is not None
        and not df_1m.is_empty()
        and exchange is not None
    )

    async def _fallback_kline(name: str) -> None:
        if name in _FAST_FRESH_KLINES:
            res = await safe_fetch(
                lambda n=name: client.fetch_klines(symbol, n, limit=limits[n]),
                context=f"klines.{symbol}.{name}",
                client=client,
            )
        else:
            cached = client.get_cached_klines(symbol, name, limit=limits[name])
            if cached is not None and not cached.is_empty():
                kline_map[name] = cached
                return
            # Cache-first HTF serve: a disk-reloaded (post-restart) or previously
            # seeded frame that already holds the latest closed bar makes a full
            # REST re-download a no-op — serve it and let REST top up only after
            # the bar rolls over.
            if name in _HTF_CACHE_SERVE_TFS:
                htf_df = get_frame_cache().get_kline_frame(symbol, name)
                if htf_df is not None and htf_cache_frame_serves(
                    htf_df, name, int(limits[name]), exchange=exchange
                ):
                    kline_map[name] = htf_df
                    return
            res = await safe_fetch(
                lambda n=name: client.fetch_klines_cached(symbol, n, limit=limits[n]),
                context=f"klines.{symbol}.{name}",
                client=client,
            )
        kline_map[name] = res
        if res is None:
            ws_df = get_frame_cache().get_kline_frame(symbol, name)
            if ws_df is not None and not ws_df.is_empty():
                kline_map[name] = ws_df
            else:
                fetch_errors[name] = "fetch_failed"

    for name in derive_names:
        if derived_ok:
            assert df_1m is not None
            derived = resample_ohlcv_from_1m(
                df_1m,
                name,
                exchange=exchange,
                limit=limits[name],
            )
            # Require the full configured limit — 1m is capped at 1500 bars so 5m
            # resample often tops out ~300; fall back to direct REST when short.
            required_bars = int(limits[name])
            if not derived.is_empty() and derived.height >= required_bars:
                kline_map[name] = derived
                continue
        await _fallback_kline(name)

    for name in direct_fetch:
        await _fallback_kline(name)

    if "1m" in limits and "1m" not in kline_map:
        await _fallback_kline("1m")
    return kline_map, fetch_errors


def rest_pack_specs(
    client: HuntCcxtClient,
    symbol: str,
    *,
    tier: SnapshotTier,
    ws_orderflow_fresh: bool,
) -> list[tuple[str, Any]]:
    """Per-symbol REST pack — fast tier keeps dump-onset fields only."""
    critical: list[tuple[str, Any]] = [
        ("oi", client.fetch_open_interest(symbol)),
        ("oi_chg_5m", client.fetch_open_interest_change(symbol, period="5m")),
        ("ls_5m", client.fetch_long_short_ratio(symbol, period="5m")),
        ("top_ls_5m", client.fetch_top_position_ls_ratio(symbol, period="5m")),
        ("global_ls_5m", client.fetch_global_ls_ratio(symbol, period="5m")),
        ("taker_5m", client.fetch_taker_ratio(symbol, period="5m")),
        ("book_depth", client.fetch_order_book_depth_snapshot(symbol, limit=100)),
    ]
    if tier == "probe_lite":
        # QoS-trimmed pack for interactive probes of COLD out-of-universe symbols
        # (ADR-0001 QoS pillar; 2026-07-12 418 incident: three /signal probes of
        # cold symbols within 40s tripped the fapi-data WAF). The expensive
        # fapi-data SERIES (basis, oi_series, gls_series, funding history, 1h
        # ratio variants) are exactly what got banned — structure/klines/ticker
        # plus the 5m criticals are enough for a deep-analysis reply. Rows carry
        # _probe_lite so the renderer says «серии не запрошены (защита от бана)».
        return critical

    if tier == "fast":
        critical.extend(
            [
                ("oi_chg_1h", client.fetch_open_interest_change(symbol, period="1h")),
                ("taker_1h", client.fetch_taker_ratio(symbol, period="1h")),
                ("funding", client.fetch_funding_rate(symbol)),
                # Z-score series — cached after first fetch; fast/hot parity with full tier.
                ("funding_hist", client.fetch_funding_rate_history(symbol, limit=16)),
                # OI/GLS series: scalar lists in pack; full-tier join_asof via
        # prepare_columns.join_derivative_series_asof when timestamps land.
        ("oi_series", client.fetch_open_interest_series(symbol, period="5m", limit=48)),
                ("gls_series", client.fetch_global_ls_series(symbol, period="5m", limit=48)),
                ("basis_5m", client.fetch_basis(symbol, period="5m", limit=3)),
            ]
        )
        if not ws_orderflow_fresh:
            critical.append(("agg_trades", client.fetch_agg_trade_snapshot(symbol, limit=100)))
        return critical

    return critical + [
        ("oi_chg_1h", client.fetch_open_interest_change(symbol, period="1h")),
        # ls_5m already fetched in `critical` above — re-adding it here fired a
        # second identical fapi call every full-tier symbol per tick (the pack dict
        # keeps only the last write, so pure wasted weight — DATA-1, 418-sensitive).
        ("ls_1h", client.fetch_long_short_ratio(symbol, period="1h")),
        ("top_ls_1h", client.fetch_top_position_ls_ratio(symbol, period="1h")),
        ("global_ls_1h", client.fetch_global_ls_ratio(symbol, period="1h")),
        ("taker_15m", client.fetch_taker_ratio(symbol, period="15m")),
        ("taker_1h", client.fetch_taker_ratio(symbol, period="1h")),
        ("funding", client.fetch_funding_rate(symbol)),
        ("funding_hist", client.fetch_funding_rate_history(symbol, limit=16)),
        ("basis_5m", client.fetch_basis(symbol, period="5m")),
        ("agg_trades", client.fetch_agg_trade_snapshot(symbol, limit=100)),
        ("oi_series", client.fetch_open_interest_series(symbol, period="5m", limit=48)),
        ("gls_series", client.fetch_global_ls_series(symbol, period="5m", limit=48)),
    ]


def ws_orderflow_fresh(ws_snap: dict[str, Any] | None, *, max_age_s: float = 45.0) -> bool:
    if not ws_snap:
        return False
    age = ws_snap.get("ws_last_msg_age_s")
    if age is None:
        return ws_snap.get("ws_connected") is True
    try:
        return float(age) <= max_age_s and ws_snap.get("agg_trade_delta_60s") is not None
    except (TypeError, ValueError):
        return False


def sort_symbols_for_tick(
    symbols: tuple[str, ...],
    *,
    last_bias: dict[str, str],
) -> tuple[str, ...]:
    """Short-bias symbols first."""

    def _rank(sym: str) -> tuple[int, str]:
        score = 0
        bias = last_bias.get(sym, "")
        if bias == "short":
            score += 80
        elif bias == "both":
            score += 40
        if sym in {"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"}:
            score += 10
        return (-score, sym)

    return tuple(sorted(symbols, key=_rank))


class TickBatchCache:
    """Loop-scoped cache for all-symbol REST batch fetches."""

    __slots__ = (
        "premium_all",
        "funding_info_all",
        "exchange_by_sym",
        "btc_work_1h",
        "btc_work_4h",
        "btc_work_1m",
        "premium_at",
        "funding_at",
        "exchange_at",
        "btc_at",
    )

    def __init__(self) -> None:
        self.premium_all: dict[str, dict[str, float]] = {}
        self.funding_info_all: dict[str, dict[str, float | int]] = {}
        self.exchange_by_sym: dict[str, Any] = {}
        self.btc_work_1h: Any | None = None
        self.btc_work_4h: Any | None = None
        self.btc_work_1m: Any | None = None
        self.premium_at = 0.0
        self.funding_at = 0.0
        self.exchange_at = 0.0
        self.btc_at = 0.0

    def _fresh(self, at: float, ttl: float) -> bool:
        return at > 0 and (time.monotonic() - at) < ttl


async def refresh_tick_batch_cache(
    cache: TickBatchCache,
    client: HuntCcxtClient,
    *,
    safe_fetch: Any,
    prepare_frame: Any,
    need_btc: bool,
    tier: SnapshotTier,
) -> None:
    """Refresh shared batch data — fast tier reuses fresh batch snapshots."""
    now = time.monotonic()
    if tier == "full" or not cache._fresh(cache.premium_at, PREMIUM_BATCH_TTL_S):
        cache.premium_all = await safe_fetch(client.fetch_premium_index_all, context="premium_index_all", client=client) or {}
        cache.premium_at = now
    if tier == "full" or not cache._fresh(cache.funding_at, FUNDING_BATCH_TTL_S):
        cache.funding_info_all = await safe_fetch(client.fetch_funding_info_all, context="funding_info_all", client=client) or {}
        cache.funding_at = now
    if not cache._fresh(cache.exchange_at, EXCHANGE_INFO_TTL_S):
        exchange_list = await safe_fetch(client.fetch_exchange_symbols, context="exchange_symbols", client=client) or []
        cache.exchange_by_sym = {r.symbol: r for r in exchange_list}
        cache.exchange_at = now
    if need_btc and (tier == "full" or not cache._fresh(cache.btc_at, BTC_1H_TTL_S)):
        btc_1h = await safe_fetch(
            lambda: client.fetch_klines_cached("BTCUSDT", "1h", limit=500),
            context="btc_1h",
            client=client,
        )
        if btc_1h is not None and not btc_1h.is_empty():
            cache.btc_work_1h = prepare_frame(btc_1h)
        btc_4h = await safe_fetch(
            lambda: client.fetch_klines_cached("BTCUSDT", "4h", limit=250),
            context="btc_4h",
            client=client,
        )
        if btc_4h is not None and not btc_4h.is_empty():
            cache.btc_work_4h = prepare_frame(btc_4h)
        btc_1m = await safe_fetch(
            lambda: client.fetch_klines_cached("BTCUSDT", "1m", limit=999),
            context="btc_1m",
            client=client,
        )
        if btc_1m is not None and not btc_1m.is_empty():
            cache.btc_work_1m = prepare_frame(btc_1m)
        cache.btc_at = now


__all__ = [
    "SnapshotTier",
    "TickBatchCache",
    "safe_fetch",
    "kline_limits",
    "probe_kline_limits",
    "fetch_rest_pack",
    "_fetch_rest_pack",
    "resolve_kline_map",
    "htf_cache_frame_serves",
    "rest_pack_specs",
    "ws_orderflow_fresh",
    "sort_symbols_for_tick",
    "refresh_tick_batch_cache",
    "_book_from_pack",
    "_overlay_ws_market",
]
