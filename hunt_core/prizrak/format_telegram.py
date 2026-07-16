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
    # Spot context — spot-perp basis, spot/fut 24h volume share, weekly spot ladder.
    spot_txt = _spot_context_text(analysis.row)
    if spot_txt:
        parts.extend(["", spot_txt])
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


def _spot_context_text(row: dict[str, Any]) -> str | None:
    """Spot block for the deep panel — data only, no verdicts.

    Method basis: «крупняк на споте покупает на наших зонах интереса» (Prizrak,
    BTC-squeeze разбор) — spot participation is his context read; a perp move the
    spot market does not join is derivatives-only. The weekly ladder mirrors his
    macro levels off the full-history spot chart (POL/MATIC разбор).
    """
    _m = row.get("market")
    market = _m if isinstance(_m, dict) else {}
    lines: list[str] = []

    spread = market.get("spot_futures_spread_bps")
    if isinstance(spread, (int, float)):
        rel = "перп дороже спота" if spread > 0 else "перп дешевле спота" if spread < 0 else "паритет"
        lines.append(f"базис спот-перп: <code>{spread:+.1f} bps</code> ({rel})")

    spot_qv = market.get("spot_quote_volume_24h")
    fut_qv_m = market.get("vol_24h_m")
    if (
        isinstance(spot_qv, (int, float))
        and isinstance(fut_qv_m, (int, float))
        and fut_qv_m > 0
    ):
        ratio = float(spot_qv) / (float(fut_qv_m) * 1e6)
        lines.append(
            f"объём 24ч спот/фьюч: <code>{ratio:.2f}</code>"
            f" (спот ${float(spot_qv) / 1e6:,.0f}M / фьюч ${float(fut_qv_m):,.0f}M)"
        )

    _lad = row.get("spot_weekly_ladder")
    ladder = _lad if isinstance(_lad, dict) else {}

    def _lvls(side: str) -> str:
        out = []
        for lv in ladder.get(side) or []:
            if not isinstance(lv, dict):
                continue
            px = lv.get("price")
            if not isinstance(px, (int, float)):
                continue
            t = int(lv.get("touches") or 0)
            out.append(f"<code>{fmt_price(float(px))}</code>" + (f"×{t}" if t > 1 else ""))
        return " · ".join(out)

    below, above = _lvls("below"), _lvls("above")
    if below or above:
        seg = []
        if below:
            seg.append(f"ниже: {below}")
        if above:
            seg.append(f"выше: {above}")
        lines.append("ladder 1w (спот, полная история): " + " | ".join(seg))

    if not lines:
        return None
    return "\n".join(["📈 <b>Спот-контекст</b>", *lines])


def format_analyst_from_row(row: dict[str, Any], **kwargs: Any) -> str:
    return format_analyst_telegram(build_analyst_report_from_row(row, **kwargs))


format_deep_analysis_telegram = format_analyst_telegram  # backward compat after deep→analyst rename

__all__ = ["format_analyst_telegram", "format_analyst_from_row", "format_deep_analysis_telegram"]
