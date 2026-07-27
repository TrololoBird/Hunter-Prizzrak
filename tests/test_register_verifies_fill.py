"""Гард: трекер не имеет права записать неисполненную лимитку как открытую позицию.

Дефект (замер на живых свечах 2026-07-27, `scripts/verify_entry_fill.py`, 283 записи).
`register_signal_open` принимал `delivery_tier` от вызывающего КАК УТВЕРЖДЕНИЕ и не проверял
его. Хуже: дефолт был `setup.get("delivery_tier") or "triggered"` — отсутствие ключа тоже
читалось как «исполнено», то есть неизвестность трактовалась в самую выгодную сторону (I-6).

Почему это сразу становится прибылью: ниже по коду `extreme_hi`/`extreme_lo` засеваются
ТЕКУЩЕЙ ЦЕНОЙ, а PnL считается от кромки ЗОНЫ. Если рынок далеко от зоны, вся дистанция
мгновенно оказывается MFE — трейл взводится, `auto_resolve` книжит победу по ордеру, которого
не было.

ЗАМЕР: **64 записи из 283 (22.6%)** имеют вход, по которому рынок не торговал ни разу за час
до регистрации; они несут **+1589.9%** записанного pnl — больше, чем весь плюс леджера
(+1575.2%). Худшее: TLMUSDT — вход 0.000823 при рынке 0.002772, разрыв +236.8%.

Правка сделана в ОБЩЕМ СЛОЕ намеренно. Дефект проявился у обеих полос —
`deliver/manipulation_delivery.py` захардкодил тир константой, а
`prizrak/orchestrator.py::_zone_edge_candidate` ставит `activation="in_entry_zone"`
безусловно, пуская цену на 35% вглубь зоны при полосе ±0.2%. Проверка на приёмнике закрывает
класс для ЛЮБОГО продюсера, не трогая логику ни одного модуля.

⚠ `prizrak/zone_watch.py` тир тоже хардкодит, и это БЕЗОПАСНО: `_entry_band` в обеих ветках
регистрирует худшую кромку (лонг → `hi`, шорт → `lo`), а гейт входа гарантирует `price <= hi`
/ `price >= lo`, поэтому мгновенный MFE там математически нулевой.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hunt_core.track import tracker
from hunt_core.track._tracker_fsm import SignalPhase, initial_signal_phase


def _register(
    *, price: float, lo: float, hi: float, direction: str = "long",
    tier: str | None = "triggered",
) -> dict:
    state: dict = {"signals": {}}
    setup: dict = {
        "entry_zone": [lo, hi],
        "stop_loss": lo * 0.95 if direction == "long" else hi * 1.05,
        "tp1": hi * 1.1 if direction == "long" else lo * 0.9,
        "phase": "test",
    }
    if tier is not None:
        setup["delivery_tier"] = tier
    tracker.register_signal_open(
        state, symbol="TESTUSDT", direction=direction, price=price,
        setup=setup, lifecycle={}, now=datetime.now(UTC),
    )
    return state["signals"][f"TESTUSDT:{direction}"]


def test_price_inside_zone_is_a_real_fill() -> None:
    """Гард не имеет права глушить настоящее исполнение."""
    sig = _register(price=100.0, lo=99.0, hi=101.0)
    assert sig["delivery_tier"] == "triggered"
    assert sig["phase"] == SignalPhase.TRIGGERED.value


def test_price_outside_zone_is_downgraded_to_armed() -> None:
    """Заявленный `triggered` при цене вне зоны — это не фил."""
    sig = _register(price=120.0, lo=99.0, hi=101.0)
    assert sig["delivery_tier"] == "armed", (
        "неисполненная лимитка снова записана как открытая позиция"
    )


def test_measured_worst_case_is_caught() -> None:
    """Худший измеренный случай: TLMUSDT, рынок на +236.8% выше зоны входа."""
    sig = _register(price=0.002772, lo=0.00080889, hi=0.000823)
    assert sig["delivery_tier"] == "armed"


def test_short_zone_above_market_is_not_a_fill() -> None:
    """Шорт-лимитка стоит ВЫШЕ рынка; пока цена не поднялась — фила нет.

    Измеренный случай: GUAUSDT, зона 1.1599 при рынке 0.05194.
    """
    sig = _register(price=0.05194, lo=1.1599, hi=1.1599, direction="short")
    assert sig["delivery_tier"] == "armed"


def test_missing_tier_key_is_not_a_fill() -> None:
    """Отсутствие ключа — «не исполнено», а не «исполнено»."""
    sig = _register(price=120.0, lo=99.0, hi=101.0, tier=None)
    assert sig["delivery_tier"] == "armed"


def test_fsm_default_is_not_triggered() -> None:
    """Тот же fail-open дефолт жил и в FSM — отдельная точка входа, отдельный гард."""
    assert initial_signal_phase({}) == SignalPhase.ARMED
    assert initial_signal_phase({"delivery_tier": "triggered"}) == SignalPhase.TRIGGERED


def test_phase_and_tier_never_disagree() -> None:
    """Фаза выводится из ПРОВЕРЕННОГО тира, а не из заявленного.

    Иначе в одной записи стояли бы два взаимоисключающих утверждения об одной сделке:
    `delivery_tier="armed"` рядом с `phase="triggered"`.
    """
    sig = _register(price=120.0, lo=99.0, hi=101.0)
    assert sig["delivery_tier"] == "armed"
    assert sig["phase"] == SignalPhase.ARMED.value


def test_extremes_seed_at_price_so_unfilled_entry_cannot_fabricate_mfe() -> None:
    """Причина, по которой понижение тира важно: экстремумы засеваются ЦЕНОЙ.

    Пока позиция `armed`, машина SL/TP до неё не доходит; когда цена реально войдёт в зону,
    `_followups::_maybe_armed_to_triggered` переставит экстремумы по цене фила.
    """
    sig = _register(price=120.0, lo=99.0, hi=101.0)
    assert sig["extreme_hi"] == pytest.approx(120.0)
    assert sig["extreme_lo"] == pytest.approx(120.0)
    assert sig["delivery_tier"] == "armed", (
        "экстремумы на 19% выше зоны при tier=triggered — это и есть фантомный MFE"
    )
