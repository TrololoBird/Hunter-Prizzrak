"""Regression: Pattern B stage 0 must not arm off an ANCIENT sweep.

Stage 0 scanned the FULL meso frame for the macro-high sweep (120 bars on 4h/1h,
220 on the 1d meso ≈ 7 months). Its context gates — ``dist_pct <= 8`` and
``meso_top >= 0.98 * macro_high`` — only test the CURRENT price, so a coin that
swept its macro high months ago, retraced, and drifted back near that high would
seed stage 1 off the months-old wick. Stage 1's fade/rejection detectors then
locate the peak bar anywhere in the frame, so the emitted "sweep→fade" could be
two causally unrelated events — the false-positive-by-construction class the
persistent state machine exists to kill.

The A-side already carries this fix (``detect_sweep_low(meso_df.tail(
_BOKOVIK_WINDOW), …)``: "scanning the whole frame let an old pierce satisfy the
gate"). These tests pin the mirror on the B-side: the sweep must fall inside the
same recent window that defines ``pump_high``.
"""
from __future__ import annotations

import polars as pl

from hunt_core.scanner.detect.patterns import (
    _B_PEAK_WINDOW,
    _advance_pattern_b,
    new_symbol_state,
)

_DAY_MS = 86_400_000
_H4_MS = 14_400_000


def _frame(bars: list[list[float]]) -> pl.DataFrame:
    """Build the OHLCV frame shape the detectors read."""
    return pl.DataFrame(
        {
            "ts": [b[0] for b in bars],
            "open": [b[1] for b in bars],
            "high": [b[2] for b in bars],
            "low": [b[3] for b in bars],
            "close": [b[4] for b in bars],
            "volume": [b[5] for b in bars],
        }
    )


def _flat(n: int, *, start_ts: float, step: float, price: float) -> list[list[float]]:
    return [
        [start_ts + i * step, price, price * 1.002, price * 0.998, price, 100.0]
        for i in range(n)
    ]


def _sweep_bar(ts: float, level: float) -> list[float]:
    """A textbook sweep-high bar: pierces ``level``, long upper wick, closes under."""
    high = level * 1.05
    close = level * 0.985
    return [ts, level * 0.98, high, level * 0.97, close, 500.0]


def _macro_frame(macro_high: float) -> pl.DataFrame:
    """1d macro whose extreme is ``macro_high``.

    ``_macro_extreme`` drops the last 7 bars, so the high sits well before the tail.
    """
    bars = _flat(40, start_ts=0.0, step=_DAY_MS, price=macro_high * 0.80)
    bars[10][2] = macro_high  # the trend high, outside the excluded recent tail
    return _frame(bars)


def test_ancient_sweep_does_not_arm_stage_1() -> None:
    """A sweep older than the peak window must leave the state disarmed.

    Fails before the fix: the whole-frame scan finds the bar-5 wick and seeds
    stage 1, even though it is ~100 bars (weeks) behind the current price.
    """
    macro_high = 100.0
    # 120 4h bars near — but under — the macro high, so the current-price gates
    # (dist_pct <= 8, meso_top >= 0.98 * macro_high) all pass.
    bars = _flat(120, start_ts=0.0, step=_H4_MS, price=macro_high * 0.985)
    bars[5] = _sweep_bar(5 * _H4_MS, macro_high)  # the ANCIENT pierce
    meso = _frame(bars)

    state, setup = _advance_pattern_b(
        _macro_frame(macro_high), meso, "4h", None, new_symbol_state(), now_ms=120 * _H4_MS
    )

    assert setup is None
    assert int(state.get("stage", 0)) == 0, "an ancient wick must not arm Pattern B"


def test_recent_sweep_still_arms_stage_1() -> None:
    """The real setup — a sweep inside the peak window — must still arm."""
    macro_high = 100.0
    bars = _flat(120, start_ts=0.0, step=_H4_MS, price=macro_high * 0.985)
    sweep_idx = 120 - 3  # comfortably inside _B_PEAK_WINDOW
    bars[sweep_idx] = _sweep_bar(sweep_idx * _H4_MS, macro_high)
    meso = _frame(bars)

    state, setup = _advance_pattern_b(
        _macro_frame(macro_high), meso, "4h", None, new_symbol_state(), now_ms=120 * _H4_MS
    )

    assert setup is None  # stage 0 arms only; emission happens on a later tick
    assert state["stage"] == 1
    assert state["pattern"] == "B"
    assert state["data"]["swept_level"] == macro_high


def test_sweep_just_outside_the_window_does_not_arm() -> None:
    """Boundary: the window edge is the cutoff, not a soft preference."""
    macro_high = 100.0
    bars = _flat(120, start_ts=0.0, step=_H4_MS, price=macro_high * 0.985)
    sweep_idx = 120 - _B_PEAK_WINDOW - 1  # one bar too old
    bars[sweep_idx] = _sweep_bar(sweep_idx * _H4_MS, macro_high)
    meso = _frame(bars)

    state, setup = _advance_pattern_b(
        _macro_frame(macro_high), meso, "4h", None, new_symbol_state(), now_ms=120 * _H4_MS
    )

    assert setup is None
    assert int(state.get("stage", 0)) == 0
