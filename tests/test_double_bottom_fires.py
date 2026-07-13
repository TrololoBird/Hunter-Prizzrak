"""detect_double_bottom must actually fire on a W (two similar lows + a peak).

The swing-HIGH mask was mis-unpacked as the swing-LOW mask, so `highs` were taken
at swing-low indices and the between-lows peak set was always empty → the pattern
never fired (FEAT-2). A clean double bottom must now be detected.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.chart_patterns import detect_double_bottom


def _w_shape() -> pl.DataFrame:
    # trough(10) → peak(20) → trough(10.05, similar) → recovery, with 3-bar swing
    # confirmation room on each side and a non-tail last swing.
    # Clean single-bar zigzag so each turn is a STRICT local extremum (n=3 swing):
    # low1 @idx8, peak @idx16, low2 @idx24 (similar low), then recovery with room.
    raw = (
        [18, 17, 16, 15, 14, 13, 12, 11, 10]          # → low1=10 @idx8
        + [11, 12, 13, 14, 15, 16, 17, 18]            # → peak=18 @idx16
        + [17, 16, 15, 14, 13, 12, 11, 10.1]          # → low2=10.1 @idx24
        + [11, 12, 13, 14, 15, 16, 17, 18, 19]        # recovery + confirmation room
    )
    close = [float(x) for x in raw]
    high = [c + 0.4 for c in close]
    low = [c - 0.4 for c in close]
    return pl.DataFrame({"high": high, "low": low, "close": close})


def test_double_bottom_detected() -> None:
    out = detect_double_bottom(_w_shape(), lookback=50)
    assert out.get("pattern") == "double_bottom"
    assert out.get("direction") == "long"
    assert float(out.get("confidence") or 0) > 0


def test_no_pattern_on_monotonic_trend() -> None:
    n = 40
    close = [10.0 + i for i in range(n)]
    df = pl.DataFrame({"high": [c + 0.4 for c in close], "low": [c - 0.4 for c in close], "close": close})
    assert detect_double_bottom(df, lookback=50).get("pattern") in (None, "")
