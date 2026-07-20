"""Telegram formatting for analyst report (structure-first — no watch hunter narrative)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.prizrak.build import AnalystReport
from hunt_core.deliver._labels import fmt_price, format_symbol_telegram


def format_analyst_telegram(analysis: AnalystReport) -> str:
    sym = format_symbol_telegram(analysis.symbol)
    price = float(analysis.view.last_price or 0)
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
    spot_txt = _spot_context_text(analysis)
    if spot_txt:
        parts.extend(["", spot_txt])
    # Structural targets («куда цена может пойти») — shown on WAIT too, not only on an
    # active LONG/SHORT. On a WAIT tick this is exactly what the reader asks — where the
    # nearest structural magnets/zones are — and the panel is explicitly labelled
    # «уверенность в структуре зоны, не вероятность достижения», so it cannot be misread
    # as a trade call. (Was gated on action ∈ {LONG,SHORT}, hiding the projection on the
    # dominant WAIT case — the very tick where the reader most wants the «облако».)
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


def _limit_block(zone: dict[str, Any]) -> str:
    """Why the course takes the limit off this zone — "" when it does not.

    The two reasons carry DIFFERENT remedies and must not be collapsed: стр.31 sends a
    worked level to «слом структуры на МТФ», while стр.28 сц.7 says a sawn level is simply
    waited out. Returning a bare bool made the briefing print the стр.31 remedy over a
    пила, i.e. confidently the wrong instruction.
    """
    if zone.get("saw"):
        return "saw"
    worked = zone.get("worked")
    if isinstance(worked, int) and worked >= 1:
        return "worked"
    # Unknown verdict ⇒ not a licence (invariant I-6), but we cannot name a reason either.
    return "" if zone.get("limit_ok") is True else "unknown"


def _nearest_zone(iz: dict[str, Any] | None, price: float) -> tuple[str, float, float, str] | None:
    """(side, near_edge, distance_pct, limit_block) for the interest zone price is nearest.

    ``limit_block`` is the course's per-zone ruling (see :func:`_limit_block` and
    orchestrator.compute_interest_zones): "" means limits are on; anything else names why
    they are off. A blocked zone is still worth WATCHING — стр.31 keeps looking at the
    level — it just must not be sold as a limit.
    """
    if not isinstance(iz, dict) or price <= 0:
        return None
    best: tuple[str, float, float, str] | None = None
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
            best = (side, edge, dist, _limit_block(z))
    return best


_ABSTAIN_PRIORITY = ("rr_below_floor", "no_structural_target", "htf_counter_trend_no_slom",
                     "degenerate_stop")


def _abstain_reason_line(reasons: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str | None:
    """Turn the structured reject-reasons (``PrizrakOutput.abstain``) into one human line, so a
    WAIT symbol explains «почему нет сделки» with numbers instead of falling silent. Picks the
    most informative reason (an RR that just missed the floor is more actionable than a veto)."""
    if not reasons:
        return None
    by_reason = {r.get("reason"): r for r in reversed(reasons) if isinstance(r, dict)}
    pick = next((by_reason[k] for k in _ABSTAIN_PRIORITY if k in by_reason), None)
    if pick is None:
        return None
    kind = pick.get("reason")
    if kind == "rr_below_floor":
        parts = [f"RR {pick.get('rr')} < {pick.get('min_rr')}"]
        if pick.get("stop") is not None:
            buf = pick.get("buffer_pct")
            parts.append(f"стоп {fmt_price(float(pick['stop']))}" + (f" (буфер {buf}%)" if buf else ""))
        if pick.get("tp1") is not None:
            parts.append(f"TP1 {fmt_price(float(pick['tp1']))}")
        return "почему нет сделки: " + " · ".join(parts)
    if kind == "no_structural_target":
        return "почему нет сделки: нет структурной цели впереди в полосе ТФ (стр.24)"
    if kind == "htf_counter_trend_no_slom":
        return f"почему нет сделки: против старшего тренда ({pick.get('htf_bias')}) без слома МТФ (стр.31)"
    if kind == "degenerate_stop":
        return "почему нет сделки: вырожденная геометрия стопа"
    return None


def _briefing_text(analysis: AnalystReport, price: float) -> str | None:
    """Three lines answering what a reader opens this card to learn.

    The card led with МТФ mechanics and made the reader assemble the conclusion out of
    ~40 numbers spread over seven sections — the verdict, the regime and the distance to
    the nearest actionable level were all derivable but none were stated. This states
    them, and states nothing the sections below do not already back up.
    """
    _ps = analysis.prizrak.summary
    ps = _ps if isinstance(_ps, dict) else {}
    action = str(ps.get("action") or "wait").strip().lower()

    struct = analysis.prizrak.structure if isinstance(analysis.prizrak.structure, dict) else {}
    _htf = struct.get("htf_bias")
    htf = _htf if isinstance(_htf, dict) else {}
    regime = str(htf.get("regime") or "")
    bias = str(htf.get("bias") or "").lower()

    near = _nearest_zone(analysis.prizrak.interest_zones, price)

    lines: list[str] = []
    if action in ("long", "short"):
        lines.append(f"<b>{_ACTION_RU[action]}</b> — сетап активен, детали ниже")
    elif near is not None and not near[3]:
        # WAIT is the common case and it is NOT nothing: it means limits are placed and
        # the card exists to say where. Say that, rather than leaving "WAIT" implied.
        lines.append("<b>⏸ ЖДЁМ</b> — активного сигнала нет, работают лимит-зоны")
    else:
        # …but only when a limit is actually on. стр.31 takes limits OFF a level that has
        # already reacted, and that is the majority case for a well-worked level, so the
        # blanket «работают лимит-зоны» was advertising the one thing the course forbids.
        lines.append("<b>⏸ ЖДЁМ</b> — активного сигнала нет, лимиты не выставляем")

    if regime == "accumulation":
        lines.append("режим: <b>накопление</b> (4h вверх против 1w/1d вниз) — шорт против набора")
    elif regime == "distribution":
        lines.append("режим: <b>распределение</b> (4h вниз против 1w/1d вверх) — лонг против раздачи")
    elif bias in ("long", "bull"):
        lines.append("режим: старшие ТФ <b>вверх</b>")
    elif bias in ("short", "bear"):
        lines.append("режим: старшие ТФ <b>вниз</b>")

    # When a setup is LIVE, the reader's "where do I act" is the SETUP's entry — not a
    # pending limit zone. Printing the zone under a «🔴 ШОРТ — сетап активен» headline
    # put two different prices one line apart and let them read as one thought: live it
    # said «ближайшая шорт-зона: 81.2800 — 7.4% от цены» directly above a setup whose
    # entry was 75.19–75.49. Same word ("short"), different objects, contradictory
    # numbers — and the briefing exists precisely so the reader does not have to
    # reconcile the card against itself.
    if action in ("long", "short"):
        lo, hi = ps.get("entry_lo"), ps.get("entry_hi")
        try:
            lo_f, hi_f = float(lo), float(hi)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            lo_f = hi_f = 0.0
        if lo_f > 0 and hi_f > 0 and price > 0:
            if lo_f <= price <= hi_f:
                lines.append("<b>цена В ЗОНЕ ВХОДА</b> — детали и стоп ниже")
            else:
                edge = hi_f if price > hi_f else lo_f
                d = abs(price / edge - 1.0) * 100.0
                lines.append(
                    f"вход: <code>{fmt_price(lo_f)}–{fmt_price(hi_f)}</code> — {d:.1f}% от цены"
                )
        return "\n".join(lines) if lines else None

    if near is not None:
        side, edge, dist, block = near
        side_ru = "лонг-зона" if side == "long" else "шорт-зона"
        # Prepositional case — «в зонЕ», not «в зонА». Upper-casing the nominative
        # rendered «цена В ЛОНГ-ЗОНА».
        side_ru_in = "ЛОНГ-ЗОНЕ" if side == "long" else "ШОРТ-ЗОНЕ"
        # Each block carries its OWN remedy — see _limit_block. "" ⇒ the limit is live.
        tail = {
            "": " — вход по факту касания",
            "worked": " — но лимит НЕ ставим: уровень отработан, только по слому МТФ",
            "saw": " — но лимит НЕ ставим: пила на уровне, ждём выхода",
            "unknown": " — но лимит НЕ ставим",
        }[block]
        if dist == 0.0:
            lines.append(f"<b>цена В {side_ru_in}</b>{tail}")
        else:
            # Away from the zone the entry wording is premature; only the block matters.
            away = "" if not block else tail.replace(" — но лимит", " · лимит")
            lines.append(
                f"ближайшая {side_ru}: <code>{fmt_price(edge)}</code> — {dist:.1f}% от цены{away}"
            )
    # WHY no trade — the structured reject-reason with numbers, so the reader sees «RR 2.3 < 3.0»
    # instead of an unexplained silence (the dominant sync-with-channel outcome).
    _why = _abstain_reason_line(analysis.prizrak.abstain)
    if _why is not None:
        lines.append(_why)
    return "\n".join(lines) if lines else None


def _spot_context_text(analysis: AnalystReport) -> str | None:
    """Spot block for the deep panel — data only, no verdicts.

    Method basis: «крупняк на споте покупает на наших зонах интереса» (Prizrak,
    BTC-squeeze разбор) — spot participation is his context read; a perp move the
    spot market does not join is derivatives-only. The weekly ladder mirrors his
    macro levels off the full-history spot chart (POL/MATIC разбор). Reads the typed
    ``view.spot`` sub-model + the precomputed ``analysis.spot_ladder`` — no row-dict.
    """
    spot = analysis.view.spot
    lines: list[str] = []

    spread = spot.spread_bps if spot is not None else None
    if isinstance(spread, (int, float)):
        rel = "перп дороже спота" if spread > 0 else "перп дешевле спота" if spread < 0 else "паритет"
        lines.append(f"базис спот-перп: <code>{spread:+.1f} bps</code> ({rel})")

    spot_qv = spot.quote_volume_24h if spot is not None else None
    fut_qv = analysis.view.quote_volume_24h
    fut_qv_m = float(fut_qv) / 1e6 if isinstance(fut_qv, (int, float)) else None
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

    # Spot taker flow — which side is actively hitting the spot book right now (net
    # buy−sell notional over a recent aggTrades window). This is the closest public read
    # of «крупняк на споте покупает на зонах»; labelled as taker flow, not literally
    # «крупные деньги», since it is not size-filtered. Absent → the line is simply omitted
    # (fail-loud: no fabricated balance).
    taker_delta = spot.taker_delta_usd if spot is not None else None
    if isinstance(taker_delta, (int, float)):
        d = float(taker_delta)
        lean = "покупатели агрессивнее" if d > 0 else "продавцы агрессивнее" if d < 0 else "баланс"
        amt = f"{d / 1e6:+.1f}M$" if abs(d) >= 1e6 else f"{d / 1e3:+.0f}K$"
        _ratio = spot.taker_buy_ratio if spot is not None else None
        r_s = f" · buy {float(_ratio) * 100:.0f}%" if isinstance(_ratio, (int, float)) else ""
        lines.append(
            f"спот-поток тейкеров (агрессивные сделки): <code>{amt}</code> ({lean}{r_s})"
        )

    _lad = analysis.spot_ladder
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


format_deep_analysis_telegram = format_analyst_telegram  # backward compat after deep→analyst rename

__all__ = ["format_analyst_telegram", "format_deep_analysis_telegram"]
