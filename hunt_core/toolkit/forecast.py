"""Maps-driven forecasts — predump / prepump / ignition target bands."""
from __future__ import annotations

from typing import Any, Literal

ForecastKind = Literal["predump_short", "prepump_long", "ignition_long"]


def _factor_confidence(factors: list[str], max_factors: int) -> float:
    """Confidence from structural factor count (no weighted blend)."""
    if max_factors <= 0:
        return 0.0
    return round(min(1.0, len(factors) / max_factors), 3)


from hunt_core.toolkit.targets import (
    collect_downward_targets as _collect_downward_base,
    collect_upward_targets as _collect_upward_targets,
)


def _collect_downward_targets(row: dict[str, Any], price: float) -> tuple[list[float], list[str]]:
    targets, factors = _collect_downward_base(row, price)
    from hunt_core.maps.oi import oi_regime_from_row

    if oi_regime_from_row(row) == "new_money_short":
        factors.append("oi_new_money_short")
    return targets, factors


def build_maps_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Pre-pump (coil) forecast — upward structural targets."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    market = row.get("market") if isinstance(row.get("market"), dict) else {}

    targets, factors = _collect_upward_targets(row, price)
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
    confidence = _factor_confidence(factors, max_factors=6)

    return {
        "kind": "prepump_long",
        "direction": "long",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": confidence,
        "factors": factors,
    }


def build_dump_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Pre-dump short forecast — downward markdown zone."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None

    targets, factors = _collect_downward_targets(row, price)
    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    # Primary = NEAREST target, symmetric with the long builders (which use
    # min(upward) = nearest-above). For downward targets the nearest is the LARGEST
    # value (closest below price) = target_hi; min() is the deepest/farthest zone,
    # so using it made the short's expected_move systematically overstate the drop
    # (FCAST-1). lo/hi still bound the zone.
    expected_move_pct = (target_hi - price) / price * 100.0
    confidence = _factor_confidence(factors, max_factors=5)

    return {
        "kind": "predump_short",
        "direction": "short",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_hi, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": confidence,
        "factors": factors,
    }


def build_ignition_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Squeeze ignition forecast — short-liq magnet above + time window."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    r1h = tf.get("1h") or {}

    targets, factors = _collect_upward_targets(row, price)
    atr = float(r1h.get("atr14") or r1h.get("atr") or 0)
    if atr > 0:
        atr_target = price + atr * 1.2
        targets.append(atr_target)
        factors.append("atr_1h_magnet")

    # is-None fallthrough: funding of exactly 0.0 is a real, common reading (flat), and
    # `or` discarded it in favour of the other source.
    funding = market.get("funding_rate")
    if funding is None:
        funding = market.get("live_funding_rate")
    if funding is not None and float(funding) < -0.0001:
        factors.append("neg_funding")
    if market.get("map_cvd_divergence") == "bullish_div":
        factors.append("cvd_absorption")

    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    expected_move_pct = (target_lo - price) / price * 100.0
    confidence = _factor_confidence(factors, max_factors=5)

    return {
        "kind": "ignition_long",
        "direction": "long",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": confidence,
        "factors": factors,
        "window_minutes": 15,
    }


def build_all_forecasts(row: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Build all three forecast kinds; attach best match to row caller."""
    return {
        "prepump_long": build_maps_forecast(row),
        "predump_short": build_dump_forecast(row),
        "ignition_long": build_ignition_forecast(row),
    }


def stamp_forecasts_on_row(row: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Evaluate forecasts and stamp primary + all on row."""
    all_fc = build_all_forecasts(row)
    row["forecasts"] = {k: v for k, v in all_fc.items() if v is not None}
    fusion = row.get("manipulation_fusion") if isinstance(row.get("manipulation_fusion"), dict) else {}
    from hunt_core.toolkit.archetypes import canonical_archetype

    archetype = canonical_archetype(str(fusion.get("archetype") or ""))
    primary_key = {
        "predump_short": "predump_short",
        "prepump_long": "prepump_long",
        "ignition_long": "ignition_long",
    }.get(archetype, "prepump_long")
    primary = all_fc.get(primary_key) or build_maps_forecast(row)
    if primary:
        row["maps_forecast"] = primary
    return all_fc


__all__ = [
    "ForecastKind",
    "build_all_forecasts",
    "build_dump_forecast",
    "build_ignition_forecast",
    "build_maps_forecast",
    "stamp_forecasts_on_row",
]
