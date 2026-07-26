"""Внутридневной горизонт и коридор между встречными уровнями (разбор ASTR, 2026-07-25).

Автор работает на ТРЁХ горизонтах сразу и называет их явно: «уровень поддержки 4ч ТФ 0.005059»,
«ближайший уровень сопротивления 0.005170», «лонг от уровня поддержки 1ч ТФ». На кадре 27 видео
он ПЕРЕКЛЮЧАЕТСЯ на 15-минутный график и размечает 0.005177/0.005165 именно там.

Без внутридневного горизонта этот уровень физически не выражался: на 4ч его нет вовсе, на 1ч он
размазан в зону шириной 3.71%, и только 15м даёт 0.005150–0.005186 — 0.70% и 56 касаний.

Второе: «есть уровень» ≠ «есть сделка». Уровень 0.005059 у него отличный (204 касания на 700
барах), и он всё равно отказался — «процент движения между уровнями слишком небольшой».
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.format_post import _TIGHT_HEADROOM_PCT, _headroom_line
from hunt_core.prizrak.setups import _HORIZONS, _headroom


def _z(lo: float, hi: float) -> dict[str, Any]:
    return {"lo": lo, "hi": hi, "poc": (lo + hi) / 2, "entry": lo, "by_fact": False}


def test_intraday_horizon_is_declared_first_and_covers_15m() -> None:
    """★ 15м обязан быть в списке — на нём живёт его «ближайшее сопротивление»."""
    names = [n for n, _ in _HORIZONS]
    assert "intraday" in names, "без внутридневного горизонта уровень 0.005170 не выражается"
    assert names.index("intraday") < names.index("local"), "младший ТФ читается первым"
    tfs = dict(_HORIZONS)["intraday"]
    assert tfs[0] == "15m", "первичный внутридневной ТФ — тот, на который он переключился в видео"


def test_corridor_measures_between_nearest_opposing_levels() -> None:
    """Коридор — это ширина между ближайшей поддержкой и ближайшим сопротивлением."""
    hz = {"intraday": {"tf": "15m", "perezakup": _z(0.005064, 0.005092),
                       "short": [_z(0.005142, 0.005186)]}}
    hr = _headroom(hz, price=0.005118)
    assert hr is not None
    assert hr["down_price"] == 0.005092
    assert hr["up_price"] == 0.005142
    assert abs(hr["width_pct"] - 0.98) < 0.02


def test_astr_case_is_flagged_tight() -> None:
    """★ Случай, ради которого это писалось: он сказал «пропущу», карточка обязана сказать «тесно»."""
    hz = {"intraday": {"tf": "15m", "perezakup": _z(0.005064, 0.005092),
                       "short": [_z(0.005142, 0.005186)]}}
    line = _headroom_line({"headroom": _headroom(hz, price=0.005118)}, 0.005118)
    # «коридор» переименован в «ход»: у автора это две РАЗНЫЕ линейки — ход между встречными
    # уровнями и толщина самой зоны входа (разбор BTC 1ч 2026-07-25, кадры f_0151 и f_0139).
    assert "ход" in line and "тесно" in line


def test_roomy_corridor_is_not_flagged() -> None:
    """Метка обязана молчать там, где ход есть — иначе она ничего не значит."""
    hz = {"local": {"tf": "4h", "perezakup": _z(0.0090, 0.0095), "short": [_z(0.0140, 0.0150)]}}
    hr = _headroom(hz, price=0.0100)
    assert hr is not None and hr["width_pct"] > _TIGHT_HEADROOM_PCT
    assert "тесно" not in _headroom_line({"headroom": hr}, 0.0100)


def test_one_sided_corridor_is_silent_not_half_printed() -> None:
    """I-6: только поддержка и никакого сопротивления — это не коридор, ширину брать неоткуда."""
    hz = {"local": {"tf": "4h", "perezakup": _z(0.0090, 0.0095)}}
    hr = _headroom(hz, price=0.0100)
    assert hr is not None and "width_pct" not in hr
    assert _headroom_line({"headroom": hr}, 0.0100) == ""


def test_no_zones_yields_none_not_zero() -> None:
    """Нет встречных уровней ⇒ None, а не сфабрикованный 0.0 (I-6)."""
    assert _headroom({}, price=0.01) is None
    assert _headroom({"local": {"tf": "4h"}}, price=0.01) is None
    assert _headroom({"local": {"tf": "4h", "short": [_z(0.02, 0.03)]}}, price=0.0) is None


def test_price_inside_a_zone_uses_both_its_edges() -> None:
    """Цена ВНУТРИ зоны: границами коридора становятся кромки самой этой зоны."""
    hz = {"local": {"tf": "4h", "perezakup": _z(0.0098, 0.0102)}}
    hr = _headroom(hz, price=0.0100)
    assert hr is not None
    assert hr["down_price"] == 0.0098 and hr["up_price"] == 0.0102
