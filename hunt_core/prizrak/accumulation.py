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

from collections.abc import Sequence
from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.pp import _pivots

# 0.6% — два пивота ближе этого считаются «одним касанием».
#
# ⚠ ЗАМЕРЕНО И ОСТАВЛЕНО КАК ЕСТЬ — это не «не дошли руки». Против случайного блуждания
# (`scripts/calibrate_level_band.py`) 0.6% даёт p(отскок) 0.547 (BTC 1h) … 0.732 (BTC 15m)
# вместо нейтральных 0.5, а формула Garzarelli δ(τ) = ⟨|ΔP|⟩ даёт 0.469–0.544 на всех 12
# сочетаниях символ/ТФ. То есть по критерию НУЛЕВОЙ ГИПОТЕЗЫ δ(τ) строго лучше константы.
#
# Но подстановка δ(τ) сюда была прогнана через `scripts/score_vs_razbor.py` со случайным
# контролем и УХУДШИЛА результат: recall 75.4% → 76.9%, зато контроль 32.4% → 39.4%, то есть
# ПРЕВЫШЕНИЕ над случайностью упало **+43.0 → +37.5 п.п.** (зон 595→609, p90 ширины 3.52→4.75%).
# Причина в том, что задачи разные: δ(τ) калибрует полосу «цена ТЕСТИРУЕТ уровень», а здесь
# решается «два пивота — одно касание». Хорошая калибровка не для той величины.
#
# Правка откачена по собственному гейту. Примитив остался — `toolkit/level_band.py`, и им
# считается нулевой базлайн в калибровочном скрипте. Пробовать δ(τ) уместно на `_RETEST_TOL`
# (вот он как раз «цена тестирует уровень»), но только вместе с гейтом, который это измерит.
_CLUSTER_TOL = 0.006


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
            # Wick extremes of the cluster's own touches. Pivot prices ARE bar
            # highs/lows (pp._pivots), so min/max here = the deepest прокол ever
            # made at this boundary — what the course anchors the stop behind when
            # a boundary with 3+ touches has been wicked (стр.18: «если на 3++
            # точках были проколы за границы — стоп всегда ставится за этот прокол»).
            "px_min": min(p for _, p in c),
            "px_max": max(p for _, p in c),
        }
        for c in clusters
    ]


def _open_test_start_idx(bars: list[dict[str, float]], lo: float, hi: float) -> int | None:
    """Индекс начала ТЕКУЩЕЙ, ещё не закрытой серии баров «цена внутри зоны», либо ``None``.

    Только трейлинг-серия: закрытые в прошлом заходы в зону — это законные касания, и именно
    их считает ``touches`` (рост вероятности отскока с числом касаний — единственное, что в
    литературе по уровням измерено дважды независимо). Замораживать надо ровно тот заход,
    который идёт ПРЯМО СЕЙЧАС.
    """
    i = len(bars) - 1
    if i < 0 or not (lo <= bars[i]["close"] <= hi):
        return None
    while i > 0 and lo <= bars[i - 1]["close"] <= hi:
        i -= 1
    return i


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
        # Wick extremes of the boundary clusters (deepest прокол per side): the zone's
        # lo/hi are cluster AVERAGES, so a stop anchored at lo can sit INSIDE the range
        # price has already wicked through. _structural_stop anchors behind these when
        # the boundary has 3+ touches (курс стр.18).
        "ext_lo": round(lo["px_min"], 8),
        "ext_hi": round(hi["px_max"], 8),
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
    combined touches >= cfg.accumulation_min_touches, ranked strongest-first by traded
    VOLUME, ties broken to the narrower box (see the sort below — touches are the
    structure GATE, not the strength scale: стр.22). Non-overlapping
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
    zones = _zones_from_pivots(pivots, bars, tf=tf, cfg=cfg, max_zones=max_zones)
    if not zones:
        return zones

    # ⚠ ЗАМОРОЗКА ГРАНИЦЫ НА ВРЕМЯ ТЕКУЩЕГО ЗАХОДА В ЗОНУ (I-5).
    #
    # `_cluster` берёт СРЕДНЕЕ пивотов кластера, поэтому каждый новый пивот сдвигает границу.
    # Пока цена тестирует зону, её собственные экстремумы становятся пивотами и утаскивают
    # границу за собой. Chung & Bellotti (arXiv:2101.07410) описывают этот дефект прямо:
    # «If the discovery procedure continues operating, a new minimum (maximum) would create a
    # new lower (upper) boundary … thus **erroneously reducing the probability of penetration**».
    # Пробой становится невозможен по построению, а «отскок от зоны» — тавтологией.
    #
    # ЗАМЕР до правки (`scripts/verify_zone_freeze.py`, 4 символа × 3 ТФ, продление окна на 40
    # баров): граница сдвигалась, пока цена внутри, на ВСЕХ 12 сочетаниях; медиана сдвига
    # 0.02–3.14%, максимум **7.34%** (XRP 4h). Для сравнения: весь буфер стопа — 2%.
    #
    # Замораживается ТОЛЬКО текущий, ещё не закрытый заход. Прошлые заходы — это законные
    # касания, и их считает `touches`: рост вероятности отскока с числом касаний — единственное
    # свойство уровней, измеренное в литературе дважды независимо (Garzarelli 2014,
    # Chung & Bellotti 2021). Их выбрасывать нельзя.
    # Точка заморозки считается по границам зоны, а сама зона — по точке заморозки, поэтому
    # берётся НЕПОДВИЖНАЯ ТОЧКА. Без итерации остаётся остаточный дрейф: первый проход даёт
    # `start` по ещё не замороженной (плывущей) зоне, и на следующем баре он прыгает вместе с
    # ней. Замер это и показал — 12 сносов вместо 39, но не ноль. Итераций максимум три:
    # отображение монотонно (уже пивотов ⇒ уже зона ⇒ не позже вход), цикла быть не может,
    # а потолок гарантирует детерминизм и отсутствие зависания.
    prev_start: int | None = None
    for _ in range(3):
        start = _open_test_start_idx(bars, float(zones[0]["lo"]), float(zones[0]["hi"]))
        if start is None or start == prev_start:
            break
        frozen = [p for p in pivots if p[0] < start]
        if len(frozen) < cfg.accumulation_min_touches:
            break  # до захода структуры не было — замораживать нечего
        rebuilt = _zones_from_pivots(frozen, bars, tf=tf, cfg=cfg, max_zones=max_zones)
        if not rebuilt:
            break
        zones, prev_start = rebuilt, start
    return zones


def _zones_from_pivots(
    pivots: Sequence[tuple[int, str, float]],
    bars: list[dict[str, float]],
    *,
    tf: str,
    cfg: PrizrakConfig,
    max_zones: int,
) -> list[dict[str, Any]]:
    """Ядро построения зон из готового набора пивотов (вынесено ради заморозки — см. выше)."""
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
