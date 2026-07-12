"""Entry is split into orders by base size (course с. 30/32).

«чтобы точно «забрало» ваш ордер — используем 2-3 ордера, на зону, и на уровень ПОК.
Если накопление очень большое ТФ 1Д-1Н-1М — то закуп всегда стоит делить на зону и на
уровень. Если накопление маленькое — 5м-1ч — эффективнее входить 1 ордером от уровня.»
"""

from __future__ import annotations

from hunt_core.prizrak.orchestrator import _entry_orders

ZONE = {"lo": 100.0, "hi": 110.0}


def test_small_base_is_a_single_order() -> None:
    for tf in ("5m", "15m", "1h"):
        assert _entry_orders(105.0, poc=103.0, zone=ZONE, tf=tf) == [105.0]


def test_big_base_splits_into_level_and_poc() -> None:
    orders = _entry_orders(108.0, poc=103.0, zone=ZONE, tf="1d")
    assert len(orders) == 2
    assert 103.0 in orders and 108.0 in orders
    assert orders == sorted(orders)


def test_big_base_uses_nearest_boundary_when_poc_coincides_with_entry() -> None:
    """Forward entries anchor to the POC (П2), so entry==POC — still need a 2nd order."""
    orders = _entry_orders(103.0, poc=103.0, zone=ZONE, tf="1w")
    assert len(orders) == 2
    # nearest boundary to 103 is the low (100), so the second order is there
    assert 100.0 in orders


def test_4h_is_treated_as_a_big_base() -> None:
    assert len(_entry_orders(108.0, poc=103.0, zone=ZONE, tf="4h")) == 2


def test_no_duplicate_order_when_poc_within_band_of_entry() -> None:
    # POC within the ±0.2% band of entry — not a distinct order; nearest boundary used.
    orders = _entry_orders(108.0, poc=108.05, zone=ZONE, tf="1d")
    assert 108.05 not in orders
    assert len(orders) == 2  # entry + nearest boundary (110)
    assert 110.0 in orders
