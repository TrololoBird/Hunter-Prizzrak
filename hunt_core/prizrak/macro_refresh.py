"""Background refresher for the PRIZRAK macro доп-факторы (dominance + market cap).

Both factors are consumed by the tick through **cache-only** synchronous reads
(``dominance_source.read_cached_changes_24h`` / ``marketcap_source.read_cached_series``) — the live
path must never block on HTTP. Something has to fill those caches, and until now nothing in the
running bot did: the refreshers existed only as manual scripts, so enabling ``dominance_enabled`` /
``marketcap_enabled`` read an empty cache and the factor silently no-opped. This loop is that missing
producer, so the config flags mean what they say.

Why these factors are worth running: the trader reads them out loud in every обзор — «догоняющее
движение на разгрузке Доминации ETH», «Стейблкоины пришли к поддержке и РСИ глобальной трендовой»
(BTC/ETH keyzone разбор), and the market-cap доп-фактор is the Павел М. supply read. Each is a
*доп-фактор* (a strength multiplier), never a gate.

Fail-soft by construction: every refresh is wrapped, a CoinGecko outage only means the cache goes
stale, and a stale/absent cache degrades the factor to neutral (I-6 — never a fabricated number).
"""
from __future__ import annotations

import asyncio
import os

import structlog

from hunt_core.prizrak.config import PrizrakConfig

LOG = structlog.get_logger("hunt.prizrak.macro_refresh")

# CoinGecko's free tier is rate-limited; both series move slowly (dominance is a 24h change, supply a
# 90d curve), so an hourly dominance append and a 6-hourly cap refresh are ample.
_DOMINANCE_INTERVAL_S = float(os.getenv("HUNT_DOMINANCE_REFRESH_S", "3600") or 3600)
_MARKETCAP_INTERVAL_S = float(os.getenv("HUNT_MARKETCAP_REFRESH_S", "21600") or 21600)
# Spacing between per-symbol market-chart calls, so a watchlist refresh never bursts the API.
_MARKETCAP_SYMBOL_GAP_S = float(os.getenv("HUNT_MARKETCAP_GAP_S", "2.0") or 2.0)


async def _refresh_dominance_once() -> None:
    from hunt_core.prizrak.dominance_source import refresh_dominance

    await refresh_dominance()
    LOG.info("dominance_cache_refreshed")


async def _refresh_marketcap_once(symbols: tuple[str, ...]) -> None:
    from hunt_core.prizrak.marketcap_source import fetch_market_cap_series

    ok = 0
    for sym in symbols:
        try:
            series = await fetch_market_cap_series(sym)
        except Exception:  # noqa: BLE001 — one symbol's failure must not stop the rest
            LOG.debug("marketcap_symbol_refresh_failed", symbol=sym)
            continue
        if series:
            ok += 1
        await asyncio.sleep(_MARKETCAP_SYMBOL_GAP_S)
    LOG.info("marketcap_cache_refreshed", symbols=len(symbols), ok=ok)


async def macro_context_refresh_loop(symbols: tuple[str, ...]) -> None:
    """Keep the dominance / market-cap caches warm for as long as the bot runs.

    Each factor is refreshed only when its config flag is on, so a disabled factor costs no request
    at all. Runs until cancelled; individual failures are logged and retried on the next due tick.

    Args:
        symbols: Compact watchlist symbols (``BTCUSDT``) whose market-cap series to keep cached.
    """
    cfg = PrizrakConfig.load()
    if not (cfg.dominance_enabled or cfg.marketcap_enabled):
        LOG.info("macro_refresh_idle", reason="both factors disabled")
        return
    LOG.info(
        "macro_refresh_started",
        dominance=cfg.dominance_enabled,
        marketcap=cfg.marketcap_enabled,
        symbols=len(symbols),
    )
    next_dom = 0.0
    next_cap = 0.0
    while True:
        loop_now = asyncio.get_running_loop().time()
        try:
            if cfg.dominance_enabled and loop_now >= next_dom:
                await _refresh_dominance_once()
                next_dom = loop_now + _DOMINANCE_INTERVAL_S
            if cfg.marketcap_enabled and symbols and loop_now >= next_cap:
                await _refresh_marketcap_once(symbols)
                next_cap = loop_now + _MARKETCAP_INTERVAL_S
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the refresher must never kill the watch loop
            LOG.exception("macro_refresh_failed")
        await asyncio.sleep(60.0)


__all__ = ["macro_context_refresh_loop"]
