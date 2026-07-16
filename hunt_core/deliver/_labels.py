"""Shared Telegram label helpers — no broadcaster dependencies."""
from __future__ import annotations

import html
from typing import Any

# Shown on every delivered signal card (dispatch.py, intra_bar_delivery.py) --
# single source so wording only needs to change in one place.
EXPERIMENTAL_DISCLAIMER_RU = (
    "<i>Сигнал экспериментальный. Направленный edge не подтверждён на бэктесте. "
    "Решение о входе и риск — на пользователе.</i>"
)

PHASE_HUMAN: dict[str, str] = {
    "dump_active": "Активный дамп",
    "dump_initiating": "Начало дампа",
    "dump_imminent": "Дамп неизбежен",
    "dump_setup_forming": "Формируется шорт",
    "dump_confirmed": "Шорт подтверждён",
    "exhaustion_at_high": "Истощение на хаях",
    "exhaustion_watch": "Наблюдение за истощением",
    "distribution": "Распределение",
    "impulse_initiating": "Начало импульса",
    "breakout_arming": "Вооружение пробоя",
    "post_dump_bounce": "Отскок после дампа",
    "accumulation": "Накопление",
    "accumulation_watch": "Наблюдение за накоплением",
    "long_imminent": "Лонг неизбежен",
    "long_setup_forming": "Формируется лонг",
    "long_confirmed": "Лонг подтверждён",
    "no_setup": "Нет сетапа",
    "no_dump_yet": "Нет дампа",
    "no_long_yet": "Нет лонга",
}

_TRIGGER_LABELS: dict[str, str] = {
    "pp_short_break": "пробой support (pp)",
    "pp_long_break": "пробой resistance (pp)",
    "pp_short_break_1h_closed": "1h пробой support (closed)",
    "pp_long_break_1h_closed": "1h пробой resistance (closed)",
    "pp_short_break_15m_closed": "15m пробой support (closed)",
    "pp_long_break_15m_closed": "15m пробой resistance (closed)",
    "1m_close_below_support": "1m закрытие ниже support",
    "1m_close_above_resistance": "1m закрытие выше resistance",
    "close_below_support": "закрытие ниже support",
    "close_above_resistance": "закрытие выше resistance",
    "dump_continuation_confirm": "продолжение дампа",
    "dump_fast_confirm": "быстрый confirm дампа",
    "taker_buy_pressure": "давление покупателей (taker)",
    "taker_sell_pressure": "давление продавцов (taker)",
    "microprice_buy_bias": "microprice в сторону покупок",
    "microprice_sell_bias": "microprice в сторону продаж",
    "ws_taker_buy_30s": "WS taker buy 30s",
    "ws_taker_sell_30s": "WS taker sell 30s",
    "ws_liq_cascade": "каскад ликвидаций (WS)",
    "ws_liq_cascade_score_only": "ликвидации (WS, слабый сигнал)",
    "poc_aligned": "цена у POC",
    "poc_contra": "POC против направления",
    "rsi1h_oversold": "RSI 1h перепродан",
    "rsi1h_overbought": "RSI 1h перекуплен",
    "rsi15_bear_regime": "RSI 15m медвежий режим",
    "macd_div_4h": "MACD дивергенция 4h",
    "bull_div_4h": "бычья дивергенция 4h",
    "bear_div_4h": "медвежья дивергенция 4h",
    "5m_rejection": "отбой 5m",
    "15m_bounce_wick": "отскок 15m (wick)",
    "at_fib_support": "у fib support",
    "regime_4h_bull": "режим 4h бычий",
    "regime_4h_bear": "режим 4h медвежий",
    "mid_dump_long_cap": "лонг ограничен mid-dump",
    "repeat_pumper": "повторный памп",
}

_VETO_LABELS: dict[str, str] = {
    "rr_below_min": "R:R ниже минимума",
    "sl_nominal_too_wide": "SL слишком широкий",
    "sl_floor_exceeds_tp2_cap": "SL не вписывается в TP2-cap",
    "tp1_at_or_above_entry": "TP1 на уровне входа или выше",
    "tp1_inside_entry_zone": "TP1 внутри зоны входа",
    "tp1_at_or_below_entry": "TP1 на уровне входа или ниже",
    "veto_lifecycle_bias_wait": "lifecycle: жди bias",
    "veto_htf_4h_distribution_vs_long": "4h distribution против лонга",
    "veto_mtf_1h_bull_vs_short": "1h бычий MTF против шорта",
    "veto_levels:rr_below_min": "R:R ниже минимума",
    "veto_levels:sl_nominal_too_wide": "SL слишком широкий",
    "veto_levels:sl_floor_exceeds_tp2_cap": "SL не вписывается в TP2-cap",
}

_STRONG_SIGNAL_PHASES = frozenset(
    {
        "dump_active",
        "exhaustion_at_high",
        "distribution",
        "dump_confirmed",
        "accumulation",
        "impulse_initiating",
        "breakout_arming",
        "long_confirmed",
    }
)


def fmt_price(value: float | None) -> str:
    # Magnitude-adaptive precision: keep ~5-6 significant figures without printing
    # below a plausible tick. A flat .3f rendered BTC as 63937.750 / an
    # invalidation as 60453.476 — three sub-tick digits (BTC perp tick is 0.1),
    # false precision the eye reads as spurious exactness. Large prices therefore
    # get fewer decimals; sub-dollar instruments keep enough to stay distinct.
    if value is None:
        return "—"
    v = float(value)
    a = abs(v)
    if a >= 10000:
        return f"{v:.1f}"   # 63937.8 — 1 decimal ≈ major-perp tick
    if a >= 1000:
        return f"{v:.2f}"   # 2345.67
    if a >= 100:
        return f"{v:.3f}"
    if a >= 1:
        return f"{v:.4f}"
    if a >= 0.01:
        return f"{v:.5f}"
    if a >= 0.001:
        return f"{v:.6f}"
    # Sub-milli-dollar perps (1000SATS ≈ 3.5e-5, DOGS, NEIRO…) trade on 1e-7/1e-8
    # ticks; a flat .6f collapsed distinct levels (entry_lo/entry_hi, SL vs TP)
    # into the same rendered string. Binance max pricePrecision is 8.
    if a >= 0.0001:
        return f"{v:.7f}"
    return f"{v:.8f}"


def format_symbol_telegram(symbol: str) -> str:
    sym = str(symbol or "").upper().strip()
    if sym.endswith("USDT"):
        base = sym[:-4]
    elif sym.endswith("-USDT"):
        base = sym[:-5]
    else:
        base = sym
    if base.isascii() and base.replace("-", "").isalnum():
        return html.escape(f"{base}-USDT")
    return html.escape(f"#{base[:8]}-USDT" if base else "?")


def phase_human(phase: str) -> str:
    return PHASE_HUMAN.get(phase, phase)


def phase_badge(phase: str, confirmed: bool, *, direction: str = "short") -> str:
    if confirmed:
        return "🚨"
    if direction == "long":
        return {
            "long_imminent": "🟢",
            "long_setup_forming": "🟡",
            "long_confirmed": "🚨",
            "accumulation_watch": "🔵",
            "no_long_yet": "⚪",
        }.get(phase, "⚪")
    return {
        "dump_imminent": "🔴",
        "dump_setup_forming": "🟠",
        "dump_confirmed": "🚨",
        "exhaustion_watch": "🟡",
        "no_dump_yet": "⚪",
    }.get(phase, "⚪")


def trigger_human(code: str) -> str:
    raw = str(code or "").strip()
    key = raw.lower()
    if key in _TRIGGER_LABELS:
        return _TRIGGER_LABELS[key]
    for prefix, label in (
        ("below_impulse_high_", "ниже impulse high "),
        ("broke_resistance_", "пробой resistance "),
        ("broke_support_", "пробой support "),
        ("pp_long_break_", "пробой resistance "),
        ("pp_short_break_", "пробой support "),
    ):
        if key.startswith(prefix):
            tail = raw[len(prefix) :]
            return f"{label}{fmt_price(float(tail)) if tail.replace('.', '', 1).isdigit() else tail}"
    return raw.replace("_", " ")


def veto_human(code: str) -> str:
    raw = str(code or "").strip()
    key = raw.lower()
    if key in _VETO_LABELS:
        return _VETO_LABELS[key]
    if key.startswith("veto_levels:"):
        inner = key.split(":", 1)[-1]
        return _VETO_LABELS.get(inner, inner.replace("_", " "))
    if key.startswith("veto_"):
        return raw.replace("veto_", "").replace("_", " ")
    return raw.replace("_", " ")


def rr_emoji(risk_reward: Any) -> str:
    try:
        rr = float(risk_reward)
    except (TypeError, ValueError):
        return "⚠️"
    return "✅" if rr >= 3.0 else "⚠️"


def rr_display(risk_reward: Any) -> str:
    if risk_reward is None:
        return ""
    try:
        rr = float(risk_reward)
    except (TypeError, ValueError):
        return ""
    return f"{rr_emoji(rr)} R:R <code>{rr:.2f}</code>"


def signal_strength_rating(conviction_pct: float, lc_phase: str) -> str:
    """Map 0-100 uncalibrated composite score to operator tier (not a probability)."""
    if conviction_pct >= 70:
        tier = "strong"
    elif conviction_pct >= 60:
        tier = "ready"
    elif conviction_pct >= 45:
        tier = "forming"
    else:
        tier = "watch"
    if tier == "strong" and lc_phase in _STRONG_SIGNAL_PHASES:
        return "🔥 СИЛЬНЫЙ"
    if tier in {"strong", "ready"} and lc_phase in _STRONG_SIGNAL_PHASES:
        return "✅ УВЕРЕННЫЙ"
    if tier == "forming":
        return "⚠️ СРЕДНИЙ"
    return "📊 СЛАБЫЙ"


__all__ = [
    "EXPERIMENTAL_DISCLAIMER_RU",
    "PHASE_HUMAN",
    "fmt_price",
    "format_symbol_telegram",
    "phase_badge",
    "phase_human",
    "rr_display",
    "rr_emoji",
    "signal_strength_rating",
    "trigger_human",
    "veto_human",
]
