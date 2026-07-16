"""Telegram follow-up / invalidate / TP messages."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price, phase_human
from hunt_core.track.tracker import duration_minutes

def _duration_str(opened: str) -> str:
    minutes = duration_minutes(opened)
    if minutes is None:
        return "—"
    total_m = int(minutes)
    h, m = divmod(total_m, 60)
    if h > 0:
        return f"{h}ч {m}м"
    return f"{m}м"


def _trade_duration_line(payload: dict[str, Any]) -> str:
    raw_min = payload.get("duration_min")
    if raw_min is not None:
        try:
            total_m = int(float(raw_min))
            h, m = divmod(total_m, 60)
            if h > 0:
                return f"{h}ч {m}м"
            return f"{m}м"
        except (TypeError, ValueError):
            pass
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    return _duration_str(opened_raw)


def _format_pnl_pct(pnl: Any) -> str:
    if pnl is None:
        return ""
    try:
        val = float(pnl)
    except (TypeError, ValueError):
        return ""
    sign = "+" if val >= 0 else ""
    emoji = "💰" if val > 0 else "💸" if val < 0 else "➖"
    return f"{emoji} PnL: <b>{sign}{val:.2f}%</b>"


def _pnl_pct_from_prices(
    *,
    direction: str,
    entry_lo: Any,
    entry_hi: Any,
    exit_price: Any,
) -> float | None:
    if entry_lo is None or entry_hi is None or exit_price is None:
        return None
    try:
        entry_mid = (float(entry_lo) + float(entry_hi)) / 2.0
        exit_p = float(exit_price)
    except (TypeError, ValueError):
        return None
    if entry_mid <= 0 or exit_p <= 0:
        return None
    if direction.upper() == "SHORT":
        return (entry_mid - exit_p) / entry_mid * 100.0
    return (exit_p - entry_mid) / entry_mid * 100.0


def format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    from hunt_core.deliver.readiness import invalidate_detail_human

    sym = html.escape(str(followup.symbol).replace("USDT", "-USDT"))
    direction = followup.direction.upper()
    price = fmt_price(followup.price)
    lc = row.get("lifecycle") or {}
    payload = followup.payload if isinstance(followup.payload, dict) else {}
    event = followup.event

    sl = fmt_price(payload.get("stop_loss"))
    tp1_lvl = fmt_price(payload.get("tp1"))
    tp2_lvl = fmt_price(payload.get("tp2"))
    entry_lo = payload.get("entry_lo")
    entry_hi = payload.get("entry_hi")
    entry_zone = (
        f"{fmt_price(entry_lo)}–{fmt_price(entry_hi)}"
        if entry_lo is not None and entry_hi is not None
        else "—"
    )
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    msg_id = payload.get("entry_message_id")
    entry_ref = f"Вход {entry_zone}"
    if msg_id:
        entry_ref += f" · сигнал TG <code>#{msg_id}</code>"

    reason_raw = str(payload.get("reason") or "")
    detail_human = invalidate_detail_human(str(followup.detail or ""), reason=reason_raw)

    if event == "fix_profit_tp1":
        fix_pct = int(payload.get("partial_fixed_pct") or 50)
        new_sl = fmt_price(payload.get("stop_loss"))
        pnl_line = _format_pnl_pct(payload.get("pnl_pct"))
        if not pnl_line:
            est = _pnl_pct_from_prices(
                direction=direction,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                exit_price=payload.get("tp1"),
            )
            pnl_line = _format_pnl_pct(est)
        duration = _trade_duration_line(payload)
        trade_meta = f"{pnl_line} · ⏱ {duration}" if pnl_line else f"⏱ {duration}"
        return (
            f"✅ <b>TP1 достигнут · {sym} {direction}</b>\n"
            f"{trade_meta}\n"
            f"🔒 Зафиксируй <b>{fix_pct}%</b> позиции · Стоп перенесён на безубыток <code>{new_sl}</code>\n"
            f"🎯 Следующая цель: TP2 <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "fix_profit_tp2":
        duration = _duration_str(opened_raw)
        skipped = bool(payload.get("tp1_skipped"))
        extra = " (TP1 пролёт)" if skipped else ""
        # Used to print the TP2 PRICE under a «PnL» label and never compute a PnL
        # at all. Mirror the TP1 branch: real percent, price kept as the exit ref.
        pnl_line = _format_pnl_pct(payload.get("pnl_pct"))
        if not pnl_line:
            est = _pnl_pct_from_prices(
                direction=direction,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                exit_price=payload.get("tp2"),
            )
            pnl_line = _format_pnl_pct(est)
        pnl_meta = f"{pnl_line} · " if pnl_line else ""
        return (
            f"📋 <b>Закрыт {sym} {direction}{extra}</b>\n"
            f"{pnl_meta}Выход: TP2 <code>{tp2_lvl}</code> · Длит: {duration}\n"
            f"📌 Причина: Достигнут TP2\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "trailing_updated":
        new_sl = fmt_price(payload.get("stop_loss"))
        protected = payload.get("protected_pnl_pct")
        try:
            prot_str = f"+{float(protected or 0):.1f}%"
        except (TypeError, ValueError):
            prot_str = "—"
        return (
            f"📈 <b>TRAILING АКТИВЕН · {sym} {direction}</b>\n"
            f"Стоп подтянут → <code>{new_sl}</code> · защита ~<b>{prot_str}</b>\n"
            f"⚡ На бирже вручную подтяни SL до этого уровня (Hunt не торгует).\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "early_breakeven":
        new_sl = fmt_price(payload.get("stop_loss"))
        try:
            mfe_str = f"{float(payload.get('mfe_pct') or 0):.1f}%"
        except (TypeError, ValueError):
            mfe_str = "—"
        phase = str(payload.get("entry_lifecycle_phase") or "—")
        return (
            f"🔒 <b>EARLY BE · {sym} {direction}</b>\n"
            f"MFE <b>{mfe_str}</b> · фаза <code>{phase}</code>\n"
            f"Стоп → <code>{new_sl}</code> (безубыток+buf)\n"
            f"⚡ На бирже вручную подтяни SL до этого уровня (Hunt не торгует).\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "entry_triggered":
        return (
            f"🎯 <b>TRIGGERED · {sym} {direction}</b>\n"
            f"✅ Цена <code>{price}</code> в зоне входа <code>{entry_zone}</code>\n"
            f"📍 Стоп: <code>{sl}</code> · TP1: <code>{tp1_lvl}</code> · TP2: <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>ARMED → TRIGGERED · limit касание · не auto-trade</i>"
        )

    if event == "invalidate":
        duration = _trade_duration_line(payload)

        _reason_map = {
            "stop_hit": ("🔴 Стоп-лосс пробит", "Позиция закрылась по стопу."),
            "trailing_stop_profit": (
                "✅ Trailing stop / фиксация",
                "Позиция закрыта по подтянутому стопу в зоне профита.",
            ),
            "tp1": ("✅ Достигнут TP1", "Взята первая цель."),
            "tp2": ("✅ Достигнут TP2", "Взята финальная цель."),
            "bounce_invalidate": (
                "🔄 Lifecycle: отскок — шорт отменён",
                "Рынок начал восстановление — тезис на дамп исчерпан.",
            ),
            "time_stall": (
                "⏳ Тезис не сработал",
                "Нет прогресса за 8ч — вероятно, сетап поглощён рынком.",
            ),
            "bias_flip": (
                "🔄 Фаза сменилась против позиции",
                "Lifecycle перешёл в противоположную фазу — продолжение маловероятно.",
            ),
            "support_lost": (
                "⚠️ Потеря поддержки",
                "Ключевая поддержка утрачена — лонг-тезис сломан.",
            ),
        }
        lc_phase_payload = str(payload.get("phase") or "")
        phase_txt = phase_human(lc_phase_payload) if lc_phase_payload else ""

        reason_title, reason_body = _reason_map.get(
            reason_raw,
            (f"📌 {html.escape(detail_human)}", ""),
        )
        if reason_raw == "lifecycle_stale" and phase_txt:
            reason_title = "🔄 Фаза сменилась против позиции"
            reason_body = f"Новая фаза: <b>{html.escape(phase_txt)}</b> — тезис исчерпан."

        # PnL from tracker payload (preferred) or entry midpoint vs exit tick
        pnl_line = _format_pnl_pct(payload.get("pnl_pct"))
        if not pnl_line:
            est = _pnl_pct_from_prices(
                direction=direction,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                exit_price=followup.price,
            )
            pnl_line = _format_pnl_pct(est)
        if pnl_line:
            pnl_line += "\n"

        action_needed = reason_raw not in {
            "stop_hit",
            "trailing_stop_profit",
            "tp1",
            "tp2",
        }
        action_line = "⚡ <b>Закрой позицию вручную</b>\n" if action_needed else ""

        if reason_raw in {"trailing_stop_profit", "tp1", "tp2"}:
            verdict = "✅ Профит"
        elif reason_raw in {"stop_hit"}:
            verdict = "🔴 Стоп"
        elif reason_raw in {"time_stall", "timeout"}:
            verdict = "⏳ Таймаут"
        else:
            verdict = "🔄 Тезис снят"

        return (
            f"📋 <b>ПОЗИЦИЯ ЗАКРЫТА · {sym} {direction}</b>\n"
            f"<b>{verdict}</b> · {reason_title}\n"
            f"{reason_body}\n"
            f"{action_line}"
            f"{pnl_line}"
            f"⏱ В сделке: {duration}\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "stop_warning":
        return (
            f"⚠️ <b>СТОП РЯДОМ · {sym} {direction}</b>\n"
            f"Цена <code>{price}</code> близко к SL <code>{sl}</code>\n"
            f"Реши: держать или фиксировать вручную.\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    badges = {"phase_change": "🔄", "avg_zone": "➕"}
    titles = {"phase_change": "PHASE CHANGE", "avg_zone": "AVG ZONE"}
    badge = badges.get(event, "📣")
    title = titles.get(event, event)
    lc_phase_now = html.escape(phase_human(str(lc.get("phase") or "—")))
    return (
        f"{badge} <b>{title}</b>\n"
        f"{sym} · <code>{direction}</code> · цена <code>{price}</code>\n"
        f"{html.escape(detail_human)}\n"
        f"{entry_ref}\n"
        f"SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
        f"Фаза: {lc_phase_now}\n"
        f"<i>Hunt follow-up · не auto-trade</i>"
    )



__all__ = ["format_followup_telegram"]
