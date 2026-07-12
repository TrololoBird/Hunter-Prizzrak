"""Multi-timeframe participation in manipulation detection.

Three defects this locks down, all traced to the author's own method
(«лучше всего начинать анализ со старших таймфреймов и постепенно переходить
к младшим»):

1. Arbitration sorted by DIRECTION before scale, so a 15m long outranked a 4h
   short — inverting the hierarchy. The transcript's GTC short *is* the
   higher-TF move, not a shakeout inside a pump.
2. No ladder had a 1d detection frame, so daily-scale manipulations (the 100-400%
   ones) could not be detected at all.
3. A flat 48h stage timeout expired 4h- and 1d-scale states long before their
   30-bar bokovik could form, leaving only fast setups to emit.
"""
from __future__ import annotations

from unittest.mock import patch

from hunt_core.scanner.detect import patterns as P
from hunt_core.scanner.detect.state import (
    STEP_TIMEOUT_HOURS,
    is_stale,
    new_symbol_state,
    step_timeout_hours,
)

_BOKOVIK_DAYS = {"1d": 30.0, "4h": 5.0, "1h": 1.25}


def _setup(direction: str, meso_tf: str) -> P.ManipulationSetup:
    return P.ManipulationSetup(
        direction=direction, pattern_type="A" if direction == "long" else "B",
        score=1.0, meso_tf=meso_tf, micro_tf="15m", micro_confirmed=True,
        swept_level=100.0, sweep_extreme=95.0, target=120.0,
        entry_ref=100.0, evidence=(), steps_covered=5, total_steps=5,
    )


def test_daily_detection_ladder_exists():
    """A 1d meso frame must exist, else daily-scale manipulations are undetectable."""
    meso_frames = {meso for _macro, meso, _micro in P._TF_LADDERS}
    assert "1d" in meso_frames
    assert ("1w", "1d", "4h") in P._TF_LADDERS


def test_higher_timeframe_outranks_direction_in_arbitration():
    """A 4h short must beat a 15m long — scale decides before direction."""
    ohlcv = {tf: [[0, 1, 1, 1, 1, 1]] for tf in ("1w", "1d", "4h", "1h", "15m", "5m")}

    def fake_advance(symbol, o, state, *, now_ms, macro_tf, meso_tf, micro_tf, family, **kw):
        if meso_tf == "4h" and family == "B":
            return new_symbol_state(), _setup("short", "4h")
        if meso_tf == "15m" and family == "A":
            return new_symbol_state(), _setup("long", "15m")
        return new_symbol_state(), None

    with patch.object(P, "advance_manipulation_state", fake_advance):
        _, winner = P.advance_manipulation_scales("TEST", ohlcv, None, now_ms=0.0)

    assert winner is not None
    assert winner.meso_tf == "4h" and winner.direction == "short"


def test_long_still_wins_a_tie_at_equal_scale():
    """At the SAME scale the confirmed long outranks the shakeout short."""
    ohlcv = {tf: [[0, 1, 1, 1, 1, 1]] for tf in ("1w", "1d", "4h", "1h", "15m", "5m")}

    def fake_advance(symbol, o, state, *, now_ms, macro_tf, meso_tf, micro_tf, family, **kw):
        if meso_tf == "4h" and family == "B":
            return new_symbol_state(), _setup("short", "4h")
        if meso_tf == "4h" and family == "A":
            return new_symbol_state(), _setup("long", "4h")
        return new_symbol_state(), None

    with patch.object(P, "advance_manipulation_scales", P.advance_manipulation_scales), \
         patch.object(P, "advance_manipulation_state", fake_advance):
        _, winner = P.advance_manipulation_scales("TEST", ohlcv, None, now_ms=0.0)

    assert winner is not None
    assert winner.meso_tf == "4h" and winner.direction == "long"


def test_stage_timeout_outlives_the_bokovik_it_waits_for():
    """Each meso frame must be given more time than its 30-bar consolidation needs."""
    for meso_tf, need_days in _BOKOVIK_DAYS.items():
        allowed_days = step_timeout_hours(meso_tf) / 24.0
        assert allowed_days > need_days, f"{meso_tf}: {allowed_days}d <= bokovik {need_days}d"


def test_fast_ladders_keep_the_48h_floor():
    assert step_timeout_hours("15m") == STEP_TIMEOUT_HOURS
    assert step_timeout_hours("5m") == STEP_TIMEOUT_HOURS
    assert step_timeout_hours(None) == STEP_TIMEOUT_HOURS


def test_is_stale_uses_the_ladder_scale():
    """A 4h-scale state must survive the 48h that used to reset it."""
    hour_ms = 3_600_000.0
    state_4h = {**new_symbol_state(), "anchor_ts": 1.0, "meso_tf": "4h"}
    state_15m = {**new_symbol_state(), "anchor_ts": 1.0, "meso_tf": "15m"}
    at_72h = 1.0 + 72 * hour_ms

    assert is_stale(state_15m, now_ms=at_72h) is True
    assert is_stale(state_4h, now_ms=at_72h) is False
    assert is_stale(state_4h, now_ms=1.0 + 200 * hour_ms) is True


def test_weekly_macro_does_not_demand_two_years_of_history():
    """90 weekly bars (~1.7y) would make the 1w ladder dead for recent listings."""
    assert P._MACRO_MIN_BARS["1w"] < P._MACRO_MIN_BARS_DEFAULT
