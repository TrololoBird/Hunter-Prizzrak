"""ПП needs 2-3 ПОЛНЫХ тела beyond its level before it is a слом.

Course, с. 55: «Уровень ПП ... требует подтверждения (то есть закрытия под/над уровнем
2-3 полных тел свечей ЭТОГО ТФ). Если же цена заходит под уровень и возвращается той же
или следующей свечой ... это просто прокол БЕЗ подтверждения, не берем позицию».

«Полное тело», а не просто закрытие — стр.6 (плашка): «цена не уходит за уровень ЦЕЛЫМИ
СВЕЧАМИ»; стр.52 (выноска KNC): «нет закрепления полными свечами под ПП». Свеча, которая
пробивает уровень, открывается по эту сторону и закрывается за ним — её тело стоит
верхом на уровне и полным не является. До 2026-07-17 код считал только `close`, поэтому
такая свеча шла в зачёт и подтверждение наступало на бар раньше.
"""

from __future__ import annotations

from typing import Any

from hunt_core.prizrak.figures import tag_figure
from hunt_core.prizrak.pp import confirmation_bodies, detect_pereprior, pp_confirmed

MIN_BODIES = 2
_LEVEL = 115.0


def _bar(o: float, h: float, low: float, c: float) -> dict[str, float]:
    return {"open": o, "high": h, "low": low, "close": c}


def _ohlcv(bars: list[dict[str, float]]) -> list[list[float]]:
    return [[i, b["open"], b["high"], b["low"], b["close"], 100.0] for i, b in enumerate(bars)]


def _early_long_pp(bodies_beyond: int, *, straddle_break: bool = True) -> list[dict[str, float]]:
    """low -> high -> no lower low -> break above the 115 high pivot.

    The break is drawn the way a real one looks: one candle that OPENS under the level
    and CLOSES over it (a straddling body — не «полное тело»), then ``bodies_beyond``
    candles sitting wholly above it.
    """
    bars = [_bar(p, p + 1, p - 1, p) for p in (110, 108, 106, 104)]
    bars.append(_bar(100, 101, 98, 100))  # low pivot
    bars.append(_bar(101, 102, 100, 101))
    bars += [_bar(p, p + 1, p - 1, p) for p in (104, 107, 110)]
    bars.append(_bar(112, _LEVEL, 111, 112))  # high pivot, level = 115
    bars.append(_bar(111, 112, 110, 111))
    bars += [_bar(p, p + 1, p - 1, p) for p in (108, 106, 105)]
    if straddle_break:
        bars.append(_bar(114, 117, 113, 116))  # тело верхом на уровне — не «полное»
    for i in range(bodies_beyond):
        c = 117.0 + i
        bars.append(_bar(c - 0.5, c + 1, c - 1, c))  # тело целиком выше 115
    return bars


def test_straddling_break_candle_is_not_a_full_body() -> None:
    """Свеча, открывшаяся под уровнем и закрывшаяся над ним, телом стоит по обе стороны —
    стр.55 требует ПОЛНЫХ тел, стр.6 «цена не уходит за уровень ЦЕЛЫМИ СВЕЧАМИ».

    Это и есть вся разница строгого чтения: раньше такая свеча засчитывалась.
    """
    only_straddle = [_bar(114, 117, 113, 116)]
    assert confirmation_bodies(only_straddle, level=_LEVEL, side="long") == 0

    whole = [_bar(116.5, 118, 116, 117)]
    assert confirmation_bodies(whole, level=_LEVEL, side="long") == 1


def test_straddling_break_candle_is_not_a_full_body_short_side() -> None:
    only_straddle = [_bar(116, 117, 113, 114)]  # открылась над, закрылась под
    assert confirmation_bodies(only_straddle, level=_LEVEL, side="short") == 0

    whole = [_bar(113.5, 114, 112, 113)]
    assert confirmation_bodies(whole, level=_LEVEL, side="short") == 1


def test_body_touching_the_level_does_not_count() -> None:
    """Граница: open РОВНО на уровне — тело не «за уровнем», оно НА нём."""
    on_level = [_bar(_LEVEL, 117, 114, 116)]
    assert confirmation_bodies(on_level, level=_LEVEL, side="long") == 0


def test_single_full_body_beyond_level_is_a_prokol_not_a_slom() -> None:
    pp = detect_pereprior(_early_long_pp(1))
    assert pp["pp_early_long"] is True
    assert pp["pp_early_long_bodies"] == 1  # straddle-свеча не в счёт
    assert pp_confirmed(pp, direction="long", min_bodies=MIN_BODIES) is False


def test_two_full_bodies_confirm_the_pp() -> None:
    pp = detect_pereprior(_early_long_pp(2))
    assert pp["pp_early_long_bodies"] == 2
    assert pp_confirmed(pp, direction="long", min_bodies=MIN_BODIES) is True


def test_break_candle_alone_never_confirms() -> None:
    """Регрессия на close-only: одна пробивающая свеча + ноль полных тел = не слом.
    Раньше straddle-свеча давала bodies=1, и уже вторая такая подтверждала ПП."""
    pp = detect_pereprior(_early_long_pp(0))
    assert pp["pp_early_long_bodies"] == 0
    assert pp_confirmed(pp, direction="long", min_bodies=MIN_BODIES) is False


def test_pp_confirmed_is_false_when_no_pp_was_detected() -> None:
    assert pp_confirmed({}, direction="long", min_bodies=MIN_BODIES) is False


def _summary() -> dict[str, Any]:
    return {"action": "long", "zone": {}}


def test_figure_is_not_tagged_as_slom_on_an_unconfirmed_break() -> None:
    out = tag_figure(_summary(), ohlcv=_ohlcv(_early_long_pp(1)))
    assert "слом структуры" not in str(out.get("pattern") or "")


def test_figure_is_tagged_as_slom_once_confirmed() -> None:
    out = tag_figure(_summary(), ohlcv=_ohlcv(_early_long_pp(2)))
    assert "слом структуры" in str(out["pattern"])
