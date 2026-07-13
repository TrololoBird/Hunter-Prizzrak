"""Morning/evening star must clear the FIRST candle's body midpoint (FEAT-3).

The old code compared the third candle's close to the small star candle's body
edge (max/min of the star's open/close), a far weaker threshold. Canonical star
reversals require the third candle to close back past the midpoint of the first
(impulse) candle's body.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.candle_patterns import add_candle_pattern_columns


def _flag(rows: list[tuple[float, float, float, float]], col: str) -> float:
    df = pl.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
        }
    )
    out = add_candle_pattern_columns(df)
    return float(out[col][-1])


# Candle 1 body spans 100..110 (bearish) → midpoint = 105. Star body top = 99.
_FIRST = (110.0, 111.0, 99.0, 100.0)
_STAR = (99.0, 100.0, 96.0, 98.0)


def test_morning_star_below_first_midpoint_does_not_fire() -> None:
    # Third close 102 clears the star top (99) but is below the first midpoint (105).
    weak = _flag([_FIRST, _STAR, (98.0, 103.0, 97.0, 102.0)], "candle_morning_star")
    assert weak == 0.0


def test_morning_star_above_first_midpoint_fires() -> None:
    strong = _flag([_FIRST, _STAR, (98.0, 107.0, 97.0, 106.0)], "candle_morning_star")
    assert strong == 1.0


# Mirror for evening star: first candle bullish 100..110, midpoint 105, star bottom 111.
_FIRST_UP = (100.0, 111.0, 99.0, 110.0)
_STAR_UP = (111.0, 114.0, 110.0, 112.0)


def test_evening_star_above_first_midpoint_does_not_fire() -> None:
    weak = _flag([_FIRST_UP, _STAR_UP, (112.0, 113.0, 107.0, 108.0)], "candle_evening_star")
    assert weak == 0.0


def test_evening_star_below_first_midpoint_fires() -> None:
    strong = _flag([_FIRST_UP, _STAR_UP, (112.0, 113.0, 103.0, 104.0)], "candle_evening_star")
    assert strong == 1.0
