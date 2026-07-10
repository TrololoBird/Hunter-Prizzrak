"""Telegram formatting for analyst report (structure-first — no watch hunter narrative)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.prizrak.build import AnalystReport, build_analyst_report_from_row
from hunt_core.deliver._labels import fmt_price, format_symbol_telegram


def format_analyst_telegram(analysis: AnalystReport) -> str:
    sym = format_symbol_telegram(analysis.symbol)
    price = float(analysis.row.get("price") or 0)
    header = f"🔬 <b>Глубокий анализ</b> — <code>{sym}</code>"
    if price > 0:
        header += f" · <code>{fmt_price(price)}</code>"

    # NB: no "снимок: … UTC" line here — the source/freshness footer appended by the
    # caller (format_freshness_footer) already carries the same timestamp plus age +
    # source, so a header timestamp only duplicated it.
    parts: list[str] = [header]

    v2_txt = analysis.prizrak_text()
    if v2_txt:
        parts.extend(["", v2_txt])
    # МТФ structure — the exact multi-scale read that gated the signal (single source).
    mtf_txt = analysis.mtf_text()
    if mtf_txt:
        parts.extend(["", mtf_txt])
    # Pending 4h interest zones (long-at-support / short-at-resistance) — shown even on
    # WAIT so the user sees where limits sit, like the real Prizrak «локальные трейды».
    iz_txt = analysis.interest_zones_text()
    if iz_txt:
        parts.extend(["", iz_txt])
    # Skip structural forecasts for WAIT signals — irrelevant if no trade
    row_v2 = analysis.row.get("prizrak_summary") or {}
    forecast_ok = str(row_v2.get("action") or "wait").strip().upper() in {"LONG", "SHORT"}
    if forecast_ok:
        fc_txt = analysis.forecast_text()
        if fc_txt:
            parts.extend(["", fc_txt])
    if analysis.include_watch_appendix:
        parts.extend(["", "<i>Статус сканера — справочно (только PRE-автоскан)</i>"])
        wd = "сигнал прошёл бы" if analysis.would_deliver else "сигнал НЕ прошёл бы"
        parts.append(f"<i>{wd}</i>")
        if analysis.blockers:
            bl = ", ".join(html.escape(str(b)) for b in analysis.blockers[:5])
            parts.append(f"<i>блокеры: {bl}</i>")
    parts.append("")
    parts.append("<i>Структура / МТФ / карты · вход вручную · не инвестрекомендация</i>")
    return "\n".join(parts)


def format_analyst_from_row(row: dict[str, Any], **kwargs: Any) -> str:
    return format_analyst_telegram(build_analyst_report_from_row(row, **kwargs))


format_deep_analysis_telegram = format_analyst_telegram  # backward compat after deep→analyst rename

__all__ = ["format_analyst_telegram", "format_analyst_from_row", "format_deep_analysis_telegram"]
