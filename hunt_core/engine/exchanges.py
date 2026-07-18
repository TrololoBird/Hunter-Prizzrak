"""ccxt.pro exchange factory — public USDⓈ-M futures, configured for the native engine (ADR-0002 §6.2).

Public data only: **no ``apiKey``/``secret``**, only ``watch_*``/``fetch_*`` public methods are ever
called. One instance per venue (the throttler and WS connection are per-instance; multiple instances
would each rate-limit independently — ccxt manual).
"""
from __future__ import annotations

from typing import Any

import ccxt.pro as ccxtpro

from hunt_core.engine import params
from hunt_core.market.factory import dns_cached_class


def _base_options() -> dict[str, Any]:
    return {
        # ccxt's built-in weighted token-bucket throttler (per-endpoint `byLimit` costs). Replaces
        # the custom weight governor — heavy klines/depth calls consume proportionally.
        "enableRateLimit": True,
        # watch_* returns ONLY what changed since the last call — an unchanged read can never
        # masquerade as fresh (the single most important native anti-stale switch).
        "newUpdates": True,
        # Transport keepalive tuned like the main client (default 180000×2; ×3 for startup robustness).
        "streaming": {
            "keepAlive": params.WS_KEEPALIVE_MS,
            "maxPingPongMisses": params.WS_MAX_PING_PONG_MISSES,
        },
        "options": {
            "defaultType": "future",
            "OHLCVLimit": params.OHLCV_LIMIT,
            "tradesLimit": params.TRADES_LIMIT,
            "watchOrderBookLimit": params.ORDER_BOOK_LIMIT,
            "watchOrderBookRate": params.WATCH_ORDER_BOOK_RATE_MS,
            # ccxt.pro's order book is nonce-validated with checksum ON by default; keep it explicit.
            "watchOrderBook": {"checksum": True, "maxRetries": 3},
        },
    }


def make_binance() -> Any:
    """A ccxt.pro Binance USDⓈ-M futures client configured for the engine (public data only).

    Uses the DNS-cached session class (``ThreadedResolver`` + sane ``ttl_dns_cache``): ccxt/aiohttp's
    default ``AsyncResolver`` (c-ares) bypasses the OS resolver and fails to resolve on macOS. This
    is necessary ccxt connector config, not a crutch — every ccxt client on this platform needs it.
    """
    cls = dns_cached_class(ccxtpro.binance)
    return cls(_base_options())


def make_binance_spot() -> Any:
    """A ccxt.pro Binance **spot** client for the spot sibling engine (public data only).

    Spot is a separate venue with its own 6000/min weight budget (never charge the fapi 2400/min
    counter). Only the generic + streaming knobs are set; ``defaultType='spot'`` selects the spot
    markets. Same DNS-cached session as the futures client (macOS resolver fix).
    """
    cls = dns_cached_class(ccxtpro.binance)
    opts = _base_options()
    opts["options"] = {
        "defaultType": "spot",
        "OHLCVLimit": params.OHLCV_LIMIT,
        "tradesLimit": params.TRADES_LIMIT,
        "watchOrderBookLimit": params.ORDER_BOOK_LIMIT,
    }
    return cls(opts)


SECONDARY_VENUES: tuple[str, ...] = ("okx", "bybit", "bitget")


def make_secondary(venue: str) -> Any:
    """A ccxt.pro client for a secondary venue (cross-venue funding/liq), public data only.

    Deliberately does NOT reuse Binance's ``streaming`` / ``watchOrderBook`` tuning — each venue ships
    its own native keepAlive/ping (OKX/Bybit 18000, Bitget 30000) and order-book config (OKX
    ``depth:'books'``); forcing Binance's 180000 keepAlive on them would break their WS. Only the
    generic, venue-agnostic knobs are set. ``defaultType='swap'`` (secondaries expose USDT perps as
    swaps); ccxt unifies symbols so ``BTC/USDT:USDT`` maps across venues.
    """
    cls = dns_cached_class(getattr(ccxtpro, venue))
    return cls(
        {
            "enableRateLimit": True,
            "newUpdates": True,
            "options": {
                "defaultType": "swap",
                "OHLCVLimit": params.OHLCV_LIMIT,
                "tradesLimit": params.TRADES_LIMIT,
                "watchOrderBookLimit": params.ORDER_BOOK_LIMIT,
            },
        }
    )
