"""Карта ликвидаций имеет СВОЁ окно, шире стаканного.

``price_range_pct``/``n_buckets`` общие для карты стакана, кросс-карты и ликвидаций, но объекты
разные: живой стакан ±5% — по делу, а кластеры ликвидаций суть исторические позиции и законно
уходят дальше. Сверка с картой ликвидаций Coinglass по ASTR (2026-07-25) это измерила: при цене
0.005119 окно ±5% давало 0.004858–0.005370, из-за чего верхний кластер автора 0.00527–0.00548
покрывался на 47%, а нижний 0.00483–0.00502 — на 85%. С ±12% оба покрыты на 100%.
"""
from __future__ import annotations

from hunt_core.maps.config import MapsConfig

_CG_LOWER = (0.004830, 0.005020)   # нижний бокс со скриншота Coinglass
_CG_UPPER = (0.005270, 0.005480)   # верхний
_ASTR_PRICE = 0.005119


def _window(price: float, rng_pct: float) -> tuple[float, float]:
    span = price * rng_pct / 100.0
    return price - span, price + span


def _coverage(win: tuple[float, float], box: tuple[float, float]) -> float:
    ov = max(0.0, min(win[1], box[1]) - max(win[0], box[0]))
    return ov / (box[1] - box[0]) * 100.0


def test_liq_window_is_independent_of_the_orderbook_window() -> None:
    """★ Стакану узкое окно нужно по делу — расширять общую константу было бы регрессией DOM."""
    cfg = MapsConfig()
    assert cfg.price_range_pct == 5.0, "окно стакана не трогаем"
    assert cfg.liq_price_range_pct > cfg.price_range_pct


def test_liq_window_covers_both_coinglass_clusters() -> None:
    """★ Измеренный кейс ASTR: при ±5% верхний кластер обрезался ровно посередине."""
    cfg = MapsConfig()
    old = _window(_ASTR_PRICE, 5.0)
    new = _window(_ASTR_PRICE, cfg.liq_price_range_pct)
    assert _coverage(old, _CG_UPPER) < 60.0, "так и было — половина кластера за окном"
    assert _coverage(new, _CG_LOWER) == 100.0
    assert _coverage(new, _CG_UPPER) == 100.0


def test_resolution_is_preserved_when_the_window_widens() -> None:
    """Корзины масштабированы вместе с окном: шире окно без потери разрешения, а не вместо него."""
    cfg = MapsConfig()
    old_step = (2 * 5.0) / 20
    new_step = (2 * cfg.liq_price_range_pct) / cfg.liq_n_buckets
    assert abs(new_step - old_step) < 1e-9, f"шаг изменился: {old_step}% → {new_step}%"
