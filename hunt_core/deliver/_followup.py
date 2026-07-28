"""Telegram follow-up / invalidate / TP messages."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price, format_symbol_telegram, phase_human
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


# Ниже этого модуля результат печатается как «0.00%», поэтому и назвать его прибылью нельзя:
# один порог на вердикт и на эмодзи, иначе «➖ Безубыток» стояло рядом с «💰 PnL: +0.02%».
_BREAKEVEN_EPS_PCT = 0.05


def _format_pnl_pct(pnl: Any) -> str:
    if pnl is None:
        return ""
    try:
        val = float(pnl)
    except (TypeError, ValueError):
        return ""
    sign = "+" if val >= 0 else ""
    if abs(val) < _BREAKEVEN_EPS_PCT:
        emoji = "➖"
    else:
        emoji = "💰" if val > 0 else "💸"
    return f"{emoji} PnL: <b>{sign}{val:.2f}%</b>"


def _pnl_pct_from_prices(
    *,
    direction: str,
    entry_lo: Any,
    entry_hi: Any,
    exit_price: Any,
) -> float | None:
    """Запасной расчёт PnL, когда трекер не положил его в payload.

    Делегирует `track/pnl.py::realized_pct` — единственной формуле проекта. Здесь стояла ТРЕТЬЯ
    её копия, считавшая от СЕРЕДИНЫ полосы: на широкой зоне это дарит половину её ширины
    (замер 2026-07-27 по 168 невырожденных зон: медиана 2.245%, максимум 13.26%), и одна и та же
    сделка приезжала читателю разными числами из разных сообщений.
    """
    if entry_lo is None or entry_hi is None or exit_price is None:
        return None
    from hunt_core.track.pnl import realized_pct

    realized = realized_pct(
        {"entry_lo": entry_lo, "entry_hi": entry_hi},
        direction=str(direction).lower(),
        exit_price=exit_price,
    )
    return None if realized is None else realized[0]


def format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    from hunt_core.deliver.readiness import invalidate_detail_human

    # Общий рендер символа (`_labels`), а не своя `replace("USDT", "-USDT")`: та подменяла
    # КАЖДОЕ вхождение и не проверяла, чем строка вообще является. Один рендер на канал — иначе
    # один и тот же инструмент называется в соседних сообщениях по-разному.
    sym = format_symbol_telegram(str(followup.symbol))
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
        # Знак ставит ЧИСЛО, а не разметка: было `f"+{...:.1f}%"` поверх значения, которое
        # знак несёт само, — на любом отрицательном это дало бы «+-1.2%».
        # ⚠ Честная граница находки: в логах за 24 ч такого рендера НЕТ (все наблюдённые
        # значения положительные), и по коду он недостижим — `_update_trailing_stop` объявляет
        # сдвиг только после того, как новый стоп ушёл за худшую кромку входа, а `protected`
        # меряется от неё же. Это защита от рассинхрона двух условий, а не починка живого бага.
        if isinstance(protected, (int, float)):
            prot_str = f"{float(protected):+.1f}%"
            guard_word = "стоп ещё в убытке" if float(protected) < 0 else "защита"
        else:
            prot_str = "—"
            guard_word = "защита"
        return (
            f"📈 <b>TRAILING АКТИВЕН · {sym} {direction}</b>\n"
            f"Стоп подтянут → <code>{new_sl}</code> · {guard_word} ~<b>{prot_str}</b>\n"
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

    if event in {"zone_approach", "zone_entry"}:
        # Zone-map alert (prizrak/zone_watch.py) — a LIMIT level from the карта зон, in the author's
        # grammar: zone + ПОК + стоп за структуру + цели. Not an emitted signal's lifecycle event.
        kind = str(payload.get("zone_kind") or "зона")
        emoji = {"перезакуп": "🟢", "добор": "🟡", "шорт": "🔴"}.get(kind, "🎯")
        z_lo, z_hi = fmt_price(payload.get("zone_lo")), fmt_price(payload.get("zone_hi"))
        band = z_lo if z_lo == z_hi else f"{z_lo}–{z_hi}"
        poc = payload.get("poc")
        poc_s = f" (ПОК <code>{fmt_price(poc)}</code>)" if isinstance(poc, (int, float)) else ""
        fact = " · <i>по факту</i>" if payload.get("by_fact") else ""
        stop_s = fmt_price(payload.get("stop_loss"))
        tgts = [t for t in (payload.get("targets") or []) if isinstance(t, (int, float))][:3]
        tgt_line = (
            "\n🎯 цели: " + " · ".join(f"<code>{fmt_price(t)}</code>" for t in tgts) if tgts else ""
        )
        if event == "zone_entry":
            head = f"🎯 <b>ЦЕНА В ЗОНЕ · {sym}</b>"
            sub = f"{emoji} {kind} <code>{band}</code>{poc_s}{fact} — вход по факту касания"
        else:
            try:
                d = f"{float(payload.get('dist_pct') or 0):.1f}%"
            except (TypeError, ValueError):
                d = "—"
            head = f"🔔 <b>ПОДХОД К ЗОНЕ · {sym}</b>"
            sub = f"{emoji} {kind} <code>{band}</code>{poc_s}{fact} — <b>{d}</b> до зоны"
        # Ордерная сетка и R:P — те же числа, что в карточке. Без RR сообщение про зону, которую
        # бот ВЕДЁТ, и про зону, отвергнутую по RR, выглядели одинаково.
        lines_s = ""
        raw_lines = payload.get("lines")
        if isinstance(raw_lines, list) and len(raw_lines) > 1:
            parts = []
            for ln in raw_lines:
                if isinstance(ln, (int, float)):
                    parts.append(f"<code>{fmt_price(float(ln))}</code>")
                elif isinstance(ln, dict) and isinstance(ln.get("price"), (int, float)):
                    parts.append(f"<code>{fmt_price(float(ln['price']))}</code>")
            if parts:
                lines_s = "\n📥 ордера: " + " · ".join(parts)
        rr_v = payload.get("rr")
        rr_s = f" · R:R <code>{float(rr_v):.2f}</code>" if isinstance(rr_v, (int, float)) else ""
        # ⚠ Ведёт ли бот эту сделку дальше — это ДАННЫЕ (`zone_watch._handoff`), а не догадка
        # читателя. Замер живого канала 2026-07-27: три события «ЦЕНА В ЗОНЕ», передач в трекер —
        # НОЛЬ (два раза направление занято, один раз нет цели ⇒ R:R не считается), но все три
        # сообщения звали «вход по факту касания» и молчали о том, что ни SL/TP, ни сообщения о
        # закрытии по этой зоне не будет. Молчание читалось как «ведём» — худший из вариантов.
        track_line = {
            "tracked": "\n✅ <i>Бот ведёт эту сделку: SL/TP придут отдельными сообщениями.</i>",
            "occupied": (
                "\n⚠️ <i>Бот НЕ ведёт: по этому символу и направлению уже открыт сигнал. "
                "Сопровождения по этой зоне не будет.</i>"
            ),
            "no_target": (
                "\n⚠️ <i>Бот НЕ ведёт: за зоной нет структурной цели, R:R не из чего считать. "
                "Уровень показан как ориентир — сопровождения не будет.</i>"
            ),
            "rr_below_floor": (
                "\n⚠️ <i>Бот НЕ ведёт: R:R по худшему заливу ниже порога метода. "
                "Уровень показан как ориентир — сопровождения не будет.</i>"
            ),
            "failed": "\n⚠️ <i>Бот НЕ ведёт: передача в трекер не удалась (см. лог).</i>",
        }.get(str(payload.get("tracking") or ""), "") if event == "zone_entry" else ""
        # Только для ВХОДА: на подходе к зоне передавать нечего, и решение ещё не принято —
        # печатать там «бот ведёт/не ведёт» значило бы сообщать исход до самого события.
        return (
            f"{head}\n{sub}\n"
            f"📍 Цена <code>{price}</code> · стоп <code>{stop_s}</code> (за структуру){rr_s}"
            f"{tgt_line}{lines_s}{track_line}\n"
            f"<i>Зона карты · лимит вручную · не auto-trade</i>"
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

        # `pnl_basis` называет базу И способ (`track/pnl.py`): суффикс `partial_fix_at_tp1`
        # означает, что часть уже снята на первой цели, и остаток вышел по ПЕРЕНЕСЁННОМУ стопу.
        # Без этого различия «стоп в безубытке после взятого TP1» и «трейл в прибыли» печатались
        # одной строкой, и читателю оставалось гадать, была ли фиксация.
        _partial_booked = "partial_fix" in str(payload.get("pnl_basis") or "")
        _reason_map = {
            "stop_hit": ("🔴 Стоп-лосс пробит", "Позиция закрылась по стопу."),
            "trailing_stop_profit": (
                "✅ Выход по перенесённому стопу",
                (
                    "Часть зафиксирована на первой цели, остаток вышел по стопу "
                    "в безубытке/прибыли."
                    if _partial_booked
                    else "Стоп был подтянут за ценой — позиция закрыта не в убыток."
                ),
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
            # Живые продюсеры без своей строки: `tracker._short_structure_invalidated` и
            # `_long_structure_invalidated` (через `_followups.py`). Без них заголовок падал в
            # сырой код («📌 reclaim_invalidation»), а тело было ПУСТЫМ — сообщение печатало
            # голую строку между заголовком и призывом закрыть позицию.
            "reclaim_invalidation": (
                "🔄 Уровень отвоёван обратно",
                "Цена вернулась выше уровня слома — тезис на шорт снят.",
            ),
            "trend_exhaustion": (
                "🔄 Фаза сменилась на истощение",
                "Рынок перешёл в истощение/раздачу — лонг-тезис исчерпан.",
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

        # PnL сделки — той же формулой, что пишет леджер (`track/pnl.py::realized_pct`).
        pnl_val = payload.get("pnl_pct")
        if not isinstance(pnl_val, (int, float)):
            pnl_val = _pnl_pct_from_prices(
                direction=direction,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                exit_price=followup.price,
            )
        pnl_line = _format_pnl_pct(pnl_val)
        if pnl_line:
            pnl_line += "\n"

        action_needed = reason_raw not in {
            "stop_hit",
            "trailing_stop_profit",
            "tp1",
            "tp2",
        }
        action_line = "⚡ <b>Закрой позицию вручную</b>\n" if action_needed else ""

        # ⚠ ВСЕ ТРИ строки исхода обязаны согласовываться с числом: вердикт, заголовок причины
        # и её пояснение. На живом канале 2026-07-27 четыре закрытия из шести вышли как
        # «🔴 Стоп · 🔴 Стоп-лосс пробит / Позиция закрылась по стопу» рядом с «💰 PnL +4.50%» —
        # три взаимоисключающих утверждения в одном сообщении из шести строк.
        #
        # Классификацию чинит `_evaluate_levels.py`, но она НЕ единственный продюсер `stop_hit`:
        # `tracker._short_structure_invalidated` / `_long_structure_invalidated` сверяют цену с
        # ТЕКУЩИМ стопом, который `apply_tp1_management` уже подвинул в безубыток, и возвращают
        # тот же код без всякой проверки знака. Поэтому форматтер — последний рубеж: если
        # результат ИЗМЕРЕН, он и решает, как назвать исход.
        _pnl = float(pnl_val) if isinstance(pnl_val, (int, float)) else None
        _flat = _pnl is not None and abs(_pnl) < _BREAKEVEN_EPS_PCT
        if reason_raw in {"trailing_stop_profit", "tp1", "tp2"}:
            verdict = "➖ Безубыток" if _flat else "✅ Профит"
        elif reason_raw == "stop_hit":
            if _pnl is None or _pnl < 0:
                verdict = "🔴 Стоп"
            else:
                # Стоп, который сработал ВЫШЕ входа (лонг) — физически не стоп-аут.
                verdict = "➖ Безубыток" if _flat else "✅ Профит"
                reason_title = "✅ Выход по перенесённому стопу"
                reason_body = (
                    "Стоп стоял уже не в убытке — позиция закрыта по нему, а не по исходному SL."
                )
        elif reason_raw in {"time_stall", "timeout"}:
            verdict = "⏳ Таймаут"
        else:
            verdict = "🔄 Тезис снят"

        # Пустое пояснение — это ПУСТАЯ СТРОКА в сообщении, а не отсутствие строки: причина без
        # своей записи в `_reason_map` печатала голый разрыв между заголовком и призывом закрыть.
        body_line = f"{reason_body}\n" if reason_body else ""
        return (
            f"📋 <b>ПОЗИЦИЯ ЗАКРЫТА · {sym} {direction}</b>\n"
            f"<b>{verdict}</b> · {reason_title}\n"
            f"{body_line}"
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
