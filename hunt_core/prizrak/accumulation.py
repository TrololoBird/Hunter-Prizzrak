"""Накопление / флет — an accumulation zone as a first-class object (hi, lo, touches).

Course rule: a base is only tradeable once it has "4+ явные точки" (4+ clear boundary
touches) on the timeframe where the structure is visible. This clusters swing-pivot
highs/lows (reusing the same fractal pivots as ``pp.py``) into boundary bands and
returns the widest recent band pair that meets the touch-count threshold — this is the
zone POC/stop-volume detection then operates on.

Each cluster/zone carries the bar index of its most recent contributing pivot
(``last_touch_idx`` / ``recency``) alongside touch count. A real macro накопление that
long-term traders still watch (course: "следующая сильная, непротестированная база")
is legitimately far away and untouched for a while — recency is not a "freshness"
filter that drops old zones. It exists so *callers* ranking zones against each other
(forward-target selection in orchestrator.py) can tell a base that predates a
subsequent regime-breaking move (e.g. a multi-week accumulation from before a
50%+ range shift) apart from one still actually in play, instead of picking whichever
has accumulated the most historical touches regardless of how long ago that was.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.pp import _pivots

_CLUSTER_TOL = 0.006  # 0.6% — pivots within this of each other count as "the same touch"


def _cluster(points: list[tuple[int, float]], *, tol: float) -> list[dict[str, Any]]:
    """Cluster ``(bar_idx, price)`` pivot points into boundary bands. Ordered by
    price (not time) so nearby touches merge regardless of when they occurred."""
    if not points:
        return []
    ordered = sorted(points, key=lambda t: t[1])
    clusters: list[list[tuple[int, float]]] = [[ordered[0]]]
    for idx, px in ordered[1:]:
        ref = sum(p for _, p in clusters[-1]) / len(clusters[-1])
        if ref > 0 and abs(px - ref) / ref <= tol:
            clusters[-1].append((idx, px))
        else:
            clusters.append([(idx, px)])
    return [
        {
            "price": sum(p for _, p in c) / len(c),
            "touches": len(c),
            "first_touch_idx": min(idx for idx, _ in c),
            "last_touch_idx": max(idx for idx, _ in c),
        }
        for c in clusters
    ]


def _zone_from_clusters(hi: dict[str, Any], lo: dict[str, Any], *, tf: str, bar_count: int) -> dict[str, Any]:
    touches = hi["touches"] + lo["touches"]
    first_touch_idx = min(hi["first_touch_idx"], lo["first_touch_idx"])
    last_touch_idx = max(hi["last_touch_idx"], lo["last_touch_idx"])
    denom = max(bar_count - 1, 1)
    return {
        "tf": tf,
        "hi": round(hi["price"], 8),
        "lo": round(lo["price"], 8),
        "touches": touches,
        "hi_touches": hi["touches"],
        "lo_touches": lo["touches"],
        "width_pct": round((hi["price"] - lo["price"]) / lo["price"] * 100, 4),
        # Span of the structure's own bars, so a volume profile can be fitted to it
        # rather than to the whole lookback ("натягиваем профиль на структуру").
        "first_touch_idx": first_touch_idx,
        "last_touch_idx": last_touch_idx,
        # 0 = last touch was at the very start of the lookback window (stale/ancient
        # relative to what we can see), 1 = touched on the most recent bar available.
        "recency": round(last_touch_idx / denom, 4),
    }


def _zone_volume(bars: list[dict[str, float]], first_idx: int, last_idx: int) -> float:
    """Traded volume across the zone's own structure bars — the course's measure of
    level strength (стр.22: "Сила уровня определяется ТФ и объёмом ... смотрим по VRVP")."""
    lo_i = max(0, int(first_idx))
    hi_i = min(len(bars) - 1, int(last_idx))
    if hi_i < lo_i:
        return 0.0
    return sum(float(b.get("volume", 0.0)) for b in bars[lo_i:hi_i + 1])


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if zone ``a`` and ``b`` share any price range."""
    return a["lo"] <= b["hi"] and b["lo"] <= a["hi"]


def find_accumulation_zones(
    bars: list[dict[str, float]],
    *,
    tf: str,
    cfg: PrizrakConfig | None = None,
    max_zones: int = 4,
) -> list[dict[str, Any]]:
    """Every distinct boundary pair (resistance cluster above a support cluster) with
    combined touches >= cfg.accumulation_min_touches, ranked strongest-first (most
    touches, then narrowest — a tighter base is a more decisive zone). Non-overlapping
    only — a weaker zone that shares price range with a stronger one is dropped rather
    than double-counting the same base. This is what forward zone-targeting ranks
    against (course: price travels toward the next strong, untouched base, not just
    the nearest one) — each returned zone also carries ``recency`` so ranking there
    can weigh in how current the base still is.
    """
    cfg = cfg or PrizrakConfig.load()
    pivots = _pivots(bars)
    if len(pivots) < cfg.accumulation_min_touches:
        return []

    highs = [(idx, price) for idx, kind, price in pivots if kind == "high"]
    lows = [(idx, price) for idx, kind, price in pivots if kind == "low"]
    high_clusters = _cluster(highs, tol=_CLUSTER_TOL)
    low_clusters = _cluster(lows, tol=_CLUSTER_TOL)
    if not high_clusters or not low_clusters:
        return []

    candidates: list[dict[str, Any]] = []
    for hi in high_clusters:
        for lo in low_clusters:
            if hi["price"] <= lo["price"]:
                continue  # degenerate — resistance below support, not a real box
            touches = hi["touches"] + lo["touches"]
            if touches < cfg.accumulation_min_touches:
                continue
            zone = _zone_from_clusters(hi, lo, tf=tf, bar_count=len(bars))
            if zone["width_pct"] > cfg.accumulation_max_width_pct:
                continue  # too wide to be a real flat — stitched-together pivots, not a box
            zone["zone_volume"] = round(
                _zone_volume(bars, zone["first_touch_idx"], zone["last_touch_idx"]), 4
            )
            candidates.append(zone)

    # Touch count (>= accumulation_min_touches) is the STRUCTURE gate — a base needs 4+
    # boundary points to exist at all (стр.22-23). Among valid bases, strength is ranked
    # by traded VOLUME, not touch count: the course explicitly prefers a smaller-touch,
    # higher-volume наторговка over a wider-touched one (стр.22). Volume ties break to the
    # tighter box (a denser base is more decisive).
    candidates.sort(key=lambda z: (z["zone_volume"], -z["width_pct"]), reverse=True)

    kept: list[dict[str, Any]] = []
    for zone in candidates:
        if any(_overlaps(zone, k) for k in kept):
            continue
        kept.append(zone)
        if len(kept) >= max_zones:
            break
    return kept


def find_accumulation_zone(
    bars: list[dict[str, float]],
    *,
    tf: str,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """The single strongest accumulation zone. Empty dict if none qualifies."""
    zones = find_accumulation_zones(bars, tf=tf, cfg=cfg, max_zones=1)
    return zones[0] if zones else {}


__all__ = ["find_accumulation_zone", "find_accumulation_zones"]
