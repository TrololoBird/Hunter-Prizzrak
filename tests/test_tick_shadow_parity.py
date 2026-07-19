"""S8-core shadow parity (ADR-0004) — _shadow_parity + _to_unified_perp.

The shadow is telemetry-only (nothing in the tick depends on it), but it is the de-risking lens for
the data-source swap, so its comparison must be correct: price bps, per-TF row counts as
[engine, legacy], funding/OI pairs, and the known kline taker-fidelity flag (engine 6-element ccxt
klines zero-fill taker_buy_base_volume; legacy 12-element fapi klines carry it).
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import polars as pl

from hunt_core.runtime.tick_assembly import _shadow_parity, _to_unified_perp


def test_to_unified_perp_normalizes_any_symbol_form():
    assert _to_unified_perp("BTCUSDT") == "BTC/USDT:USDT"
    assert _to_unified_perp("BTC") == "BTC/USDT:USDT"
    assert _to_unified_perp("BTC/USDT:USDT") == "BTC/USDT:USDT"
    assert _to_unified_perp("1000pepeusdt") == "1000PEPE/USDT:USDT"


def test_shadow_absent_view():
    assert _shadow_parity(None, price=100.0, kline_map={}, funding=None, oi=None) == {"view": "absent"}


def test_shadow_compares_price_funding_oi_and_kline_rows():
    engine_15m = pl.DataFrame({"taker_buy_base_volume": [0.0, 0.0]})  # engine-style zeroed, height 2
    view = NS(
        last_price=100.05,
        price_source="ticker",
        derivs=NS(funding=0.0001, oi=1_000_000.0),
        klines=NS(m15=engine_15m),  # other TFs absent → getattr(..., None)
    )
    out = _shadow_parity(
        view,
        price=100.0,
        kline_map={"15m": pl.DataFrame({"close": [1.0, 2.0, 3.0]})},  # legacy height 3
        funding=0.00011,
        oi=1_010_000.0,
    )
    assert out["price_src"] == "ticker"
    assert out["price_bps"] == 5.0  # +5 bps (100.05 vs 100.0)
    assert out["funding"] == [0.0001, 0.00011]  # [engine, legacy]
    assert out["oi"] == [1_000_000.0, 1_010_000.0]
    assert out["kline_rows"]["15m"] == [2, 3]  # [engine height, legacy height]
    assert out["taker_zeroed_tfs"] == ["15m"]  # engine taker_buy_base sums to 0 → flagged


def test_shadow_no_taker_flag_when_engine_frame_carries_taker():
    engine_1h = pl.DataFrame({"taker_buy_base_volume": [3.0, 4.0]})  # non-zero → NOT flagged
    view = NS(last_price=100.0, price_source="mark", derivs=NS(funding=None, oi=None), klines=NS(h1=engine_1h))
    out = _shadow_parity(view, price=100.0, kline_map={"1h": pl.DataFrame({"close": [1.0]})}, funding=None, oi=None)
    assert "taker_zeroed_tfs" not in out
    assert out["kline_rows"]["1h"] == [2, 1]
    assert out["funding"] == [None, None]  # both absent → no fabricated value
