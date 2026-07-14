"""G-29: oi_bars_from_frames must be a BACKWARD as-of join — never a future OI value.

The old two-pointer loop left the index at 0 for every bar preceding the first OI sample,
so those bars were stamped with oi_rows[0] — a reading from their own future. That is
lookahead (invariant I-5) leaking straight into the forward-liquidation model.
"""
from __future__ import annotations

import polars as pl

from hunt_core.maps.oi import oi_bars_from_frames


def _ohlcv(ts: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "time": ts,
            "high": [101.0 + i for i in range(len(ts))],
            "low": [99.0 + i for i in range(len(ts))],
            "close": [100.0 + i for i in range(len(ts))],
        }
    )


def test_bars_before_first_oi_sample_are_dropped_not_guessed() -> None:
    # OI is first observed at t=300. Bars at 100 and 200 have NO known OI.
    bars = _ohlcv([100, 200, 300, 400])
    oi_hist = [{"timestamp": 300, "openInterestAmount": 50.0},
               {"timestamp": 400, "openInterestAmount": 70.0}]
    out = oi_bars_from_frames(oi_hist, bars)
    assert [r["ts"] for r in out] == [300, 400], "pre-OI bars must not be back-filled"
    assert [r["oi"] for r in out] == [50.0, 70.0]


def test_bar_takes_the_last_oi_at_or_before_it() -> None:
    bars = _ohlcv([300, 350, 400, 450])
    oi_hist = [{"timestamp": 300, "openInterestAmount": 50.0},
               {"timestamp": 400, "openInterestAmount": 70.0}]
    out = oi_bars_from_frames(oi_hist, bars)
    # 350 still carries the t=300 reading; 450 carries t=400. Never the next one.
    assert [(r["ts"], r["oi"]) for r in out] == [(300, 50.0), (350, 50.0), (400, 70.0), (450, 70.0)]


def test_ohlcv_fields_survive() -> None:
    bars = _ohlcv([300])
    out = oi_bars_from_frames([{"timestamp": 300, "openInterestAmount": 50.0}], bars)
    assert out[0]["high"] == 101.0 and out[0]["low"] == 99.0 and out[0]["close"] == 100.0


def test_empty_inputs() -> None:
    assert oi_bars_from_frames([], _ohlcv([100])) == []
    assert oi_bars_from_frames([{"timestamp": 1, "oi": 1.0}], pl.DataFrame()) == []
