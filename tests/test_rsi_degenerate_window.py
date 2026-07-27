"""Гард: на НЕПОДВИЖНОМ окне RSI обязан быть ``null``, а не 0.

Замер 2026-07-27. Бэкенд `polars_ta` на сорока одинаковых закрытиях отдаёт **0.0** — это его
выбор для случая 0/0, не наш. Но отгружали дальше мы, а ноль читается любым потребителем с
порогом перепроданности как «предельная перепроданность». То есть неподвижная цена выглядела
сильнейшим сигналом на покупку.

Почему это не теоретический случай: идеально плоское окно на ликвидном перпе редкость, но на
неликвиде оно штатно, а на ЗАМЕРШЕМ кадре — гарантировано. Замерший кадр здесь самый дорогой
класс инцидентов (`stale-htf-cache-trap`), и сочетание «замер + предельная перепроданность»
худшее из возможных.

Заодно зафиксирован эпсилон бэкенда, чтобы его не искали заново: на монотонном росте он даёт
99.99999894 вместо ровно 100. Косметика — ни один порог в дереве этого не различает.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.polars_ta_bridge import rsi_series


def test_flat_window_yields_null_not_zero() -> None:
    """Неподвижное окно → нет значения. Ноль здесь — сфабрикованный экстремум (I-6)."""
    flat = pl.DataFrame({"close": [100.0] * 40})
    vals = rsi_series(flat, period=14).to_list()
    assert vals[-1] is None, f"плоское окно дало RSI={vals[-1]!r} вместо None"
    assert all(v is None for v in vals[-5:])


def test_flat_tail_after_movement_is_also_null() -> None:
    """Движение в прошлом не спасает: окно RSI трейлинговое, и важно именно оно."""
    px = [100.0 + i for i in range(30)] + [130.0] * 20
    vals = rsi_series(pl.DataFrame({"close": px}), period=14).to_list()
    assert vals[-1] is None, "хвост без движения обязан гаситься"
    moved = [v for v in vals[:30] if v is not None]
    assert moved, "на участке с движением значения обязаны остаться"


def test_normal_series_is_untouched() -> None:
    """Гард не имеет права глушить обычный ряд."""
    import random

    rng = random.Random(20260727)
    px = [100.0]
    for _ in range(200):
        px.append(px[-1] * (1.0 + rng.gauss(0.0, 0.01)))
    vals = rsi_series(pl.DataFrame({"close": px}), period=14).to_list()
    live = [v for v in vals if v is not None]
    assert len(live) > 150, f"гард съел живые значения: осталось {len(live)} из {len(vals)}"
    assert 0.0 <= min(live) and max(live) <= 100.0


def test_monotonic_rise_saturates_near_one_hundred() -> None:
    """Эпсилон бэкенда зафиксирован: 99.99999894, а не ровно 100. Порогов это не меняет."""
    up = pl.DataFrame({"close": [100.0 + i for i in range(40)]})
    v = rsi_series(up, period=14).to_list()[-1]
    assert v is not None
    assert 99.999 < v <= 100.0, f"ожидали насыщение у 100, получили {v!r}"
