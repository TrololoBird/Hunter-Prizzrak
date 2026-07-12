#!/usr/bin/env python3
"""Warm the market-cap cache for Павел М.'s доп-фактор — run OFF the live tick process.

The live tick path only ever *reads* ``data/marketcap_cache/`` (sync, no network); this
script is what fills it. Fetches CoinGecko cap series for the given symbols (or the live
watchlist) and writes each to cache. CoinGecko free tier is IP-rate-limited, so requests
are paced; failures are per-symbol and non-fatal.

Unlike research Binance fetchers, CoinGecko shares NO egress-rate budget with Binance, so
this cannot contribute to a Binance 418 — it is safe to run concurrently with a live watch.

Usage:
    uv run python -m scripts.refresh_marketcap_cache BTCUSDT ETHUSDT ONDOUSDT
    uv run python -m scripts.refresh_marketcap_cache            # uses data/hunt_watchlist.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import structlog

from hunt_core.paths import WATCHLIST
from hunt_core.prizrak.marketcap_source import fetch_market_cap_series

log = structlog.get_logger(__name__)

_PACE_S = float(os.getenv("HUNT_MARKETCAP_REFRESH_PACE_S", "3") or 3)


def _watchlist_symbols() -> list[str]:
    try:
        data = json.loads(WATCHLIST.read_text())
    except Exception:
        return []
    if isinstance(data, dict):
        syms = data.get("symbols") or data.get("watchlist") or list(data.keys())
    else:
        syms = data
    return [str(s).upper() for s in syms if s] if isinstance(syms, list) else []


async def refresh(symbols: list[str]) -> int:
    """Fetch + cache each symbol's cap series. Returns the count successfully warmed."""
    warmed = 0
    for i, sym in enumerate(symbols):
        # force a network refresh regardless of TTL by using ttl_s=0
        series = await fetch_market_cap_series(sym, ttl_s=0)
        if series:
            warmed += 1
            log.info("marketcap_warmed", symbol=sym, points=len(series))
        else:
            log.warning("marketcap_warm_miss", symbol=sym)
        if i < len(symbols) - 1:
            await asyncio.sleep(_PACE_S)
    return warmed


def main() -> int:
    symbols = [s.upper() for s in sys.argv[1:]] or _watchlist_symbols()
    if not symbols:
        log.error("marketcap_refresh_no_symbols")
        return 1
    warmed = asyncio.run(refresh(symbols))
    log.info("marketcap_refresh_done", requested=len(symbols), warmed=warmed)
    return 0 if warmed else 2


if __name__ == "__main__":
    raise SystemExit(main())
