"""Deep-owned structural forecasts — NATIVE typed port (ADR-0004).

Typed replacement for :mod:`hunt_core.prizrak.structural_forecast` that reads from the typed handles
(:class:`MarketView`, :class:`MapBundle`) instead of the untyped row. The ``map_*``/``liq_*`` scalars
that lived on ``row["market"]`` are re-derived via ``derive_map_features``; the ``row["maps"]``
sub-tree is ``MapBundle.to_dict()``. Both feed the unchanged pure toolkit collectors, so target
geometry is identical. Fail-loud (I-6): missing data yields ``None``/absent, never a fabricated number.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from hunt_core.maps.config import MapsConfig
from hunt_core.maps.engine import MapBundle, derive_map_features
from hunt_core.toolkit.targets import collect_downward_targets, collect_upward_targets
from hunt_core.view.models import MarketView

LOG = structlog.get_logger("hunt.prizrak.structural_forecast_native")


def _factor_confidence(factors: list[str], max_factors: int) -> float:
    """Confidence as the (capped) fraction of expected factors present.

    Args:
        factors: The factor tags that fired.
        max_factors: Denominator (the domain's max distinct factors).

    Returns:
        ``round(min(1.0, len(factors) / max_factors), 3)``, or ``0.0`` when ``max_factors <= 0``.
    """
    if max_factors <= 0:
        return 0.0
    return round(min(1.0, len(factors) / max_factors), 3)


def _oi_new_money_short_native(
    *,
    oi_regime: str | None,
    oi_change_pct: float | None,
    delta_ratio: float | None,
) -> bool:
    """Port of ``_oi_new_money_short`` reading typed OI inputs instead of ``row["market"]``.

    Note:
        ``oi_regime`` and ``delta_ratio`` were **phantom keys** on the old row
        (``market["oi_regime"]`` / ``market["delta_ratio"]`` were never written), so in production
        this predicate ALWAYS returned ``False``. Defaults ``None`` reproduce that exactly. They are
        surfaced as parameters so the branch can be revived once a typed OI-regime / signed-delta
        producer is wired (``engine/oi_stats`` + ``engine/orderflow``).

    Args:
        oi_regime: OI regime label, or ``None`` (unknown / not yet typed).
        oi_change_pct: OI change in PERCENT (e.g. ``5.0`` = +5%), or ``None`` (no data).
        delta_ratio: Signed taker delta ratio, or ``None`` (no data).

    Returns:
        ``True`` when OI shows new-money-short characteristics, else ``False``.
    """
    if str(oi_regime or "") == "new_money_short":
        return True
    if oi_change_pct is None or delta_ratio is None:
        return False
    try:
        return float(oi_change_pct) > 2.0 and float(delta_ratio) < -0.02
    except (TypeError, ValueError):
        return False


def build_structural_up_forecast_native(
    view: MarketView,
    maps: MapBundle | None,
    *,
    cfg: MapsConfig | None = None,
) -> dict[str, Any] | None:
    """Upside structural band for deep analysis (prepump / coil context) — native port.

    Args:
        view: The typed market view (supplies ``last_price``).
        maps: The per-tick :class:`MapBundle`, or ``None`` when maps were not built.
        cfg: Optional maps config forwarded to ``derive_map_features`` (defaults loaded).

    Returns:
        The upside forecast dict (same shape as the legacy builder), or ``None`` when there is no
        price or no upward targets.
    """
    price = float(view.last_price or 0)
    if price <= 0:
        return None

    market: dict[str, Any] = derive_map_features(maps, current_price=price, cfg=cfg)
    maps_dict: dict[str, Any] = maps.to_dict() if maps is not None else {}
    helper_row: dict[str, Any] = {"market": market, "maps": maps_dict}

    targets, factors = collect_upward_targets(helper_row, price)
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


def build_structural_down_forecast_native(
    view: MarketView,
    maps: MapBundle | None,
    *,
    session: Mapping[str, Any] | None = None,
    oi_regime: str | None = None,
    oi_change_pct: float | None = None,
    delta_ratio: float | None = None,
    cfg: MapsConfig | None = None,
) -> dict[str, Any] | None:
    """Downside structural band for deep analysis (predump context) — native port.

    Args:
        view: The typed market view (supplies ``last_price``).
        maps: The per-tick :class:`MapBundle`, or ``None``.
        session: The session sub-dict (``hunt_low`` / ``low_24h``) from ``session_stats_native``.
            ``None`` → the ``range_low`` target simply does not fire (fail-loud, not fabricated).
        oi_regime: See :func:`_oi_new_money_short_native` (phantom on the old row → default ``None``).
        oi_change_pct: OI %-change for the new-money-short check (not on the typed handles; derive via
            ``engine.oi_stats.oi_change() * 100``). ``None`` → check inert.
        delta_ratio: Signed taker delta (phantom on the old row → default ``None``).
        cfg: Optional maps config forwarded to ``derive_map_features``.

    Returns:
        The downside forecast dict (legacy shape), or ``None`` when there is no price / no targets.
    """
    price = float(view.last_price or 0)
    if price <= 0:
        return None

    market: dict[str, Any] = derive_map_features(maps, current_price=price, cfg=cfg)
    maps_dict: dict[str, Any] = maps.to_dict() if maps is not None else {}
    helper_row: dict[str, Any] = {
        "market": market,
        "maps": maps_dict,
        "session": dict(session or {}),
    }

    targets, factors = collect_downward_targets(helper_row, price)
    if _oi_new_money_short_native(
        oi_regime=oi_regime, oi_change_pct=oi_change_pct, delta_ratio=delta_ratio
    ):
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


_FACTOR_RU = {
    "short_liq_magnet": "магнит шорт-ликвидаций",
    "long_liq_magnet": "магнит лонг-ликвидаций",
    "naked_poc": "голый POC",
    "val_magnet": "магнит VAL",
    "vah_magnet": "магнит VAH",
    "poc_magnet": "магнит POC",
    "va_contraction": "сжатие зоны стоимости",
    "ask_thinning": "истончение продаж",
    "bid_thinning": "истончение покупок",
    "range_low": "низ диапазона",
    "range_high": "верх диапазона",
    "liquidity_void": "пустота ликвидности",
    "fib_magnet": "магнит Фибоначчи",
}


def build_structural_forecast_panel_native(
    forecasts: dict[str, dict[str, Any] | None],
    *,
    last_price: float | None = None,
) -> str:
    """Maps-derived target bands panel — native port (``row`` 2nd arg → ``last_price``).

    The only datum the legacy ``build_structural_forecast_panel`` read from ``row`` was
    ``row.get("price")``; here it is passed directly as ``last_price`` (``MarketView.last_price``).

    Args:
        forecasts: Mapping of ``structural_up`` / ``structural_down`` → forecast dict (or ``None``).
        last_price: Current price for the move-% annotations, or ``None`` (→ ``0.0``, annotations off).

    Returns:
        The rendered HTML panel, or ``""`` when nothing renders.
    """
    import html

    from hunt_core.deliver._labels import fmt_price

    if not forecasts:
        return ""
    lines = [
        "🎯 <b>Структурные цели</b> (карты / магниты ликвидности)",
        "<i>% = уверенность в структуре зоны, не вероятность достижения; "
        "верх и низ независимы</i>",
    ]
    labels = {
        "structural_up": "↑ Зона выше",
        "structural_down": "↓ Зона ниже",
    }
    price = float(last_price or 0)

    def _move_pct(target: float) -> float | None:
        if price <= 0 or target <= 0:
            return None
        return (target - price) / price * 100.0

    for key, label in labels.items():
        fc = forecasts.get(key)
        if not isinstance(fc, dict):
            continue
        tlo = fc.get("target_lo")
        thi = fc.get("target_hi")
        if tlo is None:
            continue
        conf = float(fc.get("confidence") or 0)
        lo_f = float(tlo)
        hi_f = float(thi) if thi is not None else lo_f
        band = f"{fmt_price(lo_f)}–{fmt_price(hi_f)}" if hi_f != lo_f else fmt_price(lo_f)
        near = lo_f if key == "structural_up" else hi_f
        far = hi_f if key == "structural_up" else lo_f
        mv_near = _move_pct(near)
        mv_far = _move_pct(far)
        move_s = ""
        wide_flag = ""
        if mv_near is not None and mv_far is not None:
            if abs(mv_far - mv_near) >= 0.3:
                move_s = f" ({mv_near:+.1f}% → {mv_far:+.1f}%)"
            else:
                move_s = f" ({mv_near:+.1f}%)"
            if abs(mv_far - mv_near) >= 8.0:
                wide_flag = " ⚠ широкая"
        factors = fc.get("factors") or []
        fac_s = ""
        if factors:
            fac_s = " · " + ", ".join(
                html.escape(_FACTOR_RU.get(str(f), str(f))) for f in factors[:3]
            )
        lines.append(
            f"  {label}: <code>{band}</code> увер.{conf:.0%}{move_s}{wide_flag}{fac_s}"
        )
    return "\n".join(lines) if len(lines) > 2 else ""


__all__ = [
    "build_structural_up_forecast_native",
    "build_structural_down_forecast_native",
    "build_structural_forecast_panel_native",
]
