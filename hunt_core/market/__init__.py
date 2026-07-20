"""Hunter market plane — transport-agnostic CCXT symbol/gate helpers (ADR-0004).

The legacy ccxt transport (``HuntCcxtClient`` / ``HuntCcxtStreams`` / spot companion / cross-venue REST
plane / rate-gate) was deleted at the engine cutover — the ccxt.pro engine (:mod:`hunt_core.engine`) is
the sole transport now. What remains here is the pure, transport-agnostic surface every consumer still
needs: symbol id ↔ unified resolution, the universe-ticker normalizer, the tradability gate, and the
egress/network helpers. Import submodules directly (``hunt_core.market.symbols`` /
``hunt_core.market.network`` / ``hunt_core.market.tick_registry``) for the rest.
"""

from hunt_core.market.symbol_gate import gate_symbol_list, is_allowed_for_analysis
from hunt_core.market.symbols import (
    SymbolResolutionError,
    fetch_ticker_rows,
    filter_tradable_symbols,
    is_crypto_underlying,
    is_linear_usdt_swap_market,
    normalize_ticker_rows,
    to_binance_symbol,
    to_ccxt_symbol,
    try_binance_id_from_ccxt,
    underlying_type_of,
)

__all__ = [
    "SymbolResolutionError",
    "fetch_ticker_rows",
    "filter_tradable_symbols",
    "gate_symbol_list",
    "is_allowed_for_analysis",
    "is_crypto_underlying",
    "is_linear_usdt_swap_market",
    "normalize_ticker_rows",
    "to_binance_symbol",
    "to_ccxt_symbol",
    "try_binance_id_from_ccxt",
    "underlying_type_of",
]
