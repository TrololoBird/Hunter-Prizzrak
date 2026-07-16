"""Signal trade-plan contract helpers — entry-zone geometry + provenance stamps.

The bot is signal-only: each emitted setup must be directly usable as a
manual limit-order plan. This module keeps the plan math centralized so
individual detectors do not drift into point entries or partial target gaps.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

RISK_REWARD_EPSILON = 1e-9

Direction = Literal["short", "long"]


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
    "spot_quote_volume_24h": "REST spot fetchTicker (HuntCcxtSpotCompanion)",
    "basis": "implicit fapiDataGetBasis | REST mark/index OHLCV",
}


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


def price_in_entry_zone(
    setup: Mapping[str, Any], *, price: float, direction: str = "", **_k: Any
) -> bool:
    """True when live price is inside the setup's entry zone (pure geometry).

    Lives in the spine (it is entry_zone semantics, like worst_entry_edge above) rather
    than in scanner/detect/delivery_support, which track/ and levels/ were reaching into
    — a spine→strategy import inversion. delivery_support re-exports it for its own
    callers.
    """
    del direction  # geometry only — the zone is already ordered
    ez = setup.get("entry_zone") or []
    if not isinstance(ez, (list, tuple)) or len(ez) < 2:
        return False
    try:
        lo, hi = float(ez[0]), float(ez[1])
    except (TypeError, ValueError):
        return False
    if lo > hi:
        lo, hi = hi, lo
    return lo <= float(price) <= hi


def worst_entry_edge(setup: Mapping[str, Any], *, direction: str) -> float | None:
    """Worst-case (least-favorable) fill edge for a conservative R:R.

    A LONG's worst fill is the HIGH of the entry band (you paid the most → smallest
    reward, largest risk); a SHORT's worst fill is the LOW (you sold cheapest). The
    prior version returned the opposite (long→lo, short→hi) — the BEST fill — which
    over-stated R:R everywhere and contradicted the display fallback that already used
    the correct edge. R:R computed from this edge is now genuinely conservative.
    """
    ez = setup.get("entry_zone")
    try:
        if isinstance(ez, (list, tuple)) and len(ez) >= 2:
            lo = float(ez[0])
            hi = float(ez[1])
            if direction == "short":
                return lo if lo > 0 else None
            return hi if hi > 0 else None
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
