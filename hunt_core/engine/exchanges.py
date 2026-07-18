"""ccxt.pro exchange factory — public USDⓈ-M futures, configured for the native engine (ADR-0002 §6.2).

Public data only: **no ``apiKey``/``secret``**, only ``watch_*``/``fetch_*`` public methods are ever
called. One instance per venue (the throttler and WS connection are per-instance; multiple instances
would each rate-limit independently — ccxt manual).
"""
from __future__ import annotations

from typing import Any

import ccxt.pro as ccxtpro

from hunt_core.engine import params


def _base_options() -> dict[str, Any]:
    return {
        # ccxt's built-in weighted token-bucket throttler (per-endpoint `byLimit` costs). Replaces
        # the custom weight governor — heavy klines/depth calls consume proportionally.
        "enableRateLimit": True,
        # watch_* returns ONLY what changed since the last call — an unchanged read can never
        # masquerade as fresh (the single most important native anti-stale switch).
        "newUpdates": True,
        "options": {
            "defaultType": "future",
            "OHLCVLimit": params.OHLCV_LIMIT,
            "tradesLimit": params.TRADES_LIMIT,
            "watchOrderBookLimit": params.ORDER_BOOK_LIMIT,
            "watchOrderBookRate": params.WATCH_ORDER_BOOK_RATE_MS,
        },
    }


def make_binance() -> Any:
    """A ccxt.pro Binance USDⓈ-M futures client configured for the engine (public data only)."""
    return ccxtpro.binance(_base_options())


# NB: secondary venues (OKX/Bybit/Bitget cross-venue funding/liq) keep ccxt for its multi-exchange
# abstraction; their factory is added when that plane is wired — deliberately not stubbed here so no
# unused surface ships (ADR-0002 §9).
