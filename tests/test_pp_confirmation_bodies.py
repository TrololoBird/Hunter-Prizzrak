"""ПП needs 2-3 closed bodies beyond its level before it is a слом.

Course, с. 55: «Уровень ПП ... требует подтверждения (то есть закрытия под/над уровнем
2-3 полных тел свечей ЭТОГО ТФ). Если же цена заходит под уровень и возвращается той же
или следующей свечой ... это просто прокол БЕЗ подтверждения, не берем позицию».
"""

from __future__ import annotations

from typing import Any

from hunt_core.prizrak.figures import tag_figure
from hunt_core.prizrak.pp import detect_pereprior, pp_confirmed

MIN_BODIES = 2


def _bar(o: float, h: float, low: float, c: float) -> dict[str, float]:
    return {"open": o, "high": h, "low": low, "close": c}


def _ohlcv(bars: list[dict[str, float]]) -> list[list[float]]:
    return [[i, b["open"], b["high"], b["low"], b["close"], 100.0] for i, b in enumerate(bars)]


def _early_long_pp(bodies_beyond: int) -> list[dict[str, float]]:
    """low -> high -> no lower low -> break above the high pivot (115) with N closed bodies."""
    bars = [_bar(p, p + 1, p - 1, p) for p in (110, 108, 106, 104)]
    bars.append(_bar(100, 101, 98, 100))  # low pivot
    bars.append(_bar(101, 102, 100, 101))
    bars += [_bar(p, p + 1, p - 1, p) for p in (104, 107, 110)]
    bars.append(_bar(112, 115, 111, 112))  # high pivot, level = 115
    bars.append(_bar(111, 112, 110, 111))
    bars += [_bar(p, p + 1, p - 1, p) for p in (108, 106, 105)]
    for i in range(bodies_beyond):
        c = 116 + i
        bars.append(_bar(c - 1, c + 1, c - 2, c))
    return bars


def test_single_close_beyond_level_is_a_prokol_not_a_slom() -> None:
    pp = detect_pereprior(_early_long_pp(1))
    assert pp["pp_early_long"] is True
    assert pp["pp_early_long_bodies"] == 1
    assert pp_confirmed(pp, direction="long", min_bodies=MIN_BODIES) is False


def test_two_closed_bodies_confirm_the_pp() -> None:
    pp = detect_pereprior(_early_long_pp(2))
    assert pp["pp_early_long_bodies"] == 2
    assert pp_confirmed(pp, direction="long", min_bodies=MIN_BODIES) is True


def test_pp_confirmed_is_false_when_no_pp_was_detected() -> None:
    assert pp_confirmed({}, direction="long", min_bodies=MIN_BODIES) is False


def _summary() -> dict[str, Any]:
    return {"action": "long", "zone": {}}


def test_figure_is_not_tagged_as_slom_on_an_unconfirmed_break() -> None:
    out = tag_figure(_summary(), ohlcv=_ohlcv(_early_long_pp(1)))
    assert "слом структуры" not in str(out.get("pattern") or "")


def test_figure_is_tagged_as_slom_once_confirmed() -> None:
    out = tag_figure(_summary(), ohlcv=_ohlcv(_early_long_pp(2)))
    assert out["pattern"] == "гип/слом структуры (ПП лонг)"
