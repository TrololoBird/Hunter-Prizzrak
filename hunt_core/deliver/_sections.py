"""Telegram analysis sections — MTF, volume profile, walls, cross-exchange."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price



from hunt_core.deliver._math import pct_str as _pct_str
from hunt_core.deliver._math import risk_pct_str as _risk_pct_str
from hunt_core.deliver._math import worst_entry_from_setup as _worst_entry_from_setup_raw


def _worst_entry_edge(entry_lo: float, entry_hi: float, *, direction: str, price: float) -> float:
    return _worst_entry_from_setup_raw({"entry_zone": [entry_lo, entry_hi]}, direction=direction, price=price)


_fmt_price = fmt_price

# Beyond this age a carried DOM/microstructure snapshot is context-only, never a
# touch-entry basis (#6). CALIBRATED, not guessed: over a 45-min live run the
# rendered dom_age_s distribution (42 genuine cross-venue samples) was min 2.2s /
# p50 5.2 / p90 9.2 / p95 12.7 / p99 54.2 — 93% under 10s (the render-pipeline
# floor is ~2s), then a thin genuinely-stale carry tail (30-90s). p95≈12.7s
# separates normal carry-reuse from that tail; rounded to 15s to absorb the ~1-2s
# lift from stamping fetched_at at the primary's fetch time (cross.py) rather than
# the merge moment. The old 45s guess sat past p99 and effectively never fired.
# Tune via HUNT_DOM_ACTIONABLE_MAX_AGE_S.
import os as _os

_DOM_ACTIONABLE_MAX_AGE_S = float(_os.getenv("HUNT_DOM_ACTIONABLE_MAX_AGE_S", "15") or 15.0)

# Canonical venue short-codes. One scheme everywhere (was three: a `[:3].upper()`
# path rendered binance→BIN / bybit→BYB / bitget→BIT — the last ambiguous with
# bybit — while the funding path used BNC/BYB/OKX/BGT). Unknown real names fall
# back to a 3-letter uppercase slice; only the missing/sentinel case defaults to
# the primary (binance).
_PRIMARY_VENUE = "binance"
_VENUE_CODES = {"binance": "BNC", "bybit": "BYB", "okx": "OKX", "bitget": "BGT"}

# A realized-liquidation cluster below this USD notional is too thin to be a
# magnet. `intensity` is normalized to the map's own max, so a single tiny
# force-order — and Binance forceOrder streams only the LARGEST event per 1s,
# heavily undersampled — renders as "100% плотн." on e.g. $128. Below the floor
# we drop the size/density tail entirely rather than dignify noise as a cluster.
# Env: HUNT_LIQ_MIN_CLUSTER_USD.
_LIQ_MIN_CLUSTER_NOTIONAL_USD = float(
    _os.getenv("HUNT_LIQ_MIN_CLUSTER_USD", "10000") or 10000.0
)


def _venue_code(name: Any) -> str:
    """Short display code for a venue name, consistent across every section."""
    s = str(name or "").strip().lower()
    if not s or s in {"?", "non", "none"}:
        return "BNC"
    return _VENUE_CODES.get(s, s[:3].upper())


def _fmt_usd_compact(value: float) -> str:
    """Human-readable USD notional: $920 / $7.3k / $133.4M / $1.2B (no '$133427.0k')."""
    v = abs(float(value or 0))
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}k"
    return f"${v:.0f}"


def plain_delivery_reasons(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    confirm_reasons: list[str] | None = None,
    max_items: int = 5,
) -> list[str]:
    """3–5 plain-language reasons for message v2 cards."""
    from hunt_core.deliver._context_lines import humanize_trigger
    from hunt_core.deliver._labels import phase_human, trigger_human

    reasons: list[str] = []
    lc = row.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    if phase:
        reasons.append(f"Фаза: {phase_human(phase)}")
    _struct_raw = row.get("structure")
    struct = _struct_raw if isinstance(_struct_raw, dict) else {}
    sb = str(struct.get("structure_bias") or (lc or {}).get("structure_bias") or "")
    if sb in {"long", "short"}:
        reasons.append(f"Structure bias: {sb}")
    raw = confirm_reasons if confirm_reasons is not None else list(setup.get("confirm_hard") or [])
    for item in raw[: max_items - len(reasons)]:
        label = humanize_trigger(str(item)) or trigger_human(str(item))
        if label and label not in reasons:
            reasons.append(label)
    fuel = float(
        setup.get("dump_fuel" if direction == "short" else "long_fuel")
        or setup.get("dump_score" if direction == "short" else "long_score")
        or 0
    )
    if fuel >= 70 and len(reasons) < max_items:
        reasons.append(f"Fuel {fuel:.0f}")
    return reasons[:max_items]


def format_mtf_section(
    mtf: Any, *, row: dict[str, Any] | None = None, include_scenarios: bool = True
) -> str:
    """
    Format MTF structure table + two scenarios for a PINNED /signal reply.

    ``mtf`` is a ``MTFConfluence`` dataclass from ``hunt_core.confluence.mtf``.
    """
    from hunt_core.deliver.geometry import geometry_block_reason

    def _geometry_blocked(direction: str) -> bool:
        if not row:
            return False
        setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
        if not isinstance(setup, dict):
            return False
        return geometry_block_reason(setup, row=row, direction=direction) is not None

    _TREND_EMOJI = {"bull": "🟢", "bear": "🔴", "neutral": "🟡"}
    _TREND_RU = {"bull": "Bull", "bear": "Bear", "neutral": "Нейт"}
    _TF_NAME = {"1w": "1W ", "1d": "1D ", "4h": "4H ", "15m": "15M"}

    sym = html.escape(str(getattr(mtf, "symbol", "?")).replace("USDT", "-USDT"))
    lines: list[str] = [
        f"🔭 <b>АНАЛИЗ · {sym}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 <b>МТФ СТРУКТУРА</b>",
    ]
    for tf_key in ("1w", "1d", "4h", "15m"):
        sig = (mtf.tf_signals or {}).get(tf_key)
        if sig is None:
            continue
        emoji = _TREND_EMOJI.get(sig.trend, "🟡")
        tlabel = _TREND_RU.get(sig.trend, "Нейт")
        name = _TF_NAME.get(tf_key, tf_key.upper())
        lines.append(
            f"<code>{name}</code> {emoji} {tlabel:4s} | RSI {sig.rsi14:5.1f} | {html.escape(sig.label)}"
        )

    dominant = getattr(mtf, "dominant", "neutral")
    dom_ru = {"long": "ЛОНГ", "short": "ШОРТ", "neutral": "БОКОВИК"}.get(dominant, "—")
    lines.append("")
    lines.append(f"🎯 <b>MTF bias:</b> {dom_ru} <i>(контекст)</i>")

    def _hunt_confirmed_direction(r: dict[str, Any]) -> str:
        dump_s = r.get("dump") if isinstance(r.get("dump"), dict) else {}
        long_s = r.get("long") if isinstance(r.get("long"), dict) else {}
        if (dump_s or {}).get("confirmed") or (dump_s or {}).get("intrabar_confirmed"):
            return "short"
        if (long_s or {}).get("confirmed") or (long_s or {}).get("intrabar_confirmed"):
            return "long"
        return ""

    hunt_conf = _hunt_confirmed_direction(row or {})
    if hunt_conf == "short":
        lines.append("✅ <b>Hunt confirm:</b> ШОРТ · closed-bar")
    elif hunt_conf == "long":
        lines.append("✅ <b>Hunt confirm:</b> ЛОНГ · closed-bar")

    lc_phase = str(((row or {}).get("lifecycle") or {}).get("phase") or "")
    if not include_scenarios or lc_phase in {"no_setup", "accumulation_watch", "exhaustion_watch"}:
        lines.append("")
        lines.append(
            "<i>⚠️ Сценарии входа скрыты — lifecycle без confirm; MTF bias справочно.</i>"
        )
        return "\n".join(lines)

    scenarios = [mtf.long_scenario, mtf.short_scenario]
    if not hunt_conf and dominant in {"long", "short"}:
        scenarios = [
            s for s in scenarios if getattr(s, "direction", "") == dominant
        ] or scenarios

    for sc in scenarios:
        dir_str = getattr(sc, "direction", "long")
        geo_block = _geometry_blocked(dir_str)
        is_confirmed = hunt_conf == dir_str
        is_main = is_confirmed or (dir_str == dominant and dominant != "neutral")
        if is_confirmed:
            star = " ★ HUNT CONFIRM"
        elif is_main and geo_block:
            star = " · ⚠️ watch-only (уровни)"
        elif is_main:
            star = " ★ ОСНОВНОЙ"
        else:
            star = ""
        dir_emoji = "📈" if dir_str == "long" else "📉"
        dir_ru = "ЛОНГ" if dir_str == "long" else "ШОРТ"
        score = float(getattr(sc, "score", 0))
        htf_aligned = int(getattr(sc, "htf_count", 0))
        htf_total = int(getattr(sc, "htf_total", 0))
        evidence: list[str] = list(getattr(sc, "evidence", []))

        entry_lo = float(getattr(sc, "entry_lo", 0))
        entry_hi = float(getattr(sc, "entry_hi", 0))
        tp1 = float(getattr(sc, "tp1", 0))
        tp2 = float(getattr(sc, "tp2", 0))
        stop = float(getattr(sc, "stop", 0))

        price_ref = float((row or {}).get("price") or 0)
        edge = _worst_entry_edge(entry_lo, entry_hi, direction=dir_str, price=price_ref)
        if edge <= 0:
            edge = entry_hi if dir_str == "long" else entry_lo
        tp1_pct = _pct_str(edge, tp1, dir_str) if tp1 > 0 else ""
        tp2_pct = _pct_str(edge, tp2, dir_str) if tp2 > 0 else ""
        sl_pct = _risk_pct_str(edge, stop, dir_str) if stop > 0 else ""

        lines.append("")
        lines.append(
            f"{dir_emoji} <b>СЦЕНАРИЙ {dir_ru}</b>  [Score: {score:.2f}]{html.escape(star)}"
        )
        if htf_total:
            ev_str = ", ".join(evidence[1:4]) if len(evidence) > 1 else ""
            lines.append(
                f"HTF {htf_aligned}/{htf_total}"
                + (f" · {html.escape(ev_str)}" if ev_str else "")
            )
        lines.append(f"Зона входа:  <code>{_fmt_price(entry_lo)} – {_fmt_price(entry_hi)}</code>")
        lines.append(
            f"TP1:         <code>{_fmt_price(tp1)}</code>"
            + (f"  ({tp1_pct})" if tp1_pct else "")
        )
        lines.append(
            f"TP2:         <code>{_fmt_price(tp2)}</code>"
            + (f"  ({tp2_pct})" if tp2_pct else "")
        )
        lines.append(
            f"Стоп:        <code>{_fmt_price(stop)}</code>"
            + (f"  ({sl_pct})" if sl_pct else "")
        )

    lines.append("")
    lines.append("<i>⚠️ Watch-only — вход только по confirmed-сигналу системы.</i>")
    return "\n".join(lines)


def format_volume_profile_section(row: dict[str, Any]) -> str:
    """POC/VAH/VAL from maps VP, cross merge, or regime fallback."""
    maps = row.get("maps") or {}
    vp_map = maps.get("volume_profile") if isinstance(maps, dict) else None
    if isinstance(vp_map, dict):
        poc = vp_map.get("poc")
        profiles = vp_map.get("profiles") or []
        prof_1h = next((p for p in profiles if isinstance(p, dict) and p.get("period") == "1h"), None)
        vah = prof_1h.get("vah") if prof_1h else None
        val = prof_1h.get("val") if prof_1h else None
        src = "maps"
    else:
        cx = row.get("cross_microstructure") or {}
        vp1h = cx.get("volume_profile_1h") or {}
        regime = row.get("regime") or {}
        poc = vp1h.get("poc") or regime.get("poc_1h")
        vah = vp1h.get("vah") or regime.get("vah_1h")
        val = vp1h.get("val") or regime.get("val_1h")
        src = "cross" if vp1h.get("poc") is not None else "BNC"
    if poc is None:
        return ""
    lines = [
        f"📊 <b>Volume profile 1h</b> ({src}): POC <code>{_fmt_price(float(poc))}</code>",
    ]
    if vah is not None:
        lines[-1] += f" · VAH <code>{_fmt_price(float(vah))}</code>"
    if val is not None:
        lines[-1] += f" · VAL <code>{_fmt_price(float(val))}</code>"
    poc_15m: float | None = None
    vah_15m: float | None = None
    if isinstance(vp_map, dict):
        prof_15 = next((p for p in (vp_map.get("profiles") or []) if isinstance(p, dict) and p.get("period") == "15m"), None)
        if prof_15 and prof_15.get("poc") is not None:
            poc_15m = float(prof_15["poc"])
    if poc_15m is None:
        cx = row.get("cross_microstructure") or {}
        vp15 = cx.get("volume_profile_15m") or {}
        if vp15.get("poc") is not None:
            poc_15m = float(vp15["poc"])
            vah_15m = float(vp15["vah"]) if vp15.get("vah") else None
    if poc_15m is not None:
        line_15 = f"15m POC <code>{_fmt_price(poc_15m)}</code>"
        if vah_15m is not None:
            line_15 += f" · VAH <code>{_fmt_price(vah_15m)}</code>"
        lines.append(line_15)
    return "\n".join(lines)


def format_forecast_section(
    row: dict[str, Any],
    *,
    primary_direction: str | None = None,
    archetype: str | None = None,
) -> str:
    """Generalized forecast section for predump / coil / ignition."""
    fusion = row.get("manipulation_fusion") if isinstance(row.get("manipulation_fusion"), dict) else {}
    arch = archetype or str((fusion or {}).get("archetype") or "")
    forecasts = row.get("forecasts") if isinstance(row.get("forecasts"), dict) else {}
    fc = row.get("maps_forecast")
    if arch == "predump_short":
        fc = (forecasts or {}).get("predump_short") or fc
        title = "Pre-dump forecast"
        hint = "цели markdown ↓"
    elif arch == "ignition_long":
        fc = (forecasts or {}).get("ignition_long") or fc
        title = "Ignition forecast"
        hint = "squeeze magnet ↑"
    else:
        return format_accumulation_forecast_section(
            row, primary_direction=primary_direction
        )
    if not isinstance(fc, dict) or not fc.get("target_primary"):
        return ""
    if primary_direction == "short" and arch != "predump_short":
        return ""
    conf = float(fc.get("confidence") or 0)
    direction = str(fc.get("direction") or "long").upper()
    target_lo = fc.get("target_lo")
    target_hi = fc.get("target_hi")
    lines = [f"🎯 <b>{title}</b> <i>({hint})</i>"]
    if target_lo is not None and target_hi is not None:
        if float(target_lo) == float(target_hi):
            lines.append(
                f"{direction} → target <code>{_fmt_price(float(target_lo))}</code> "
                f"({conf:.0%} conf)"
            )
        else:
            lines.append(
                f"{direction} → band <code>{_fmt_price(float(target_lo))}</code>–"
                f"<code>{_fmt_price(float(target_hi))}</code> ({conf:.0%} conf)"
            )
    factors = fc.get("factors") or []
    if factors:
        lines.append("Factors: " + ", ".join(html.escape(str(f)) for f in factors[:4]))
    window = fc.get("window_minutes")
    if window:
        lines.append(f"Window: ~{int(window)}m")
    return "\n".join(lines)


def format_accumulation_forecast_section(
    row: dict[str, Any],
    *,
    primary_direction: str | None = None,
) -> str:
    """Pre-pump forecast — long-only accumulation breakout; hide during dump lifecycle."""
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    lc_phase = str((lc or {}).get("phase") or "")
    if lc_phase in {
        "dump_initiating",
        "dump_active",
        "exhaustion_at_high",
        "post_dump_bounce",
    }:
        return ""
    if primary_direction == "short":
        return ""
    fc = row.get("maps_forecast")
    if not isinstance(fc, dict) or not fc.get("target_primary"):
        return ""
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    lines = ["🎯 <b>Pre-pump forecast</b> <i>(цели ликвидаций ↑, не вход)</i>"]
    direction = str(fc.get("direction") or "long").upper()
    conf = float(fc.get("confidence") or 0)
    move = fc.get("expected_move_pct")
    target_lo = fc.get("target_lo")
    target_hi = fc.get("target_hi")
    if target_lo is not None and target_hi is not None:
        if float(target_lo) == float(target_hi):
            lines.append(
                f"{direction} → target <code>{_fmt_price(float(target_lo))}</code> "
                f"({conf:.0%} conf)"
            )
        else:
            lines.append(
                f"{direction} → band <code>{_fmt_price(float(target_lo))}</code>–"
                f"<code>{_fmt_price(float(target_hi))}</code> ({conf:.0%} conf)"
            )
    if move is not None:
        mv = float(move)
        if abs(mv) >= 0.05:
            sign = "+" if mv >= 0 else ""
            lines.append(f"Expected move: <code>{sign}{mv:.2f}%</code>")
    factors = fc.get("factors") or []
    if factors:
        lines.append("Fuel: " + ", ".join(html.escape(str(f)) for f in factors[:4]))
    acc = (market or {}).get("map_vp_accumulation")
    if acc is not None:
        lines.append(f"VP accumulation: <code>{float(acc):.2f}</code>")
    if (market or {}).get("map_accum_bid_absorption"):
        lines.append("Bid absorption + sticky support")
    void_above = (market or {}).get("map_void_above")
    if void_above:
        lines.append(f"Void path ↑ <code>{_fmt_price(float(void_above))}</code>")
    return "\n".join(lines)


def format_liquidation_map_section(row: dict[str, Any]) -> str:
    """Liquidation squeeze zones — realized + forward magnets (R9: synthetic honesty)."""
    market = row.get("market") or {}
    maps = row.get("maps") or {}
    liq = maps.get("liquidation") if isinstance(maps, dict) else None
    if not market and not liq:
        return ""
    nearest_long = market.get("liq_heatmap_nearest_long")
    nearest_short = market.get("liq_heatmap_nearest_short")
    cascade = market.get("liq_cascade_risk")
    synthetic_only = bool(market.get("liq_synthetic_only"))
    if nearest_long is None and nearest_short is None and not cascade:
        return ""
    header = "💥 <b>Ликвидации</b>"
    # Show EVERY live venue with its event count + completeness, so a live-but-quiet
    # feeder (0ev) is distinguishable from a dead one (absent). Falls back to the
    # completeness-only map when per-venue counts aren't available.
    ve = market.get("liq_venue_events")
    vc = market.get("liq_venue_completeness")
    venue_str = ""
    if isinstance(ve, dict) and ve:
        venue_str = ", ".join(
            f"{v}={vc.get(v, '?') if isinstance(vc, dict) else '?'}·{int(n)}ev"
            for v, n in ve.items()
        )
    elif isinstance(vc, dict) and vc:
        venue_str = ", ".join(f"{v}={c}" for v, c in vc.items())
    if synthetic_only:
        # Honest: the forward estimate is Binance-OI-based (cross-venue OI is 1в-2,
        # not yet done); only the realized tape is multi-exchange. If live feeders
        # exist but had 0 events this window, name them so «quiet» ≠ «feeder dead».
        header += " · <i>оценка по leverage-tier (Binance OI), без реальных ликвидаций"
        header += f"; вены: {html.escape(venue_str)}</i>" if venue_str else "</i>"
    else:
        header += (
            f" · <i>реальные ликвидации ({html.escape(venue_str)})</i>"
            if venue_str else " · <i>реальные ликвидации</i>"
        )
    lines = [header]

    clusters = market.get("liq_heatmap_clusters")
    clusters = clusters if isinstance(clusters, list) else []
    cur_price = 0.0
    try:
        cur_price = float(row.get("price") or market.get("mark_price") or 0.0)
    except (TypeError, ValueError):
        cur_price = 0.0

    def _cluster_size_tail(price: float, *, side: str) -> str:
        # Distance % alone ("0.2%") says nothing about how much sits there. Attach
        # the nearest cluster's notional + intensity so the magnet's pull is legible.
        # SIDE-AWARE: long-liquidation mass sits BELOW price, short-squeeze mass
        # ABOVE. The old side-agnostic nearest-by-abs-distance attached the SAME
        # central cluster to BOTH lines, printing an identical (and misleading)
        # "$X · Y% плотн." on the long and short rows. Restrict each row to
        # clusters on its own side of the current price.
        best = None
        best_d = None
        for c in clusters:
            if not isinstance(c, dict) or c.get("price") is None:
                continue
            c_price = float(c["price"])
            if cur_price > 0:
                if side == "long" and c_price >= cur_price:
                    continue
                if side == "short" and c_price <= cur_price:
                    continue
            d = abs(c_price - price)
            if best_d is None or d < best_d:
                best, best_d = c, d
        if best is None or price <= 0 or best_d is None or best_d / price > 0.005:
            return ""
        notional = float(best.get("total_notional") or 0.0)
        intensity = float(best.get("intensity") or 0.0)
        # Below the floor the cluster is an undersampled single force-order, not a
        # density — suppress the whole tail so "100% плотн." never rides on $128.
        if notional < _LIQ_MIN_CLUSTER_NOTIONAL_USD:
            return ""
        parts: list[str] = []
        if notional > 0:
            parts.append(_fmt_usd_compact(notional))
        if intensity > 0:
            parts.append(f"{intensity:.0%} плотн.")
        return f" · {' · '.join(parts)}" if parts else ""

    # State absence explicitly (a side with no magnet, or a magnet whose cluster is
    # below the significance floor) instead of silently dropping the line — so the
    # reader can tell "no meaningful cluster there" from a render miss. Only when at
    # least one side has a magnet (avoid a section of pure negatives).
    any_side = nearest_long is not None or nearest_short is not None
    if nearest_long is not None:
        pull = market.get("liq_magnet_pull_long_pct")
        dist = f" ({pull:.1f}%)" if pull is not None else ""
        tail = _cluster_size_tail(float(nearest_long), side="long") or " · <i>без значимого кластера</i>"
        lines.append(f"Лонг-ликвидации ↓ <code>{_fmt_price(float(nearest_long))}</code>{dist}{tail}")
    elif any_side:
        lines.append("Лонг-ликвидации ↓ <i>нет значимого кластера снизу</i>")
    if nearest_short is not None:
        pull = market.get("liq_magnet_pull_short_pct")
        dist = f" ({pull:.1f}%)" if pull is not None else ""
        tail = _cluster_size_tail(float(nearest_short), side="short") or " · <i>без значимого кластера</i>"
        lines.append(f"Шорт-сквиз ↑ <code>{_fmt_price(float(nearest_short))}</code>{dist}{tail}")
    elif any_side:
        lines.append("Шорт-сквиз ↑ <i>нет значимого кластера сверху</i>")
    if cascade:
        label = "лонг-флаш" if cascade == "long_flush" else "шорт-сквиз"
        lines.append(f"Риск каскада: <b>{label}</b>")
    return "\n".join(lines)


def format_liquidity_heatmap_section(row: dict[str, Any]) -> str:
    """Liquidity heatmap — sticky walls, spoof flags, depth bands (time-weighted book)."""
    maps = row.get("maps") or {}
    ob = maps.get("orderbook") if isinstance(maps, dict) else None
    market = row.get("market") or {}
    if not isinstance(ob, dict) and not market.get("map_sticky_bid"):
        return ""
    sticky = (ob or {}).get("sticky_walls") or []
    spoof = (ob or {}).get("spoof_flags") or []
    matrix = (ob or {}).get("depth_heatmap_matrix") or []
    voids = (ob or {}).get("liquidity_voids") or []
    if not sticky and not spoof and not matrix and not voids:
        return ""
    lines = [
        "🌡 <b>Тепловая карта ликвидности</b> "
        "<i>(история стакана · sticky/spoof · не ликвидации)</i>"
    ]
    # Top sticky walls PER SIDE by notional within ±4% — not just the nearest. Deep
    # walls (1.5-3% off price) are detected (_detect_sticky_walls tracks distance_pct)
    # but the old nearest-only render hid them, so a large wall a couple % away never
    # reached the text. Sort by notional so the biggest defended level shows first. (WO #6)
    _WALL_MAX_DIST_PCT = 4.0
    _WALL_TOP_N = 3
    for side in ("bid", "ask"):
        side_walls = [
            s for s in sticky
            if isinstance(s, dict) and s.get("price") is not None
            and str(s.get("side") or "?") == side
            and abs(float(s.get("distance_pct") or 0.0)) <= _WALL_MAX_DIST_PCT
        ]
        side_walls.sort(key=lambda w: float(w.get("notional_usd") or 0.0), reverse=True)
        for s in side_walls[:_WALL_TOP_N]:
            sticky_px = float(s["price"])
            bits = [f"Sticky {side} @ <code>{_fmt_price(sticky_px)}</code>"]
            notional = s.get("notional_usd")
            if isinstance(notional, (int, float)) and notional > 0:
                bits.append(_fmt_usd_compact(float(notional)))
            dist = s.get("distance_pct")
            if isinstance(dist, (int, float)):
                arrow = "ниже" if side == "bid" else "выше"
                bits.append(f"{float(dist):.2f}% {arrow}")
            samples = s.get("samples")
            if samples:
                bits.append(f"{samples} snap")
            lines.append(bits[0] + " (" + " · ".join(bits[1:]) + ")" if len(bits) > 1 else bits[0])
    for sp in spoof[:2]:
        if not isinstance(sp, dict):
            continue
        side = str(sp.get("side") or "?")
        px = sp.get("price")
        if px is not None:
            lines.append(f"Spoof? {side} @ <code>{_fmt_price(float(px))}</code>")
    # Both blocks below used to read keys the producers never emit, so they rendered
    # NOTHING: depth bands keyed on `price` while _depth_heatmap_matrix writes
    # `price_center`, and voids keyed on `price_lo`/`price_hi`/`direction` while
    # _detect_voids writes `price_center`/`depth_usd`/`distance_pct`. Read the real keys.
    hot = sorted(
        [m for m in matrix if isinstance(m, dict) and m.get("price_center") is not None],
        key=lambda m: float(m.get("intensity") or 0),
        reverse=True,
    )[:3]
    if hot:
        bits = [
            f"{_fmt_price(float(m['price_center']))} ({float(m.get('intensity') or 0):.0%})"
            for m in hot
        ]
        lines.append("Depth bands: " + " · ".join(bits))
    try:
        cur_px = float(row.get("price") or 0)
    except (TypeError, ValueError):
        cur_px = 0.0
    for v in voids[:1]:
        if not isinstance(v, dict) or v.get("price_center") is None:
            continue
        center = float(v["price_center"])
        arrow = "↑" if cur_px > 0 and center > cur_px else "↓"
        dist = v.get("distance_pct")
        tail = f" ({float(dist):.2f}%)" if isinstance(dist, (int, float)) else ""
        lines.append(f"Разрежение {arrow} <code>{_fmt_price(center)}</code>{tail}")
    return "\n".join(lines) if len(lines) > 1 else ""


def format_intraday_maps_telegram(row: dict[str, Any]) -> str:
    """Three-map block for /signal: DOM · liquidity heatmap · liquidation map."""
    blocks: list[str] = []
    dom = format_book_walls_section(row)
    if dom:
        blocks.append(dom)
    heat = format_liquidity_heatmap_section(row)
    if heat:
        blocks.append(heat)
    liq = format_liquidation_map_section(row)
    if liq:
        blocks.append(liq)
    if not blocks:
        return ""
    return "\n\n".join(blocks)


def format_orderflow_section(row: dict[str, Any]) -> str:
    """Footprint / stacked imbalance from maps orderbook."""
    maps = row.get("maps") or {}
    ob = maps.get("orderbook") if isinstance(maps, dict) else None
    market = row.get("market") or {}
    stacked = market.get("map_stacked_imbalance") or (ob or {}).get("stacked_imbalance")
    if not stacked and not ob:
        return ""
    lines = ["📈 <b>Order flow (footprint)</b>"]
    if stacked:
        label = "buy stack" if stacked == "buy_stack" else "sell stack"
        lines.append(f"Stacked imbalance: <b>{label}</b>")
    bins = (ob or {}).get("footprint_bins") or []
    if bins:
        top = sorted(bins, key=lambda b: abs(float(b.get("delta") or 0)), reverse=True)[:2]
        for b in top:
            if not isinstance(b, dict):
                continue
            px = b.get("price")
            delta = b.get("delta")
            if px is None:
                continue
            sign = "+" if float(delta or 0) >= 0 else ""
            lines.append(f"Δ @ <code>{_fmt_price(float(px))}</code>: {sign}{float(delta or 0):,.0f}")
    sticky = (ob or {}).get("sticky_walls") or []
    if sticky:
        _sticky_shown: set[str] = set()
        for s in sticky[:3]:
            if not isinstance(s, dict):
                continue
            px = float(s.get("price") or 0)
            side = str(s.get("side", "?")).lower()
            samples = s.get("samples", 0)
            _key = f"{px:.6f}" if px < 1 else f"{px:.4f}"
            if _key in _sticky_shown:
                continue
            _sticky_shown.add(_key)
            lines.append(
                f"Sticky {side} @ <code>{_fmt_price(px)}</code>"
                f" ({samples} samples)"
            )
    return "\n".join(lines) if len(lines) > 1 else ""


def format_book_walls_section(row: dict[str, Any]) -> str:
    """Top cross-venue or single-exchange limit clusters."""
    cx = row.get("cross_microstructure") or {}
    walls = cx.get("book_walls") or row.get("book_walls") or {}
    if not isinstance(walls, dict):
        return ""
    bids = walls.get("bid_levels") or []
    asks = walls.get("ask_levels") or []
    per_ex = walls.get("per_exchange") or {}
    venues = walls.get("venues") or []
    if not bids and not asks and not per_ex:
        return ""

    def _fmt_level(side: str, lvl: dict[str, Any], emoji: str) -> str:
        px = lvl.get("price")
        notional = lvl.get("notional_usd")
        if px is None:
            return ""
        ex_raw = lvl.get("exchange") or walls.get("source") or walls.get("venue") or "BNC"
        tag = _venue_code(ex_raw)
        vc = lvl.get("venue_count") or 1
        agg = " агр." if int(vc) > 1 else ""
        return f"{emoji} {side}: {tag}{agg} {_fmt_price(float(px))} (${float(notional or 0)/1e3:.1f}k)"

    _CROSS_VENUE_CLUSTER_TOL_PCT = 0.05  # levels within this % of each other = one wall, not three

    def _top_per_venue(side_key: str) -> list[dict[str, Any]]:
        """Best level per venue, then merged across venues within a tight price band —
        otherwise each exchange's own top-of-book (always within a few bps of price)
        renders as 2-3 near-duplicate "walls" a few ticks apart, reading as noise
        rather than one real level with real aggregate size behind it.
        """
        raw: list[dict[str, Any]] = []
        for ex, snap in per_ex.items():
            if not isinstance(snap, dict):
                continue
            levels = snap.get(side_key) or []
            if not levels:
                continue
            best = max(
                (lvl for lvl in levels if isinstance(lvl, dict)),
                key=lambda r: float(r.get("notional_usd") or 0),
                default=None,
            )
            if best:
                row_lvl = dict(best)
                row_lvl["exchange"] = ex
                raw.append(row_lvl)
        if not raw:
            return []

        price = float(row.get("price") or 0)
        tol = price * _CROSS_VENUE_CLUSTER_TOL_PCT / 100.0 if price > 0 else 0.0
        raw.sort(key=lambda r: float(r.get("price") or 0))
        clusters: list[dict[str, Any]] = []
        for lvl in raw:
            px = float(lvl.get("price") or 0)
            notional = float(lvl.get("notional_usd") or 0)
            if clusters and tol > 0 and abs(px - clusters[-1]["_ref_px"]) <= tol:
                c = clusters[-1]
                c["notional_usd"] = c.get("notional_usd", 0.0) + notional
                c["venue_count"] = c.get("venue_count", 1) + 1
                # Weighted-average anchor so the cluster price tracks its biggest venue.
                if notional > c.get("_max_notional", 0.0):
                    c["price"] = px
                    c["_max_notional"] = notional
                    c["exchange"] = lvl.get("exchange")
            else:
                clusters.append(
                    {
                        "price": px,
                        "notional_usd": notional,
                        "venue_count": 1,
                        "exchange": lvl.get("exchange"),
                        "_ref_px": px,
                        "_max_notional": notional,
                    }
                )
        clusters.sort(key=lambda c: c["notional_usd"], reverse=True)
        return clusters[:3]

    freshness = row.get("freshness") if isinstance(row.get("freshness"), dict) else {}
    dom_age = (freshness or {}).get("dom_age_s")
    # Actionability gate for carried microstructure (#6): a hot-carry tick reuses
    # the prior snapshot's book_walls, so DOM/hidden orders can be minutes old while
    # printed under "сейчас". Past _DOM_ACTIONABLE_MAX_AGE_S the data stays VISIBLE
    # (context) but is explicitly marked not-for-touch-entry — showing stale as
    # current is the same defect class as a wrong side, just in time.
    dom_age_f = float(dom_age) if dom_age is not None else None
    dom_stale = dom_age_f is not None and dom_age_f > _DOM_ACTIONABLE_MAX_AGE_S
    # Label threshold is the SAME as the staleness gate: past it the header must
    # not say "сейчас" while the body warns "устарела" — that 15–30s window printed
    # both at once (a milder "stale shown as current"). "сейчас" now means ≤ the
    # actionable age; beyond it the header shows the age, consistent with the flag.
    if dom_age_f is not None and dom_age_f > _DOM_ACTIONABLE_MAX_AGE_S:
        dom_label = f"DOM · {dom_age_f:.0f}с назад"
    else:
        dom_label = "DOM · сейчас"
    lines = [f"📋 <b>Карта ордеров ({dom_label})</b>"]
    # Surviving venues, always shown by code (not just a count) so the reader can
    # see WHAT feeds the DOM. If binance dropped out of the aggregate (its fetch
    # failed — after the primary-alignment fix it is never excluded for skew), say
    # so loudly: a DOM built from secondaries only is a different object.
    venue_names = [str(v) for v in venues] if isinstance(venues, list) else []
    if venue_names:
        codes = "+".join(_venue_code(v) for v in venue_names)
        lines[0] += f" <i>({codes})</i>"
        if _PRIMARY_VENUE not in {v.lower() for v in venue_names}:
            lines.append("<i>⚠️ DOM без Binance — только вторичные площадки</i>")
    if dom_stale and dom_age_f is not None:
        lines.append(
            f"<i>⚠️ микроструктура устарела ({dom_age_f:.0f}с) — "
            f"справочно, НЕ для входа по касанию</i>"
        )
    stale_excluded = walls.get("stale_venues_excluded")
    if isinstance(stale_excluded, list) and stale_excluded:
        lines.append(
            f"<i>⏱ рассинхрон, исключены: "
            f"{', '.join(_venue_code(v) for v in stale_excluded)}</i>"
        )

    # Prefer real full-depth cross-venue buckets (merge_full_depth_bins) over top-of-book
    # per-venue levels — each exchange's own best bid/ask sits within a few bps of price
    # by definition, so displaying them individually reads as "3 separate walls a tick
    # apart" when it's really one shallow front-of-book with real depth sitting a bit
    # further out. depth_bins sums actual book size (not just best tick) into fixed
    # price buckets (~0.5% wide by default) across every venue's full snapshot.
    depth_bins = walls.get("depth_bins") if isinstance(walls.get("depth_bins"), dict) else None

    def _fmt_bin(side: str, b: dict[str, Any], emoji: str) -> str:
        center = b.get("price_center")
        depth = b.get("depth_usd")
        if center is None:
            return ""
        intensity = b.get("intensity")
        pct = f" · {float(intensity):.0%} от макс." if intensity is not None else ""
        # A bin aggregates a price band ~0.5% wide; printing its centre alone reads
        # as a single resting order at that exact price.
        p_lo, p_hi = b.get("price_lo"), b.get("price_hi")
        if p_lo is not None and p_hi is not None:
            band = f"{_fmt_price(float(p_lo))}–{_fmt_price(float(p_hi))}"
        else:
            band = f"≈{_fmt_price(float(center))}"
        return f"{emoji} {side}: {band} ({_fmt_usd_compact(float(depth or 0))}{pct})"

    if depth_bins and (depth_bins.get("bid_bins") or depth_bins.get("ask_bins")):
        for b in (depth_bins.get("bid_bins") or [])[:2]:
            line = _fmt_bin("Покупка", b, "🟢")
            if line:
                lines.append(line)
        for b in (depth_bins.get("ask_bins") or [])[:2]:
            line = _fmt_bin("Продажа", b, "🔴")
            if line:
                lines.append(line)
    elif isinstance(per_ex, dict) and len(per_ex) > 1:
        bid_levels = _top_per_venue("bid_levels")
        ask_levels = _top_per_venue("ask_levels")
        for lvl in bid_levels:
            line = _fmt_level("Покупка", lvl, "🟢")
            if line:
                lines.append(line)
        for lvl in ask_levels:
            line = _fmt_level("Продажа", lvl, "🔴")
            if line:
                lines.append(line)
    else:
        def _wall_line(side: str, levels: list[Any], emoji: str) -> str:
            parts: list[str] = []
            for lvl in levels[:3]:
                if isinstance(lvl, dict):
                    px = lvl.get("price")
                    notional = lvl.get("notional_usd")
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    px, qty = float(lvl[0]), float(lvl[1])
                    notional = round(px * qty, 0)
                else:
                    continue
                if px is None:
                    continue
                src = lvl.get("exchange") if isinstance(lvl, dict) else None
                ex_raw = src or walls.get("source") or walls.get("venue") or "BNC"
                tag = str(ex_raw)[:3].upper()
                if tag in {"?", "NON"}:
                    tag = "BNC"
                parts.append(f"{tag} {_fmt_price(float(px))} ({_fmt_usd_compact(float(notional or 0))})")
            if not parts:
                return ""
            return f"{emoji} {side}: " + " · ".join(parts)

        bids_sorted = sorted(
            [lvl for lvl in bids if isinstance(lvl, dict) and lvl.get("price") is not None],
            key=lambda r: float(r["price"]),
            reverse=True,
        ) if bids else []
        asks_sorted = sorted(
            [lvl for lvl in asks if isinstance(lvl, dict) and lvl.get("price") is not None],
            key=lambda r: float(r["price"]),
        ) if asks else []
        bid_line = _wall_line("Покупка", bids_sorted, "🟢")
        ask_line = _wall_line("Продажа", asks_sorted, "🔴")
        if bid_line:
            lines.append(bid_line)
        if ask_line:
            lines.append(ask_line)
    imb = walls.get("depth_imbalance")
    if imb is not None:
        imb_f = float(imb)
        lean = "перевес покупателей" if imb_f > 0.15 else ("перевес продавцов" if imb_f < -0.15 else "баланс")
        # Window label matters: this is TOP-OF-BOOK (первые ~20 уровней у цены),
        # while the «Покупка/Продажа» bands above aggregate WIDE price bins — the
        # two can legitimately disagree (huge ask wall 0.3% away vs bid-heavy top).
        # The explicit «≠ полосы выше» stops a reader from deriving this figure by
        # hand from the two band notionals above and seeing a contradiction — they
        # are different windows, not the same number computed twice.
        lines.append(
            f"Дисбаланс верха стакана (первые уровни, ≠ полосы выше): "
            f"<code>{imb_f:+.3f}</code> ({lean})"
        )
    maps = row.get("maps") or {}
    ob = maps.get("orderbook") if isinstance(maps, dict) else None
    if isinstance(ob, dict):
        sticky = ob.get("sticky_walls") or []
        if sticky and isinstance(sticky[0], dict):
            s = sticky[0]
            lines.append(
                f"Sticky {s.get('side', '?')} @ <code>{_fmt_price(float(s.get('price') or 0))}</code>"
                f" ({s.get('samples')} samples)"
            )
        ice = ob.get("iceberg_levels") or []
        if ice and isinstance(ice[0], dict):
            i0 = ice[0]
            side_ru = "покупка" if i0.get("side") == "bid" else "продажа"
            ratio = i0.get("replenishment_ratio")
            ratio_suffix = "+" if i0.get("replenishment_ratio_capped") else ""
            ratio_s = f" · пополнение ×{float(ratio):.1f}{ratio_suffix}" if ratio else ""
            lines.append(
                f"🧊 Скрытый ордер ({side_ru}) @ <code>{_fmt_price(float(i0.get('price') or 0))}</code>{ratio_s}"
            )
    return "\n".join(lines)


def format_cross_microstructure_section(row: dict[str, Any]) -> str:
    """Cross-exchange taker flow + liq estimate / forward confidence."""
    cx = row.get("cross_microstructure") or {}
    market = row.get("market") or {}
    if not cx and not market.get("liq_forward_confidence"):
        return ""
    lines: list[str] = []
    taker = cx.get("taker_flow") or {}
    per = taker.get("per_exchange") or {}
    if per:
        bits = [f"{_venue_code(ex)} {float(v):.2f}" for ex, v in per.items()]
        consensus = taker.get("consensus")
        tail = f" → consensus <code>{consensus:.2f}</code>" if consensus is not None else ""
        lines.append("Order flow (taker): " + " · ".join(bits) + tail)
    note = cx.get("liquidation_note")
    if note:
        lines.append(f"<i>{html.escape(str(note))}</i>")
    liq_est = cx.get("liquidation_estimate") or {}
    if isinstance(liq_est, dict):
        skip = liq_est.get("skip_reason")
        if skip:
            lines.append(f"<i>Forward liq: {html.escape(str(skip))}</i>")
        elif liq_est.get("forward_confidence") is not None:
            fc = float(liq_est["forward_confidence"])
            lines.append(f"Forward liq confidence <code>{fc:.0%}</code>")
    fc_mkt = market.get("liq_forward_confidence")
    if fc_mkt is not None and not any("Forward liq confidence" in ln for ln in lines):
        lines.append(f"Forward liq confidence <code>{float(fc_mkt):.0%}</code>")
    return "\n".join(lines)


def format_pinned_deep_analysis(row: dict[str, Any]) -> str:
    """Deep /signal block via hunt_core.prizrak (Module 1 — not hunter lake_panel)."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return ""
    try:
        from hunt_core.prizrak.build import build_deep_report
        from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram

        analysis = build_deep_report(row, include_watch_appendix=False)
        return format_deep_analysis_telegram(analysis)
    except Exception:
        return ""


def format_cross_exchange_section(cx: dict[str, Any]) -> str:
    """Format cross-exchange intel block for /signal reply."""
    if not cx:
        return ""
    funding: dict[str, Any] = cx.get("funding") or {}
    cx.get("oi_usd") or {}
    mark_price: dict[str, Any] = cx.get("mark_price") or {}
    float(cx.get("funding_spread") or 0)
    consensus = str(cx.get("funding_consensus") or "neutral")
    oi_total = float(cx.get("oi_total") or 0)
    price_div = float(cx.get("price_divergence_pct") or 0)

    funding_parts: list[str] = []
    for ex, rate in funding.items():
        if rate is None:
            continue
        sign = "+" if rate >= 0 else ""
        funding_parts.append(f"{_venue_code(ex)} {sign}{rate*100:.4f}%")

    price_parts: list[str] = []
    for ex, mp in mark_price.items():
        if not mp:
            continue
        price_parts.append(f"{_venue_code(ex)} {_fmt_price(mp)}")

    listed = cx.get("listed") or {}
    listed_parts = [
        f"{_venue_code(ex)}{'✓' if ok else '✗'}"
        for ex, ok in listed.items()
    ]
    lines: list[str] = ["🌐 <b>КРОСС-БИРЖА</b> <i>(universe: Binance)</i>"]
    if listed_parts:
        lines.append("Листинг: " + " ".join(listed_parts))
    if funding_parts:
        lines.append("Funding:  " + "  |  ".join(funding_parts))
        if consensus == "divergent":
            lines.append("          ⚠️ Дивергенция — биржи не согласованы")
        elif consensus == "bull":
            lines.append("          🟢 Фандинг бычий на всех биржах")
        elif consensus == "bear":
            lines.append("          🔴 Фандинг медвежий на всех биржах")
    if oi_total > 0:
        oi_b = oi_total / 1e9
        lines.append(f"OI Total: <code>${oi_b:.2f}B</code>")
    if price_parts:
        spread_str = f"  (spread {price_div:.3f}%)" if price_div > 0 else ""
        lines.append("Цены:     " + "  |  ".join(price_parts) + html.escape(spread_str))
    return "\n".join(lines) if len(lines) > 1 else ""



__all__ = [
    "format_book_walls_section",
    "format_cross_exchange_section",
    "format_cross_microstructure_section",
    "format_intraday_maps_telegram",
    "format_liquidity_heatmap_section",
    "format_liquidation_map_section",
    "format_mtf_section",
    "format_pinned_deep_analysis",
    "format_volume_profile_section",
]
