"""Фигуры (v2) — geometric pattern tagging, course chapter «Фигуры».

Every figure in the course is a special case of primitives already implemented,
so this reuses them rather than re-recognizing geometry from scratch:

- ГиП (голова и плечи)          = частный случай Переприора (стр.61)  -> pp.py
- Двойное/тройное дно-вершина   = накопление, граница которого = слом ПП (стр.62)
                                   -> accumulation touches at a boundary + pp.py
- Вымпел/клин/треугольник       = сужающееся накопление (стр.57-60)   -> narrowing range
- Флаг                          = коррекционный канал после импульса (стр.56)

A figure normally does NOT create a new candidate and NEVER gates one — it only
tags an existing candidate's ``summary["pattern"]`` (course: фигуры это контекст
входа от уровня/ПП, доп-фактор). Falls back to the v1 squeeze proxy when no
richer figure is recognized.

ONE deliberate exception (explicit user decision, 2026-07-15): the вымпел 6-е
касание entry (course стр.57: «не успели взять от уровня, то ждем 6 касание»
+ доливка на расширение; стоп за всю структуру с запасом 1-3%, стр.58) IS a candidate —
implemented as ``orchestrator._figure_pennant_candidate``, which reuses this
module's ``_narrowing`` detector. Everything else here remains tag-only.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.confluence import _bb_width_pctile, _closes
from hunt_core.prizrak.pp import _pivots, detect_pereprior, pp_confirmed
from hunt_core.prizrak.structure import bars_from_ohlcv

# Клин vs вымпел: насколько должна уехать середина ОТНОСИТЕЛЬНО РАЗМАХА самой фигуры,
# чтобы сужение считалось наклонным (стр.60 «как флаг, только в сужение»), а не
# симметричным (стр.57-58). Нормировка именно на размах, а не на цену: доля цены — это
# мера волатильности, и порог в % цены признаёт клином всё подряд на старших ТФ (замер:
# при пороге 2% цены клином объявлялись 41% сужений на 15м и 95% на 1Д — ровно по росту
# среднего размаха фигуры с 10% до 73%, а не по наклону).
_WEDGE_MID_DRIFT = 0.25
# ГиП: насколько плечи могут разойтись по высоте и всё ещё быть «плечами» (стр.61).
_SHOULDER_TOL = 0.15


def _narrowing(ohlcv: list[list[float]], *, window: int = 40) -> bool:
    """Range in the second half of the window is materially tighter than the first
    (вымпел/клин/треугольник — сужение)."""
    tail = ohlcv[-window:]
    if len(tail) < 20:
        return False
    half = len(tail) // 2

    def _rng(rows: list[list[float]]) -> float:
        hi = max(r[2] for r in rows)
        lo = min(r[3] for r in rows)
        mid = (hi + lo) / 2.0
        return (hi - lo) / mid if mid > 0 else 0.0

    first, second = _rng(tail[:half]), _rng(tail[half:])
    return first > 0 and second <= first * 0.6


def _wedge(ohlcv: list[list[float]], *, window: int = 40) -> bool:
    """A narrowing range whose MIDLINE also travels — клин (стр.60: «Клин — выглядит как
    флаг, только в сужение»), as opposed to a вымпел/треугольник, which narrows around a
    roughly level mid (стр.57-58, incl. «с поджатием» and «с равными границами»).

    The split matters beyond the label: стр.57-58 give the вымпел the «6-е касание» entry,
    while стр.60 gives the клин a different one entirely — «вход на тесте лонг уровня, или
    вход на тесте слома тенденции». ``orchestrator._figure_pennant_candidate`` keys off
    ``_narrowing`` alone, so without this check a клин was handed the вымпел's rule.
    """
    tail = ohlcv[-window:]
    if len(tail) < 20:
        return False
    half = len(tail) // 2

    def _mid(rows: list[list[float]]) -> float:
        return (max(r[2] for r in rows) + min(r[3] for r in rows)) / 2.0

    first = tail[:half]
    span = max(r[2] for r in first) - min(r[3] for r in first)
    if span <= 0:
        return False
    return abs(_mid(tail[half:]) - _mid(first)) / span >= _WEDGE_MID_DRIFT


def _head_and_shoulders(bars: list[dict[str, float]], direction: str) -> bool:
    """True head-and-shoulders geometry (стр.61), not merely "a ПП happened".

    Short: three highs where the middle (head) tops both shoulders and the shoulders sit
    at a comparable height. Long: the mirror on lows (перевёрнутый ГиП). The course calls
    ГиП «фактически частный случай "Переприора"» — a SUBSET of ПП — so tagging every
    confirmed ПП «гип» inverted the implication and named a shape nothing had checked.
    """
    pivots = _pivots(bars)
    kind = "high" if direction == "short" else "low"
    pts = [p[2] for p in pivots if p[1] == kind]
    if len(pts) < 3:
        return False
    left, head, right = pts[-3], pts[-2], pts[-1]
    if left <= 0 or right <= 0:
        return False
    beats = (head > left and head > right) if direction == "short" else (head < left and head < right)
    if not beats:
        return False
    return abs(right - left) / left <= _SHOULDER_TOL


def _flag(ohlcv: list[list[float]], *, impulse_window: int = 8, pullback_window: int = 8) -> bool:
    """Strong impulse (>=8% over impulse_window) then a tight counter pullback
    (range <= 1/3 of the impulse) — флаг."""
    if len(ohlcv) < impulse_window + pullback_window + 1:
        return False
    imp = ohlcv[-(impulse_window + pullback_window):-pullback_window]
    pull = ohlcv[-pullback_window:]
    if not imp or not pull or imp[0][4] <= 0:
        return False
    imp_move = abs(imp[-1][4] - imp[0][4]) / imp[0][4]
    pull_hi = max(r[2] for r in pull)
    pull_lo = min(r[3] for r in pull)
    pull_rng = (pull_hi - pull_lo) / pull_lo if pull_lo else 0.0
    return imp_move >= 0.08 and pull_rng <= imp_move / 3.0


def tag_figure(summary: dict[str, Any], *, ohlcv: list[list[float]], cfg: PrizrakConfig | None = None) -> dict[str, Any]:
    """Add a ``pattern`` field if a course figure is present. Tagging only.

    Order is most-specific-first, and that ordering is load-bearing. Все три структурные
    фигуры курса — подмножества ПП, поэтому широкая ПП-ветка, стоящая выше, забирает их
    случаи себе. До 2026-07-17 она стояла первой и метила ЛЮБОЙ подтверждённый ПП как
    «гип», из-за чего двойное дно/вершина не выставлялись НИКОГДА в том самом случае,
    который стр.62 называет их определением («граница накопления являлась сломом
    структуры - "переприором"»): раз есть ПП — ветка уже занята.
    """
    cfg = cfg or PrizrakConfig.load()
    direction = summary.get("action")
    zone = summary.get("zone") if isinstance(summary.get("zone"), dict) else {}
    bars = bars_from_ohlcv(ohlcv) if ohlcv else []

    figure: str | None = None

    # Все структурные фигуры требуют ПОДТВЕРЖДЁННОГО ПП (стр.55: неподтверждённый пробой
    # — это прокол, и называть его «сломом» значит утверждать разворот, которого курс не
    # признал). Считаем один раз — дальше ветки только уточняют, ЧТО это за слом.
    pp_ok = False
    if bars and direction in ("long", "short"):
        pp_ok = pp_confirmed(
            detect_pereprior(bars), direction=direction, min_bodies=cfg.trap_proboy_min_bodies,
        )

    # 1. Двойное/тройное дно-вершина (стр.62): «по сути - это просто накопление, но
    #    граница накопления являлась сломом структуры - "переприором"». Т.е. накопление
    #    с 2-3 касаниями границы И ПП на ней — самая специфичная из ПП-фигур.
    if pp_ok and zone:
        lo_t = int(zone.get("lo_touches") or 0)
        hi_t = int(zone.get("hi_touches") or 0)
        if direction == "long" and lo_t in (2, 3):
            figure = f"{'двойное' if lo_t == 2 else 'тройное'} дно"
        elif direction == "short" and hi_t in (2, 3):
            figure = f"{'двойная' if hi_t == 2 else 'тройная'} вершина"

    # 2. ГиП (стр.61) — только при РЕАЛЬНОЙ геометрии плеч/головы, а не «раз ПП, значит
    #    ГиП»: курс называет его ЧАСТНЫМ случаем ПП, т.е. подмножеством.
    if figure is None and pp_ok and _head_and_shoulders(bars, str(direction)):
        figure = f"гип ({'лонг' if direction == 'long' else 'шорт'})"

    # 3. Слом структуры без более узкой геометрии — честное общее имя для ПП.
    if figure is None and pp_ok:
        figure = f"слом структуры (ПП {'лонг' if direction == 'long' else 'шорт'})"

    # 4. Вымпел (стр.57-58) vs клин (стр.60): оба «сужение», но правила входа разные.
    if figure is None and ohlcv and _narrowing(ohlcv):
        figure = "клин (сужение с наклоном)" if _wedge(ohlcv) else "вымпел/треугольник (сужение)"

    # Флаг = импульс + тугой откат (стр.56)
    if figure is None and ohlcv and _flag(ohlcv):
        figure = "флаг"

    # v1 fallback: BB-squeeze proxy
    if figure is None and ohlcv:
        pctile = _bb_width_pctile(_closes(ohlcv))
        if pctile is not None and pctile <= cfg.squeeze_bb_pctile_max:
            figure = "сужение (squeeze proxy)"
            summary["pattern_bb_pctile"] = round(pctile, 3)

    if figure is not None:
        summary["pattern"] = figure
        summary["geometry_confidence"] = min(1.0, summary.get("geometry_confidence", 0.5) + 0.05)
    return summary


# Back-compat alias — orchestrator historically imported this name.
def tag_squeeze_pattern(summary: dict[str, Any], *, ohlcv: list[list[float]], cfg: PrizrakConfig | None = None) -> dict[str, Any]:
    return tag_figure(summary, ohlcv=ohlcv, cfg=cfg)


__all__ = ["tag_figure", "tag_squeeze_pattern"]
