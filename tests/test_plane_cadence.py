"""Гард измерителя темпа планов (`engine/state.py::PlaneCadence`).

Тест допустим здесь по правилу репозитория: он фиксирует дефект, ИЗМЕРЕННЫЙ на живых данных, и
гоняет код модуля на этих измеренных числах — не считает ничего собственной арифметикой.

Числа взяты из двух живых прогонов 2026-07-26/27, 7 пиннед-символов:
  ДО правки: период планов позиционирования median 377.9 с, p90 379.7 с при бонде 360 с —
             17 сбросов из 17 за бондом, `not_ready` у 57% строк тика.
  ПОСЛЕ:     период РОВНО 300.0 с (дедлайнный сон), бонд 375 с, ratio 1.25 — ни одного нарушения.
  bbo:       темп 5.0 с → 0.1185 с после передачи списка символов в подписку (запас 42×).
"""
from __future__ import annotations

from hunt_core.engine.params import fresh_kline_s
from hunt_core.engine.state import (
    MIN_CADENCE_SAMPLES,
    PlaneCadence,
    PlaneStamp,
    Source,
    SymbolState,
)


def _cad(median_s: float, p90_s: float, bound_s: float | None, samples: int = 32) -> PlaneCadence:
    return PlaneCadence(
        plane="taker_5m", samples=samples, median_s=median_s,
        p90_s=p90_s, max_s=p90_s, bound_s=bound_s,
    )


def test_the_shipped_defect_is_detected() -> None:
    """Состояние ДО правки обязано опознаваться как недостижимый бонд."""
    before = _cad(median_s=377.9, p90_s=379.7, bound_s=360.0)
    assert before.bound_unreachable, "бонд 360 с при периоде 377.9 с обязан быть недостижимым"
    assert before.bound_too_tight


def test_the_fixed_state_is_silent() -> None:
    """Состояние ПОСЛЕ правки не должно предупреждать — иначе это alert fatigue.

    Здесь и была ошибка первой редакции: универсальный порог «бонд ≥ 2×медианы» ругался на
    ratio 1.25 вечно, хотя измеренный джиттер этих планов p90/median = 1.005.
    """
    after = _cad(median_s=300.0, p90_s=300.0, bound_s=375.0)
    assert not after.bound_unreachable
    assert not after.bound_too_tight, "375 с покрывает p90=300 с — предупреждать не о чем"
    assert after.bound_ratio == 1.25


def test_tightness_follows_the_measured_spread_not_a_constant() -> None:
    """При ОДНОЙ и той же медиане вердикт зависит от разброса — в этом весь смысл."""
    steady = _cad(median_s=300.0, p90_s=305.0, bound_s=340.0)
    jittery = _cad(median_s=300.0, p90_s=600.0, bound_s=340.0)
    assert not steady.bound_too_tight
    assert jittery.bound_too_tight, "p90=600 с срывает бонд 340 с штатной вариацией"


def test_a_single_interval_is_not_a_measurement() -> None:
    """`kline.5m` в первом прогоне дал `unreachable` при samples=1 — это был артефакт старта."""
    thin = _cad(median_s=354.5, p90_s=354.5, bound_s=320.0, samples=1)
    assert not thin.measured
    assert not thin.bound_unreachable, "вывод по одной точке делать нельзя"
    assert _cad(354.5, 354.5, 320.0, samples=MIN_CADENCE_SAMPLES).bound_unreachable


def test_bbo_after_the_subscription_fix() -> None:
    """Замеренные 0.1185 с против бонда 5 с — запас 42×, ни одного предупреждения."""
    bbo = PlaneCadence(plane="bbo", samples=32, median_s=0.1185, p90_s=0.30, max_s=1.2, bound_s=5.0)
    assert not bbo.bound_too_tight
    assert bbo.bound_ratio is not None and bbo.bound_ratio > 40


def test_kline_planes_are_silent_on_a_healthy_feed() -> None:
    """Бонд кадров АДДИТИВЕН (`interval + 20 с`) — мультипликативный порог его ломает.

    Дефект, который этот тест закрывает: редакция с порогом «бонд ≥ 1.1×p90» вырождалась в
    `20 < 0.1×interval`, то есть предупреждала на КАЖДОМ ТФ длиннее 200 с — шесть планов из
    семи, вечно, порядка 4000 строк в сутки в логе, который здесь читают как основной способ
    верификации. Ни один тест этого не ловил, потому что кадровых планов в наборе не было.
    """
    for interval_s in (60, 300, 900, 3600, 14400, 86400, 604800):
        bound = fresh_kline_s(float(interval_s))
        cad = PlaneCadence(
            plane=f"kline.{interval_s}s", samples=32, median_s=float(interval_s),
            p90_s=float(interval_s), max_s=float(interval_s) + 5.0, bound_s=bound,
        )
        assert not cad.bound_unreachable, f"{interval_s}s: бонд {bound} обязан покрывать период"
        assert not cad.bound_too_tight, (
            f"{interval_s}s: бонд {bound} против p90 {interval_s} — ложное предупреждение"
        )


def test_kline_plane_that_really_misses_bars_does_warn() -> None:
    """И наоборот: если бары реально приходят через раз, предупреждение обязано быть."""
    cad = PlaneCadence(
        plane="kline.1m", samples=32, median_s=60.0, p90_s=123.7, max_s=180.0,
        bound_s=fresh_kline_s(60.0),
    )
    assert cad.bound_too_tight, "p90=123.7 с срывает бонд 80 с — это надо видеть"
    assert not cad.bound_unreachable, "медиана 60 с в бонд укладывается — не 'недостижим'"


def test_plane_without_bound_yields_no_ratio() -> None:
    """Нет бонда — нет отношения. Не 1.0, не 0.0 (I-6: никакого сфабрикованного числа)."""
    cad = _cad(median_s=10.0, p90_s=12.0, bound_s=None)
    assert cad.bound_ratio is None
    assert not cad.bound_unreachable and not cad.bound_too_tight


def test_liveness_touch_is_not_counted_as_an_update() -> None:
    """`touch_liveness` НЕ должен занижать темп: «сокет жив» ≠ «пришли данные».

    Иначе событийный план (ликвидации) показывал бы темп в доли секунды при полном отсутствии
    событий — и бонд, выведенный из такого темпа, был бы фикцией.
    """
    st = SymbolState("BTCUSDT")
    st.stamp_only("liq", PlaneStamp(Source.WS, 1_000_000, 1_000_000, 60_000))
    for tick in range(1, 40):  # много кадров «поток жив», ни одного события
        st.touch_liveness("liq", 1_000_000 + tick * 100)
    assert "liq" not in st.cadences(), "liveness-касания не образуют темп"

    st.stamp_only("liq", PlaneStamp(Source.WS, 1_300_000, 1_300_000, 60_000))
    st.stamp_only("liq", PlaneStamp(Source.WS, 1_600_000, 1_600_000, 60_000))
    cad = st.cadences()["liq"]
    assert cad.median_s == 300.0, "темп считается ТОЛЬКО по реальным событиям"


def test_cadence_is_measured_from_real_state_writes() -> None:
    """Сквозная проверка: интервалы берутся из настоящих писателей `SymbolState`."""
    st = SymbolState("ETHUSDT")
    for i in range(MIN_CADENCE_SAMPLES + 1):
        st.put_value("taker_5m", 1.0 + i, PlaneStamp(Source.REST_SEED, i * 300_000, i * 300_000, 375_000))
    cad = st.cadences()["taker_5m"]
    assert cad.measured and cad.samples == MIN_CADENCE_SAMPLES
    assert cad.median_s == 300.0
    assert cad.bound_s == 375.0
    assert not cad.bound_too_tight
