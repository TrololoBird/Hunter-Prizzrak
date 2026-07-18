"""Spot taker-flow helper: net buy−sell notional from public aggTrades, fail-loud.

CCXT's ``trade['side']`` is the TAKER side ('buy' = lifted the ask). The helper must
(1) net buy vs sell notional, (2) fall back to price×amount when ``cost`` is absent,
(3) return ``(None, None)`` on an empty/garbage window — never a fabricated ``0.0`` that
would read as «perfect balance» (invariant I-6).
"""
from __future__ import annotations

from hunt_core.market.spot import HuntCcxtSpotCompanion as _S


def test_net_delta_and_ratio() -> None:
    trades = [
        {"side": "buy", "cost": 100_000.0},
        {"side": "buy", "price": 2.0, "amount": 50_000.0},   # cost via price×amount = 100_000
        {"side": "sell", "cost": 40_000.0},
    ]
    delta, ratio = _S._taker_flow(trades)
    assert delta == 160_000.0  # (100k + 100k) buy − 40k sell
    assert ratio is not None and abs(ratio - 200_000.0 / 240_000.0) < 1e-9


def test_empty_window_is_none_not_zero() -> None:
    # No usable trades ⇒ «нет данных», NOT a fabricated balanced 0.0.
    assert _S._taker_flow([]) == (None, None)
    assert _S._taker_flow(None) == (None, None)
    # Garbage / unknown side is skipped, and an all-garbage window is still None.
    assert _S._taker_flow([{"side": "?", "cost": 1.0}, {"cost": 5.0}, 42]) == (None, None)


def test_one_sided_flow() -> None:
    delta, ratio = _S._taker_flow([{"side": "buy", "cost": 10.0}, {"side": "buy", "cost": 5.0}])
    assert delta == 15.0
    assert ratio == 1.0  # all buys
