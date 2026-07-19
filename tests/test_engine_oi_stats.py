"""Pure OI stats (ADR-0004 S8) — oi_change matches client.fetch_open_interest_change exactly.

The legacy computation was `series[-1]/series[-2] - 1`, None when <2 points or prev<=0. These pin
that contract + the fail-loud (I-6) edges so the features cutover can drop the transport method.
"""
from __future__ import annotations

import math

import pytest

from hunt_core.engine.oi_stats import oi_change, oi_series


def test_oi_change_last_vs_previous_fraction():
    assert oi_change([100.0, 105.0]) == pytest.approx(0.05)  # +5% as a fraction (float: 0.05000…04)
    assert oi_change([200.0, 150.0]) == pytest.approx(-0.25)
    # only the last two points matter (matches limit=2 legacy fetch)
    assert oi_change([1.0, 2.0, 100.0, 105.0]) == pytest.approx(0.05)


def test_oi_change_none_when_too_few_points():
    assert oi_change([100.0]) is None
    assert oi_change([]) is None
    assert oi_change(None) is None


def test_oi_change_none_when_previous_nonpositive():
    assert oi_change([0.0, 5.0]) is None       # prev == 0 → division guard (legacy: series[-2] <= 0)
    assert oi_change([-1.0, 5.0]) is None       # prev < 0 → guard
    # a zero LAST over a positive prev is valid data (−100%), not "no data":
    assert oi_change([5.0, 0.0]) == -1.0


def test_oi_change_skips_nonfinite_endpoints():
    assert oi_change([100.0, math.inf]) is None
    assert oi_change([math.nan, 105.0]) is None


def test_oi_series_extracts_finite_sum_open_interest():
    rows = [
        {"sumOpenInterest": "100.0"},
        {"sumOpenInterest": None},     # missing → skipped (fail-loud, not fabricated 0.0)
        {"sumOpenInterest": "abc"},    # unparseable → skipped
        {"sumOpenInterest": 105.0},
    ]
    series = oi_series(rows)
    assert series == [100.0, 105.0]
    assert oi_change(series) == pytest.approx(0.05)  # end-to-end: raw rows → series → change
