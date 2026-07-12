"""Фигуры (v2) — geometric pattern tagging, course chapter «Фигуры».

Every figure in the course is a special case of primitives already implemented,
so this reuses them rather than re-recognizing geometry from scratch:

- ГиП (голова и плечи)          = частный случай Переприора (стр.61)  -> pp.py
- Двойное/тройное дно-вершина   = накопление, граница которого = слом ПП (стр.62)
                                   -> accumulation touches at a boundary + pp.py
- Вымпел/клин/треугольник       = сужающееся накопление (стр.57-60)   -> narrowing range
- Флаг                          = коррекционный канал после импульса (стр.56)

A figure NEVER creates a new candidate and NEVER gates one — it only tags an
existing candidate's ``summary["pattern"]`` (course: фигуры это контекст входа
от уровня/ПП, доп-фактор). Falls back to the v1 squeeze proxy when no richer
figure is recognized.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.confluence import _bb_width_pctile, _closes
from hunt_core.prizrak.pp import detect_pereprior, pp_confirmed
from hunt_core.prizrak.structure import bars_from_ohlcv


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
    Priority: structural (ГиП / двойное-тройное) first, then geometric
    (вымпел/клин, флаг), then the v1 squeeze proxy.
    """
    cfg = cfg or PrizrakConfig.load()
    direction = summary.get("action")
    zone = summary.get("zone") if isinstance(summary.get("zone"), dict) else {}
    bars = bars_from_ohlcv(ohlcv) if ohlcv else []

    figure: str | None = None

    # ГиП = частный случай ПП (стр.61). Only tag it once the ПП is confirmed by 2-3
    # closed bodies (стр.55) — an unconfirmed break is a прокол, and calling that a
    # "слом структуры" asserts a reversal the course says has not happened yet.
    if bars and direction in ("long", "short"):
        pp = detect_pereprior(bars)
        if pp_confirmed(pp, direction=direction, min_bodies=cfg.trap_proboy_min_bodies):
            figure = f"гип/слом структуры (ПП {'лонг' if direction == 'long' else 'шорт'})"

    # Двойное/тройное дно-вершина = 2-3 касания границы накопления (стр.62)
    if figure is None and zone:
        lo_t = int(zone.get("lo_touches") or 0)
        hi_t = int(zone.get("hi_touches") or 0)
        if direction == "long" and lo_t in (2, 3):
            figure = f"{'двойное' if lo_t == 2 else 'тройное'} дно"
        elif direction == "short" and hi_t in (2, 3):
            figure = f"{'двойная' if hi_t == 2 else 'тройная'} вершина"

    # Вымпел/клин/треугольник = сужение (стр.57-60)
    if figure is None and ohlcv and _narrowing(ohlcv):
        figure = "вымпел/клин (сужение)"

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
