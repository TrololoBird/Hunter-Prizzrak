"""Symbol data readiness gate (shortlist Phase 3)."""
from __future__ import annotations



import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    import polars as pl

    from hunt_core.domain.config import HuntSettings as BotSettings
    from hunt_core.domain.schemas import PreparedSymbol, UniverseSymbol


from hunt_core.contract import MARKET_FIELD_CCXT_SOURCE

REQUIRED_DERIVATIVES_KEYS: tuple[str, ...] = (
    "oi_change_pct",
    "ls_ratio",
    "top_position_ls_ratio",
    "global_ls_ratio",
    "taker_ratio",
    "funding_rate",
    "funding_trend",
    "premium_zscore_5m",
    "premium_slope_5m",
)

# Positioning REST fields required before kline-only analysis under strict_data_quality.
# Order-flow columns (depth_imbalance, microprice_bias, agg_trade_delta_30s) are gated
# per strategy pool via assess_strategy_data_capability - not a global hard stop.
REQUIRED_PREPARED_LIVE_FIELDS: tuple[str, ...] = (
    "oi_change_pct",
    "ls_ratio",
    "global_ls_ratio",
    "taker_ratio",
    "funding_rate",
)

# prepare_frame drops leading rows until ema200/donchian are populated (~200 bars on 15m).
_PREPARED_FRAME_WARMUP_RESERVE: dict[str, int] = {
    "5m": 120,
    "15m": 200,
    "1h": 200,
    "4h": 200,
}

# REST may return one bar fewer than requested; tolerate on raw-frame gate.
_RAW_FRAME_ROW_TOLERANCE: dict[str, int] = {
    "5m": 2,
    "15m": 2,
    "1h": 2,
    "4h": 2,
}

_KLINE_FETCH_BUFFER_BARS = 80
_KLINE_FETCH_BASELINE: dict[str, int] = {
    "5m": 300,
    "15m": 500,
    "1h": 500,
    "4h": 500,
}
_BINANCE_KLINE_MAX = 1500


def kline_fetch_limit(configured_min_bars: int, timeframe: str) -> int:
    """Bars to request from REST/WS so prepare survives warmup trimming."""
    baseline = _KLINE_FETCH_BASELINE.get(timeframe, 240)
    warmup = _PREPARED_FRAME_WARMUP_RESERVE.get(timeframe, 120)
    required = max(
        baseline,
        int(configured_min_bars) + warmup + _KLINE_FETCH_BUFFER_BARS,
    )
    return min(_BINANCE_KLINE_MAX, required)


def configured_frame_minimums(settings: BotSettings) -> dict[str, int]:
    filters = settings.filters
    return {
        "5m": int(filters.min_bars_5m),
        "15m": int(filters.min_bars_15m),
        "1h": int(filters.min_bars_1h),
        "4h": int(filters.min_bars_4h),
    }


def raw_frame_minimums(settings: BotSettings) -> dict[str, int]:
    """Minimum raw kline rows before prepare (warmup reserve + REST shortfall tolerance)."""
    configured = configured_frame_minimums(settings)
    return {
        tf: max(
            30,
            int(min_bars)
            + _PREPARED_FRAME_WARMUP_RESERVE.get(tf, 120)
            - _RAW_FRAME_ROW_TOLERANCE.get(tf, 2),
        )
        for tf, min_bars in configured.items()
    }


def effective_prepared_minimums(settings: BotSettings) -> dict[str, int]:
    configured = configured_frame_minimums(settings)
    return {tf: _effective_prepared_minimum(min_bars, tf) for tf, min_bars in configured.items()}


def _effective_prepared_minimum(configured: int, timeframe: str) -> int:
    reserve = _PREPARED_FRAME_WARMUP_RESERVE.get(timeframe, 120)
    return max(30, int(configured) - reserve)


@dataclass(frozen=True, slots=True)
class DataReadinessResult:
    ready: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def missing_derivatives_context(enrichments: Mapping[str, Any]) -> list[str]:
    """Return enrichment keys required before analysis that are absent or null."""
    return [key for key in REQUIRED_DERIVATIVES_KEYS if enrichments.get(key) is None]


def _finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _last_column_finite(frame: pl.DataFrame | None, column: str) -> float | None:
    if frame is None or frame.is_empty() or column not in frame.columns:
        return None
    return _finite_float(frame[column][-1])


def is_radar_promoted_item(item: UniverseSymbol | None) -> bool:
    """True when symbol entered shortlist via radar promotion funnel."""
    if item is None:
        return False
    reasons = getattr(item, "shortlist_reasons", ()) or ()
    return getattr(item, "shortlist_bucket", "") == "radar" or any(
        "radar" in str(reason) for reason in reasons
    )


_is_radar_promoted_item = is_radar_promoted_item


def assess_symbol_data_readiness(
    prepared: PreparedSymbol,
    settings: BotSettings,
    *,
    universe_item: UniverseSymbol | None = None,
    snapshot_tier: str | None = None,
) -> DataReadinessResult:
    """Return whether required frames and live enrichments are fresh enough."""
    filters = settings.filters
    radar_promoted = is_radar_promoted_item(universe_item)
    details: dict[str, Any] = {
        "symbol": prepared.symbol,
        "radar_promoted": radar_promoted,
    }
    minimums = effective_prepared_minimums(settings)
    rows = {
        "5m": prepared.work_5m.height if prepared.work_5m is not None else 0,
        "15m": prepared.work_15m.height if prepared.work_15m is not None else 0,
        "1h": prepared.work_1h.height if prepared.work_1h is not None else 0,
        "4h": prepared.work_4h.height if prepared.work_4h is not None else 0,
    }
    details["frame_rows"] = rows
    details["minimums"] = minimums
    missing = [tf for tf, need in minimums.items() if rows.get(tf, 0) < need]
    if missing:
        return DataReadinessResult(
            ready=False,
            reason="data.insufficient_required_history",
            details={**details, "missing_frames": missing},
        )

    mark_price = getattr(prepared, "mark_price", None)
    if mark_price is None or float(mark_price or 0.0) <= 0.0:
        return DataReadinessResult(
            ready=False,
            reason="data.mark_price_missing",
            details={
                **details,
                "ccxt_source": MARKET_FIELD_CCXT_SOURCE.get("mark_price"),
            },
        )

    spread_bps = getattr(prepared, "spread_bps", None)
    if spread_bps is None:
        details["spread_bps"] = None
        if bool(getattr(settings.runtime, "strict_data_quality", True)):
            return DataReadinessResult(
                ready=False,
                reason="data.spread_missing",
                details=details,
            )

    strict = bool(getattr(settings.runtime, "strict_data_quality", True))
    is_fast_tier = snapshot_tier == "fast"
    if strict and not radar_promoted:
        book_missing = [
            column
            for column in ("bid_price", "ask_price", "bid_qty", "ask_qty")
            if _last_column_finite(prepared.work_15m, column) is None
        ]
        if book_missing:
            ccxt_sources = {
                col: MARKET_FIELD_CCXT_SOURCE.get(col, "see hunt/docs/CCXT.md")
                for col in book_missing
            }
            return DataReadinessResult(
                ready=False,
                reason="data.orderbook_columns_missing",
                details={
                    **details,
                    "missing_book_columns": book_missing,
                    "ccxt_sources": ccxt_sources,
                },
            )

        if not is_fast_tier:
            missing_live = [
                field_name
                for field_name in REQUIRED_PREPARED_LIVE_FIELDS
                if getattr(prepared, field_name, None) is None
            ]
            if missing_live:
                ccxt_sources = {
                    f: MARKET_FIELD_CCXT_SOURCE.get(f, "see hunt/docs/CCXT.md")
                    for f in missing_live
                }
                return DataReadinessResult(
                    ready=False,
                    reason="data.derivatives_context_missing",
                    details={
                        **details,
                        "missing_live_fields": missing_live,
                        "ccxt_sources": ccxt_sources,
                    },
                )
        else:
            details["fast_tier_derivatives_skipped"] = True
    elif strict and radar_promoted:
        details["strict_derivatives_skipped"] = True

    return DataReadinessResult(ready=True, details=details)


def composite_readiness_pct(result: DataReadinessResult) -> int:
    """Graded 0–100 data readiness for TG (P0-D) — not a probability."""
    if not result.ready:
        return 0
    details = result.details or {}
    rows = details.get("frame_rows") or {}
    score = 0.0
    for tf, count in rows.items():
        if int(count or 0) >= 200:
            score += 20.0
        elif int(count or 0) >= 100:
            score += 12.0
        elif int(count or 0) >= 50:
            score += 6.0
    if details.get("spread_bps") is not None:
        score += 10.0
    if not details.get("missing_live_fields") and not details.get("fast_tier_derivatives_skipped"):
        score += 10.0
    return int(min(100.0, max(0.0, score)))


def readiness_dict_for_row(result: DataReadinessResult) -> dict[str, int | bool]:
    return {
        "ready": result.ready,
        "composite_pct": composite_readiness_pct(result),
    }
