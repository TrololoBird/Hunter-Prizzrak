"""Hunter market data plane — CCXT (REST + Pro watch) only."""

from hunt_core.market.ccxt_guard import ccxt_method_available, ccxt_ws_method_available
from hunt_core.market.ccxt_rest import HuntCcxtRestGate
from hunt_core.market.factory import (
    create_async_binance_future,
    create_async_binance_spot,
    create_pro_binance_future,
    create_sync_binance_future,
    create_hunt_market_plane,
    create_hunt_market_plane_from_settings,
    fetch_klines_sync,
    fetch_klines_async,
    ccxt_ohlcv_to_frame,
    finalize_kline_frame,
)
from hunt_core.market.client import HuntCcxtClient, normalize_depth_levels
from hunt_core.market.capacity import HuntLoadPlanner
from hunt_core.market.cross import (
    CrossExchangeConfig,
    SECONDARY_EXCHANGES,
    apply_cross_exchange_env,
    apply_cross_snapshot_to_market,
    attach_cross_fields,
    attach_cross_microstructure,
    fetch_secondary_ticker_overlay,
    load_cross_exchange_config,
    merge_ws_cross_into_snapshot,
    refresh_cross_exchange_cache,
)
from hunt_core.market.live_price import apply_live_price_to_row, resolve_live_price
from hunt_core.market.symbol_gate import (
    gate_symbol_dict_keys,
    gate_symbol_list,
    is_allowed_for_analysis,
)
from hunt_core.market.symbols import SymbolResolutionError
from hunt_core.market.spot import HuntCcxtSpotCompanion
from hunt_core.market.streams import HuntCcxtStreams

__all__ = [
    "HuntCcxtClient",
    "HuntCcxtSpotCompanion",
    "HuntCcxtStreams",
    "HuntCcxtRestGate",
    "HuntLoadPlanner",
    "ccxt_method_available",
    "ccxt_ws_method_available",
    "create_async_binance_future",
    "create_async_binance_spot",
    "create_hunt_market_plane",
    "create_hunt_market_plane_from_settings",
    "create_pro_binance_future",
    "create_sync_binance_future",
    "fetch_klines_sync",
    "fetch_klines_async",
    "ccxt_ohlcv_to_frame",
    "finalize_kline_frame",
    "SymbolResolutionError",
    "CrossExchangeConfig",
    "SECONDARY_EXCHANGES",
    "apply_cross_exchange_env",
    "apply_cross_snapshot_to_market",
    "attach_cross_fields",
    "attach_cross_microstructure",
    "fetch_secondary_ticker_overlay",
    "load_cross_exchange_config",
    "merge_ws_cross_into_snapshot",
    "refresh_cross_exchange_cache",
    "apply_live_price_to_row",
    "resolve_live_price",
    "normalize_depth_levels",
    "gate_symbol_dict_keys",
    "gate_symbol_list",
    "is_allowed_for_analysis",
]
