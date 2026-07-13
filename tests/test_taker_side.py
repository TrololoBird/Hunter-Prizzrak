"""Aggressor-side resolution must not silently default side-less trades to sell.

Crypto exchanges hand the taker side directly (no tick-rule inference needed).
CCXT sets `side`; but a raw Binance aggTrade carries only the maker flag `m`
(m=true → buyer is maker → taker SELLS). Previously a payload without `side`
defaulted to sell, biasing CVD/footprint/delta. `_taker_is_buy` now falls back
to `m`.
"""
from __future__ import annotations

from hunt_core.market.streams import _taker_is_buy


def test_ccxt_side_buy() -> None:
    assert _taker_is_buy({"side": "buy"}, {}) is True


def test_ccxt_side_sell() -> None:
    assert _taker_is_buy({"side": "sell"}, {}) is False


def test_maker_flag_fallback_taker_sells() -> None:
    # No side; m=true → buyer is maker → taker is the seller.
    assert _taker_is_buy({}, {"m": True}) is False


def test_maker_flag_fallback_taker_buys() -> None:
    # No side; m=false → buyer is taker.
    assert _taker_is_buy({}, {"m": False}) is True


def test_side_wins_over_maker_flag() -> None:
    # Explicit CCXT side takes precedence over the raw flag.
    assert _taker_is_buy({"side": "buy"}, {"m": True}) is True


def test_no_side_no_flag_defaults_false() -> None:
    # Genuinely undeterminable → the old default (sell); nothing better to do.
    assert _taker_is_buy({}, {}) is False
