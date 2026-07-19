"""Free, no-auth market-cap series source (CoinGecko) — feeds ``marketcap.py``.

Павел М.'s market-cap доп-фактор needs a circulating-supply-aware cap series that public
Binance/CCXT does not expose. CoinGecko's free public REST provides it without any key,
login, or payment. This client is deliberately **off the critical tick plane**:

- disk-cached per symbol (``data/marketcap_cache/``) with a long TTL — supply moves slowly,
  so we refetch at most every ``HUNT_MARKETCAP_TTL_S`` (default 12h);
- **silent-fail**: any network/parse error returns the stale cache if present, else
  ``None`` — the factor then reads neutral (multiplier 1.0) and the live path is untouched;
- **no proxy, no venue coupling**: a CoinGecko 429 can never extend the Binance REST pause
  or contribute to a futures-data IP-ban (the 418 lesson);
- aiohttp only (project rule), imported lazily so this module stays importable offline.

Only used when ``PrizrakConfig.marketcap_enabled`` is true. When false, callers skip it
entirely and no request is ever made.
"""
from __future__ import annotations

import os
import time
from typing import Any

import structlog

from hunt_core import serde
from hunt_core.paths import MARKETCAP_CACHE

log = structlog.get_logger(__name__)

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_DEFAULT_TTL_S = int(os.getenv("HUNT_MARKETCAP_TTL_S", "43200") or 43200)  # 12h
_HTTP_TIMEOUT_S = float(os.getenv("HUNT_MARKETCAP_TIMEOUT_S", "8") or 8)
_DEFAULT_DAYS = int(os.getenv("HUNT_MARKETCAP_DAYS", "90") or 90)

# CoinGecko ``symbol`` is ambiguous (many coins reuse a ticker). Pin the majors to the
# canonical id so we never resolve e.g. a scam "BTC" clone. Extend as needed.
_ID_OVERRIDE: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "TRX": "tron",
    "MATIC": "matic-network",
    "TON": "the-open-network",
    "LTC": "litecoin",
    "ONDO": "ondo-finance",
    "HYPE": "hyperliquid",
    "ARKM": "arkham",
}


def _base_ticker(symbol: str) -> str:
    """``BTCUSDT``/``BTC/USDT:USDT`` → ``BTC``. Strips venue quote + settlement suffixes."""
    s = str(symbol or "").upper().strip()
    for sep in ("/", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
    for quote in ("USDT", "USDC", "USD", "BUSD", "FDUSD"):
        if s.endswith(quote) and len(s) > len(quote):
            s = s[: -len(quote)]
            break
    return s


def _cache_path(symbol: str) -> Any:
    return MARKETCAP_CACHE / f"{_base_ticker(symbol)}.json"


def _read_cache(symbol: str) -> dict[str, Any] | None:
    path = _cache_path(symbol)
    try:
        if not path.exists():
            return None
        return serde.loads(path.read_text())
    except Exception:
        return None


def _write_cache(symbol: str, days: int, series: list[list[float]]) -> None:
    try:
        MARKETCAP_CACHE.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_ms": int(time.time() * 1000), "days": days, "series": series}
        _cache_path(symbol).write_text(serde.dumps_str(payload))
    except Exception:
        pass  # cache write is best-effort; never fatal


def _cache_fresh(entry: dict[str, Any], *, ttl_s: int) -> bool:
    try:
        return (time.time() * 1000 - float(entry["fetched_ms"])) < ttl_s * 1000
    except Exception:
        return False


def _parse_market_caps(payload: dict[str, Any]) -> list[list[float]]:
    """CoinGecko ``market_chart`` → ``{"market_caps": [[ts_ms, cap], ...]}``."""
    raw = payload.get("market_caps") if isinstance(payload, dict) else None
    out: list[list[float]] = []
    if isinstance(raw, list):
        for point in raw:
            if isinstance(point, (list, tuple)) and len(point) >= 2 and point[1] is not None:
                out.append([float(point[0]), float(point[1])])
    return out


async def _resolve_id(session: Any, ticker: str) -> str | None:
    """Ticker → CoinGecko id. Overrides first; else pick the highest-market-cap match so a
    ticker collision resolves to the real asset, not a low-cap clone."""
    if ticker in _ID_OVERRIDE:
        return _ID_OVERRIDE[ticker]
    try:
        url = f"{_COINGECKO_BASE}/coins/markets"
        params = {"vs_currency": "usd", "symbols": ticker.lower(), "order": "market_cap_desc", "per_page": "1", "page": "1"}
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            rows = await resp.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0].get("id")
    except Exception:
        return None
    return None


async def fetch_market_cap_series(
    symbol: str,
    *,
    days: int = _DEFAULT_DAYS,
    ttl_s: int = _DEFAULT_TTL_S,
) -> list[list[float]] | None:
    """Return ``[[ts_ms, market_cap], ...]`` for ``symbol`` or ``None`` (silent-fail).

    Serves a fresh disk cache without any network call; otherwise fetches from CoinGecko,
    updates the cache, and on ANY failure falls back to a stale cache if present, else
    ``None``. Never raises — a market-cap outage must not affect the live path.
    """
    ticker = _base_ticker(symbol)
    cached = _read_cache(symbol)
    if cached and _cache_fresh(cached, ttl_s=ttl_s):
        series = cached.get("series")
        return series if isinstance(series, list) else None

    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        # Own bare session (no proxy, trust_env=False): decoupled from the venue plane.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            coin_id = await _resolve_id(session, ticker)
            if not coin_id:
                log.debug("marketcap_id_unresolved", symbol=symbol, ticker=ticker)
                return _stale(cached)
            url = f"{_COINGECKO_BASE}/coins/{coin_id}/market_chart"
            params = {"vs_currency": "usd", "days": str(days)}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.debug("marketcap_http_error", symbol=symbol, status=resp.status)
                    return _stale(cached)
                payload = await resp.json()
        series = _parse_market_caps(payload)
        if len(series) >= 2:
            _write_cache(symbol, days, series)
            return series
        return _stale(cached)
    except Exception as exc:  # noqa: BLE001 — silent-fail is the contract
        log.debug("marketcap_fetch_failed", symbol=symbol, error=str(exc))
        return _stale(cached)


def _stale(cached: dict[str, Any] | None) -> list[list[float]] | None:
    if cached:
        series = cached.get("series")
        if isinstance(series, list):
            return series
    return None


def read_cached_series(symbol: str) -> list[list[float]] | None:
    """Synchronous cache-only read for the critical tick path — NEVER touches the network.

    The tick path must not block on HTTP, so it consumes only what a separate off-process
    refresher (``scripts/refresh_marketcap_cache.py`` / ``fetch_market_cap_series``) has
    already written. Staleness is tolerated: a stale cap series is still a useful доп-фактор
    (supply moves slowly), and freshness is the refresher's job. ``None`` when no cache."""
    return _stale(_read_cache(symbol))


__all__ = ["fetch_market_cap_series", "read_cached_series"]
