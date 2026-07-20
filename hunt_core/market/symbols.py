"""Binance USD-M symbol id ↔ CCXT unified symbol (strict — ``exchange.market()`` only)."""
from __future__ import annotations



import structlog
from typing import Any

import ccxt

LOG = structlog.get_logger("hunt_core.market.symbols")
def is_linear_usdt_swap_market(market: Any) -> bool:
    """True for USDⓈ-M linear perp rows in CCXT ``markets``."""
    if not isinstance(market, dict):
        return False
    if market.get("spot"):
        return False
    if str(market.get("settle") or "").upper() != "USDT":
        return False
    return str(market.get("type") or "") in {"swap", "future"}


def underlying_type_of(market: Any) -> str:
    """Binance ``underlyingType`` for a CCXT market row (COIN | EQUITY | COMMODITY | …).

    Binance USDⓈ-M now lists tokenized equities, commodities, index and pre-market
    perps alongside real crypto. exchangeInfo tags each: crypto is ``COIN``; the rest
    are ``EQUITY`` (BABA, COIN, MSTR…), ``COMMODITY`` (NATGAS, XAU/XAG), ``INDEX``,
    ``KR_EQUITY``, ``PREMARKET``. Empty string when unknown (older market rows).
    """
    if not isinstance(market, dict):
        return ""
    info = market.get("info")
    info = info if isinstance(info, dict) else {}
    return str(info.get("underlyingType") or "").upper()


def is_crypto_underlying(market: Any) -> bool:
    """True only for real-crypto perps (``underlyingType == COIN``).

    Unknown/absent type is treated as crypto (fail-open) so an exchangeInfo shape
    change never silently empties the scanner universe — the explicit non-COIN classes
    (EQUITY/COMMODITY/INDEX/…) are what we exclude.
    """
    ut = underlying_type_of(market)
    return ut in {"", "COIN"}


class SymbolResolutionError(LookupError):
    """Symbol cannot be resolved against loaded CCXT markets."""


def to_binance_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _require_loaded_exchange(exchange: Any) -> None:
    if exchange is None:
        raise TypeError("exchange is required for symbol resolution")
    if not getattr(exchange, "markets", None):
        raise RuntimeError(
            f"{getattr(exchange, 'id', 'exchange')}: markets not loaded — call load_markets() first"
        )


def to_ccxt_symbol(symbol: str, *, exchange: Any) -> str:
    """Resolve Binance id or unified symbol via CCXT ``exchange.market()``."""
    _require_loaded_exchange(exchange)
    sym = to_binance_symbol(symbol)
    if not sym:
        raise SymbolResolutionError("empty symbol")
    market = exchange.market(sym)
    unified = str(market.get("symbol") or "")
    if not unified:
        raise SymbolResolutionError(f"ccxt market has no unified symbol for {sym!r}")
    return unified


def resolve_linear_usdt_swap(binance_sym: str, *, exchange: Any) -> str:
    """Map Binance linear USDT id → unified CCXT swap symbol on any venue."""
    _require_loaded_exchange(exchange)
    sym = to_binance_symbol(binance_sym)
    if not sym.endswith("USDT"):
        raise SymbolResolutionError(f"not a USDT linear id: {sym}")
    base = sym[:-4]
    if not base:
        raise SymbolResolutionError(f"empty base in {sym}")

    for candidate in (sym, f"{base}/USDT:USDT"):
        try:
            market = exchange.market(candidate)
        except (ccxt.BadSymbol, ccxt.ExchangeError):
            continue
        if is_linear_usdt_swap_market(market):
            unified = str(market.get("symbol") or "")
            if unified:
                return unified

    for market in exchange.markets.values():
        if not is_linear_usdt_swap_market(market):
            continue
        if to_binance_symbol(str(market.get("id") or "")) == sym:
            return str(market["symbol"])
        if (
            str(market.get("base") or "").upper() == base
            and str(market.get("quote") or "").upper() == "USDT"
        ):
            return str(market["symbol"])

    ex_id = getattr(exchange, "id", "exchange")
    raise SymbolResolutionError(f"no USDT linear swap for {sym!r} on {ex_id}")


def try_resolve_linear_usdt_swap(binance_sym: str, *, exchange: Any) -> str | None:
    try:
        return resolve_linear_usdt_swap(binance_sym, exchange=exchange)
    except (SymbolResolutionError, ccxt.BadSymbol, ccxt.ExchangeError):
        return None


def from_ccxt_symbol(symbol: str, *, exchange: Any) -> str:
    """Map CCXT unified symbol → Binance market id (e.g. BTCUSDT)."""
    _require_loaded_exchange(exchange)
    raw = str(symbol or "").strip()
    if not raw:
        raise SymbolResolutionError("empty ccxt symbol")
    market = exchange.market(raw)
    market_id = to_binance_symbol(str(market.get("id") or ""))
    if not market_id:
        raise SymbolResolutionError(f"ccxt market has no id for {raw!r}")
    return market_id


def try_binance_id_from_ccxt(symbol: str, *, exchange: Any) -> str | None:
    """Best-effort map for CCXT bulk payloads (skip empty/malformed keys with warning)."""
    raw = str(symbol or "").strip()
    if not raw:
        return None
    try:
        return from_ccxt_symbol(raw, exchange=exchange)
    except (SymbolResolutionError, ccxt.BadSymbol, ccxt.ExchangeError) as exc:
        LOG.debug("ccxt_symbol_to_binance_skipped | raw=%s error=%s", raw, exc)
        return None


def is_tradable_linear_usdt(symbol: str, *, exchange: Any) -> bool:
    """True when symbol resolves to an active USDⓈ-M linear perp in loaded CCXT markets."""
    sym = to_binance_symbol(symbol)
    if not sym:
        return False
    return try_resolve_linear_usdt_swap(sym, exchange=exchange) is not None


def filter_tradable_symbols(
    symbols: list[str] | tuple[str, ...] | set[str],
    *,
    exchange: Any,
    label: str = "universe",
) -> list[str]:
    """Drop delisted / unknown ids before watch, WS, or REST analysis."""
    out: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        sym = to_binance_symbol(str(raw or ""))
        if not sym or sym in seen:
            continue
        if is_tradable_linear_usdt(sym, exchange=exchange):
            seen.add(sym)
            out.append(sym)
        else:
            dropped.append(sym)
    if dropped:
        LOG.info(
            "symbol_gate_dropped | label=%s dropped=%s kept=%d",
            label,
            dropped[:12],
            len(out),
        )
    return out


def _ticker_float(value: Any) -> float:
    """ccxt ticker field → finite float, ``0.0`` fail-safe (a missing/garbage field is not fabricated)."""
    if value is None:
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and out not in (float("inf"), float("-inf")) else 0.0


def normalize_ticker_rows(
    exchange: Any, tickers: dict[str, dict[str, Any]]
) -> list[dict[str, float | str]]:
    """ccxt bulk ``fetch_tickers`` payload → the normalized 24h ticker rows the scanner funnel reads.

    Projects every linear-USDT-swap ccxt ticker onto the row shape the universe funnel / regime
    calibration consume (``symbol`` as the Binance id, ``last_price``, ``price_change_percent``,
    ``quote_volume``, ``trade_count``, ``underlying_type`` + optional ``high_price``/``low_price``).
    This is the engine-native replacement for the old ``HuntCcxtClient.fetch_ticker_24h`` body —
    fail-loud: a symbol with no last/quote-volume is dropped, never fabricated. ``exchange.markets``
    must be loaded (the engine loads them on start).
    """
    rows: list[dict[str, float | str]] = []
    for ccxt_sym, item in (tickers or {}).items():
        if not isinstance(item, dict):
            continue
        market = exchange.markets.get(ccxt_sym) if getattr(exchange, "markets", None) else None
        if not is_linear_usdt_swap_market(market):
            continue
        sym = try_binance_id_from_ccxt(ccxt_sym, exchange=exchange)
        if not sym:
            continue
        last_price = _ticker_float(item.get("last"))
        quote_volume = _ticker_float(item.get("quoteVolume"))
        if last_price <= 0 or quote_volume <= 0:
            continue
        raw_info = item.get("info")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        row: dict[str, float | str] = {
            "symbol": sym,
            "last_price": last_price,
            "price_change_percent": _ticker_float(item.get("percentage")),
            "quote_volume": quote_volume,
            "trade_count": _ticker_float(info.get("count")),
            # Asset class from exchangeInfo so the scanner gate can drop tokenized
            # equities/commodities (COIN = real crypto). Kept on the row — not filtered
            # here — so /signal and pinned metals still get their ticker.
            "underlying_type": underlying_type_of(market),
        }
        high = _ticker_float(item.get("high"))
        low = _ticker_float(item.get("low"))
        if high > 0:
            row["high_price"] = high
        if low > 0:
            row["low_price"] = low
        rows.append(row)
    return rows


async def fetch_ticker_rows(exchange: Any) -> list[dict[str, float | str]]:
    """Fetch + normalize the whole-universe 24h tickers off the engine's ccxt exchange (fail-loud ``[]``).

    The single universe-wide REST batch (weight ~40) the scanner funnel, prescan and regime
    calibration all rank against. Delegates the raw call to :func:`hunt_core.engine.rest.fetch_all_tickers`
    (which returns ``{}`` on failure) and projects it via :func:`normalize_ticker_rows`.
    """
    from hunt_core.engine.rest import fetch_all_tickers

    return normalize_ticker_rows(exchange, await fetch_all_tickers(exchange))
