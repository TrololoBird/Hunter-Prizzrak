"""Static parameter-aware endpoint weights — ADR-0001 pillar 1.

Endpoint weight is a spec fact (Binance USDⓈ-M docs), not something to measure:
every weight-consuming call declares its cost from THIS table before it leaves
the client. Parameter-aware entries (klines/depth scale with ``limit`` — ccxt
#27844) are functions; constant endpoints live in the context-prefix table that
``HuntCcxtRestGate.invoke`` consults when the caller does not declare a weight.

Buckets (Binance publishes several independent limiters — the registry is
multi-bucket, mirroring the exchange's own ``rateLimits`` array):
- ``weight``      — REQUEST_WEIGHT 2400/min per IP (shared REST + WS-API);
- ``fapi_data``   — /futures/data/* request-count window (handled by the gate's
                    fapi budget; charged a nominal weight of 1 here);
- ``funding_hist``— funding-rate endpoints share a 500/5min side-pool.
"""
from __future__ import annotations

from typing import Literal

WeightBucket = Literal["weight", "fapi_data", "funding_hist"]


def klines_weight(limit: int) -> int:
    """/fapi/v1/klines (and mark/index/premium variants): weight scales with limit."""
    n = max(1, int(limit))
    if n <= 100:
        return 1
    if n <= 499:
        return 2
    if n <= 1000:
        return 5
    return 10


def depth_weight(limit: int) -> int:
    """/fapi/v1/depth: weight scales with requested depth."""
    n = max(1, int(limit))
    if n <= 50:
        return 2
    if n <= 100:
        return 5
    if n <= 500:
        return 10
    return 20


# Longest-prefix match against the gate ``context`` string (the part before
# ``:``/``.`` separators is the endpoint family). Values are per-call weights
# from the USDⓈ-M spec; anything absent falls back to the caller's declared
# weight or the conservative default of 5.
CONTEXT_WEIGHTS: dict[str, int] = {
    "bootstrap_exchange_info": 1,
    "exchange_status": 1,
    "server_time": 1,
    "bids_asks": 2,           # bookTicker, multi-symbol
    "ticker_24h": 40,         # 24hr ticker, ALL symbols
    "ticker": 1,              # single-symbol variants
    "order_book": 5,          # fallback; call sites pass depth_weight(limit)
    "open_interest": 1,       # /fapi/v1/openInterest
    "funding_rate": 1,
    "funding_hist": 1,        # weight 1, but ALSO the 500/5min side-pool bucket
    "funding_info_all": 1,
    "premium_index_all": 10,
    "agg_trades": 20,         # /fapi/v1/aggTrades — spec weight 20 (was undercharged 5)
    "ohlcv": 5,               # fallback; call sites pass klines_weight(limit)
    "klines": 5,
    "mark_ohlcv": 5,
    "index_ohlcv": 5,
    "premium_ohlcv": 5,
    "leverage_tiers": 1,
}

_FUNDING_POOL_PREFIXES = ("funding_hist",)

# Background QoS class — these context families yield to the watch tick under
# scarcity (ADR-0001 QoS): admitted only while usage is below the background
# ceiling. Wired by context prefix so callers need no plumbing changes.
BACKGROUND_CONTEXT_PREFIXES = ("hot_enrich", "path_backfill", "lake_warmup")


def _context_family(context: str) -> str:
    for sep in (":", "."):
        idx = context.find(sep)
        if idx > 0:
            context = context[:idx]
    return context


def weight_for_context(context: str, *, default: int = 5) -> int:
    """Registry weight for a gate context; ``default`` when unknown."""
    fam = _context_family(str(context or ""))
    if fam in CONTEXT_WEIGHTS:
        return CONTEXT_WEIGHTS[fam]
    # longest-prefix fallback (e.g. "ohlcv" family variants)
    for prefix, w in CONTEXT_WEIGHTS.items():
        if fam.startswith(prefix):
            return w
    return default


def bucket_for_context(context: str) -> WeightBucket:
    fam = _context_family(str(context or ""))
    if any(fam.startswith(p) for p in _FUNDING_POOL_PREFIXES):
        return "funding_hist"
    return "weight"


def is_background_context(context: str) -> bool:
    fam = _context_family(str(context or ""))
    return any(fam.startswith(p) for p in BACKGROUND_CONTEXT_PREFIXES)


__all__ = [
    "BACKGROUND_CONTEXT_PREFIXES",
    "CONTEXT_WEIGHTS",
    "WeightBucket",
    "bucket_for_context",
    "depth_weight",
    "is_background_context",
    "klines_weight",
    "weight_for_context",
]
