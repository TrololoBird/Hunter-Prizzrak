"""Фигуры курса — подмножества ПП, и порядок веток решает, какая из них вообще достижима.

Стр.62: «Двойное дно, Тройное дно / Двойная вершина, Тройная вершина. По сути - это просто
накопление, но граница накопления являлась сломом структуры - "переприором"».
Стр.61: «Фигура Голова и плечи(ГИП) - фактически ЧАСТНЫЙ СЛУЧАЙ "Переприора"».
Стр.60: «Клин - выглядит как флаг, только в сужение. Торговля: вход на тесте лонг уровня,
или вход на тесте слома тенденции» — НЕ «6-е касание», это правило вымпела (стр.57-58).

До 2026-07-17 широкая ПП-ветка стояла первой и метила любой подтверждённый ПП как «гип»:
дно/вершина не выставлялись никогда именно там, где стр.62 их определяет.
"""

from __future__ import annotations

from typing import Any

from hunt_core.prizrak.figures import _head_and_shoulders, _narrowing, _wedge, tag_figure
from hunt_core.prizrak.structure import bars_from_ohlcv


def _bar(o: float, h: float, low: float, c: float) -> list[float]:
    return [0.0, o, h, low, c, 100.0]


def _confirmed_long_pp() -> list[list[float]]:
    """low -> high(115) -> хай не обновлён -> пробой вверх полными телами."""
    rows = [_bar(p, p + 1, p - 1, p) for p in (110, 108, 106, 104)]
    rows.append(_bar(100, 101, 98, 100))
    rows.append(_bar(101, 102, 100, 101))
    rows += [_bar(p, p + 1, p - 1, p) for p in (104, 107, 110)]
    rows.append(_bar(112, 115, 111, 112))  # хай = 115
    rows.append(_bar(111, 112, 110, 111))
    rows += [_bar(p, p + 1, p - 1, p) for p in (108, 106, 105)]
    rows.append(_bar(114, 117, 113, 116))  # пробивающая свеча (straddle)
    for c in (117.0, 118.0):
        rows.append(_bar(c - 0.5, c + 1, c - 1, c))  # полные тела выше 115
    return rows


def test_double_bottom_is_reachable_when_a_pp_exists() -> None:
    """Регрессия: ровно курсовой случай стр.62 — накопление, чья граница = ПП. Раньше
    ГиП-ветка стояла выше и забирала его себе, поэтому «двойное дно» было недостижимо."""
    out = tag_figure(
        {"action": "long", "zone": {"lo_touches": 2, "hi_touches": 4}},
        ohlcv=_confirmed_long_pp(),
    )
    assert out["pattern"] == "двойное дно"


def test_triple_bottom_label() -> None:
    out = tag_figure(
        {"action": "long", "zone": {"lo_touches": 3, "hi_touches": 4}},
        ohlcv=_confirmed_long_pp(),
    )
    assert out["pattern"] == "тройное дно"


def test_plain_pp_is_not_called_gip() -> None:
    """Без геометрии плеч это просто слом структуры. Курс: ГиП — ЧАСТНЫЙ случай ПП,
    а не синоним; ярлык «гип» на каждом ПП переворачивал импликацию."""
    out = tag_figure({"action": "long", "zone": {}}, ohlcv=_confirmed_long_pp())
    assert out["pattern"] == "слом структуры (ПП лонг)"
    assert "гип" not in out["pattern"]


def test_unconfirmed_pp_tags_no_structural_figure() -> None:
    """Стр.55: неподтверждённый пробой — прокол, не слом."""
    rows = _confirmed_long_pp()[:-2]  # убрать полные тела за уровнем
    out = tag_figure({"action": "long", "zone": {"lo_touches": 2}}, ohlcv=rows)
    assert "дно" not in str(out.get("pattern") or "")
    assert "слом" not in str(out.get("pattern") or "")


def _peaks(vals: list[float]) -> list[dict[str, float]]:
    """Чистые хай-пивоты (3-барный фрактал pp._pivots): подъём → пик → спад."""
    rows: list[list[float]] = []
    for v in vals:
        for o in (8, 6, 4):
            rows.append(_bar(v - o, v - o + 1, v - o - 1, v - o))
        rows.append(_bar(v - 1, v, v - 2, v - 1))  # пик: high = v
        for o in (4, 6):
            rows.append(_bar(v - o, v - o + 1, v - o - 1, v - o))
    return bars_from_ohlcv(rows)


def _troughs(vals: list[float]) -> list[dict[str, float]]:
    """Зеркало: чистые лой-пивоты для перевёрнутого ГиП."""
    rows: list[list[float]] = []
    for v in vals:
        for o in (8, 6, 4):
            rows.append(_bar(v + o, v + o + 1, v + o - 1, v + o))
        rows.append(_bar(v + 1, v + 2, v, v + 1))  # впадина: low = v
        for o in (4, 6):
            rows.append(_bar(v + o, v + o + 1, v + o - 1, v + o))
    return bars_from_ohlcv(rows)


def test_head_and_shoulders_geometry_requires_shoulders() -> None:
    """Голова выше обоих плеч, плечи сопоставимы по высоте (стр.61)."""
    assert _head_and_shoulders(_peaks([100.0, 112.0, 101.0]), "short") is True
    assert _head_and_shoulders(_peaks([100.0, 112.0, 140.0]), "short") is False  # правое выше головы
    assert _head_and_shoulders(_peaks([100.0, 101.0, 102.0]), "short") is False  # головы нет


def test_inverted_head_and_shoulders_on_lows() -> None:
    """Перевёрнутый ГиП строится по ЛОЯМ (стр.61 — лонговый пример на графике)."""
    assert _head_and_shoulders(_troughs([100.0, 88.0, 99.0]), "long") is True
    assert _head_and_shoulders(_troughs([100.0, 88.0, 60.0]), "long") is False


def test_shoulders_must_be_comparable_in_height() -> None:
    """Плечи, разъехавшиеся по высоте, — это не ГиП, а просто три хая."""
    assert _head_and_shoulders(_peaks([100.0, 130.0, 60.0]), "short") is False


def _narrow(*, drift: float) -> list[list[float]]:
    """Сужающийся диапазон; drift — насколько уезжает середина за бар.

    Порог `_wedge` нормирован на РАЗМАХ фигуры (а не на цену), поэтому «наклон» здесь
    измеряется в долях диапазона: при размахе ~20 снос середины на ~8 (drift 0.4 × 20
    баров) — это клин, а на ~3 (drift 0.15) — всё ещё симметричный вымпел.
    """
    rows: list[list[float]] = []
    for i in range(40):
        half = 10.0 * (1 - i / 45)          # сужение
        mid = 100.0 + drift * i
        rows.append(_bar(mid, mid + half, mid - half, mid))
    return rows


def test_wedge_is_distinguished_from_pennant() -> None:
    """Вымпел сужается вокруг ровной середины (стр.57-58), клин — с наклоном (стр.60)."""
    flat, sloped = _narrow(drift=0.0), _narrow(drift=0.4)
    assert _narrowing(flat) and _narrowing(sloped)
    assert _wedge(flat) is False
    assert _wedge(sloped) is True
    assert tag_figure({"action": "long", "zone": {}}, ohlcv=flat)["pattern"] == "вымпел/треугольник (сужение)"
    assert tag_figure({"action": "long", "zone": {}}, ohlcv=sloped)["pattern"] == "клин (сужение с наклоном)"


def test_pennant_6touch_entry_never_fires_on_a_wedge() -> None:
    """Стр.60 даёт клину СВОЁ правило входа («на тесте уровня или слома тенденции»), а
    «6-е касание + доливка» — правило вымпела (стр.57-58). `_figure_pennant_candidate`
    стоял на общем `_narrowing` и выдавал бы клину чужое правило."""
    from hunt_core.prizrak.config import PrizrakConfig
    from hunt_core.prizrak.orchestrator import _figure_pennant_candidate

    sloped = _narrow(drift=0.4)
    sig: Any = _figure_pennant_candidate(
        ohlcv=sloped, ohlcv_by_tf={"1h": sloped}, price=sloped[-1][4], tf="1h",
        tier_name="meso", cfg=PrizrakConfig.load(), htf_bias={"bias": "long"},
    )
    assert sig is None
