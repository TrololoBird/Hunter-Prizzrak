"""Гард порога устойчивости ПОКа — пин на ИЗМЕРЕННЫЙ провал в распределении.

Тест допустим по правилу репозитория: фиксирует величину, измеренную на живых данных
(`scripts/verify_poc_origin_guard.py`, 6 символов × 3 ТФ, 55 настоящих зон), и гоняет на ней
код модуля.

Измеренная гистограмма разброса ПОК (% ширины зоны, максимум по числу корзин и по началу сетки):

    [0,2)% 3 · [2,5)% 14 · [5,10)% 9 · [10,15)% 7 · **[15,20)% 0** · [20,30)% 4 · [30,50)% 5 ·
    [50,100)% 5 · [100,200)% 5 · [200,∞)% 3

Пустая корзина `[15, 20)` — это и есть провал между режимами. Порог обязан стоять в нём:
ниже 15 он начнёт резать устойчивые зоны (в корзине [10,15) их семь), выше 20 — пропускать
явно бимодальные (в [20,30) их четыре).
"""
from __future__ import annotations

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _POC_STABILITY_MAX_SPREAD, _poc_is_stable

# Замер 2026-07-27: границы пустой корзины гистограммы.
MEASURED_VALLEY = (15.0, 20.0)


def test_threshold_sits_in_the_measured_valley() -> None:
    """Порог обязан лежать в пустой корзине распределения, а не рядом с ней."""
    lo, hi = MEASURED_VALLEY
    assert lo <= _POC_STABILITY_MAX_SPREAD < hi, (
        f"порог {_POC_STABILITY_MAX_SPREAD} вышел из измеренного провала {MEASURED_VALLEY}; "
        "менять его можно только вместе с новым прогоном verify_poc_origin_guard.py"
    )


def _frame(rows: list[tuple[float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "high": [r[0] for r in rows],
        "low": [r[1] for r in rows],
        "volume": [r[2] for r in rows],
    })


def test_unimodal_zone_is_stable() -> None:
    """Один доминирующий пик — ПОК не должен скакать при смене разбиения."""
    cfg = PrizrakConfig.load()
    rows = [(101.0, 100.0, 5.0) for _ in range(20)]
    rows += [(105.5, 105.0, 400.0) for _ in range(20)]   # единственный тяжёлый узел
    rows += [(110.0, 109.0, 5.0) for _ in range(20)]
    fr = _frame(rows)
    poc = 105.25
    assert _poc_is_stable(fr, poc, lo=100.0, hi=110.0, cfg=cfg)


def test_bimodal_zone_is_unstable() -> None:
    """Два почти равных пика — argmax перескакивает, и это обязано быть видно.

    Именно этот симптом мерится: у бимодальной LTC 40.78–45.59 разброс был 54.1% ширины —
    в измеренном распределении такие зоны лежат далеко за порогом.

    Конструкция подобрана так, чтобы перескок реально происходил, а не постулировался:
    пик A узкий и лёгкий (500 в одной корзине при любом разбиении), пик B шире и тяжелее
    (600, ширина 0.20). На грубой сетке B целиком в одной корзине и побеждает; на тонкой он
    дробится на три корзины по ~200 и проигрывает A. Проверено: ПОК = 108.125 при 40 корзинах
    и 101.08 при 60 — разброс 70.8% ширины зоны, то есть далеко за порогом 15%.
    """
    cfg = PrizrakConfig.load()
    rows = [(101.01, 101.00, 25.0) for _ in range(20)]   # A: 500, не дробится
    rows += [(108.20, 108.00, 30.0) for _ in range(20)]  # B: 600, дробится на тонкой сетке
    rows += [(110.0, 100.0, 0.01) for _ in range(2)]     # растягиваем окно до 100..110
    fr = _frame(rows)
    poc = 108.125  # то, что отдаёт профиль при 40 корзинах
    assert not _poc_is_stable(fr, poc, lo=100.0, hi=110.0, cfg=cfg)


def test_degenerate_span_does_not_claim_instability() -> None:
    """Нулевая ширина зоны — мерить нечем; объявлять неустойчивость без основания нельзя (I-6)."""
    cfg = PrizrakConfig.load()
    fr = _frame([(101.0, 100.0, 5.0) for _ in range(20)])
    assert _poc_is_stable(fr, 100.5, lo=100.0, hi=100.0, cfg=cfg)
