"""Telegram liquidity sections for the deep card — liquidity heatmap + liquidation magnets.

What remains here is exactly what the Prizrak-post card renders: the aggregated orderbook heatmap
(sticky walls / density bands / voids) and the realized-liquidation magnets. Everything else this
module used to hold — the DOM/book-walls block, the МТФ table, the volume-profile and forecast
sections, order flow, cross-venue microstructure — was deleted with the post-format rewrite: those
renderers had no production caller left, and keeping them alive on test coverage alone is the
"looks alive because CI fixes it" artifact this repo refuses to accumulate.
"""
from __future__ import annotations

import html
import os as _os
from typing import TYPE_CHECKING, Any

from hunt_core.deliver._labels import fmt_price as _fmt_price

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView

# A realized-liquidation cluster below this USD notional is too thin to be a magnet. `intensity` is
# normalized to the map's own max, so a single tiny force-order — and Binance forceOrder streams only
# the LARGEST event per 1s, heavily undersampled — renders as "100% плотн." on e.g. $128. Below the
# floor we drop the size/density tail entirely rather than dignify noise as a cluster.
# Env: HUNT_LIQ_MIN_CLUSTER_USD.
_LIQ_MIN_CLUSTER_NOTIONAL_USD = float(_os.getenv("HUNT_LIQ_MIN_CLUSTER_USD", "10000") or 10000.0)


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

    # `liq_heatmap_clusters` — это ТОП-3 ПО НОТИОНАЛУ (`maps/liquidation.py`: `clusters[:3]`
    # после сортировки по объёму), а сам магнит `nearest_*_liquidation` выбирается по БЛИЗОСТИ
    # из ПОЛНОГО списка. Размер и близость не связаны, поэтому промах — норма, а не край. Прежний
    # текст «без значимого кластера» утверждал ОТСУТСТВИЕ там, где система кластер измерила и
    # молча не показала: человек обесценивал реальный ликвидационный магнит. (2026-07-26)
    _NOT_IN_TOP3 = " · <i>вне топ-3 по объёму</i>"
    _SUBFLOOR = " · <i>без значимого кластера</i>"
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
            return ""  # на этой стороне в топ-3 ничего рядом — это НЕ «кластера нет»
        notional = float(best.get("total_notional") or 0.0)
        intensity = float(best.get("intensity") or 0.0)
        # Below the floor the cluster is an undersampled single force-order, not a
        # density — suppress the whole tail so "100% плотн." never rides on $128.
        if notional < _LIQ_MIN_CLUSTER_NOTIONAL_USD:
            # Кластер НАЙДЕН и измерен, но слишком тонок, чтобы звать его магнитом — это
            # содержательное «незначим», в отличие от «не попал в топ-3». Раньше оба случая
            # возвращали "" и печатались одним текстом.
            return _SUBFLOOR
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
        tail = _cluster_size_tail(float(nearest_long), side="long") or _NOT_IN_TOP3
        lines.append(f"Лонг-ликвидации ↓ <code>{_fmt_price(float(nearest_long))}</code>{dist}{tail}")
    elif any_side:
        lines.append("Лонг-ликвидации ↓ <i>нет значимого кластера снизу</i>")
    if nearest_short is not None:
        pull = market.get("liq_magnet_pull_short_pct")
        dist = f" ({pull:.1f}%)" if pull is not None else ""
        tail = _cluster_size_tail(float(nearest_short), side="short") or _NOT_IN_TOP3
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
    # The guard used to also accept `market["map_sticky_bid"]` — a phantom key nothing
    # produces, so that half always evaluated False. The section needs an orderbook map.
    if not isinstance(ob, dict):
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
    # _depth_heatmap_matrix emits one row per (TIME SAMPLE × price bucket) — up to 12
    # samples of the same bucket. Ranking the raw rows by intensity therefore returned
    # the SAME band several times over ("64054.7 (100%) · 64054.7 (100%) · 64054.7
    # (100%)" live): three readings of one price dressed up as three bands. Collapse by
    # price first, keeping each band's strongest sample, so the three slots hold three
    # distinct prices.
    by_price: dict[float, dict[str, Any]] = {}
    for m in matrix:
        if not isinstance(m, dict) or m.get("price_center") is None:
            continue
        center = round(float(m["price_center"]), 6)
        best = by_price.get(center)
        if best is None or float(m.get("intensity") or 0) > float(best.get("intensity") or 0):
            by_price[center] = m
    hot = sorted(
        by_price.values(), key=lambda m: float(m.get("intensity") or 0), reverse=True
    )[:3]
    if hot:
        bits = [
            f"{_fmt_price(float(m['price_center']))} ({float(m.get('intensity') or 0):.0%})"
            for m in hot
        ]
        lines.append("Плотность стакана (устойчивые полосы): " + " · ".join(bits))
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


def _native_map_render_inputs(native: NativeAnalystView) -> dict[str, Any]:
    """Narrow map-render input assembled purely from typed handles (NOT a legacy row).

    The two retained map sub-sections (``format_liquidity_heatmap_section`` /
    ``format_liquidation_map_section``) are dict-over-map-scalars renderers with their own unit tests;
    each reads only ``price``/``market``/``maps``. This is the ADR-0004 Phase-9 "typed overload" entry
    point: the ``map_*``/``liq_*`` scalars from :func:`derive_map_features` + the ``maps`` sub-tree from
    ``MapBundle.to_dict()``, price from ``MarketView.last_price``. (The DOM ``book_walls``/``freshness``
    slice was dropped with the DOM block — nothing left reads it; ``format_book_walls_section`` is now
    only exercised directly by its own tests with a hand-built row.)
    """
    from hunt_core.maps.engine import derive_map_features

    price = float(native.view.last_price or 0)
    bundle = native.maps
    market = derive_map_features(bundle, current_price=price) if bundle is not None else {}
    maps = bundle.to_dict() if bundle is not None else {}
    return {"price": price, "market": market, "maps": maps}


def format_intraday_maps_telegram(native: NativeAnalystView) -> str:
    """Liquidity block for the deep card: aggregated liquidity heatmap + liquidation magnets.

    DOM (``format_book_walls_section``) is intentionally NOT rendered: the Prizrak method trades
    limit orders on 5m+ structural zones, not the top-of-book microstructure the DOM section pushed —
    and its cross-venue name-lies + carried-snapshot staleness were the «serious errors» this format
    cleanup targets. The heatmap + liquidation sections stay (aggregated sticky walls / density bands
    and the nearest realized-liq magnets) — which the author DOES read («у призрака своя heatmap»); the
    post's «🌪 По приборам» line carries the one-line nearest-magnet summary, this block is the detail.
    """
    render = _native_map_render_inputs(native)
    blocks: list[str] = []
    heat = format_liquidity_heatmap_section(render)
    if heat:
        blocks.append(heat)
    liq = format_liquidation_map_section(render)
    if liq:
        blocks.append(liq)
    if not blocks:
        return ""
    return "\n\n".join(blocks)
