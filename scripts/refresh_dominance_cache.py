#!/usr/bin/env python3
"""Warm the dominance cache for Prizrak's TOTAL3/BTC.D доп-фактор — run OFF the tick process.

The live tick path only ever *reads* ``data/dominance_cache.json`` (sync, no network) and
derives the 24h change from the rolling snapshots this script appends. Fetches one CoinGecko
``/global`` snapshot per run; the 24h change becomes available once the cache holds a snapshot
~24h old (before that the factor stays neutral). Global, not per-symbol, so one call per run.

CoinGecko free tier shares NO egress budget with Binance, so this cannot contribute to a
Binance 418 — safe to run concurrently with a live watch. Run it hourly (cron) when
``PrizrakConfig.dominance_enabled`` is true; a no-op otherwise on the live path.

Usage:
    uv run python -m scripts.refresh_dominance_cache
"""
from __future__ import annotations

import asyncio

import structlog

from hunt_core.prizrak.dominance_source import read_cached_changes_24h, refresh_dominance

log = structlog.get_logger(__name__)


async def _main() -> int:
    await refresh_dominance(ttl_s=0)  # force a fresh append on an explicit refresh run
    changes = read_cached_changes_24h()
    if changes is None:
        log.info("dominance_refresh_done", changes="cold-start (need a ~24h-old snapshot)")
    else:
        log.info("dominance_refresh_done", **{k: round(v, 3) for k, v in changes.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
