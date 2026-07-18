"""Pure taker-flow / price-change over the trades read-through — fail-loud, windowed (ADR-0003 E5)."""
from __future__ import annotations

from typing import Any

from hunt_core.engine.orderflow import price_change_pct, taker_flow


def _tr(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"timestamp": 1000, "price": 100.0, "amount": 1.0, "side": "buy"}
    base.update(kw)
    return base


def test_taker_flow_buy_sell_split_and_ratios() -> None:
    out = taker_flow([_tr(side="buy", amount=3.0), _tr(side="sell", amount=1.0)])
    assert out["buy_notional"] == 300.0 and out["sell_notional"] == 100.0
    assert out["delta"] == 200.0  # signed CVD
    assert out["delta_ratio"] == 0.5  # (300-100)/400
    assert out["buy_ratio"] == 0.75  # 300/400 buy share
    assert out["count"] == 2


def test_taker_flow_prefers_cost_over_price_amount() -> None:
    out = taker_flow([_tr(cost=1234.0, price=1.0, amount=1.0)])
    assert out["buy_notional"] == 1234.0  # cost wins when present


def test_taker_flow_empty_ratios_are_none_not_fabricated() -> None:
    out = taker_flow([])
    assert out["delta_ratio"] is None and out["buy_ratio"] is None  # no 0.5/0 invented
    assert out["buy_notional"] == 0.0 and out["count"] == 0


def test_taker_flow_skips_uncomputable_and_unknown_side() -> None:
    out = taker_flow([_tr(price=None, amount=None, cost=None), _tr(side="unknown"), _tr(amount=2.0)])
    assert out["count"] == 1 and out["buy_notional"] == 200.0  # only the last valid buy counts


def test_taker_flow_windowed_filters_old_trades() -> None:
    now = 100_000
    trades = [_tr(timestamp=now - 90_000, amount=5.0), _tr(timestamp=now - 10_000, amount=1.0)]
    out = taker_flow(trades, window_ms=30_000, now_ms=now)
    assert out["count"] == 1 and out["buy_notional"] == 100.0  # the 90s-old trade excluded


def test_price_change_pct_last_vs_first_in_window() -> None:
    now = 100_000
    trades = [
        _tr(timestamp=now - 50_000, price=100.0),  # out of a 30s window
        _tr(timestamp=now - 20_000, price=100.0),
        _tr(timestamp=now - 5_000, price=110.0),
    ]
    assert price_change_pct(trades, window_ms=30_000, now_ms=now) == 0.1  # (110-100)/100


def test_price_change_pct_none_when_under_two_trades() -> None:
    now = 100_000
    assert price_change_pct([_tr(timestamp=now)], window_ms=30_000, now_ms=now) is None
    assert price_change_pct([], window_ms=30_000, now_ms=now) is None
