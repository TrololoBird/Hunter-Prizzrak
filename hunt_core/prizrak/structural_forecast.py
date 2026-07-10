"""Deep-owned structural forecasts — shared facts only (no maps.forecast / scanner)."""
from __future__ import annotations

from typing import Any

from hunt_core.toolkit.targets import collect_downward_targets, collect_upward_targets


def _factor_confidence(factors: list[str], max_factors: int) -> float:
    if max_factors <= 0:
        return 0.0
    return round(min(1.0, len(factors) / max_factors), 3)


def _oi_new_money_short(row: dict[str, Any]) -> bool:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    regime = str(market.get("oi_regime") or "")
    if regime == "new_money_short":
        return True
    oi_chg = market.get("oi_change_pct")
    try:
        return oi_chg is not None and float(oi_chg) > 2.0 and float(market.get("delta_ratio") or 0) < -0.02
    except (TypeError, ValueError):
        return False


def build_structural_up_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Upside structural band for deep analysis (prepump / coil context)."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    market = row.get("market") if isinstance(row.get("market"), dict) else {}

    targets, factors = collect_upward_targets(row, price)
    if market.get("map_accum_bid_absorption"):
        factors.append("bid_absorption")
    if market.get("map_ask_thinning"):
        factors.append("ask_thinning")
    if market.get("map_cvd_divergence") == "bullish_div":
        factors.append("bull_cvd_div")
    contraction = market.get("map_vp_va_contraction")
    if contraction is not None and float(contraction) < 0.85:
        factors.append("va_contraction")
    acc = float(market.get("map_vp_accumulation") or 0)
    if acc >= 0.55:
        factors.append("vp_accumulation")

    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    expected_move_pct = (target_lo - price) / price * 100.0
    return {
        "kind": "prepump_long",
        "direction": "long",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": _factor_confidence(factors, max_factors=6),
        "factors": factors,
    }


def build_structural_down_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Downside structural band for deep analysis (predump context)."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None

    targets, factors = collect_downward_targets(row, price)
    if _oi_new_money_short(row):
        factors.append("oi_new_money_short")
    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    expected_move_pct = (target_lo - price) / price * 100.0
    return {
        "kind": "predump_short",
        "direction": "short",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": _factor_confidence(factors, max_factors=5),
        "factors": factors,
    }


__all__ = ["build_structural_down_forecast", "build_structural_up_forecast"]
