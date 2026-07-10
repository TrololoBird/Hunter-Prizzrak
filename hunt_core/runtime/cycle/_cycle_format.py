"""Watch tick Telegram / digest line formatters (cycle split)."""
from __future__ import annotations

from typing import Any

def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    if abs(v) >= 0.01:
        return f"{v:.5f}"
    return f"{v:.6f}"


def _phase_badge(phase: str, confirmed: bool, *, direction: str = "short") -> str:
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


def _format_setup_lines(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
    suppress_context: bool = False,
) -> list[str]:
    score_key = "dump_score" if direction == "short" else "long_score"
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("impulse_confirmed"))
    badge = _phase_badge(phase, confirmed, direction=direction)

    def _opt_num(val: Any, *, digits: int = 4) -> str:
        if val is None:
            return "—"
        try:
            return f"{float(val):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    def _fmt_oi(val: Any, px: float) -> str:
        if val is None:
            return "—"
        try:
            contracts = float(val)
        except (TypeError, ValueError):
            return "—"
        if contracts <= 0:
            return "—"
        if px > 0 and contracts < px * 100:
            notional = contracts * px
            if notional >= 1_000_000_000:
                return f"${notional / 1_000_000_000:.2f}B"
            if notional >= 1_000_000:
                return f"${notional / 1_000_000:.1f}M"
            return f"${notional:,.0f}"
        if contracts >= 1_000_000_000:
            return f"${contracts / 1_000_000_000:.2f}B"
        if contracts >= 1_000_000:
            return f"${contracts / 1_000_000:.1f}M"
        return _fmt_price(contracts)

    from hunt_core.deliver.readiness import readiness_label_for_setup
    score_val = setup.get(score_key)
    readiness_line = readiness_label_for_setup(
        setup, direction=direction, row=row
    )
    score_str = f"{float(score_val):.0f}" if score_val is not None else "—"
    dir_label = "SHORT" if direction == "short" else "LONG"

    def _rsi(key: str) -> str:
        val = (tf.get(key) or {}).get("rsi14")
        return "—" if val is None else f"{val:.0f}"

    div_bits: list[str] = []
    if direction == "short":
        if (tf.get("1h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear1h✓")
        if (tf.get("4h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear4h✓")
    else:
        if (tf.get("1h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull1h✓")
        if (tf.get("4h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull4h✓")
    div_txt = " · " + " ".join(div_bits) if div_bits else ""

    triggers = setup.get("triggers") or []
    headwinds = [t for t in triggers if str(t).startswith("headwind_")]
    tailwinds = [t for t in triggers if not str(t).startswith("headwind_")]
    trig_txt = html.escape(", ".join(str(t) for t in tailwinds[:5]))
    if len(tailwinds) > 5:
        trig_txt += "…"
    headwind_txt = html.escape(", ".join(str(t) for t in headwinds[:3])) if headwinds else ""

    ez = setup.get("entry_zone") or [price, price]

    oi = pos.get("oi")
    oi_chg = pos.get("oi_chg_5m")
    fund = pos.get("funding_pct")
    taker = pos.get("taker_5m")
    ls = pos.get("ls_5m")

    if direction == "short":
        fib1272 = setup.get("fib_1272") or setup.get("resistance_liq")
        level_line = (
            f"Support <code>{_fmt_price(setup.get('support_break_level'))}</code> · "
            f"fib1272 <code>{_fmt_price(fib1272)}</code> · impulse H "
            f"<code>{_fmt_price(row.get('impulse_high'))}</code>"
        )
    else:
        level_line = (
            f"Resistance <code>{_fmt_price(setup.get('resistance_break_level'))}</code> · support "
            f"<code>{_fmt_price(setup.get('support_zone'))}</code> · impulse L "
            f"<code>{_fmt_price(row.get('impulse_low'))}</code>"
        )

    lines = [
        f"{badge} <b>{dir_label}</b> · <code>{phase}</code> · "
        f"{readiness_line} · score триггеров <code>{score_str}</code>",
        level_line,
        (
            f"Entry <code>{_fmt_price(ez[0])}-{_fmt_price(ez[1])}</code> · "
            f"SL <code>{_fmt_price(setup.get('stop_loss'))}</code> · "
            f"TP1 <code>{_fmt_price(setup.get('tp1'))}</code> · "
            f"TP2 <code>{_fmt_price(setup.get('tp2'))}</code>"
            + (
                f" · R:R <code>{setup.get('risk_reward')}</code>"
                if setup.get("risk_reward")
                else ""
            )
        ),
        (
            f"RSI 1m/5m/15m/1h/4h: "
            f"<code>{_rsi('1m')}/{_rsi('5m')}/{_rsi('15m')}/{_rsi('1h')}/{_rsi('4h')}</code>"
            f"{div_txt}"
        ),
        (
            f"OI <code>{_fmt_oi(oi, price)}</code> · "
            f"Δ5m <code>{_opt_num(oi_chg)}</code> · "
            f"fund <code>{_opt_num(fund, digits=3)}%</code> · "
            f"taker5m <code>{_opt_num(taker)}</code> · "
            f"L/S <code>{_opt_num(ls)}</code>"
        ),
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
    if headwind_txt:
        lines.append(f"Headwinds: <code>{headwind_txt}</code>")
    regime = row.get("regime") or {}
    poc1h = regime.get("poc_1h")
    vah1h = regime.get("vah_1h")
    val1h = regime.get("val_1h")
    # Pinned deep-analysis already renders the volume profile (cross-exchange merged) —
    # skip the Binance-only copy here to avoid a duplicate with mismatched numbers.
    if poc1h is not None and not suppress_context:
        lines.append(
            f"Volume profile 1h: POC <code>{_fmt_price(float(poc1h))}</code>"
            + (f" · VAH <code>{_fmt_price(float(vah1h))}</code>" if vah1h else "")
            + (f" · VAL <code>{_fmt_price(float(val1h))}</code>" if val1h else "")
        )
    if confirmed:
        hard = setup.get("confirm_hard") or []
        lines.append(f"<b>✅ CONFIRM</b> {html.escape(', '.join(str(x) for x in hard))}")
    return lines


_PHASE_HUMAN: dict[str, str] = {
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


def _phase_human(phase: str) -> str:
    return _PHASE_HUMAN.get(phase, phase)


def _pct_str(a: float, b: float, direction: str) -> str:
    from hunt_core.deliver._math import pct_str
    return pct_str(a, b, direction)


def _reason_human(setup: dict[str, Any], *, direction: str, lc_phase: str) -> str:
    """Build human-readable reason line from phase + triggers + fuel."""
    phase_txt = _phase_human(lc_phase) if lc_phase and lc_phase != "—" else _phase_human(
        str(setup.get("phase") or "")
    )
    triggers = setup.get("triggers") or []
    trig_short: list[str] = []
    for t in triggers[:3]:
        ts = str(t)
        if "volume" in ts or "vol" in ts:
            trig_short.append("аномальный объём")
        elif "support" in ts or "break" in ts:
            trig_short.append("пробой поддержки")
        elif "resistance" in ts:
            trig_short.append("пробой сопротивления")
        elif "cascade" in ts or "liq" in ts:
            trig_short.append("каскад ликвидаций")
        elif "rejection" in ts:
            trig_short.append("отбой от уровня")
        elif "rsi" in ts or "div" in ts:
            trig_short.append("RSI-дивергенция")
        elif "funding" in ts:
            trig_short.append("перегрев фандинга")
        elif "oi" in ts:
            trig_short.append("аномалия OI")
        elif "whale" in ts:
            trig_short.append("крупный продавец")
        else:
            trig_short.append(ts.replace("_", " ").split(":")[0])
    trig_txt = ", ".join(dict.fromkeys(trig_short))  # deduplicate, keep order
    if phase_txt and trig_txt:
        return f"{phase_txt} · {trig_txt}"
    return phase_txt or trig_txt or "—"



__all__ = [
    "_fmt_price",
    "_format_setup_lines",
    "_phase_badge",
    "_phase_human",
    "_pct_str",
    "_reason_human",
]
