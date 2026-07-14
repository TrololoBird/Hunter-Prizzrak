"""Pattern C long target = reachable measured move, not the fantasy distance ladder.

_target_ladder[0] is ≥20%-floored + farther-biased → fantasy-RR / timeouts (full
dataset_v11: C avgR −0.484, 46 timeouts). The measured-move target (entry + recent
high−low amplitude) is the reachable magnitude the move banks — validated to flip C
to +0.127. This pins the target helper (SCAN Pattern-C fix).
"""
from __future__ import annotations

import polars as pl

from hunt_core.scanner.detect.patterns import _measured_move_target


def _df(highs: list[float], lows: list[float]) -> pl.DataFrame:
    n = len(highs)
    return pl.DataFrame({
        "open": [1.0] * n, "high": highs, "low": lows,
        "close": [1.0] * n, "volume": [1.0] * n,
    })


def test_measured_move_is_entry_plus_amplitude() -> None:
    # amplitude = max(high) − min(low) = 120 − 80 = 40; target = entry(100) + 40 = 140.
    df = _df(highs=[110.0, 120.0, 105.0], lows=[95.0, 80.0, 90.0])
    assert _measured_move_target(df, 100.0) == 140.0


def test_reachable_target_below_the_fantasy_pool() -> None:
    # A modest amplitude gives a nearby, reachable target — not a ≥20%-floored far pool.
    df = _df(highs=[102.0, 103.0, 101.0], lows=[99.0, 98.0, 100.0])
    tgt = _measured_move_target(df, 100.0)
    assert tgt == 105.0  # 100 + (103 − 98); +5%, reachable — NOT +20%+ fantasy
    assert tgt < 120.0


def test_degenerate_inputs_return_zero() -> None:
    assert _measured_move_target(_df([100.0], [100.0]), 100.0) == 0.0  # <2 bars
    assert _measured_move_target(_df([100.0, 100.0], [100.0, 100.0]), 0.0) == 0.0  # bad entry
