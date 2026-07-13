"""SuperTrend must use the CORRECT (non-inverted) final-band trailing.

The final-band comparisons were inverted vs canonical Pine ta.supertrend, so the
trailing band reset to the basic band every bar in the active trend instead of
locking. This pins production `supertrend_dir` to the correct band logic and
proves it is NOT the inverted (buggy) logic, on a series with pullbacks that make
the two differ. The reference replays the exact production ATR + band/direction
formulas so it matches bit-for-bit; only the comparison direction is varied.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.shared import finite_float, supertrend_series, wilder_mean


def _dir_with(df: pl.DataFrame, period: int, mult: float, *, inverted: bool) -> list[int]:
    """Replay production SuperTrend direction; `inverted` flips the two band
    comparisons to reproduce the old bug. inverted=False == the fixed production."""
    high = [finite_float(v) for v in df["high"].to_list()]
    low = [finite_float(v) for v in df["low"].to_list()]
    close = [finite_float(v) for v in df["close"].to_list()]
    n = len(close)
    tr = [abs(high[0] - low[0])]
    for i in range(1, n):
        tr.append(max(abs(high[i] - low[i]), abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    atr = [
        finite_float(v)
        for v in wilder_mean(
            pl.Series("supertrend_tr", tr, dtype=pl.Float64), period=period, name="supertrend_atr"
        ).to_list()
    ]
    basic_up = [(high[i] + low[i]) / 2 + mult * atr[i] for i in range(n)]
    basic_lo = [(high[i] + low[i]) / 2 - mult * atr[i] for i in range(n)]
    fu = list(basic_up)
    fl = list(basic_lo)
    direction = [1] * n
    for i in range(1, n):
        pc, pfu, pfl = close[i - 1], fu[i - 1], fl[i - 1]
        upper_hold = (pc > pfu) if inverted else (pc < pfu)
        lower_hold = (pc < pfl) if inverted else (pc > pfl)
        fu[i] = min(basic_up[i], pfu) if upper_hold else basic_up[i]
        fl[i] = max(basic_lo[i], pfl) if lower_hold else basic_lo[i]
        if direction[i - 1] == -1 and close[i] > fu[i]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close[i] < fl[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return direction


def _pullback_df(n: int = 120) -> pl.DataFrame:
    close = []
    for i in range(n):
        saw = -9.0 if (i % 7 == 0) else 0.0
        wiggle = 3.0 * (((i * 53) % 13) - 6) / 6.0
        close.append(100.0 + 1.4 * i + saw + wiggle)
    high = [c + 1.0 for c in close]  # symmetric → hl2 == close
    low = [c - 1.0 for c in close]
    return pl.DataFrame({"high": high, "low": low, "close": close})


def test_production_matches_correct_band_logic() -> None:
    df = _pullback_df()
    prod = supertrend_series(df, period=10, multiplier=3.0)[1].to_list()
    assert prod == _dir_with(df, 10, 3.0, inverted=False)


def test_production_is_not_the_inverted_bug() -> None:
    # The series has pullbacks where correct (ratchet) and inverted (reset) diverge,
    # so this genuinely discriminates the fix.
    df = _pullback_df()
    prod = supertrend_series(df, period=10, multiplier=3.0)[1].to_list()
    assert prod != _dir_with(df, 10, 3.0, inverted=True)
