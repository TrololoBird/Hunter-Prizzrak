from __future__ import annotations


import structlog
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

LOG = structlog.get_logger(__name__)
if TYPE_CHECKING:
    import polars as pl

    from .config import BotSettings


@dataclass(frozen=True, slots=True)
class SymbolMeta:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    onboard_date_ms: int


@dataclass(frozen=True, slots=True)
class UniverseSymbol:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    onboard_date_ms: int
    quote_volume: float
    price_change_pct: float
    last_price: float
    trade_count_24h: int | None = None
    shortlist_bucket: str = ""
    shortlist_score: float | None = None
    shortlist_reasons: tuple[str, ...] = ()
    seed_source: str = "unknown"
    liquidity_rank: int | None = None
    strategy_fits: tuple[str, ...] = ()


@dataclass(slots=True)
class SymbolFrames:
    symbol: str
    df_1h: pl.DataFrame
    df_15m: pl.DataFrame
    bid_price: float | None
    ask_price: float | None
    df_5m: pl.DataFrame | None = None
    df_4h: pl.DataFrame | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    book_bids: list[tuple[float, float]] | None = None
    book_asks: list[tuple[float, float]] | None = None
    frame_source_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AggTradeSnapshot:
    symbol: str
    trade_count: int
    buy_qty: float
    sell_qty: float
    delta_ratio: float | None


@dataclass(frozen=True, slots=True)
class AggTrade:
    symbol: str
    trade_id: int
    price: float
    quantity: float
    trade_time_ms: int
    is_buyer_maker: bool

    @property
    def trade_time(self) -> datetime:
        return datetime.fromtimestamp(self.trade_time_ms / 1000.0, tz=UTC)


@dataclass(slots=True)
class PreparedSymbol:
    universe: UniverseSymbol
    work_1h: pl.DataFrame
    work_15m: pl.DataFrame
    bid_price: float | None
    ask_price: float | None
    spread_bps: float | None
    work_5m: pl.DataFrame | None = None
    work_1m: pl.DataFrame | None = None
    work_4h: pl.DataFrame | None = None
    work_primary: pl.DataFrame | None = None
    bias_4h: str = "neutral"  # 4H macro context (market regime)
    bias_1h: str = "neutral"  # 1H trading context for 15M signals
    # Optional fields populated from WS global streams (mark price / liquidations)
    mark_price: float | None = None
    ticker_price: float | None = None
    funding_rate: float | None = None
    funding_recent_extreme_rate: float | None = None
    funding_recent_extreme_age_hours: float | None = None
    oi_current: float | None = None
    oi_change_pct: float | None = None
    ls_ratio: float | None = None
    top_account_ls_ratio: float | None = None
    taker_ratio: float | None = None  # taker buy/sell volume ratio (>1.0 = net buyers)
    liquidation_score: float | None = None  # [0, 1]: short-liq share (0=long flush, 1=short squeeze)
    liquidation_cascade_5m: bool | None = None
    funding_rate_zscore_48h: float | None = None
    funding_trend: str | None = None  # "rising" | "falling" | "flat" | None
    estimated_settle_price: float | None = None
    interest_rate: float | None = None
    next_funding_time_ms: int | None = None
    funding_rate_cap: float | None = None
    funding_rate_floor: float | None = None
    funding_interval_hours: int | None = None
    basis_pct: float | None = (
        None  # (futures - index) / index * 100; + = contango, - = backwardation
    )
    global_ls_ratio: float | None = None
    global_account_ls_ratio: float | None = None
    top_trader_position_ratio: float | None = None
    top_position_ls_ratio: float | None = None
    top_vs_global_ls_gap: float | None = None
    mark_index_spread_bps: float | None = None
    premium_zscore_5m: float | None = None
    premium_slope_5m: float | None = None
    oi_slope_5m: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None
    depth_wall_pressure: float | None = None
    depth_imbalance_source: str | None = None
    microprice_bias_source: str | None = None
    depth_book_age_seconds: float | None = None
    agg_trade_delta_30s: float | None = None
    agg_trade_delta_60s: float | None = None
    agg_trade_buy_ratio_30s: float | None = None
    agg_trade_buy_ratio_60s: float | None = None
    aggression_shift: float | None = None
    orderflow_source: str | None = None
    liquidation_score_source: str | None = None
    liquidation_score_age_seconds: float | None = None
    spot_lead_return_1m: float | None = None
    spot_futures_spread_bps: float | None = None
    btc_bias: str | None = None
    eth_bias: str | None = None
    sol_bias: str | None = None
    xau_bias: str | None = None
    xag_bias: str | None = None
    pax_bias: str | None = None
    altcoin_season_index: float | None = None
    btc_phase: str | None = None
    global_market_regime: str | None = None
    macro_risk_mode: str | None = None
    benchmark_context: dict[str, Any] = field(default_factory=dict)
    market_ctx: dict[str, Any] = field(default_factory=dict)
    market_context_age_seconds: float | None = None
    mark_price_age_seconds: float | None = None
    ticker_price_age_seconds: float | None = None
    book_ticker_age_seconds: float | None = None
    context_snapshot_age_seconds: float | None = None
    data_freshness_flags: tuple[str, ...] = ()
    data_quality_flags: list[str] = field(default_factory=list)
    factor_panel: dict[str, float | None] = field(default_factory=dict)
    data_source_mix: str = "futures_only"
    degraded: bool = False
    degrade_reason: str | None = None
    fallback_used: str | None = None
    market_regime: str = "neutral"  # "trending" | "neutral" | "choppy"
    # Structure-based fields (Фаза 2 рефакторинга)
    structure_1h: str = "ranging"  # "uptrend" | "downtrend" | "ranging"
    regime_4h_confirmed: str = (
        "ranging"  # "uptrend" | "downtrend" | "ranging" (3+ bars) - macro only
    )
    regime_1h_confirmed: str = (
        "ranging"  # "uptrend" | "downtrend" | "ranging" (3+ bars) - trading context
    )
    poc_1h: float | None = None  # Point of Control on 1h (highest volume price)
    poc_15m: float | None = None  # Point of Control on 15m
    poc_direction_1h: str | None = None
    poc_direction_15m: str | None = None
    vah_1h: float | None = None
    val_1h: float | None = None
    vah_15m: float | None = None
    val_15m: float | None = None
    nearest_bid_wall: dict[str, Any] | None = None
    nearest_ask_wall: dict[str, Any] | None = None
    depth_zone_imbalance: dict[str, float] = field(default_factory=dict)
    maps_snapshot: dict[str, Any] | None = None
    liq_forward_confidence: float | None = None
    map_stacked_imbalance: str | None = None
    primary_timeframe: str = "15m"
    context_timeframes: tuple[str, ...] = ("1h", "4h")
    settings: BotSettings | None = None
    reject_log: tuple[dict[str, Any], ...] = ()
    btc_change_pct: float | None = None
    eth_change_pct: float | None = None
    btc_corr_1h: float | None = None
    btc_beta_1h: float | None = None
    pump_cycle: dict[str, Any] | None = None
    btc_decoupled_pump: bool | None = None
    btc_decoupled_dump: bool | None = None

    def __post_init__(self) -> None:
        if self.top_account_ls_ratio is None and self.ls_ratio is not None:
            self.top_account_ls_ratio = self.ls_ratio
        if self.ls_ratio is None and self.top_account_ls_ratio is not None:
            self.ls_ratio = self.top_account_ls_ratio
        if self.global_account_ls_ratio is None and self.global_ls_ratio is not None:
            self.global_account_ls_ratio = self.global_ls_ratio
        if self.global_ls_ratio is None and self.global_account_ls_ratio is not None:
            self.global_ls_ratio = self.global_account_ls_ratio
        if self.top_position_ls_ratio is None and self.top_trader_position_ratio is not None:
            self.top_position_ls_ratio = self.top_trader_position_ratio
        if self.top_trader_position_ratio is None and self.top_position_ls_ratio is not None:
            self.top_trader_position_ratio = self.top_position_ls_ratio
        if not isinstance(self.reject_log, tuple):
            self.reject_log = tuple(self.reject_log)
        if self.work_primary is None:
            if self.primary_timeframe == "5m" and self.work_5m is not None:
                self.work_primary = self.work_5m
            elif self.primary_timeframe == "1h":
                self.work_primary = self.work_1h
            elif self.primary_timeframe == "4h" and self.work_4h is not None:
                self.work_primary = self.work_4h
            else:
                self.work_primary = self.work_15m

    @property
    def symbol(self) -> str:
        return self.universe.symbol

    @property
    def atr_pct(self) -> float | None:
        if self.work_15m.is_empty() or "atr_pct" not in self.work_15m.columns:
            return None
        value = self.work_15m.item(-1, "atr_pct")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @property
    def volume_ratio(self) -> float | None:
        if self.work_15m.is_empty() or "volume_ratio20" not in self.work_15m.columns:
            return None
        value = self.work_15m.item(-1, "volume_ratio20")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @property
    def adx_1h(self) -> float | None:
        if self.work_1h.is_empty() or "adx14" not in self.work_1h.columns:
            return None
        value = self.work_1h.item(-1, "adx14")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None




# ── Typed Events ─────────────────────────────────────────────────────────────


from typing import Protocol, runtime_checkable


@runtime_checkable
class Event(Protocol):
    """Typed event protocol — timestamp + symbol for all pipeline events."""
    timestamp: datetime
    symbol: str


@dataclass(frozen=True, slots=True)
class TickProcessed:
    """Emitted after each symbol tick is fully processed."""
    timestamp: datetime
    symbol: str
    plane: str
    tick_path: str
    has_error: bool
    verdict_action: str
    fresh_row: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerdictProduced:
    """Emitted when Verdict V2 produces a scenario verdict for a symbol."""
    timestamp: datetime
    symbol: str
    action: str
    path_type: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SignalEmitted:
    """Emitted when a signal passes lifecycle and is ready for delivery."""
    timestamp: datetime
    symbol: str
    setup_id: str
    direction: str
    event: str
    score: float
    state: str
