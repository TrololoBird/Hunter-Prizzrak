"""Signal trade-plan contract helpers — overlaps with engine imported from canonical source.

Shared constants/types/functions live in ``engine.contract_base`` and
``engine.contract``. This module re-exports them and adds hunt-specific
validation logic (``validate_signal_contract``, ``worst_entry_edge``, etc.).

The bot is signal-only: each emitted setup must be directly usable as a
manual limit-order plan. This module keeps the plan math centralized so
individual detectors do not drift into point entries or partial target gaps.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from hunt_core.errors import DEFENSIVE_EXC

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# ── Shared contract types/constants (inlined from engine.contract_base) ──

DEFAULT_SCALE_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)
DEFAULT_TARGET_RR: tuple[float, float, float] = (1.9, 3.0, 5.0)
DEFAULT_MIN_RISK_REWARD = 1.9
DEFAULT_MAX_RISK_REWARD = 10.0
RISK_REWARD_EPSILON = 1e-9
SIGNAL_ENTRY_PAD_ATR: float = 0.35

TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


@dataclass(frozen=True, slots=True)
class SignalContractIssue:
    field: str
    reason: str
    value: object = None

    def to_dict(self) -> dict[str, object]:
        return {"field": self.field, "reason": self.reason, "value": self.value}


# ── Shared contract functions (inlined from engine.contract) ──

def _finite_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def positive_float(value: object, default: float | None = None) -> float | None:
    numeric = _finite_float(value, default=None)
    if numeric is None or numeric <= 0.0:
        return default
    return numeric


def normalize_direction(direction: str) -> str | None:
    value = str(direction or "").strip().lower()
    if value in {"long", "buy", "bull", "bullish"}:
        return "long"
    if value in {"short", "sell", "bear", "bearish"}:
        return "short"
    return None


def resolve_target_rr(settings: Any | None = None) -> tuple[float, float, float]:
    if settings is None:
        return DEFAULT_TARGET_RR
    try:
        raw = getattr(settings, "target_rr", None)
        if raw is not None and len(raw) == 3:
            return tuple(float(x) for x in raw)  # type: ignore[return-value]
    except (TypeError, ValueError):
        pass
    return DEFAULT_TARGET_RR


def _timeframe_minutes(timeframe: str | None) -> int:
    raw = str(timeframe or "15m").lower().strip()
    primary = raw.split("+", 1)[0].strip()
    if primary in TIMEFRAME_MINUTES:
        return TIMEFRAME_MINUTES[primary]
    if primary.endswith("m"):
        numeric = positive_float(primary[:-1])
        return int(numeric) if numeric else 15
    if primary.endswith("h"):
        numeric = positive_float(primary[:-1])
        return int(numeric * 60) if numeric else 60
    return 15


_SETUP_TTL_MINUTES: dict[str, int] = {}
_SETUP_TTL_BARS: dict[str, int] = {}
_FAMILY_TTL_BARS: dict[str, int] = {}


def default_ttl_bars(setup_id: str, strategy_family: str, timeframe: str | None = None) -> int:
    if setup_id in _SETUP_TTL_MINUTES:
        tf_min = max(1, _timeframe_minutes(timeframe))
        return max(1, min(96, round(_SETUP_TTL_MINUTES[setup_id] / tf_min)))
    if setup_id in _SETUP_TTL_BARS:
        return _SETUP_TTL_BARS[setup_id]
    family = str(strategy_family or "").strip().lower()
    if family in _FAMILY_TTL_BARS:
        return _FAMILY_TTL_BARS[family]
    minutes = _timeframe_minutes(timeframe)
    if minutes <= 5:
        return 24
    if minutes >= 60:
        return 8
    return 24


def valid_until_from(
    *,
    created_at: datetime | None,
    setup_id: str,
    strategy_family: str,
    timeframe: str | None,
    ttl_bars: int | None = None,
) -> datetime:
    anchor = created_at or datetime.now(UTC)
    anchor = anchor.replace(tzinfo=UTC) if anchor.tzinfo is None else anchor.astimezone(UTC)
    bars = int(ttl_bars) if ttl_bars is not None else default_ttl_bars(setup_id, strategy_family, timeframe)
    bars = max(1, min(bars, 96))
    return anchor + timedelta(minutes=_timeframe_minutes(timeframe) * bars)


def normalize_scale_weights(
    weights: tuple[float, float, float] | list[float] | None,
) -> tuple[float, float, float]:
    if not weights or len(weights) != 3:
        return DEFAULT_SCALE_WEIGHTS
    cleaned: list[float] = []
    for value in weights:
        numeric = _finite_float(value, default=0.0) or 0.0
        cleaned.append(max(0.0, numeric))
    total = sum(cleaned)
    if total <= 0.0:
        return DEFAULT_SCALE_WEIGHTS
    normalized = tuple(round(value / total, 4) for value in cleaned)
    drift = round(1.0 - sum(normalized), 4)
    return (normalized[0] + drift, normalized[1], normalized[2])

if TYPE_CHECKING:
    from collections.abc import Mapping

LOG = logging.getLogger("hunt_core.contract")



def validate_signal_contract(
    signal: Any,
    *,
    now: datetime | None = None,
    min_risk_reward: float | None = None,
) -> list[SignalContractIssue]:
    effective_min_rr = (
        float(min_risk_reward) if min_risk_reward is not None else DEFAULT_MIN_RISK_REWARD
    )
    issues: list[SignalContractIssue] = []
    direction = normalize_direction(getattr(signal, "direction", ""))
    if direction is None:
        issues.append(
            SignalContractIssue("direction", "invalid", getattr(signal, "direction", None))
        )
        direction = "long"

    entry_low = positive_float(getattr(signal, "entry_low", None))
    entry_high = positive_float(getattr(signal, "entry_high", None))
    stop_loss = positive_float(
        getattr(signal, "stop_loss", None)
        if hasattr(signal, "stop_loss")
        else getattr(signal, "stop", None)
    )
    tp1 = positive_float(
        getattr(signal, "tp1", None)
        if hasattr(signal, "tp1")
        else getattr(signal, "take_profit_1", None)
    )
    tp2 = positive_float(
        getattr(signal, "tp2", None)
        if hasattr(signal, "tp2")
        else getattr(signal, "take_profit_2", None)
    )
    tp3 = positive_float(
        getattr(signal, "tp3", None)
        if hasattr(signal, "tp3")
        else getattr(signal, "take_profit_3", None)
    )
    valid_until = getattr(signal, "valid_until", None)
    scale_weights_raw = getattr(signal, "scale_weights", DEFAULT_SCALE_WEIGHTS)

    required_values = {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
    }
    for field, value in required_values.items():
        if value is None:
            issues.append(
                SignalContractIssue(field, "missing_or_non_positive", getattr(signal, field, None))
            )
    if entry_low is not None and entry_high is not None:
        if entry_low >= entry_high:
            issues.append(SignalContractIssue("entry_zone", "not_a_range", (entry_low, entry_high)))
        entry_mid = (entry_low + entry_high) / 2.0
        if stop_loss is not None:
            if direction == "long" and stop_loss >= entry_low:
                issues.append(
                    SignalContractIssue(
                        "stop_loss",
                        "long_stop_not_below_entry_low",
                        stop_loss,
                    )
                )
            if direction == "short" and stop_loss <= entry_high:
                issues.append(
                    SignalContractIssue(
                        "stop_loss",
                        "short_stop_not_above_entry_high",
                        stop_loss,
                    )
                )
            if direction == "long" and stop_loss >= entry_mid:
                issues.append(
                    SignalContractIssue("stop_loss", "long_stop_not_below_entry", stop_loss)
                )
            if direction == "short" and stop_loss <= entry_mid:
                issues.append(
                    SignalContractIssue("stop_loss", "short_stop_not_above_entry", stop_loss)
                )
        if tp1 is not None and tp2 is not None and tp3 is not None:
            if direction == "long" and not (entry_mid < tp1 <= tp2 <= tp3):
                issues.append(
                    SignalContractIssue("targets", "long_targets_not_ordered", (tp1, tp2, tp3))
                )
            if direction == "short" and not (entry_mid > tp1 >= tp2 >= tp3):
                issues.append(
                    SignalContractIssue("targets", "short_targets_not_ordered", (tp1, tp2, tp3))
                )
            if stop_loss is not None:
                worst = entry_high if direction == "short" else entry_low
                risk = abs(worst - stop_loss)
                reward = abs(tp1 - worst)
                if risk <= 0.0:
                    issues.append(SignalContractIssue("risk_reward", "zero_or_negative_risk", risk))
                else:
                    risk_reward = reward / risk
                    if risk_reward + RISK_REWARD_EPSILON < effective_min_rr:
                        issues.append(
                            SignalContractIssue(
                                "risk_reward",
                                "tp1_rr_below_minimum",
                                round(risk_reward, 6),
                            )
                        )
                    elif risk_reward > DEFAULT_MAX_RISK_REWARD:
                        issues.append(
                            SignalContractIssue(
                                "risk_reward",
                                "tp1_rr_above_maximum",
                                round(risk_reward, 6),
                            )
                        )
    try:
        scale_weights = [float(item) for item in scale_weights_raw]
    except (TypeError, ValueError):
        scale_weights = []
    if len(scale_weights) < 2:
        issues.append(
            SignalContractIssue(
                "scale_weights", "less_than_two_entry_allocations", scale_weights_raw
            )
        )
    elif any(not math.isfinite(item) or item <= 0.0 for item in scale_weights):
        issues.append(
            SignalContractIssue("scale_weights", "non_positive_or_non_finite", scale_weights_raw)
        )
    else:
        total_weight = sum(scale_weights)
        max_weight = max(scale_weights)
        if max_weight <= 1.0 and total_weight > 1.000001:
            issues.append(
                SignalContractIssue(
                    "scale_weights", "fraction_sum_above_one", round(total_weight, 6)
                )
            )
        if max_weight > 1.0 and total_weight > 100.000001:
            issues.append(
                SignalContractIssue(
                    "scale_weights", "percent_sum_above_100", round(total_weight, 6)
                )
            )
    if not isinstance(valid_until, datetime):
        issues.append(SignalContractIssue("valid_until", "missing_or_not_datetime", valid_until))
    else:
        check_now = now or datetime.now(UTC)
        if check_now.tzinfo is None:
            check_now = check_now.replace(tzinfo=UTC)
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        if valid_until <= check_now.astimezone(UTC):
            issues.append(SignalContractIssue("valid_until", "expired", valid_until.isoformat()))
    return issues


def signal_contract_row(signal: Any) -> dict[str, object]:
    issues = validate_signal_contract(signal)
    valid_until = getattr(signal, "valid_until", None)
    return {
        "symbol": getattr(signal, "symbol", None),
        "setup_id": getattr(signal, "setup_id", None),
        "direction": getattr(signal, "direction", None),
        "entry_zone": [
            getattr(signal, "entry_low", None),
            getattr(signal, "entry_high", None),
        ],
        "stop_loss": getattr(signal, "stop_loss", getattr(signal, "stop", None)),
        "tp1": getattr(signal, "tp1", getattr(signal, "take_profit_1", None)),
        "tp2": getattr(signal, "tp2", getattr(signal, "take_profit_2", None)),
        "tp3": getattr(signal, "tp3", getattr(signal, "take_profit_3", None)),
        "valid_until": valid_until.isoformat()
        if isinstance(valid_until, datetime)
        else valid_until,
        "scale_weights": list(getattr(signal, "scale_weights", DEFAULT_SCALE_WEIGHTS)),
        "ok": not issues,
        "issues": [issue.to_dict() for issue in issues],
    }

Direction = Literal["short", "long"]
BtOutcome = Literal["tp1_hit", "tp2_hit", "sl_hit", "timeout"]
CloseReason = Literal[
    "stop_hit",
    "tp1",
    "tp2",
    "invalidate",
    "lifecycle_stale",
    "bias_flip",
    "timeout",
    "manual",
    "reclaim",
]


class LifecycleBlock(TypedDict, total=False):
    phase: str
    recommended_bias: str
    short_entry_ok: bool
    long_entry_ok: bool
    fall_from_high_pct: float | None
    bounce_from_low_pct: float | None


class DumpBlock(TypedDict, total=False):
    phase: str
    score: float | None
    fuel: float | None
    triggers: list[str]
    confirm_hard: list[str]
    impulse_confirmed: bool
    entry_zone: list[float] | None
    support_break_level: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    invalidation_above: float | None
    levels_viable: bool
    levels_veto: str | None


class LongBlock(TypedDict, total=False):
    impulse_confirmed: bool
    score: float | None
    fuel: float | None
    entry_zone: list[float] | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None


class MarketBlock(TypedDict, total=False):
    taker_5m: float | None
    oi_chg_1h: float | None
    oi_z_score: float | None
    funding_pct: float | None
    top_ls_1h: float | None
    depth_imbalance: float | None
    liquidation_score_5m: float | None
    microprice_bias: float | None
    liq_heatmap_nearest_long: float | None
    liq_heatmap_nearest_short: float | None
    liq_cascade_risk: str | None
    liq_forward_confidence: float | None
    map_sticky_wall_count: int | None
    map_stacked_imbalance: str | None
    map_vp_poc: float | None
    map_vp_accumulation: float | None
    map_vp_va_width_pct: float | None
    map_vp_va_contraction: float | None
    map_cvd_divergence: str | None
    map_accum_bid_absorption: bool | None
    map_void_above: float | None
    map_void_above_pct: float | None
    map_ask_thinning: bool | None
    liq_forward_weight: float | None
    liq_squeeze_fuel_long: float | None
    liq_squeeze_fuel_short: float | None
    liq_funding_rate: float | None
    map_accumulation_score: float | None
    map_oi_z: float | None


MARKET_DESCRIPTIONS: dict[str, str] = {
    "liq_heatmap_nearest_long": "Nearest long liquidation magnet below price (real+forward map)",
    "liq_heatmap_nearest_short": "Nearest short squeeze magnet above price",
    "liq_cascade_risk": "Cascade direction: long_flush | short_squeeze",
    "liq_forward_confidence": "Forward OID overlay confidence vs realized WS events",
    "liq_forward_weight": "Effective forward zone weight in signals (confidence × blend)",
    "liq_squeeze_fuel_short": "Short-squeeze fuel 0-1 (crowded shorts + neg funding + short magnets above) → pump",
    "liq_squeeze_fuel_long": "Long-squeeze fuel 0-1 (crowded longs + pos funding + long magnets below) → dump",
    "liq_funding_rate": "Funding rate threaded into liquidation squeeze model",
    "map_accumulation_score": "Pre-pump accumulation fusion 0-1 (VP coil + bid absorption + thin asks + bullish CVD + rising OI)",
    "map_oi_z": "Open-interest z-score in maps (OI↑ + price flat = accumulation)",
    "map_sticky_wall_count": "Count of persistent resting walls (anti-spoof)",
    "map_stacked_imbalance": "Footprint stacked buy/sell imbalance",
    "map_cvd_divergence": "Price vs CVD divergence (bullish_div | bearish_div)",
    "map_vp_poc": "Volume profile point of control (1h primary)",
    "map_vp_accumulation": "VP coil/accumulation score 0-1 (contraction + stable POC)",
    "map_vp_va_width_pct": "Value-area width (VAH-VAL) as % of POC — tighter = coil",
    "map_vp_va_contraction": "Value-area width ratio vs longer period (<1 = coil)",
    "map_accum_bid_absorption": "Sticky bid + bid-side absorption (accumulation)",
    "map_void_above": "Nearest liquidity void above price (fast-move path)",
    "map_void_above_pct": "Distance % to nearest liquidity void above price",
    "map_ask_thinning": "Ask-side thinning above — path of least resistance up",
    "map_iceberg_count": "Detected iceberg/replenishment levels",
    "map_absorption_count": "Absorption zones (resting + aggressive opposite flow)",
}


class TickRow(TypedDict, total=False):
    ts: str
    symbol: str
    price: float
    chg_24h_pct: float | None
    range_24h_pct: float | None
    lifecycle: LifecycleBlock
    dump: DumpBlock
    long: LongBlock
    market: MarketBlock
    regime: dict[str, Any]
    session: dict[str, Any]
    book_walls: dict[str, Any]
    maps: dict[str, Any]


class TrackerFeatureVector(TypedDict, total=False):
    ts: str | None
    price: float | None
    market: dict[str, Any]
    regime: dict[str, Any]
    lifecycle_phase: str | None
    lifecycle_bias: str | None
    fall_from_high_pct: float | None
    bounce_from_low_pct: float | None
    pos_in_range: float | None


class SignalRecord(TypedDict, total=False):
    symbol: str
    direction: Direction
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    invalidation_above: float | None
    invalidation_below: float | None
    fuel: float | None
    entry_lifecycle_phase: str | None
    entry_lifecycle_bias: str | None
    close_reason: CloseReason | str | None
    exit_price: float | None
    pnl_pct: float | None
    mfe_pct: float | None
    duration_min: float | None
    extreme_hi: float | None
    extreme_lo: float | None
    entry_message_id: int | None
    opened_at: str | None
    closed_at: str | None
    features_open: TrackerFeatureVector
    features_peak: TrackerFeatureVector
    features_close: TrackerFeatureVector


class OutcomeRecord(TypedDict, total=False):
    symbol: str
    direction: Direction
    lifecycle_phase: str
    fuel: float | None
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    bt_outcome: BtOutcome
    bt_mfe_pct: float | None
    bt_mae_pct: float | None
    bt_candles_to_tp1: int | None
    opened_at: str | None
    source: str
    grade_id: str | None


def normalize_tick_row(row: dict[str, Any]) -> dict[str, Any]:
    """Dedupe positioning==market; ensure nested dicts."""
    out = dict(row)
    market = out.get("market") or out.get("positioning") or {}
    if isinstance(market, dict):
        out["market"] = dict(market)
    out.pop("positioning", None)
    for key in ("lifecycle", "dump", "long", "regime", "session", "book_walls"):
        val = out.get(key)
        if val is not None and not isinstance(val, dict):
            out[key] = {}
    return out


def outcome_from_row(row: dict[str, Any], *, source: str) -> OutcomeRecord:
    """Build OutcomeRecord from graded JSONL row."""
    phase = row.get("lifecycle_phase") or row.get("entry_lifecycle_phase") or "unknown"
    return OutcomeRecord(
        symbol=str(row.get("symbol", "")),
        direction=row.get("direction", "short"),  # type: ignore[typeddict-item]
        lifecycle_phase=str(phase),
        fuel=row.get("fuel"),
        entry_lo=float(row.get("entry_lo") or 0),
        entry_hi=float(row.get("entry_hi") or 0),
        stop_loss=float(row.get("stop_loss") or 0),
        tp1=float(row.get("tp1") or 0),
        tp2=float(row.get("tp2") or 0),
        bt_outcome=row.get("bt_outcome", "timeout"),  # type: ignore[typeddict-item]
        bt_mfe_pct=row.get("bt_mfe_pct"),
        bt_mae_pct=row.get("bt_mae_pct"),
        bt_candles_to_tp1=row.get("bt_candles_to_tp1"),
        opened_at=row.get("opened_at"),
        source=source,
        grade_id=row.get("grade_id"),
    )


# --- Feature Contract ---

PUBLIC_FEATURE_SCHEMA_VERSION = "v1"
PUBLIC_FEATURE_FIELDS: tuple[str, ...] = (
    "rsi_15m",
    "rsi_1h",
    "rsi_4h",
    "adx_1h",
    "adx_4h",
    "atr_pct_15m",
    "volume_ratio_15m",
    "macd_histogram_15m",
    "ema20_above_ema50_15m",
    "ema50_above_ema200_15m",
    "ema20_above_ema50_1h",
    "ema50_above_ema200_1h",
    "supertrend_dir_1h",
    "supertrend_dir_15m",
    "obv_above_ema_15m",
    "bb_pct_b_15m",
    "bb_width_15m",
    "funding_rate",
    "oi_current",
    "oi_change_pct",
    "oi_slope_5m",
    "ls_ratio",
    "global_ls_ratio",
    "top_trader_position_ratio",
    "top_vs_global_ls_gap",
    "liquidation_score",
    "mark_index_spread_bps",
    "premium_zscore_5m",
    "premium_slope_5m",
    "context_snapshot_age_seconds",
    "depth_imbalance",
    "microprice_bias",
    "agg_trade_delta_30s",
    "aggression_shift",
    "spot_lead_return_1m",
    "spot_futures_spread_bps",
    "mark_price_age_seconds",
    "ticker_price_age_seconds",
    "book_ticker_age_seconds",
    "data_source_mix",
    "market_regime",
    "vah_1h",
    "val_1h",
    "vah_15m",
    "val_15m",
    "funding_rate_zscore_48h",
    "liquidation_cascade_5m",
    "taker_imbalance_cusum",
    "agg_trade_buy_ratio_60s",
    "agg_trade_buy_ratio_30s",
)

# CCXT source map for ops/debug when readiness gates fail (see hunt/docs/CCXT.md).
MARKET_FIELD_CCXT_SOURCE: dict[str, str] = {
    "mark_price": "WS watchMarkPrices | REST fetchMarkOHLCV",
    "funding_rate": "REST fetchFundingRate | WS watchMarkPrices",
    "funding_trend": "REST fetchFundingRateHistory",
    "oi_current": "REST fetchOpenInterest",
    "oi_change_pct": "REST fetchOpenInterest + history",
    "oi_slope_5m": "REST fetchOpenInterest series",
    "ls_ratio": "implicit fapiDataGetTopLongShortAccountRatio",
    "top_position_ls_ratio": "implicit fapiDataGetTopLongShortPositionRatio",
    "global_ls_ratio": "implicit fapiDataGetGlobalLongShortAccountRatio",
    "taker_ratio": "implicit fapiDataGetTakerlongshortRatio",
    "premium_zscore_5m": "REST fetchPremiumIndexOHLCV / mark-index",
    "premium_slope_5m": "REST fetchPremiumIndexOHLCV",
    "mark_index_spread_bps": "WS watchMarkPrices | REST mark/index",
    "bid_price": "REST fetchOrderBook | WS watchOrderBookForSymbols | watchBidsAsks",
    "ask_price": "REST fetchOrderBook | WS watchOrderBookForSymbols | watchBidsAsks",
    "bid_qty": "REST fetchOrderBook | WS watchOrderBookForSymbols",
    "ask_qty": "REST fetchOrderBook | WS watchOrderBookForSymbols",
    "depth_imbalance": "REST fetchOrderBook depth | WS watchOrderBookForSymbols",
    "microprice_bias": "REST fetchOrderBook | WS watchOrderBookForSymbols",
    "agg_trade_delta_30s": "REST fetchTrades | WS watchTradesForSymbols",
    "aggression_shift": "REST fetchTrades | WS watchTradesForSymbols",
    "liquidation_score": "WS watchLiquidationsForSymbols",
    "liquidation_cascade_5m": "WS watchLiquidationsForSymbols",
    "spot_lead_return_1m": "REST spot fetchOHLCV (HuntCcxtSpotCompanion)",
    "spot_futures_spread_bps": "REST spot + futures ticker",
    "basis": "implicit fapiDataGetBasis | REST mark/index OHLCV",
}

PRIVATE_KEYS = {"balance", "position", "order", "account", "margin"}


def validate_public_feature_payload(payload: Mapping[str, Any]) -> None:
    if any(key in payload for key in PRIVATE_KEYS):
        msg = f"Private data in public feature payload: {payload.keys()}"
        raise ValueError(msg)
    expected = set(PUBLIC_FEATURE_FIELDS)
    provided = set(payload.keys())

    missing = tuple(sorted(expected - provided))
    extra = tuple(sorted(provided - expected))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise ValueError("public feature payload schema mismatch: " + "; ".join(details))


def normalize_public_feature_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_public_feature_payload(payload)
    return {name: payload.get(name) for name in PUBLIC_FEATURE_FIELDS}


def _normalized_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def parse_liquidation_score(value: Any) -> float | None:
    """WS liquidation score: short_notional / total in [0, 1]. None if missing/invalid.

    Rejects the legacy -1.0 sentinel and [-1, +1] convention leakage (T1/A1).
    """
    parsed = _normalized_float(value)
    if parsed is None or parsed < 0.0 or parsed > 1.0:
        return None
    return parsed


def normalize_funding_fraction(value: Any) -> float | None:
    """Normalize funding to fraction (0.0008 = 0.08%). Accepts percent or fraction."""
    parsed = _normalized_float(value)
    if parsed is None:
        return None
    if abs(parsed) > 0.05:
        return parsed / 100.0
    return parsed


def worst_entry_edge(setup: Mapping[str, Any], *, direction: str) -> float | None:
    """Worst-case fill edge for R:R (short → entry high, long → entry low)."""
    ez = setup.get("entry_zone")
    try:
        if isinstance(ez, (list, tuple)) and len(ez) >= 2:
            lo = float(ez[0])
            hi = float(ez[1])
            if direction == "short":
                return hi if hi > 0 else None
            return lo if lo > 0 else None
    except (TypeError, ValueError, IndexError):
        pass
    return None


def stamp_market_field_provenance(
    market: dict[str, Any],
    field: str,
    *,
    source: str,
    age_seconds: float | None = None,
) -> None:
    """Attach source + age for a populated market derivative (Phase 2 / #45)."""
    if not isinstance(market, dict) or market.get(field) is None:
        return
    prov = market.setdefault("_provenance", {})
    if not isinstance(prov, dict):
        prov = {}
        market["_provenance"] = prov
    entry: dict[str, Any] = {"source": source, "value": market.get(field)}
    if age_seconds is not None:
        entry["age_seconds"] = round(float(age_seconds), 1)
        market[f"{field}_age_seconds"] = entry["age_seconds"]
    prov[field] = entry


def stamp_market_derivatives_provenance(market: dict[str, Any]) -> None:
    """Batch #45: stamp known derivative fields from market dict metadata."""
    if not isinstance(market, dict):
        return
    for field in (
        "funding_rate",
        "open_interest",
        "oi_z",
        "gls_z",
        "long_short_ratio",
        "taker_buy_ratio",
        "cvd_px",
        "liquidation_score",
    ):
        if market.get(field) is None:
            continue
        age = market.get(f"{field}_age_seconds")
        src = str(market.get(f"{field}_source") or market.get("_source") or "enrich")
        stamp_market_field_provenance(
            market,
            field,
            source=src,
            age_seconds=float(age) if age is not None else None,
        )


def record_data_quality_violation(
    row: dict[str, Any],
    field: str,
    *,
    reason: str,
) -> None:
    """#43 fail-loud: mark hot-path field as missing instead of silent fill_0."""
    dq = row.setdefault("data_quality", {})
    if not isinstance(dq, dict):
        dq = {}
        row["data_quality"] = dq
    violations = dq.setdefault("violations", [])
    if not isinstance(violations, list):
        violations = []
        dq["violations"] = violations
    violations.append({"field": field, "reason": reason})
    row[field] = None


def compute_setup_ev(
    evidence: Mapping[str, Any],
    levels_dict: Mapping[str, Any],
    *,
    direction: str,
    structure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 3B EV: P from catalog evidence + worst_entry_edge geometry."""
    norm_dir = normalize_direction(direction)
    if norm_dir is None:
        return {"ev": None, "confidence_score": None, "reason": "invalid_direction"}
    setup = dict(levels_dict)
    p = _normalized_float(evidence.get("confidence_score") or evidence.get("probability"))
    if p is None:
        strength = _normalized_float(evidence.get("strength"))
        if strength is not None:
            p = min(0.85, max(0.15, 0.35 + strength * 0.45))
    entry = worst_entry_edge(setup, direction=norm_dir)
    sl = _normalized_float(setup.get("stop_loss"))
    tp1 = _normalized_float(setup.get("tp1"))
    if p is None or entry is None or sl is None or tp1 is None:
        return {"ev": None, "confidence_score": p, "reason": "incomplete_levels"}
    if norm_dir == "short":
        risk = sl - entry
        reward = entry - tp1
    else:
        risk = entry - sl
        reward = tp1 - entry
    if risk <= RISK_REWARD_EPSILON or reward <= 0:
        return {"ev": None, "confidence_score": round(p, 3), "reason": "degenerate_geometry"}
    struct = structure if isinstance(structure, dict) else {}
    if struct.get("at_level"):
        p = min(0.85, p + 0.05)
    if struct.get("choch_detected"):
        p = min(0.85, p + 0.04)
    sb = str(struct.get("structure_bias") or "")
    if (norm_dir == "short" and sb == "short") or (norm_dir == "long" and sb == "long"):
        p = min(0.85, p + 0.03)
    p = min(0.85, max(0.15, p))
    ev = round(p * reward - (1.0 - p) * risk, 6)
    return {
        "ev": ev,
        "confidence_score": round(p, 3),
        "reward": reward,
        "risk": risk,
        "rr": round(reward / risk, 2),
        "setup_id": evidence.get("setup_id"),
    }


def compute_rule_based_ev(
    setup: Mapping[str, Any],
    *,
    direction: str,
    structure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 6 shadow EV: P from structure+ignition+RR geometry (no ML)."""
    from hunt_core.contract import compute_setup_risk_reward, worst_entry_edge

    rr = compute_setup_risk_reward(setup, direction=direction)
    entry = worst_entry_edge(setup, direction=direction)
    sl = _normalized_float(setup.get("stop_loss"))
    tp1 = _normalized_float(setup.get("tp1"))
    if rr is None or entry is None or sl is None or tp1 is None:
        return {"ev": None, "confidence_score": None, "reason": "incomplete_levels"}
    risk = abs(sl - entry) if direction == "short" else abs(entry - sl)
    reward = abs(entry - tp1) if direction == "short" else abs(tp1 - entry)
    if risk <= 0 or reward <= 0:
        return {"ev": None, "confidence_score": None, "reason": "degenerate_geometry"}
    struct = structure if isinstance(structure, dict) else {}
    p = 0.45
    if struct.get("at_level"):
        p += 0.08
    if struct.get("choch_detected"):
        p += 0.10
    sb = str(struct.get("structure_bias") or "")
    if (direction == "short" and sb == "short") or (direction == "long" and sb == "long"):
        p += 0.07
    ign = _normalized_float(setup.get("ignition_score"))
    if ign is not None:
        p += min(0.15, ign / 100.0 * 0.15)
    p = min(0.85, max(0.15, p))
    ev = round(p * reward - (1.0 - p) * risk, 6)
    return {"ev": ev, "confidence_score": round(p, 3), "reward": reward, "risk": risk, "rr": rr}


def compute_setup_risk_reward(setup: Mapping[str, Any], *, direction: str) -> float | None:
    """Measured R:R from latched entry edge, SL, and TP1."""
    stored = _normalized_float(setup.get("risk_reward"))
    entry = worst_entry_edge(setup, direction=direction)
    sl = _normalized_float(setup.get("stop_loss"))
    tp1 = _normalized_float(setup.get("tp1"))
    if entry is None or sl is None or tp1 is None or entry <= 0:
        return stored
    if direction == "short":
        risk = sl - entry
        reward = entry - tp1
    else:
        risk = entry - sl
        reward = tp1 - entry
    if risk <= RISK_REWARD_EPSILON or reward <= 0:
        return stored
    return round(reward / risk, 2)


# Binance USDS-M taker fee (public schedule, base tier, no BNB discount assumed)
# and a conservative slippage allowance -- both CALIBRATABLE, not measured per-symbol.
# Funding carry is NOT modeled here (holding duration is unknown at signal time,
# not just unmodeled by oversight) -- net R:R below is a cost floor, not a full estimate.
NET_RR_TAKER_FEE_PCT = 0.0005
NET_RR_SLIPPAGE_PCT = 0.001
NET_RR_DEFAULT_ENTRIES = 1  # averaging entries multiply the cost, see n_entries


def compute_setup_risk_reward_net(
    setup: Mapping[str, Any],
    *,
    direction: str,
    n_entries: int = NET_RR_DEFAULT_ENTRIES,
    taker_fee_pct: float = NET_RR_TAKER_FEE_PCT,
    slippage_pct: float = NET_RR_SLIPPAGE_PCT,
) -> float | None:
    """R:R net of taker fees + slippage (round-trip, scaled by averaging entries).

    Additive alongside compute_setup_risk_reward (gross) -- does not replace it
    and is not wired into any existing gate threshold. Funding carry is excluded
    (see module note); this is a cost FLOOR, real net R:R for a held position is
    likely lower still.
    """
    entry = worst_entry_edge(setup, direction=direction)
    sl = _normalized_float(setup.get("stop_loss"))
    tp1 = _normalized_float(setup.get("tp1"))
    if entry is None or sl is None or tp1 is None or entry <= 0:
        return None
    if direction == "short":
        risk = sl - entry
        reward = entry - tp1
    else:
        risk = entry - sl
        reward = tp1 - entry
    if risk <= RISK_REWARD_EPSILON or reward <= 0:
        return None
    n = max(1, int(n_entries))
    # round-trip cost per entry-exit pair, in price units, scaled by number of fills
    cost_per_unit = entry * (taker_fee_pct * 2 + slippage_pct) * n
    net_reward = reward - cost_per_unit
    net_risk = risk + cost_per_unit
    if net_reward <= 0 or net_risk <= 0:
        return 0.0
    return round(net_reward / net_risk, 2)


def _normalized_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return bool(value)


def build_public_feature_snapshot(prepared: Any) -> dict[str, Any]:
    """Build a normalized public feature snapshot from PreparedSymbol-like data."""
    if prepared is None:
        return normalize_public_feature_payload(dict.fromkeys(PUBLIC_FEATURE_FIELDS))

    features: dict[str, Any] = {}

    def _frame_value(frame: Any, column: str) -> float | None:
        if frame is None or getattr(frame, "is_empty", lambda: True)():
            return None
        if column not in getattr(frame, "columns", []):
            return None
        try:
            return _normalized_float(frame.item(-1, column))
        except DEFENSIVE_EXC as exc:
            LOG.debug("public feature snapshot read failed | column=%s error=%s", column, exc)
            return None

    def _ema_stack(frame: Any, fast: str, slow: str) -> bool | None:
        fast_value = _frame_value(frame, fast)
        slow_value = _frame_value(frame, slow)
        if fast_value is None or slow_value is None or slow_value <= 0.0:
            return None
        return fast_value > slow_value

    work_1m = getattr(prepared, "work_1m", None)
    work_15m = getattr(prepared, "work_15m", None)
    work_1h = getattr(prepared, "work_1h", None)
    work_4h = getattr(prepared, "work_4h", None)

    features["rsi_15m"] = _frame_value(work_15m, "rsi14")
    features["rsi_1h"] = _frame_value(work_1h, "rsi14")
    features["rsi_4h"] = _frame_value(work_4h, "rsi14")
    features["adx_1h"] = _frame_value(work_1h, "adx14")
    features["adx_4h"] = _frame_value(work_4h, "adx14")
    features["atr_pct_15m"] = _frame_value(work_15m, "atr_pct")
    features["volume_ratio_15m"] = _frame_value(work_15m, "volume_ratio20")
    features["macd_histogram_15m"] = _frame_value(work_15m, "macd_hist")

    features["ema20_above_ema50_15m"] = _normalized_bool(_ema_stack(work_15m, "ema20", "ema50"))
    features["ema50_above_ema200_15m"] = _normalized_bool(_ema_stack(work_15m, "ema50", "ema200"))
    features["ema20_above_ema50_1h"] = _normalized_bool(_ema_stack(work_1h, "ema20", "ema50"))
    features["ema50_above_ema200_1h"] = _normalized_bool(_ema_stack(work_1h, "ema50", "ema200"))

    features["supertrend_dir_1h"] = _frame_value(work_1h, "supertrend_dir")
    features["supertrend_dir_15m"] = _frame_value(work_15m, "supertrend_dir")
    features["obv_above_ema_15m"] = _frame_value(work_15m, "obv_above_ema")
    features["bb_pct_b_15m"] = _frame_value(work_15m, "bb_pct_b")
    features["bb_width_15m"] = _frame_value(work_15m, "bb_width")

    features["funding_rate"] = _normalized_float(getattr(prepared, "funding_rate", None))
    features["oi_current"] = _normalized_float(getattr(prepared, "oi_current", None))
    features["oi_change_pct"] = _normalized_float(getattr(prepared, "oi_change_pct", None))
    features["oi_slope_5m"] = _normalized_float(getattr(prepared, "oi_slope_5m", None))
    features["ls_ratio"] = _normalized_float(getattr(prepared, "ls_ratio", None))
    features["global_ls_ratio"] = _normalized_float(getattr(prepared, "global_ls_ratio", None))
    features["top_trader_position_ratio"] = _normalized_float(
        getattr(prepared, "top_trader_position_ratio", None)
    )
    features["top_vs_global_ls_gap"] = _normalized_float(
        getattr(prepared, "top_vs_global_ls_gap", None)
    )
    features["liquidation_score"] = parse_liquidation_score(
        getattr(prepared, "liquidation_score", None)
    )
    features["mark_index_spread_bps"] = _normalized_float(
        getattr(prepared, "mark_index_spread_bps", None)
    )
    features["premium_zscore_5m"] = _normalized_float(getattr(prepared, "premium_zscore_5m", None))
    features["premium_slope_5m"] = _normalized_float(getattr(prepared, "premium_slope_5m", None))
    features["context_snapshot_age_seconds"] = _normalized_float(
        getattr(prepared, "context_snapshot_age_seconds", None)
    )
    features["depth_imbalance"] = _normalized_float(getattr(prepared, "depth_imbalance", None))
    features["microprice_bias"] = _normalized_float(getattr(prepared, "microprice_bias", None))
    features["agg_trade_delta_30s"] = _normalized_float(
        getattr(prepared, "agg_trade_delta_30s", None)
    )
    features["aggression_shift"] = _normalized_float(getattr(prepared, "aggression_shift", None))
    features["spot_lead_return_1m"] = _normalized_float(
        getattr(prepared, "spot_lead_return_1m", None)
    )
    features["spot_futures_spread_bps"] = _normalized_float(
        getattr(prepared, "spot_futures_spread_bps", None)
    )
    features["mark_price_age_seconds"] = _normalized_float(
        getattr(prepared, "mark_price_age_seconds", None)
    )
    features["ticker_price_age_seconds"] = _normalized_float(
        getattr(prepared, "ticker_price_age_seconds", None)
    )
    features["book_ticker_age_seconds"] = _normalized_float(
        getattr(prepared, "book_ticker_age_seconds", None)
    )
    features["data_source_mix"] = (
        getattr(prepared, "data_source_mix", "futures_only") or "futures_only"
    )
    features["market_regime"] = getattr(prepared, "market_regime", "neutral") or "neutral"
    features["vah_1h"] = _normalized_float(getattr(prepared, "vah_1h", None))
    features["val_1h"] = _normalized_float(getattr(prepared, "val_1h", None))
    features["vah_15m"] = _normalized_float(getattr(prepared, "vah_15m", None))
    features["val_15m"] = _normalized_float(getattr(prepared, "val_15m", None))
    features["funding_rate_zscore_48h"] = _normalized_float(
        getattr(prepared, "funding_rate_zscore_48h", None)
    )
    cascade = getattr(prepared, "liquidation_cascade_5m", None)
    if cascade is None:
        features["liquidation_cascade_5m"] = None
    else:
        features["liquidation_cascade_5m"] = bool(cascade)

    cusum = _frame_value(work_1m, "taker_imbalance_cusum")
    if cusum is None:
        cusum = _frame_value(work_15m, "taker_imbalance_cusum")
    features["taker_imbalance_cusum"] = cusum

    market_ctx = getattr(prepared, "market_ctx", None) or {}
    if not isinstance(market_ctx, dict):
        market_ctx = {}
    features["agg_trade_buy_ratio_60s"] = _normalized_float(
        getattr(prepared, "agg_trade_buy_ratio_60s", None) or market_ctx.get("agg_trade_buy_ratio_60s")
    )
    features["agg_trade_buy_ratio_30s"] = _normalized_float(
        getattr(prepared, "agg_trade_buy_ratio_30s", None) or market_ctx.get("agg_trade_buy_ratio_30s")
    )

    return normalize_public_feature_payload(features)


# --- Hunt delivery contract (Phase 3b) ---

DeliveryTierKind = Literal["armed", "triggered"]
DeliveryStageKind = Literal["early", "dump_hunt", "squeeze", "confirm", "analyze"]


class SetupDeliveryContract(TypedDict, total=False):
    """Typed payload for Telegram delivery + tracker registration."""

    symbol: str
    direction: Literal["short", "long"]
    setup_id: str
    delivery_tier: DeliveryTierKind
    delivery_stage: DeliveryStageKind
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    invalidation_above: float | None
    invalidation_below: float | None
    fuel: float | None
    score: float | None
    lifecycle_phase: str | None
    lifecycle_bias: str | None
    confirm_hard: list[str]
    triggers: list[str]
    risk_reward: float | None
    gate_code: str | None
    card_html: str | None
    telegram_message_id: int | None
    opened_at: str | None


def build_setup_delivery_contract(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    delivery_tier: str,
    delivery_stage: DeliveryStageKind = "confirm",
    gate_code: str | None = None,
    card_html: str | None = None,
) -> SetupDeliveryContract:
    """Materialize delivery contract from live tick row + setup block."""
    ez = setup.get("entry_zone") or [0, 0]
    try:
        entry_lo = float(ez[0])
        entry_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        entry_lo = entry_hi = 0.0
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2") or tp1
    tp3 = setup.get("tp3") or tp2
    return SetupDeliveryContract(
        symbol=str(row.get("symbol") or "").upper(),
        direction="short" if direction == "short" else "long",
        setup_id=str(setup.get("setup_id") or setup.get("phase") or "unknown"),
        delivery_tier="armed" if str(delivery_tier).lower() == "armed" else "triggered",
        delivery_stage=delivery_stage,
        entry_lo=entry_lo,
        entry_hi=entry_hi,
        stop_loss=float(setup.get("stop_loss") or 0),
        tp1=float(tp1 or 0),
        tp2=float(tp2 or 0),
        tp3=float(tp3 or 0),
        invalidation_above=setup.get("invalidation_above"),
        invalidation_below=setup.get("invalidation_below"),
        fuel=float(setup.get(fuel_key) or 0) if setup.get(fuel_key) is not None else None,
        score=float(setup.get(score_key) or 0) if setup.get(score_key) is not None else None,
        lifecycle_phase=str(lc.get("phase") or setup.get("lifecycle_phase") or ""),
        lifecycle_bias=str(lc.get("recommended_bias") or ""),
        confirm_hard=list(setup.get("confirm_hard") or []),
        triggers=list(setup.get("triggers") or []),
        risk_reward=setup.get("risk_reward"),
        gate_code=gate_code,
        card_html=card_html,
        opened_at=str(row.get("ts") or ""),
    )

