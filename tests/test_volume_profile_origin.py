"""Гард на сдвиг НАЧАЛА сетки корзин в объёмном профиле.

Зачем возможность вообще есть. Положение моды гистограммы зависит и от ШАГА корзины, и от её
НАЧАЛА, а ни один вендор (Sierra Chart, TradingView, CQG, thinkorswim) начало не документирует.
Замер `scripts/verify_poc_plateau.py` на 300-барном окне: при НЕИЗМЕННОМ числе корзин один
только сдвиг сетки уводил ПОК до 11.87% цены.

Зачем гард. Проверка `poc._poc_is_stable` этот перебор НЕ делает — и это записанное решение,
а не забывчивость: замер на 55 настоящих зонах (`scripts/verify_poc_origin_guard.py`) показал
ноль добавленных обнаружений сверх перебора по числу корзин. Значит `origin_shift` живёт
только ради замеров — а параметр без потребителя в проде гниёт первым. Тест держит контракт.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.volume_profile import volume_profile_levels


def _frame(rows: list[tuple[float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "high": [r[0] for r in rows],
        "low": [r[1] for r in rows],
        "volume": [r[2] for r in rows],
    })


def test_zero_shift_is_the_previous_behaviour() -> None:
    """Дефолт обязан совпадать с поведением до появления параметра — иначе это тихая правка."""
    rows = [(100.0 + i % 7, 99.0 + i % 7, 10.0 + i) for i in range(40)]
    fr = _frame(rows)
    a = volume_profile_levels(fr, buckets=30)
    b = volume_profile_levels(fr, buckets=30, origin_shift=0.0)
    assert a == b


def test_origin_shift_moves_the_grid() -> None:
    """Сдвиг обязан РЕАЛЬНО двигать сетку — иначе параметр декоративный.

    Проверяется на профиле, где объём сосредоточен у одной цены: сдвиг сетки на половину
    корзины меняет, в какую корзину попадает пик, и ПОК смещается.
    """
    rows = [(100.5, 100.0, 1.0) for _ in range(30)]
    rows += [(100.5, 100.0, 500.0) for _ in range(5)]
    rows += [(120.0, 80.0, 1.0) for _ in range(5)]  # растягиваем окно, чтобы корзины были широкими
    fr = _frame(rows)
    base, _v, _a = volume_profile_levels(fr, buckets=8)
    shifted, _v2, _a2 = volume_profile_levels(fr, buckets=8, origin_shift=0.5)
    assert base is not None and shifted is not None
    assert base != shifted, "сдвиг начала сетки не изменил ничего — параметр не работает"


def test_shift_keeps_poc_inside_the_traded_range() -> None:
    """Сдвиг не имеет права вынести ПОК за пределы торгованного диапазона."""
    rows = [(100.0 + (i % 5), 99.0 + (i % 5), 10.0 + (i % 3)) for i in range(60)]
    fr = _frame(rows)
    lo = min(r[1] for r in rows)
    hi = max(r[0] for r in rows)
    for shift in (0.0, 0.25, 0.5, 0.75):
        poc, _v, _a = volume_profile_levels(fr, buckets=40, origin_shift=shift)
        assert poc is not None
        # Сетка начинается ЛЕВЕЕ минимума, поэтому нижняя граница послабее на одну корзину.
        assert lo - (hi - lo) / 40 <= poc <= hi, f"ПОК вне диапазона при сдвиге {shift}: {poc}"
