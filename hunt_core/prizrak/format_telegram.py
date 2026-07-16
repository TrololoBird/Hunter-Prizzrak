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

    brief = _briefing_text(analysis, price)
    if brief:
        parts.extend(["", brief])

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


_ACTION_RU = {"long": "🟢 ЛОНГ", "short": "🔴 ШОРТ"}


def _nearest_zone(row: dict[str, Any], price: float) -> tuple[str, float, float] | None:
    """(side, near_edge, distance_pct) for whichever interest zone price is closest to."""
    iz = row.get("prizrak_interest_zones")
    if not isinstance(iz, dict) or price <= 0:
        return None
    best: tuple[str, float, float] | None = None
    for side in ("long", "short"):
        z = iz.get(side)
        if not isinstance(z, dict):
            continue
        lo, hi = z.get("lo"), z.get("hi")
        try:
            lo_f, hi_f = float(lo), float(hi)  # type: ignore[arg-type]  # None → caught
        except (TypeError, ValueError):
            continue
        if lo_f <= 0 or hi_f <= 0:
            continue
        # Distance to the zone as a whole: 0 when price is inside it, else to the
        # nearer edge — that is the number that decides whether it is live right now.
        if lo_f <= price <= hi_f:
            edge, dist = (hi_f if side == "long" else lo_f), 0.0
        else:
            edge = hi_f if price > hi_f else lo_f
            dist = abs(price / edge - 1.0) * 100.0
        if best is None or dist < best[2]:
            best = (side, edge, dist)
    return best


def _briefing_text(analysis: AnalystReport, price: float) -> str | None:
    """Three lines answering what a reader opens this card to learn.

    The card led with МТФ mechanics and made the reader assemble the conclusion out of
    ~40 numbers spread over seven sections — the verdict, the regime and the distance to
    the nearest actionable level were all derivable but none were stated. This states
    them, and states nothing the sections below do not already back up.
    """
    row = analysis.row
    _ps = row.get("prizrak_summary")
    ps = _ps if isinstance(_ps, dict) else {}
    action = str(ps.get("action") or "wait").strip().lower()

    _struct = row.get("prizrak_structure")
    struct = _struct if isinstance(_struct, dict) else {}
    _htf = struct.get("htf_bias")
    htf = _htf if isinstance(_htf, dict) else {}
    regime = str(htf.get("regime") or "")
    bias = str(htf.get("bias") or "").lower()

    lines: list[str] = []
    if action in ("long", "short"):
        lines.append(f"<b>{_ACTION_RU[action]}</b> — сетап активен, детали ниже")
    else:
        # WAIT is the common case and it is NOT nothing: it means limits are placed and
        # the card exists to say where. Say that, rather than leaving "WAIT" implied.
        lines.append("<b>⏸ ЖДЁМ</b> — активного сигнала нет, работают лимит-зоны")

    if regime == "accumulation":
        lines.append("режим: <b>накопление</b> (4h вверх против 1w/1d вниз) — шорт против набора")
    elif regime == "distribution":
        lines.append("режим: <b>распределение</b> (4h вниз против 1w/1d вверх) — лонг против раздачи")
    elif bias in ("long", "bull"):
        lines.append("режим: старшие ТФ <b>вверх</b>")
    elif bias in ("short", "bear"):
        lines.append("режим: старшие ТФ <b>вниз</b>")

    near = _nearest_zone(row, price)
    if near is not None:
        side, edge, dist = near
        side_ru = "лонг-зона" if side == "long" else "шорт-зона"
        if dist == 0.0:
            lines.append(f"<b>цена В {side_ru.upper()}</b> — вход по факту касания")
        else:
            lines.append(f"ближайшая {side_ru}: <code>{fmt_price(edge)}</code> — {dist:.1f}% от цены")
    return "\n".join(lines) if lines else None


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
