"""Pure spot enrichment metrics (E6a) — spot-vs-perp lead/spread/ref/volume + taker, fail-loud."""
from __future__ import annotations


from hunt_core.engine.spot_metrics import (
    lead_return_pct,
    quote_volume_24h,
    spot_reference_price,
    spot_taker_flow,
    spread_bps,
)


def test_spot_reference_price_mid_when_quoted_else_last() -> None:
    assert spot_reference_price({"bid": 99.0, "ask": 101.0}, 100.5) == 100.0  # mid
    assert spot_reference_price({"bid": 0, "ask": 0}, 100.5) == 100.5  # no book → last
    assert spot_reference_price({"bid": 101.0, "ask": 99.0}, 100.5) == 100.5  # crossed → last
    assert spot_reference_price(None, 100.5) == 100.5


def test_spread_bps_sign_and_none() -> None:
    assert spread_bps(100.0, 100.5) == 50.0  # perp 0.5% above spot → +50 bps
    assert spread_bps(100.0, None) is None  # no futures mid
    assert spread_bps(0.0, 100.0) is None  # degenerate spot ref


def test_lead_return_pct_forming_vs_prev_close() -> None:
    ohlcv = [[0, 1, 1, 1, 100.0, 1], [60_000, 1, 1, 1, 101.0, 1]]  # prev 100 → forming 101
    assert lead_return_pct(ohlcv) == 1.0
    assert lead_return_pct([[0, 1, 1, 1, 100.0, 1]]) is None  # < 2 bars
    assert lead_return_pct([[0, 1, 1, 1, 0.0, 1], [1, 1, 1, 1, 5.0, 1]]) is None  # prev_close 0


def test_spot_taker_flow_delta_and_ratio() -> None:
    trades = [
        {"side": "buy", "price": 100.0, "amount": 3.0},
        {"side": "sell", "price": 100.0, "amount": 1.0},
    ]
    delta, ratio = spot_taker_flow(trades)
    assert delta == 200.0 and ratio == 0.75  # net +200 USD, 75% buy share


def test_spot_taker_flow_empty_is_none_none_not_zero() -> None:
    assert spot_taker_flow([]) == (None, None)  # нет данных, not (0.0, ...)
    assert spot_taker_flow(None) == (None, None)


def test_quote_volume_24h_zero_is_valid_absent_is_none() -> None:
    assert quote_volume_24h({"quoteVolume": 0.0}) == 0.0  # dead market is real data
    assert quote_volume_24h({"quoteVolume": 1234.5}) == 1234.5
    assert quote_volume_24h({}) is None  # absent → None
    assert quote_volume_24h(None) is None
