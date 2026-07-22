"""Per-symbol exchange tick size — registry + conservative price quantization.

Binance USDⓈ-M ticks span 1e-8 (1000SATSUSDT, DOGSUSDT) to 1.0 (YFIUSDT).
Global ``round(x, 6)`` grids therefore sit 10-100× COARSER than the tick on the
cheap-meme tail (a 0.15% breakeven buffer on a 3.5e-5 price is 5e-8 — rounding
to 1e-6 erases it entirely), while for BTC-like prices they merely carry
sub-tick false precision. This module gives geometry/management code the real
exchange grid.

CCXT binanceusdm runs in ``TICK_SIZE`` precision mode, so
``market["precision"]["price"]`` IS the tick (a float like 0.1 or 1e-7), read
from the public ``exchangeInfo`` endpoint — no private API involved. The
registry is populated by :meth:`hunt_core.view.runtime.MarketRuntime.start` right
after the primary engine's ``load_markets`` (via :func:`register_ticks_from_markets`).

Conservative rounding sides (never promise more than the grid allows):

* long  → ``floor``: stop lands further below entry (honest, larger risk shown)
  and TP lands closer to entry (smaller promised reward).
* short → ``ceil``: mirror — stop further above, TP closer from below.

When the tick is unknown (registry not yet populated, foreign symbol) the
helpers fall back to ``round(price, 8)`` — Binance's maximum ``pricePrecision``
— which is strictly finer than every real tick and never coarser than the old
``round(x, 6)``.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any, Iterable, Literal, overload

import structlog

from hunt_core.market.symbols import is_linear_usdt_swap_market, to_binance_symbol

LOG = structlog.get_logger("hunt_core.market.tick_registry")

_FALLBACK_DECIMALS = 8  # Binance USDⓈ-M max pricePrecision

_TICKS: dict[str, float] = {}

QuantizeMode = Literal["nearest", "floor", "ceil"]

_ROUNDING = {
    "nearest": ROUND_HALF_EVEN,
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
}


def _normalize_symbol(symbol: str) -> str:
    """``BTC/USDT:USDT`` and ``BTCUSDT`` both key as ``BTCUSDT``."""
    sym = to_binance_symbol(symbol)
    return sym.split(":", 1)[0].replace("/", "")


def register_ticks_from_markets(markets: Iterable[Any]) -> int:
    """Populate the registry from loaded CCXT market rows (public metadata).

    Keeps only linear USDT perps (the scanner universe). Returns the number of
    ticks registered. Never raises — a malformed row is skipped.
    """
    count = 0
    for market in markets:
        if not is_linear_usdt_swap_market(market):
            continue
        precision = market.get("precision")
        if not isinstance(precision, dict):
            continue
        try:
            tick = float(precision.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if tick <= 0:
            continue
        key = _normalize_symbol(str(market.get("id") or market.get("symbol") or ""))
        if not key:
            continue
        _TICKS[key] = tick
        count += 1
    if count:
        LOG.debug("tick_registry_updated | n=%d", count)
    return count


def set_tick_sizes(mapping: dict[str, float]) -> None:
    """Directly seed ticks (tests / offline tooling)."""
    for sym, tick in mapping.items():
        if tick > 0:
            _TICKS[_normalize_symbol(sym)] = float(tick)


def tick_size_for(symbol: str) -> float | None:
    """Exchange tick for a symbol, or ``None`` when unknown."""
    return _TICKS.get(_normalize_symbol(symbol))


def quantize_to_tick(price: float, tick: float, *, mode: QuantizeMode = "nearest") -> float:
    """Snap ``price`` onto the ``tick`` grid (Decimal-exact, no float dust)."""
    if tick <= 0 or price <= 0:
        return price
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick))
    steps = (d_price / d_tick).to_integral_value(rounding=_ROUNDING[mode])
    return float(steps * d_tick)


@overload
def quantize_price(price: float, symbol: str, *, mode: QuantizeMode = ...) -> float: ...
@overload
def quantize_price(price: None, symbol: str, *, mode: QuantizeMode = ...) -> None: ...
def quantize_price(
    price: float | None, symbol: str, *, mode: QuantizeMode = "nearest"
) -> float | None:
    """Tick-quantized price; ``round(price, 8)`` fallback when tick unknown.

    ``None`` and non-positive prices pass through unchanged (phantom-safe).
    """
    if price is None:
        return None
    p = float(price)
    if p <= 0:
        return p
    tick = tick_size_for(symbol)
    if tick is None:
        return round(p, _FALLBACK_DECIMALS)
    return quantize_to_tick(p, tick, mode=mode)


@overload
def quantize_conservative(price: float, symbol: str, *, direction: str) -> float: ...
@overload
def quantize_conservative(price: None, symbol: str, *, direction: str) -> None: ...
def quantize_conservative(
    price: float | None, symbol: str, *, direction: str
) -> float | None:
    """Conservative-side quantization for stops AND take-profits.

    For a long, both the stop (below entry) and the TP (above entry) round
    DOWN — the stop moves further away (risk never understated), the TP moves
    closer (reward never overstated). For a short both round UP, mirrored.
    """
    mode: QuantizeMode = "ceil" if str(direction).lower() == "short" else "floor"
    return quantize_price(price, symbol, mode=mode)
