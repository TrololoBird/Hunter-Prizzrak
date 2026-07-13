"""/signals level map grid (§N.2) — surfaces Prizrak structural levels (4h, 1w),
legacy donchian/volume levels, and multi-level support/resistance below/above price
so the user sees deep zones (e.g. 60500–58550) not just the nearest swing pivot."""
from __future__ import annotations

from typing import Any

from hunt_core.deliver._labels import fmt_price as _fmt_price_adaptive


def build_confluence_grid(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Level map: POC/structure/fib magnets per TF, enriched with Prizrak deep levels."""
    price = float(row.get("price") or 0)
    grid: list[dict[str, Any]] = []

    # --- legacy TF levels (1h, 15m, 5m) from features/snapshot ---
    for tf_name in ("1h", "15m", "5m"):
        block = (row.get("timeframes") or {}).get(tf_name) or {}
        if not block or block.get("status") == "empty":
            continue
        support = block.get("local_support") or block.get("donchian_low20")
        resistance = block.get("local_resistance") or block.get("donchian_high20")
        if support is not None and float(support) <= 0:
            support = None
        if resistance is not None and float(resistance) <= 0:
            resistance = None
        if price > 0:
            if support is not None and float(support) >= price:
                support = None
            if resistance is not None and float(resistance) <= price:
                resistance = None
        entry = {
            "tf": tf_name,
            "poc": block.get("poc") or block.get("poc_1h"),
            "vah": block.get("vah"),
            "val": block.get("val"),
            "support": support,
            "resistance": resistance,
        }
        grid.append(entry)

    # --- Prizrak structural levels per-TF (4h, 1w, plus 1h/1d fallback) ---
    _ps = row.get("prizrak_structure")
    ps: dict[str, Any] = _ps if isinstance(_ps, dict) else {}
    _sbt = ps.get("struct_by_tf") if isinstance(ps, dict) else None
    struct_by_tf: dict[str, Any] = _sbt if isinstance(_sbt, dict) else {}
    for tf_name in ("1w", "1d", "4h", "1h"):
        _s3 = struct_by_tf.get(tf_name)
        s = _s3 if isinstance(_s3, dict) else {}
        if not s:
            continue
        kl = s.get("key_levels") or {}
        support = kl.get("support")
        resistance = kl.get("resistance")
        # Validate against price
        if price > 0:
            if support is not None and float(support) >= price:
                support = None
            if resistance is not None and float(resistance) <= price:
                resistance = None
        entry = {
            "tf": tf_name,
            "support": support,
            "resistance": resistance,
            "last_swing_high": kl.get("last_swing_high"),
            "last_swing_low": kl.get("last_swing_low"),
            "_all_swing_highs": s.get("all_swing_highs"),
            "_all_swing_lows": s.get("all_swing_lows"),
        }
        grid.append(entry)

    # Levels already printed on a per-TF row — the "глубже/выше" lists must not
    # repeat them (the 1h support/resist reappearing below was pure noise).
    shown_levels: list[float] = []
    for g in grid:
        for k in ("support", "resistance"):
            v = g.get(k)
            if isinstance(v, (int, float)) and v > 0:
                shown_levels.append(float(v))

    def _dedup_levels(vals: list[float], *, min_sep_pct: float = 0.15) -> list[float]:
        """Drop levels within min_sep_pct of an already-kept OR already-shown
        level — collapses the 63668.8/63657.4 (~0.02%) near-duplicates and the
        per-TF repeats into a clean ladder."""
        kept: list[float] = []
        for p in vals:
            if any(abs(p - s) / max(p, 1e-9) * 100.0 < min_sep_pct for s in shown_levels):
                continue
            if any(abs(p - k) / max(p, 1e-9) * 100.0 < min_sep_pct for k in kept):
                continue
            kept.append(p)
        return kept

    # --- Multi-level deep zones: collect all swing lows below price as "deeper support" ---
    deeper_supports: list[float] = []
    deeper_resistances: list[float] = []
    # Bound the deep lists to the SAME near-term window as the per-TF levels
    # (_GRID_MAX_DISTANCE_PCT). Without this the "выше"/"глубже" lists pulled in
    # every swing high/low regardless of distance, so an HTF swing / psychological
    # level +24% away (e.g. 79455 vs a 63937 price, past a 3300pt gap of no nodes)
    # rendered as near-term structure — low-utility noise on an intraday map.
    for g in grid:
        lows = g.get("_all_swing_lows")
        if isinstance(lows, list):
            for p in lows:
                if isinstance(p, (int, float)) and p > 0 and p < price:
                    if p not in deeper_supports and _level_within_range(float(p), price):
                        deeper_supports.append(p)
        highs = g.get("_all_swing_highs")
        if isinstance(highs, list):
            for p in highs:
                if isinstance(p, (int, float)) and p > 0 and p > price:
                    if p not in deeper_resistances and _level_within_range(float(p), price):
                        deeper_resistances.append(p)
    if deeper_supports:
        # Nearest-first, de-duplicated (no per-TF repeats, no ~0.02% pairs).
        sorted_lows = _dedup_levels(sorted(deeper_supports, reverse=True))
        if sorted_lows:
            grid.append({
                "tf": "глубже",
                "support": sorted_lows[:6],
            })
        # Clustered zones: group nearby swing lows into ranges.
        # A cluster is formed when consecutive levels are within 2.5% of each other.
        clusters = _cluster_levels(sorted_lows, pct_gap=2.5)
        if len(clusters) > 1:
            for i, cl in enumerate(clusters):
                if len(cl) < 2:
                    continue
                lo = min(cl)
                hi = max(cl)
                if hi > price:
                    continue
                # Chained levels (each within 2.5% of the next) can accrete into a
                # 9%+ band — useless as a zone row (e.g. BTC «зона 1: 63669–58030»).
                # A zone row is only actionable when the WHOLE band is tight.
                if lo <= 0 or (hi - lo) / hi * 100.0 > _ZONE_ROW_MAX_WIDTH_PCT:
                    continue
                grid.append({
                    "tf": f"зона {i+1}",
                    "support": f"{_fmt_price_adaptive(hi)}–{_fmt_price_adaptive(lo)}",
                    "_skip_generic": True,
                })
    if deeper_resistances:
        sorted_highs = _dedup_levels(sorted(deeper_resistances))
        if sorted_highs:
            grid.append({
                "tf": "выше",
                "resistance": sorted_highs[:6],
            })
        clusters_h = _cluster_levels(sorted_highs, pct_gap=2.5)
        if len(clusters_h) > 1:
            for i, cl in enumerate(clusters_h):
                if len(cl) < 2:
                    continue
                lo = min(cl)
                hi = max(cl)
                if lo < price:
                    continue
                if lo <= 0 or (hi - lo) / hi * 100.0 > _ZONE_ROW_MAX_WIDTH_PCT:
                    continue
                grid.append({
                    "tf": f"зона {i+1}",
                    "resistance": f"{_fmt_price_adaptive(hi)}–{_fmt_price_adaptive(lo)}",
                    "_skip_generic": True,
                })

    regime = row.get("regime") or {}
    if regime.get("poc_1h"):
        grid.append({"tf": "regime", "poc": regime.get("poc_1h"), "note": "session POC"})
    return grid


_GRID_MAX_DISTANCE_PCT = 15.0
_CLUSTER_PCT_GAP = 2.0  # swing lows: split at gaps wider than this
# Max total width of a rendered «зона N» row. Consecutive levels each within the
# 2.5% cluster gap can chain into an arbitrarily wide band; beyond this the row
# stops being a limit band and becomes «the whole range».
_ZONE_ROW_MAX_WIDTH_PCT = 5.0


def _cluster_levels(levels: list[float], *, pct_gap: float = _CLUSTER_PCT_GAP) -> list[list[float]]:
    """Group sorted levels into clusters by finding the widest gaps.
    
    Uses the widest gap(s) as splitting points so the deep zone (60500–58550)
    is separated from near support levels automatically.
    """
    if not levels:
        return []
    if len(levels) <= 4:
        return [levels]
    # Absolute gaps so clustering is order-agnostic: the support path feeds
    # DESCENDING levels (gaps were positive) but the resistance path feeds
    # ASCENDING levels — signed gaps were then all-negative, so no split ever fired
    # and resistance «зона N» rows silently never rendered. abs() fixes both.
    gaps = [
        abs(levels[i] - levels[i + 1]) / max(abs(levels[i]), 0.01) * 100
        for i in range(len(levels) - 1)
    ]
    # Split at all gaps wider than pct_gap — if none, take the single widest.
    split_indices = [i for i, g in enumerate(gaps) if g > pct_gap]
    if not split_indices:
        widest = max(range(len(gaps)), key=lambda i: gaps[i])
        if gaps[widest] > pct_gap * 0.6:
            split_indices = [widest]
    if not split_indices:
        return [levels]
    clusters: list[list[float]] = []
    start = 0
    for si in sorted(split_indices):
        clusters.append(levels[start:si + 1])
        start = si + 1
    clusters.append(levels[start:])
    return clusters


def _level_within_range(level: float, price: float, max_pct: float = _GRID_MAX_DISTANCE_PCT) -> bool:
    if price <= 0 or level <= 0:
        return False
    return abs(level - price) / price * 100 <= max_pct


def _fmt_price(v: Any) -> str:
    # Delegate to the adaptive formatter so sub-dollar instruments (e.g. XTZ ~0.228)
    # keep enough precision — a hardcoded .2f collapsed distinct levels to the same
    # number (support==resistance==0.23, "0.24/0.24/0.24").
    try:
        return _fmt_price_adaptive(float(v)) if isinstance(v, (int, float)) else str(v)
    except (ValueError, TypeError):
        return str(v)


def format_grid_telegram(grid: list[dict[str, Any]], *, price: float = 0) -> str:
    if not grid:
        return ""

    lines = ["<b>Карта уровней</b> <i>(POC/структура · не стакан и не ликвидации)</i>"]
    _K_RU = {"poc": "POC", "support": "поддержка", "resistance": "сопротивл", "vah": "VAH", "val": "VAL"}
    # The same TF can appear twice — once from legacy donchian levels, once from
    # Prizrak structure — which rendered as two identical "1h:" lines. Merge parts
    # under one line per TF (order preserved, identical parts de-duplicated).
    by_tf: dict[str, list[str]] = {}
    order: list[str] = []
    for g in grid:
        tf = str(g.get("tf", "?"))
        parts: list[str] = []
        for k in ("poc", "support", "resistance", "vah", "val"):
            v = g.get(k)
            if v is None:
                continue
            # String-valued = pre-formatted range (e.g. "63000–62000").
            if isinstance(v, str):
                parts.append(f"{_K_RU.get(k, k)}={v}")
                continue
            # List-valued = multi-level (e.g. deeper supports).
            if isinstance(v, list):
                sub = [_fmt_price(x) for x in v if isinstance(x, (int, float)) and x > 0]
                if sub:
                    parts.append(f"{_K_RU.get(k, k)}={'/'.join(sub)}")
                continue
            # Single numeric level.
            if isinstance(v, (int, float)):
                if not _level_within_range(float(v), price):
                    continue
                parts.append(f"{_K_RU.get(k, k)}={_fmt_price(v)}")
        if not parts:
            continue
        if tf not in by_tf:
            order.append(tf)
            by_tf[tf] = []
        for p in parts:
            if p not in by_tf[tf]:
                by_tf[tf].append(p)
    for tf in order:
        lines.append(f"· {tf}: " + ", ".join(by_tf[tf][:6]))
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = ["build_confluence_grid", "format_grid_telegram"]
