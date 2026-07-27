"""Гард: подвинутый стоп проверяется только против движения ПОСЛЕ сдвига.

Дефект был подтверждён прогоном настоящего кода. `_bar_extremes` копил экстремумы за ВСЮ
жизнь сделки, а сдвинутый стоп сверялся с замороженным минимумом — то есть срабатывал на
следующем же опросе:

    тик 0  px=100.00  lo=100.00  stop= 94.0
    тик 1  px=103.00  lo=100.00  stop=101.5   трейл сдвинулся
    тик 2  px=106.00  lo=100.00  stop=104.5   TP1 защёлкнут
    тик 3  px=105.50  lo=100.00  stop=104.5 → ЗАКРЫТ trailing_stop_profit +5.70 → «win»

Рынок ниже 104.5 после сдвига не торговался ни разу. Следствия: раннера не существовало
вовсе, TP2 через этот путь был недостижим, а выход книжился по цене стопа — которая для лонга
стоит ВЫШЕ рынка, — то есть результат завышался систематически и попадал в победы.

Тест держит ОБА направления ошибки: ложное срабатывание и пропуск настоящего стопа.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hunt_core.track._evaluate_levels import _bar_extremes
from hunt_core.track._trailing import reset_stop_window

_ROW: dict[str, Any] = {"timeframes": {}}


def _fresh(direction: str = "long") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "symbol": "TESTUSDT", "direction": direction, "opened_at": now,
        "entry_lo": 100.0, "entry_hi": 100.0, "stop_loss": 94.0,
    }


def _tick(active: dict[str, Any], px: float, stop: float) -> tuple[float, float]:
    if stop != active["stop_loss"]:
        active["stop_loss"] = stop
        reset_stop_window(active, price=0.0)
    return _bar_extremes(_ROW, active, price=px, ts=datetime.now(UTC))


def test_moved_stop_does_not_fire_on_pre_move_low() -> None:
    """Точный сценарий из репродукции: цена только росла — ни одного срабатывания."""
    active = _fresh()
    for px, stop in ((100.0, 94.0), (103.0, 101.5), (106.0, 104.5), (105.5, 104.5)):
        _hi, lo = _tick(active, px, stop)
        assert lo > stop, (
            f"стоп {stop} сработал о минимум {lo}, которого после сдвига не было: "
            "окно SL/TP снова считается за всю жизнь сделки"
        )


def test_real_pullback_below_moved_stop_still_fires() -> None:
    """Обратная ошибка не менее опасна: настоящий откат обязан закрывать позицию."""
    active = _fresh()
    _tick(active, 100.0, 94.0)
    _tick(active, 106.0, 104.5)
    _hi, lo = _bar_extremes(_ROW, active, price=104.0, ts=datetime.now(UTC))
    assert lo <= 104.5, "настоящий проход ниже стопа пропущен — сеть перестала ловить"


def test_short_side_is_symmetric() -> None:
    """Шорт: подвинутый вниз стоп не должен ловить максимум, бывший до сдвига."""
    active = _fresh("short")
    active["stop_loss"] = 106.0
    for px, stop in ((100.0, 106.0), (97.0, 98.5), (94.0, 95.5), (94.5, 95.5)):
        hi, _lo = _tick(active, px, stop)
        assert hi < stop, f"шорт-стоп {stop} сработал о максимум {hi} из времени до сдвига"


def test_lifetime_extremes_survive_for_mfe() -> None:
    """Пожизненные экстремумы обязаны остаться — на них считается MFE."""
    active = _fresh()
    _tick(active, 100.0, 94.0)
    _tick(active, 106.0, 104.5)   # сброс окна
    _tick(active, 105.5, 104.5)
    assert active["extreme_lo"] == 100.0, "жизненный минимум затёрт сбросом окна SL/TP"
    assert active["extreme_hi"] == 106.0
    assert active["sl_window_lo"] > active["extreme_lo"], "окно SL обязано быть уже жизненного"


def test_reset_without_price_clears_rather_than_seeds_zero() -> None:
    """Нет цены — ключи удаляются, а не засеваются нулём (I-6: ноль тут был бы фальшью)."""
    active = _fresh()
    _bar_extremes(_ROW, active, price=100.0, ts=datetime.now(UTC))
    assert "sl_window_lo" in active
    reset_stop_window(active, price=0.0)
    assert "sl_window_lo" not in active and "sl_window_hi" not in active
