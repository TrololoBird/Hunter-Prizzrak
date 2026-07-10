"""Forecast bands panel for deep analysis."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price

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


def build_forecast_panel(forecasts: dict[str, dict[str, Any] | None]) -> str:
    if not forecasts:
        return ""
    lines = ["🎯 <b>Forecasts</b>"]
    labels = {
        "predump_short": "Pre-dump ↓",
        "prepump_long": "Coil ↑",
        "ignition_long": "Ignition ↑",
    }
    for key, label in labels.items():
        fc = forecasts.get(key)
        if not isinstance(fc, dict):
            continue
        conf = float(fc.get("confidence") or 0)
        tlo = fc.get("target_lo")
        thi = fc.get("target_hi")
        if tlo is None:
            continue
        if thi is not None and float(thi) != float(tlo):
            band = f"{fmt_price(float(tlo))}–{fmt_price(float(thi))}"
        else:
            band = fmt_price(float(tlo))
        move = fc.get("expected_move_pct")
        move_s = ""
        if move is not None and abs(float(move)) >= 0.05:
            mv = float(move)
            move_s = f" ({'+' if mv >= 0 else ''}{mv:.0f}%)"
        factors = fc.get("factors") or []
        fac_s = ""
        if factors:
            fac_s = " · " + ", ".join(html.escape(str(f)) for f in factors[:3])
        lines.append(f"  {label}: <code>{band}</code> {conf:.0%}{move_s}{fac_s}")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_structural_forecast_panel(
    forecasts: dict[str, dict[str, Any] | None],
    row: dict[str, Any] | None = None,
) -> str:
    """Maps-derived target bands — no pump/dump archetype labels."""
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
    price = float((row or {}).get("price") or 0) if row else 0.0

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
        if hi_f != lo_f:
            band = f"{fmt_price(lo_f)}–{fmt_price(hi_f)}"
        else:
            band = fmt_price(lo_f)
        # Show move% for BOTH band edges (nearest → farthest) so a wide band
        # cannot masquerade as a tight target via a single near-edge number.
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
            # A band wider than 8% is too diffuse to be a usable target.
            if abs(mv_far - mv_near) >= 8.0:
                wide_flag = " ⚠ широкая"
        factors = fc.get("factors") or []
        fac_s = ""
        if factors:
            fac_s = " · " + ", ".join(html.escape(_FACTOR_RU.get(str(f), str(f))) for f in factors[:3])
        lines.append(f"  {label}: <code>{band}</code> увер.{conf:.0%}{move_s}{wide_flag}{fac_s}")
    return "\n".join(lines) if len(lines) > 2 else ""


__all__ = ["build_forecast_panel", "build_structural_forecast_panel"]
