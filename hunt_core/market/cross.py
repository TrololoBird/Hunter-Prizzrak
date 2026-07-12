"""Cross-exchange and cross-venue microstructure helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import polars as pl

from hunt_core.market.ccxt_guard import ccxt_method_available
from hunt_core.features.volume_profile import volume_profile_levels
from hunt_core.market.client import aggregate_cross_exchange_walls, depth_snapshot_from_book

LOG = logging.getLogger("hunt_core.market.cross")

# Per-venue circuit breaker (Task 3)
_venue_error_count: dict[str, int] = {}
_venue_temp_skip: dict[str, float] = {}
_VENUE_CIRCUIT_BREAKER_MAX_ERRORS = 3
_VENUE_CIRCUIT_BREAKER_COOLDOWN_S = 300.0
_VENUE_FETCH_TIMEOUT_S = 10.0


def _venue_is_skipped(venue: str) -> bool:
    expiry = _venue_temp_skip.get(venue)
    if expiry is not None and time.monotonic() < expiry:
        return True
    _venue_temp_skip.pop(venue, None)
    _venue_error_count.pop(venue, None)
    return False


def _record_venue_error(venue: str) -> None:
    _venue_error_count[venue] = _venue_error_count.get(venue, 0) + 1
    if _venue_error_count[venue] >= _VENUE_CIRCUIT_BREAKER_MAX_ERRORS:
        _venue_temp_skip[venue] = time.monotonic() + _VENUE_CIRCUIT_BREAKER_COOLDOWN_S
        LOG.warning(
            "venue_circuit_breaker | venue=%s errors=%s skip_until=%s",
            venue, _venue_error_count[venue],
            _venue_temp_skip[venue],
        )


def _record_venue_success(venue: str) -> None:
    _venue_error_count.pop(venue, None)


SECONDARY_EXCHANGES: tuple[str, ...] = ("bybit", "okx", "bitget")

# Explicit cross-venue funding plane (Hunt-owned — not CCXT ``has=None`` semantics).
# Binance primary: ``watchMarkPrices`` in :class:`HuntCcxtStreams` (not here).
VENUE_FUNDING_WS: frozenset[str] = frozenset({"okx"})
VENUE_FUNDING_REST_POLL: frozenset[str] = frozenset({"bybit", "bitget"})

# Exchange ids hunt knows how to drive via the ccxt factory (linear USDT swap).
SUPPORTED_SECONDARY_EXCHANGES: frozenset[str] = frozenset(
    {"bybit", "okx", "bitget", "gate", "gateio", "kucoinfutures", "mexc", "htx"}
)


def configured_secondary_exchanges() -> tuple[str, ...]:
    """Cross-exchange venue ids from ``HUNT_CROSS_EXCHANGES`` (comma-separated).

    Unset/empty falls back to :data:`SECONDARY_EXCHANGES`. Ids are lowercased,
    de-duplicated (order preserved) and filtered to those the factory supports;
    an unknown id is logged and skipped rather than silently kept.
    """
    raw = os.getenv("HUNT_CROSS_EXCHANGES", "").strip()
    if not raw:
        return SECONDARY_EXCHANGES
    out: list[str] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name or name in out:
            continue
        if name not in SUPPORTED_SECONDARY_EXCHANGES:
            LOG.warning("cross_exchange_unsupported_id | id=%s skipped", name)
            continue
        out.append(name)
    if not out:
        LOG.warning(
            "cross_exchange_env_all_unsupported | raw=%s falling_back=%s",
            raw,
            ",".join(SECONDARY_EXCHANGES),
        )
        return SECONDARY_EXCHANGES
    return tuple(out)


def funding_ws_venues() -> tuple[str, ...]:
    """Secondaries with live Pro ``watchFundingRates`` (when CCXT ``has`` is True)."""
    return tuple(ex for ex in configured_secondary_exchanges() if ex in VENUE_FUNDING_WS)


def funding_rest_poll_venues() -> tuple[str, ...]:
    """Secondaries without funding WS — REST poll fills ``_live_funding_by_exchange``."""
    return tuple(ex for ex in configured_secondary_exchanges() if ex in VENUE_FUNDING_REST_POLL)


def sanitize_funding_map(raw: dict[str, Any] | None) -> dict[str, float]:
    """Cross funding dict without JSON nulls — omit unknown venues instead."""
    out: dict[str, float] = {}
    for key, val in (raw or {}).items():
        if val is None:
            continue
        try:
            out[str(key)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def apply_cross_snapshot_to_market(
    market: dict[str, Any],
    snap: dict[str, Any],
    *,
    ws_cross: dict[str, dict[str, float]] | None = None,
) -> None:
    """Promote merged REST+WS cross intel onto ``market`` (no null funding rates)."""
    merged = merge_ws_cross_into_snapshot(snap, ws_cross)
    _funding_raw = merged.get("funding")
    funding = sanitize_funding_map(_funding_raw if isinstance(_funding_raw, dict) else {})
    _oi_raw = merged.get("oi_usd")
    oi_raw: dict[str, Any] = _oi_raw if isinstance(_oi_raw, dict) else {}
    oi_usd = {k: float(v) for k, v in oi_raw.items() if v is not None}
    secondary_funding = {k: v for k, v in funding.items() if k != "binance"}
    secondary_oi = dict(oi_usd)
    has_ws = bool(ws_cross)
    bool(merged.get("symbol"))
    if has_ws and secondary_funding:
        source = "hybrid"
    elif has_ws:
        source = "ws"
    elif secondary_funding or secondary_oi:
        source = "rest"
    else:
        source = "unavailable"
    market["cross_data_source"] = source
    if secondary_funding:
        market["cross_funding_secondary"] = secondary_funding
    if secondary_oi:
        market["cross_oi_usd_secondary"] = secondary_oi
    if merged.get("funding_spread") is not None:
        market["cross_funding_spread"] = merged.get("funding_spread")
    if merged.get("oi_total") is not None:
        market["cross_oi_total"] = merged.get("oi_total")
    if merged.get("funding_consensus") is not None:
        market["cross_funding_consensus"] = merged.get("funding_consensus")
    listed = merged.get("listed")
    if isinstance(listed, dict):
        market["cross_listed"] = listed


@dataclass(frozen=True, slots=True)
class CrossExchangeConfig:
    """Binance = signal universe; secondaries = cross-venue intel only."""

    enabled: bool = True
    exchanges: tuple[str, ...] = SECONDARY_EXCHANGES
    refresh_interval_s: float = 300.0
    max_symbols_per_refresh: int = 24
    ws_enabled: bool = True
    refresh_concurrency: int = 4


def load_cross_exchange_config() -> CrossExchangeConfig:
    def _flag(name: str, *, default: bool) -> bool:
        raw = os.getenv(name, "1" if default else "0").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return default

    return CrossExchangeConfig(
        enabled=_flag("HUNT_MULTI_EXCHANGE", default=True),
        exchanges=configured_secondary_exchanges(),
        ws_enabled=_flag("HUNT_CROSS_WS", default=True),
        refresh_interval_s=float(os.getenv("HUNT_CROSS_REFRESH_S", "300")),
        max_symbols_per_refresh=int(os.getenv("HUNT_CROSS_MAX_SYMBOLS", "24")),
        refresh_concurrency=int(os.getenv("HUNT_CROSS_CONCURRENCY", "4")),
    )


def apply_cross_exchange_env(cfg: CrossExchangeConfig) -> None:
    """Ensure WS plane sees cross-exchange flag before ``HuntCcxtStreams.start()``."""
    if cfg.enabled and cfg.ws_enabled:
        os.environ["HUNT_CROSS_WS"] = "1"
    elif not cfg.ws_enabled:
        os.environ["HUNT_CROSS_WS"] = "0"


def merge_ws_cross_into_snapshot(
    snapshot: dict[str, Any],
    ws_live: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    """Overlay Pro WS funding/mark/index on REST cross snapshot (WS wins when present)."""
    if not ws_live:
        return snapshot
    out = dict(snapshot)
    funding = sanitize_funding_map(out.get("funding") if isinstance(out.get("funding"), dict) else {})
    mark_price: dict[str, float | None] = dict(out.get("mark_price") or {})
    for ex_name, fields in ws_live.items():
        if not isinstance(fields, dict):
            continue
        fr = fields.get("fundingRate")
        if fr is not None:
            funding[str(ex_name)] = float(fr)
        mp = fields.get("markPrice")
        if mp is not None and float(mp) > 0:
            mark_price[ex_name] = float(mp)
    out["funding"] = funding
    out["mark_price"] = mark_price
    out["ws_overlay"] = True
    rates = [v for v in funding.values() if v is not None]
    if len(rates) >= 2:
        out["funding_spread"] = round(max(rates) - min(rates), 6)
    prices = [v for v in mark_price.values() if v and v > 0]
    if len(prices) >= 2:
        mean_p = sum(prices) / len(prices)
        out["price_divergence_pct"] = round(
            (max(prices) - min(prices)) / mean_p * 100,
            4,
        ) if mean_p > 0 else 0.0
    return out


async def fetch_secondary_ticker_overlay(
    client: Any,
    *,
    cfg: CrossExchangeConfig,
) -> dict[str, dict[str, Any]]:
    """Gather 24h tickers from each configured secondary venue (soft overlay).

    Returns ``{binance_symbol: {exchange: {change_pct, quote_volume}}}``. A venue
    that fails to respond is skipped (degrade gracefully); malformed numeric
    fields are dropped at the client boundary, so values here are already finite.
    """
    if not cfg.enabled or not cfg.exchanges:
        return {}

    async def _one(name: str) -> tuple[str, list[dict[str, Any]]]:
        if _venue_is_skipped(name):
            LOG.info("secondary_ticker_overlay_skipped | exchange=%s circuit_open", name)
            return name, []
        try:
            result = await asyncio.wait_for(
                client.fetch_secondary_tickers(name),
                timeout=_VENUE_FETCH_TIMEOUT_S,
            )
            _record_venue_success(name)
            return name, result
        except asyncio.TimeoutError:
            _record_venue_error(name)
            LOG.warning("secondary_ticker_overlay_timeout | exchange=%s", name)
            return name, []
        except Exception as exc:
            _record_venue_error(name)
            LOG.warning("secondary_ticker_overlay_failed | exchange=%s error=%s", name, exc)
            return name, []

    results = await asyncio.gather(*(_one(n) for n in cfg.exchanges))
    overlay: dict[str, dict[str, Any]] = {}
    for name, rows in results:
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            chg = row.get("price_change_percent")
            qvol = row.get("quote_volume")
            if chg is None or qvol is None:
                continue
            overlay.setdefault(sym, {})[name] = {
                "change_pct": float(chg),
                "quote_volume": float(qvol),
            }
    return overlay


def attach_cross_fields(row: dict[str, Any], cx: dict[str, Any]) -> None:
    out = dict(cx)
    if isinstance(out.get("funding"), dict):
        out["funding"] = sanitize_funding_map(out["funding"])
    row["cross_exchange"] = out
    row["cross_funding_spread"] = cx.get("funding_spread")
    row["cross_funding_consensus"] = cx.get("funding_consensus")
    row["cross_oi_total"] = cx.get("oi_total")
    row["cross_price_divergence_pct"] = cx.get("price_divergence_pct")
    row["cross_listed"] = cx.get("listed")


async def refresh_cross_exchange_cache(
    client: Any,
    symbols: tuple[str, ...] | list[str],
    cache: dict[str, dict[str, Any]],
    *,
    cfg: CrossExchangeConfig,
) -> int:
    """Refresh cross snapshots for Binance watch-universe symbols (capped)."""
    if not cfg.enabled or not symbols:
        return 0
    targets = list(dict.fromkeys(str(s).upper() for s in symbols))[: cfg.max_symbols_per_refresh]
    sem = asyncio.Semaphore(max(1, cfg.refresh_concurrency))
    updated = 0

    async def _one(sym: str) -> None:
        nonlocal updated
        async with sem:
            try:
                snap = await asyncio.wait_for(
                    client.fetch_cross_exchange_snapshot(sym),
                    timeout=_VENUE_FETCH_TIMEOUT_S,
                )
                cache[sym] = snap
                updated += 1
            except asyncio.TimeoutError:
                LOG.warning("cross_exchange_refresh_timeout | symbol=%s", sym)

    results = await asyncio.gather(*(_one(s) for s in targets), return_exceptions=True)
    for sym, res in zip(targets, results):
        if isinstance(res, Exception):
            LOG.warning("cross_exchange_refresh_failed | symbol=%s error=%s", sym, res)
    LOG.info(
        "cross_exchange_cache_refreshed | symbols=%s updated=%s exchanges=%s",
        len(targets),
        updated,
        ",".join(cfg.exchanges),
    )
    return updated



_PRIMARY = "binance"
# Max wall-clock lag a venue's order-book snapshot may trail the freshest venue before
# it's excluded from the cross-book merge (avoids blending stale depth as simultaneous).
_CROSS_BOOK_STALE_MS = 750.0


async def fetch_exchange_order_book(
    client: Any,
    symbol: str,
    exchange: str,
    *,
    limit: int = 100,
) -> dict[str, Any] | None:
    """Depth snapshot for one venue."""
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001
    try:
        if exchange == _PRIMARY:
            snap = await client.fetch_order_book_depth_snapshot(bin_sym, limit=limit)
            if snap.get("bid_price"):
                snap["fetched_at_ms"] = time.time() * 1000.0
                return snap
            return None
        ccxt_sym = await client._secondary_ccxt_symbol(exchange, bin_sym)  # noqa: SLF001
        if ccxt_sym is None:
            return None
        ex = await client._get_secondary(exchange)  # noqa: SLF001
        if ex is None:
            return None
        if not ccxt_method_available(ex, "fetchOrderBook"):
            return None
        ob = await client.rest_gate.invoke_secondary(
            exchange,
            ex,
            lambda: ex.fetch_order_book(ccxt_sym, limit=min(100, max(5, int(limit)))),
            context=f"order_book:{ccxt_sym}",
        )
        bids = [(float(row[0]), float(row[1])) for row in (ob.get("bids") or []) if row]
        asks = [(float(row[0]), float(row[1])) for row in (ob.get("asks") or []) if row]
        if not bids or not asks:
            return None
        snap = depth_snapshot_from_book(bids, asks)
        snap["exchange"] = exchange
        snap["bids"] = bids
        snap["asks"] = asks
        snap["fetched_at_ms"] = time.time() * 1000.0
        return snap
    except Exception as exc:
        LOG.warning(
            "cross_book_fetch_failed | symbol=%s exchange=%s error=%s",
            bin_sym,
            exchange,
            exc,
        )
        return None


def _reference_mid(snap: dict[str, Any] | None) -> float:
    """Mid price of a venue snapshot, falling back to its best bid.

    The depth-bin grid is centred on this, so a best-bid reference would offset
    every bucket boundary by half a spread and push the bid bin adjacent to the
    price across to the ask side.
    """
    if not isinstance(snap, dict):
        return 0.0
    bid = float(snap.get("bid_price") or 0.0)
    asks = snap.get("asks") or []
    ask = 0.0
    if asks:
        first = asks[0]
        try:
            ask = float(first.get("price", 0.0)) if isinstance(first, dict) else float(first[0])
        except (TypeError, ValueError, IndexError, KeyError):
            ask = 0.0
    if bid > 0 and ask > bid:
        return (bid + ask) / 2.0
    return bid


async def fetch_cross_exchange_book_walls(
    client: Any,
    symbol: str,
    *,
    cfg: CrossExchangeConfig | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Ranked walls from Binance + configured secondaries."""
    cfg = cfg or load_cross_exchange_config()
    venues = [_PRIMARY, *cfg.exchanges] if cfg.enabled else [_PRIMARY]
    results = await asyncio.gather(
        *(fetch_exchange_order_book(client, symbol, ex, limit=limit) for ex in venues),
        return_exceptions=True,
    )
    per_ex: dict[str, dict[str, Any]] = {}
    for ex, res in zip(venues, results, strict=True):
        if isinstance(res, dict) and res.get("bid_price"):
            per_ex[ex] = res
    if not per_ex:
        return {"venues": [], "bid_levels": [], "ask_levels": [], "source": "cross_exchange"}

    # Time-alignment (no-degradation rule): the venue fetches complete at different
    # wall-clock moments (a slow OKX REST can be 1-2s behind a 50ms Binance snapshot).
    # Merging them as if simultaneous blurs the aggregate wall notional. Drop any venue
    # whose snapshot is older than _CROSS_BOOK_STALE_MS behind the freshest one, rather
    # than blend stale depth into a live cross-book.
    excluded_stale: list[str] = []
    stamped: dict[str, float] = {}
    for ex, s in per_ex.items():
        ts = s.get("fetched_at_ms")
        if isinstance(ts, (int, float)):
            stamped[ex] = float(ts)
    if len(stamped) >= 2:
        newest = max(stamped.values())
        for ex, ts in stamped.items():
            if newest - ts > _CROSS_BOOK_STALE_MS:
                excluded_stale.append(ex)
        for ex in excluded_stale:
            per_ex.pop(ex, None)
        if excluded_stale:
            LOG.info(
                "cross_book_stale_excluded | symbol=%s excluded=%s kept=%s",
                symbol, ",".join(excluded_stale), ",".join(per_ex.keys()),
            )
    if not per_ex:
        return {"venues": [], "bid_levels": [], "ask_levels": [], "source": "cross_exchange"}
    merged = aggregate_cross_exchange_walls(per_ex)
    if excluded_stale:
        merged["stale_venues_excluded"] = excluded_stale
    from datetime import UTC, datetime

    merged["fetched_at"] = datetime.now(UTC).isoformat()
    try:
        from hunt_core.maps.config import load_maps_config
        from hunt_core.maps.orderbook import merge_full_depth_bins

        maps_cfg = load_maps_config()
        price = _reference_mid(per_ex.get(_PRIMARY))
        if price <= 0:
            for snap in per_ex.values():
                price = _reference_mid(snap)
                if price > 0:
                    break
        if price > 0:
            merged["depth_bins"] = merge_full_depth_bins(
                per_ex,
                current_price=price,
                n_buckets=maps_cfg.n_buckets,
                price_range_pct=maps_cfg.price_range_pct,
            )
    except Exception:
        LOG.debug("cross_depth_bins_merge_failed", exc_info=True)
    merged["per_exchange"] = {
        ex: {
            "bid_levels": snap.get("bid_levels") or [],
            "ask_levels": snap.get("ask_levels") or [],
            "bids": snap.get("bids") or [],
            "asks": snap.get("asks") or [],
            "depth_imbalance": snap.get("depth_imbalance"),
        }
        for ex, snap in per_ex.items()
    }
    return merged


async def fetch_cross_exchange_taker_flow(
    client: Any,
    symbol: str,
    *,
    cfg: CrossExchangeConfig | None = None,
    period: str = "5m",
) -> dict[str, Any]:
    """Taker buy/sell ratio per venue + OI-weighted consensus."""
    cfg = cfg or load_cross_exchange_config()
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001

    async def _primary() -> tuple[str, float | None]:
        try:
            val = await client.fetch_taker_ratio(bin_sym, period=period)
            return _PRIMARY, float(val) if val is not None else None
        except Exception:
            return _PRIMARY, None

    async def _secondary(name: str) -> tuple[str, float | None]:
        ccxt_sym = await client._secondary_ccxt_symbol(name, bin_sym)  # noqa: SLF001
        if ccxt_sym is None:
            return name, None
        ex = await client._get_secondary(name)  # noqa: SLF001
        if ex is None:
            return name, None
        try:
            if not ccxt_method_available(ex, "fetchLongShortRatio"):
                return name, None
            payload = await client.rest_gate.invoke_secondary(
                name,
                ex,
                lambda: ex.fetch_long_short_ratio(ccxt_sym, period=period, limit=1),
                context=f"long_short:{ccxt_sym}",
            )
            if isinstance(payload, list) and payload:
                item = payload[-1]
                ratio = item.get("longShortRatio") or item.get("ratio")
                return name, float(ratio) if ratio is not None else None
        except Exception as exc:
            LOG.debug("cross_taker_failed | ex=%s sym=%s err=%s", name, bin_sym, exc)
        return name, None

    tasks = [_primary()]
    if cfg.enabled:
        tasks.extend(_secondary(ex) for ex in cfg.exchanges)
    rows = await asyncio.gather(*tasks)
    per_ex = {ex: val for ex, val in rows if val is not None}
    values = list(per_ex.values())
    consensus = round(sum(values) / len(values), 4) if values else None
    return {
        "period": period,
        "per_exchange": per_ex,
        "consensus": consensus,
        "venues": len(per_ex),
        "source": "cross_exchange",
    }


async def fetch_cross_exchange_volume_profile(
    client: Any,
    symbol: str,
    interval: str = "1h",
    *,
    cfg: CrossExchangeConfig | None = None,
    lookback: int = 48,
    buckets: int = 24,
) -> dict[str, Any]:
    """Merge kline volume from Binance + secondaries (volume-weighted POC)."""
    cfg = cfg or load_cross_exchange_config()
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001
    limit = max(lookback + 5, 60)

    async def _klines(exchange: str) -> tuple[str, pl.DataFrame | None, float]:
        try:
            if exchange == _PRIMARY:
                df = await client.fetch_klines(bin_sym, interval, limit=limit)
                if df is None or df.is_empty():
                    return exchange, None, 0.0
                qv = float(df["volume"].tail(lookback).sum() or 0)
                return exchange, df, qv
            ccxt_sym = await client._secondary_ccxt_symbol(exchange, bin_sym)  # noqa: SLF001
            if ccxt_sym is None:
                return exchange, None, 0.0
            sec = await client._get_secondary(exchange)  # noqa: SLF001
            if sec is None:
                return exchange, None, 0.0
            if not ccxt_method_available(sec, "fetchOHLCV"):
                return exchange, None, 0.0
            raw = await client.rest_gate.invoke_secondary(
                exchange,
                sec,
                lambda: sec.fetch_ohlcv(ccxt_sym, timeframe=interval, limit=limit),
                context=f"ohlcv:{ccxt_sym}:{interval}",
            )
            if not raw:
                return exchange, None, 0.0
            df = pl.DataFrame(
                raw,
                schema=["open_time", "open", "high", "low", "close", "volume"],
                orient="row",
            )
            qv = float(df["volume"].tail(lookback).sum() or 0)
            return exchange, df, qv
        except Exception as exc:
            LOG.debug("cross_vp_klines_failed | ex=%s sym=%s err=%s", exchange, bin_sym, exc)
            return exchange, None, 0.0

    venues = [_PRIMARY, *cfg.exchanges] if cfg.enabled else [_PRIMARY]
    parts = await asyncio.gather(*(_klines(v) for v in venues))
    weighted_frames: list[pl.DataFrame] = []
    weights: list[float] = []
    per_ex: dict[str, dict[str, float | None]] = {}
    for ex, df, qv in parts:
        if df is None or df.is_empty() or qv <= 0:
            per_ex[ex] = {"poc": None, "weight": 0.0}
            continue
        poc, vah, val = volume_profile_levels(df, lookback=lookback, buckets=buckets)
        per_ex[ex] = {"poc": poc, "vah": vah, "val": val, "weight": qv}
        tail = df.tail(lookback).select(
            [
                pl.col("high"),
                pl.col("low"),
                (pl.col("volume") * pl.lit(qv)).alias("volume"),
            ]
        )
        weighted_frames.append(tail)
        weights.append(qv)

    if not weighted_frames:
        return {"interval": interval, "poc": None, "vah": None, "val": None, "per_exchange": per_ex}

    merged = pl.concat(weighted_frames, how="vertical")
    total_w = sum(weights) or 1.0
    merged = merged.with_columns((pl.col("volume") / pl.lit(total_w)).alias("volume"))
    poc, vah, val = volume_profile_levels(merged, buckets=buckets)
    return {
        "interval": interval,
        "poc": poc,
        "vah": vah,
        "val": val,
        "per_exchange": per_ex,
        "venues": len(weighted_frames),
        "source": "cross_exchange",
    }


async def fetch_cross_exchange_liquidation_estimate(
    client: Any,
    symbol: str,
    *,
    cfg: CrossExchangeConfig | None = None,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Slow-path forward liquidation overlay from cross-venue OI (no WS)."""
    cfg = cfg or load_cross_exchange_config()
    if current_price is None or current_price <= 0:
        return {"source": "cross_exchange", "skipped": "no_price", "skip_reason": "no_price"}
    bin_sym = client._bin_sym(symbol)  # noqa: SLF001
    try:
        oi_bars = await client.fetch_oi_bars_for_maps(bin_sym, period="1h", limit=48)
    except Exception as exc:
        LOG.debug("cross_liq_oi_failed | sym=%s err=%s", bin_sym, exc)
        return {"source": "cross_exchange", "skipped": "oi_fetch_failed", "skip_reason": str(exc)}
    if not oi_bars:
        return {"source": "cross_exchange", "skipped": "no_oi_history", "skip_reason": "no_oi_bars"}
    from hunt_core.maps.liquidation import (
        entry_anchored_forward_zones,
        leverage_tiers_from_brackets,
        maintenance_rates_from_tiers,
    )
    from hunt_core.maps.config import load_maps_config

    maps_cfg = load_maps_config()
    tiers = client.get_cached_leverage_tiers(bin_sym) if hasattr(client, "get_cached_leverage_tiers") else None
    mmr = maintenance_rates_from_tiers(tiers or []) or None
    lev = leverage_tiers_from_brackets(tiers or [])
    gls = None
    try:
        gls = await client.fetch_global_ls_ratio(bin_sym, period="1h")
    except Exception:
        gls = None
    fwd = entry_anchored_forward_zones(
        oi_bars,
        current_price=current_price,
        n_buckets=maps_cfg.n_buckets,
        price_range_pct=maps_cfg.price_range_pct,
        leverage_tiers=lev,
        maintenance_margin_rates=mmr,
        leverage_weights=maps_cfg.leverage_weights,
        global_ls_ratio=float(gls) if gls is not None else None,
    )
    zones: list[dict[str, Any]] = []
    span = current_price * maps_cfg.price_range_pct / 100.0
    price_min = current_price - span
    bucket_size = (2.0 * span) / max(1, maps_cfg.n_buckets)
    max_f = max((r["total"] for r in fwd.values()), default=1.0) or 1.0
    for b, row in sorted(fwd.items(), key=lambda kv: kv[1]["total"], reverse=True)[:8]:
        center = price_min + (b + 0.5) * bucket_size
        zones.append(
            {
                "price_center": round(center, 6),
                "intensity": round(row["total"] / max_f, 4),
                "source": "entry_anchored_cross",
            }
        )
    return {
        "source": "cross_exchange",
        "forward_zones": zones,
        "oi_bars_used": len(oi_bars),
        "oi_bars": oi_bars,
        "venues": [_PRIMARY, *cfg.exchanges] if cfg.enabled else [_PRIMARY],
    }


async def attach_cross_microstructure(
    client: Any,
    row: dict[str, Any],
    *,
    cfg: CrossExchangeConfig | None = None,
) -> None:
    """Populate row['cross_microstructure'] for pinned / deep probes."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return
    cfg = cfg or load_cross_exchange_config()
    from hunt_core.maps.config import load_maps_config

    maps_cfg = load_maps_config()
    price = float(row.get("price") or 0)
    book, taker5, vp1h, vp15, liq_est = await asyncio.gather(
        fetch_cross_exchange_book_walls(client, sym, cfg=cfg, limit=maps_cfg.book_deep_top_n),
        fetch_cross_exchange_taker_flow(client, sym, cfg=cfg, period="5m"),
        fetch_cross_exchange_volume_profile(client, sym, "1h", cfg=cfg, lookback=48),
        fetch_cross_exchange_volume_profile(client, sym, "15m", cfg=cfg, lookback=96),
        fetch_cross_exchange_liquidation_estimate(client, sym, cfg=cfg, current_price=price),
    )
    row["cross_microstructure"] = {
        "book_walls": book,
        "taker_flow": taker5,
        "volume_profile_1h": vp1h,
        "volume_profile_15m": vp15,
        "liquidation_estimate": liq_est,
        "liquidation_note": (
            "Liquidations: Binance+Bybit+OKX WS (real events); forward OID from aligned OI/OHLCV"
        ),
    }
    try:
        from hunt_core.maps.engine import get_map_store

        store = get_map_store(maps_cfg)
        oi_bars = liq_est.get("oi_bars") if isinstance(liq_est, dict) else None
        if isinstance(oi_bars, list) and oi_bars:
            store.cache_oi_bars(sym, oi_bars)
        if isinstance(liq_est, dict):
            store.cache_liq_estimate(sym, liq_est)
    except Exception:
        LOG.debug("cross_map_store_cache_failed sym=%s", sym, exc_info=True)
    if book.get("depth_imbalance") is not None:
        row.setdefault("market", {})["cross_depth_imbalance"] = book["depth_imbalance"]
    if taker5.get("consensus") is not None:
        row.setdefault("market", {})["cross_taker_5m"] = taker5["consensus"]

__all__ = [
    "attach_cross_microstructure",
    "fetch_cross_exchange_book_walls",
    "fetch_cross_exchange_liquidation_estimate",
    "fetch_cross_exchange_taker_flow",
    "fetch_cross_exchange_volume_profile",
]
