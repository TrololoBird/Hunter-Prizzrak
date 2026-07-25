"""Full-history weekly-spot ladder — ATL/ATH must be prices the market actually TRADED at.

«Уровень есть уровень» presupposes business was done at the price. A single liquidation wick is
not business: on the 2025-10-06 Binance cascade, 3 of the 10 alts in the author's 2026-07-25 обзор
had their raw weekly ATL captured by that one print — ANKR 0.00001 (that bar closed at 0.01117),
UNI 0.30 (closed 5.223), SAND 0.008333 (closed 0.060852) — against real ATLs of 0.000637 / 1.7563 /
0.0288. The card then advertised an "all-time low" three orders of magnitude below any structure,
and could even list a ladder level BELOW its own ATL.
"""
from __future__ import annotations

import pytest

from hunt_core.prizrak.structure import spot_weekly_ladder


def _bar(o: float, h: float, low: float, c: float, v: float = 1000.0) -> list[float]:
    return [0.0, o, h, low, c, v]


def _swings(lo: float, hi: float, cycles: int) -> list[list[float]]:
    """Weekly bars oscillating lo↔hi so ``_pivots`` finds confirmed 3-bar swings at both edges."""
    bars: list[list[float]] = []
    mid = (lo + hi) / 2
    for _ in range(cycles):
        bars.append(_bar(mid, hi, mid * 0.99, hi * 0.998))
        bars.append(_bar(hi * 0.998, hi * 0.999, mid, mid))
        bars.append(_bar(mid, mid * 1.01, lo, lo * 1.002))
        bars.append(_bar(lo * 1.002, mid, lo * 0.999, mid))
    return bars


def test_liquidation_wick_does_not_become_the_all_time_low() -> None:
    """★ A bar closing far above its own low is a wick print, not the ATL."""
    bars = _swings(10.0, 20.0, cycles=6)
    clean = spot_weekly_ladder(bars, price=15.0)
    assert clean["atl"] is not None and 9.9 <= float(clean["atl"]) <= 10.1

    # Same series + one cascade bar: low 0.01, but it closed back at 14 — nothing traded at 0.01.
    bars.append(_bar(14.0, 14.2, 0.01, 14.0))
    bars += _swings(10.0, 20.0, cycles=2)
    wicked = spot_weekly_ladder(bars, price=15.0)
    assert float(wicked["atl"]) > 1.0, f"wick leaked into ATL: {wicked['atl']}"
    assert abs(float(wicked["atl"]) - float(clean["atl"])) < 0.5


def test_ladder_never_lists_a_level_outside_its_own_extremes() -> None:
    """A card saying «🟢 0.01700 · ATL 0.02880» contradicts itself — measured on SAND."""
    bars = _swings(10.0, 20.0, cycles=6)
    bars.append(_bar(14.0, 14.2, 0.01, 14.0))  # cascade wick
    bars += _swings(10.0, 20.0, cycles=2)
    out = spot_weekly_ladder(bars, price=15.0, max_levels_per_side=24)
    atl, ath = float(out["atl"]), float(out["ath"])
    for side in ("below", "above"):
        for lv in out[side]:
            assert atl <= float(lv["price"]) <= ath, f"{side} level outside [{atl}, {ath}]: {lv}"


def test_genuine_extreme_survives_when_the_bar_closed_near_it() -> None:
    """The filter rejects wicks, not real capitulation — a bar that CLOSED at the low keeps it."""
    bars = _swings(10.0, 20.0, cycles=6)
    bars.append(_bar(10.0, 10.1, 6.0, 6.2))  # closed at the low: the market did business there
    bars += _swings(10.0, 20.0, cycles=2)
    out = spot_weekly_ladder(bars, price=15.0)
    assert abs(float(out["atl"]) - 6.0) < 1e-9


def test_all_bars_rejected_still_reports_a_number() -> None:
    """I-6: an unusual series must not silently lose the field — fall back to the raw extreme.

    Exercised on the helper, not the ladder: a series degenerate enough to have EVERY bar rejected
    also has no confirmed swing pivots, so ``spot_weekly_ladder`` short-circuits before it gets here.
    """
    from hunt_core.prizrak.structure import _traded_extreme

    bars = [_bar(10.0, 10.0, 0.1, 10.0) for _ in range(12)]  # every bar is a 100× wick
    atl = _traded_extreme(bars, low=True)
    assert atl is not None and abs(atl - 0.1) < 1e-9
    assert _traded_extreme([], low=True) is None  # nothing to measure ⇒ None, not a fabricated 0.0


def test_the_atl_level_itself_survives_the_bounds_filter() -> None:
    """★ The deepest rung IS usually the ATL — a bare `>=` bound dropped it on float noise.

    A level merged from N pivots that all sit exactly AT the extreme gets a running-mean price a few
    ULPs below it (7 × 49.95 → 49.949999999999996), so an exact `price >= atl` test discarded the
    most-touched, deepest level in the ladder — silently, and precisely for the cleanest structures.
    """
    # Every low prints at the same price: the merged level lands a hair under it.
    bars: list[list[float]] = []
    for _ in range(8):
        bars.append(_bar(60.0, 70.0, 59.4, 69.86))
        bars.append(_bar(69.86, 70.0, 60.0, 60.0))
        bars.append(_bar(60.0, 60.6, 50.0, 50.1))
        bars.append(_bar(50.1, 60.0, 49.95, 60.0))
    out = spot_weekly_ladder(bars, price=58.5, max_levels_per_side=24)
    atl = float(out["atl"])
    assert abs(atl - 49.95) < 1e-9
    assert out["below"], "the ATL rung must survive its own bound"
    assert min(float(lv["price"]) for lv in out["below"]) == pytest.approx(atl, rel=1e-9)
