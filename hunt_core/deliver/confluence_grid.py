"""/signals level map grid (§N.2) — surfaces Prizrak structural levels (4h, 1w),
legacy donchian/volume levels, and multi-level support/resistance below/above price
so the user sees deep zones (e.g. 60500–58550) not just the nearest swing pivot."""
from __future__ import annotations

from typing import Any

import structlog

from hunt_core.deliver._labels import fmt_dist as _fmt_dist
from hunt_core.deliver._labels import fmt_price as _fmt_price_adaptive
from hunt_core.features.models import FeaturePanel
from hunt_core.prizrak.models import PrizrakOutput

LOG = structlog.get_logger(__name__)


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


def build_confluence_grid_native(
    prizrak: PrizrakOutput,
    features: FeaturePanel,
    *,
    price: float,
) -> list[dict[str, Any]]:
    """Level map from TYPED handles — native replacement for :func:`build_confluence_grid`.

    Same return shape (a ``list[dict]`` consumed by :func:`format_grid_telegram`) and same geometry
    (deep-zone collection, clustering, dedup); only the SOURCES move from the untyped row-dict to the
    typed spine:

    * ``price``               ← ``MarketView.last_price`` (passed in).
    * donchian support/resist ← ``FeaturePanel.tf[tf].donchian_low20 / .donchian_high20``.
    * ``poc``/``vah``/``val`` ← ``FeaturePanel.vp[tf]``.
    * trailing session POC    ← ``FeaturePanel.vp["1h"].poc``.
    * prizrak structure       ← ``PrizrakOutput.structure`` (still a dict payload).

    Gap — ``local_support`` / ``local_resistance``:
        The untyped grid read ``block.get("local_support") or block.get("donchian_low20")``. But
        ``tf_snapshot`` — the sole producer of ``row["timeframes"][tf]`` — NEVER wrote those keys, so
        they were phantom reads that always resolved to ``None`` and the ``or`` always fell through to
        donchian. ``TfSummary`` has no such field either → this form uses donchian directly (identical
        behaviour, dead slot removed).

    Behaviour change — ``poc``/``vah``/``val``:
        Those TF blocks also never carried ``poc``/``vah``/``val`` (always ``None``); sourcing them
        from ``FeaturePanel.vp[tf]`` POPULATES per-TF POC/VAH/VAL the old grid dropped — a
        correct-wiring change, not a byte-parity reproduction.

    Args:
        prizrak: The typed PRIZRAK verdict; ``.structure`` carries ``struct_by_tf`` deep levels.
        features: The typed feature panel; ``.tf`` (donchian) and ``.vp`` (POC/VAH/VAL) per TF.
        price: Live price (``MarketView.last_price``) used to validate levels against spot.

    Returns:
        The level-map grid — a ``list[dict[str, Any]]`` in the exact shape
        :func:`format_grid_telegram` consumes.
    """
    price = float(price or 0)
    grid: list[dict[str, Any]] = []

    # --- legacy TF levels (1h, 15m, 5m): donchian from tf summaries, POC/VAH/VAL from vp ---
    for tf_name in ("1h", "15m", "5m"):
        tsum = features.tf.get(tf_name)
        vp = features.vp.get(tf_name)
        if tsum is None and vp is None:
            LOG.debug("confluence_grid_native.tf_empty", tf=tf_name)
            continue
        support = tsum.donchian_low20 if tsum is not None else None
        resistance = tsum.donchian_high20 if tsum is not None else None
        if support is not None and float(support) <= 0:
            support = None
        if resistance is not None and float(resistance) <= 0:
            resistance = None
        if price > 0:
            if support is not None and float(support) >= price:
                support = None
            if resistance is not None and float(resistance) <= price:
                resistance = None
        grid.append(
            {
                "tf": tf_name,
                "poc": vp.poc if vp is not None else None,
                "vah": vp.vah if vp is not None else None,
                "val": vp.val if vp is not None else None,
                "support": support,
                "resistance": resistance,
            }
        )

    # --- Prizrak structural levels per-TF (4h, 1w, + 1h/1d fallback) — dict payload unchanged ---
    ps = prizrak.structure if isinstance(prizrak.structure, dict) else {}
    _sbt = ps.get("struct_by_tf")
    struct_by_tf: dict[str, Any] = _sbt if isinstance(_sbt, dict) else {}
    for tf_name in ("1w", "1d", "4h", "1h"):
        _s3 = struct_by_tf.get(tf_name)
        s = _s3 if isinstance(_s3, dict) else {}
        if not s:
            continue
        kl = s.get("key_levels") or {}
        support = kl.get("support")
        resistance = kl.get("resistance")
        if price > 0:
            if support is not None and float(support) >= price:
                support = None
            if resistance is not None and float(resistance) <= price:
                resistance = None
        grid.append(
            {
                "tf": tf_name,
                "support": support,
                "resistance": resistance,
                "last_swing_high": kl.get("last_swing_high"),
                "last_swing_low": kl.get("last_swing_low"),
                "_all_swing_highs": s.get("all_swing_highs"),
                "_all_swing_lows": s.get("all_swing_lows"),
            }
        )

    # Levels already printed on a per-TF row — the "глубже/выше" lists must not repeat them.
    shown_levels: list[float] = []
    for g in grid:
        for k in ("support", "resistance"):
            v = g.get(k)
            if isinstance(v, (int, float)) and v > 0:
                shown_levels.append(float(v))

    def _dedup_levels(vals: list[float], *, min_sep_pct: float = 0.15) -> list[float]:
        """Drop levels within ``min_sep_pct`` of an already-kept OR already-shown level."""
        kept: list[float] = []
        for p in vals:
            if any(abs(p - sv) / max(p, 1e-9) * 100.0 < min_sep_pct for sv in shown_levels):
                continue
            if any(abs(p - kv) / max(p, 1e-9) * 100.0 < min_sep_pct for kv in kept):
                continue
            kept.append(p)
        return kept

    # --- Multi-level deep zones: collect swing lows below / highs above price ---
    deeper_supports: list[float] = []
    deeper_resistances: list[float] = []
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
        sorted_lows = _dedup_levels(sorted(deeper_supports, reverse=True))
        if sorted_lows:
            grid.append({"tf": "глубже", "support": sorted_lows[:6]})
        clusters = _cluster_levels(sorted_lows, pct_gap=2.5)
        if len(clusters) > 1:
            for i, cl in enumerate(clusters):
                if len(cl) < 2:
                    continue
                lo, hi = min(cl), max(cl)
                if hi > price:
                    continue
                if lo <= 0 or (hi - lo) / hi * 100.0 > _ZONE_ROW_MAX_WIDTH_PCT:
                    continue
                grid.append(
                    {
                        "tf": f"зона {i + 1}",
                        "support": f"{_fmt_price_adaptive(hi)}–{_fmt_price_adaptive(lo)}",
                        "_skip_generic": True,
                    }
                )

    if deeper_resistances:
        sorted_highs = _dedup_levels(sorted(deeper_resistances))
        if sorted_highs:
            grid.append({"tf": "выше", "resistance": sorted_highs[:6]})
        clusters_h = _cluster_levels(sorted_highs, pct_gap=2.5)
        if len(clusters_h) > 1:
            for i, cl in enumerate(clusters_h):
                if len(cl) < 2:
                    continue
                lo, hi = min(cl), max(cl)
                if lo < price:
                    continue
                if lo <= 0 or (hi - lo) / hi * 100.0 > _ZONE_ROW_MAX_WIDTH_PCT:
                    continue
                grid.append(
                    {
                        "tf": f"зона {i + 1}",
                        "resistance": f"{_fmt_price_adaptive(hi)}–{_fmt_price_adaptive(lo)}",
                        "_skip_generic": True,
                    }
                )

    # Trailing session-POC row — was row["regime"]["poc_1h"]; Regime has no poc_1h, its value is
    # the 1h volume-profile POC (features/prepare.py poc_1h=profile_1h[0]) == vp["1h"].poc.
    vp_1h = features.vp.get("1h")
    poc_1h = vp_1h.poc if vp_1h is not None else None
    if poc_1h is not None:
        grid.append({"tf": "regime", "poc": poc_1h, "note": "session POC"})
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
    _KIND_ORDER = ("poc", "support", "resistance", "vah", "val")
    # The same TF can appear twice — once from legacy donchian levels, once from
    # Prizrak structure — which rendered as two identical "1h:" lines. Merge under
    # one line per TF. Single numeric levels are collected per (tf, kind) so they
    # can be ordered NEAREST-first at emit time (support below price → higher is
    # nearer; resistance above → lower is nearer); the old code appended in grid
    # order, so two donchian/structure resistances printed out of proximity.
    # String ranges and list-valued kinds (глубже/выше) keep their upstream order.
    num_by: dict[str, dict[str, list[float]]] = {}
    tok_by: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []

    def _touch(tf: str) -> None:
        if tf not in num_by:
            num_by[tf] = {}
            tok_by[tf] = {}
            order.append(tf)

    for g in grid:
        tf = str(g.get("tf", "?"))
        for k in _KIND_ORDER:
            v = g.get(k)
            if v is None:
                continue
            # String-valued = pre-formatted range (e.g. "63000–62000").
            if isinstance(v, str):
                _touch(tf)
                bucket = tok_by[tf].setdefault(k, [])
                if v not in bucket:
                    bucket.append(v)
                continue
            # List-valued = multi-level (e.g. deeper supports) — keep upstream order.
            if isinstance(v, list):
                sub = [_fmt_price(x) for x in v if isinstance(x, (int, float)) and x > 0]
                if sub:
                    _touch(tf)
                    joined = "/".join(sub)
                    bucket = tok_by[tf].setdefault(k, [])
                    if joined not in bucket:
                        bucket.append(joined)
                continue
            # Single numeric level.
            if isinstance(v, (int, float)):
                fv = float(v)
                if not _level_within_range(fv, price):
                    continue
                _touch(tf)
                bucket_n = num_by[tf].setdefault(k, [])
                if fv not in bucket_n:
                    bucket_n.append(fv)
    # Multi-TF confluence, computed BEFORE the per-TF lines so a shared level is LIFTED
    # out of them rather than duplicated: a level on ≥2 TFs used to print on every per-TF
    # line AND in the highlight — 62505.1 as «1h», «4h» AND «(1h+4h)», the same number
    # three times. Now it prints once, in the «усиленные» line, with its distance; only
    # genuinely single-TF numeric levels stay on the per-TF lines. (String ranges and the
    # глубже/выше list tokens carry no TF set and are never folded.)
    _tf_rank = {t: i for i, t in enumerate(("1m", "5m", "15m", "1h", "4h", "1d", "1w"))}
    conf_bits: list[str] = []
    absorbed: set[tuple[str, str, float]] = set()
    for k in ("support", "resistance"):
        groups: list[dict[str, Any]] = []
        pts = sorted((v, tf) for tf in order for v in num_by[tf].get(k, []))
        for pv, tf in pts:
            matched: dict[str, Any] | None = None
            for g in groups:
                if pv > 0 and abs(g["price"] - pv) / pv <= 0.001:
                    matched = g
                    break
            if matched is None:
                groups.append({"price": pv, "tfs": {tf}, "members": [(tf, pv)]})
            else:
                matched["tfs"].add(tf)
                matched["members"].append((tf, pv))
        for g in groups:
            if len(g["tfs"]) >= 2:
                for m_tf, m_pv in g["members"]:
                    absorbed.add((m_tf, k, m_pv))
                tfs = "+".join(sorted(g["tfs"], key=lambda t: _tf_rank.get(t, 99)))
                # Same distance grammar as the per-TF lines, but inside the (TF · %)
                # group — fmt_dist wraps its own parens, so strip them before nesting.
                dist_raw = _fmt_dist(g["price"], price).strip()
                inner = dist_raw[1:-1] if dist_raw[:1] == "(" and dist_raw[-1:] == ")" else dist_raw
                tail = f" · {inner}" if inner else ""
                conf_bits.append(f"{_K_RU[k]} {_fmt_price(g['price'])} ({tfs}{tail})")

    for tf in order:
        parts: list[str] = []
        for k in _KIND_ORDER:
            nums = num_by[tf].get(k, [])
            if k == "support":
                nums = sorted(nums, reverse=True)  # nearest (highest) first
            elif k == "resistance":
                nums = sorted(nums)  # nearest (lowest) first
            for val in nums:
                if (tf, k, val) in absorbed:
                    continue  # shown once in the «усиленные» confluence line instead
                # Distance is what makes a level readable at a glance: a support 0.1%
                # away and one 9.6% away used to render identically, leaving the reader
                # to divide every number on the card by spot in their head.
                parts.append(f"{_K_RU.get(k, k)}={_fmt_price(val)}{_fmt_dist(val, price)}")
            for tok in tok_by[tf].get(k, []):
                parts.append(f"{_K_RU.get(k, k)}={tok}")
        if parts:
            lines.append(f"· {tf}: " + ", ".join(parts[:6]))

    if conf_bits:
        lines.append("🔗 <b>мульти-ТФ конфлюенс</b> (усиленные): " + ", ".join(conf_bits))
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = ["build_confluence_grid", "format_grid_telegram"]
