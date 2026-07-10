from __future__ import annotations

from hunt_core.scanner.detect.patterns import (
    advance_manipulation_state,
    _C_TOTAL_STEPS,
)
from hunt_core.scanner.detect.events import ohlcv_to_df


def test_pattern_c_emits_after_break_above_prior_high():
    """Pattern C must arm when prior_high is found and emit once price breaks above it,
    even if the break happens on a later tick (no hard reset on sub-95% proximity)."""

    # ── macro (1d): flat 100 bars — just needs to be long enough to pass the
    #    _MACRO_LOOKBACK_BARS // 2 gate in advance_manipulation_state.
    macro_rows = [
        [i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for i in range(100)
    ]

    # ── meso (1h): build a clear swing-high peak at bar 50 (high=111),
    #    then a fall back to 100, then flat. 80 bars total so
    #    _prior_swing_high(lookback=60, exclude_last=15) can find it.
    meso_rows = []
    for i in range(80):
        ts = i * 3_600_000.0
        if i < 40:
            o, h, l, c, v = 100.0, 102.0, 98.0, 100.0, 100.0
        elif i < 50:
            p = 100.0 + (i - 39) * 1.0
            o, h, l, c, v = p - 0.3, p + 0.3, p - 0.5, p, 150.0
        elif i == 50:
            o, h, l, c, v = 109.5, 111.0, 109.0, 110.0, 200.0
        elif i < 65:
            p = 110.0 - (i - 50) * 0.67
            o, h, l, c, v = p - 0.3, p + 0.3, p - 0.5, p, 100.0
        else:
            o, h, l, c, v = 100.0, 101.0, 99.0, 100.0, 100.0
        meso_rows.append([ts, o, h, l, c, v])

    # ── micro (15m): 30 bars flat around 100 — not the break bar yet.
    micro_rows = [
        [i * 900_000.0, 100.0, 101.0, 99.0, 100.0, 50.0]
        for i in range(30)
    ]

    ohlcv_by_tf = {
        "1d": macro_rows,
        "1h": meso_rows,
        "15m": micro_rows,
    }

    # Tick 1 — pattern should arm (stage 0 → 1) and persist prior_high.
    state, setup = advance_manipulation_state(
        "TEST", ohlcv_by_tf, None,
        now_ms=80 * 3_600_000.0,
        macro_tf="1d", meso_tf="1h", micro_tf="15m", family="C",
    )
    assert setup is None, "Pattern C must not emit before the break"
    assert state.get("pattern") == "C"
    assert state.get("stage") == 1
    assert "prior_high" in state.get("data", {})
    prior_high = state["data"]["prior_high"]
    assert prior_high > 0

    # Tick 2 — append bars where price closes well above prior_high.
    base_ts_meso = 80 * 3_600_000.0
    base_ts_micro = 30 * 900_000.0
    new_meso = list(meso_rows)
    new_micro = list(micro_rows)
    for i in range(10):
        ts_meso = base_ts_meso + i * 3_600_000.0
        ts_micro = base_ts_micro + i * 900_000.0
        break_price = prior_high + 1.0 + i * 0.1
        new_meso.append([ts_meso, break_price - 0.5, break_price + 0.5, break_price - 1.0, break_price, 300.0])
        new_micro.append([ts_micro, break_price - 0.5, break_price + 0.5, break_price - 1.0, break_price, 150.0])

    ohlcv_by_tf["1h"] = new_meso
    ohlcv_by_tf["15m"] = new_micro

    state, setup = advance_manipulation_state(
        "TEST", ohlcv_by_tf, state,
        now_ms=base_ts_meso + 10 * 3_600_000.0,
        macro_tf="1d", meso_tf="1h", micro_tf="15m", family="C",
    )

    assert setup is not None, "Pattern C must emit after break above prior_high"
    assert setup.direction == "long"
    assert setup.pattern_type == "C"
    assert setup.steps_covered == _C_TOTAL_STEPS
    assert "break_above_prior_high" in setup.evidence


def test_pattern_c_does_not_arm_without_prior_swing_high():
    """Pattern C must reset when no prior swing high exists in the lookback window."""
    macro_rows = [
        [i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for i in range(100)
    ]
    # Pure flat data — no swing highs.
    meso_rows = [
        [i * 3_600_000.0, 100.0, 102.0, 98.0, 100.0, 100.0]
        for i in range(80)
    ]
    micro_rows = [
        [i * 900_000.0, 100.0, 101.0, 99.0, 100.0, 50.0]
        for i in range(30)
    ]

    ohlcv_by_tf = {
        "1d": macro_rows,
        "1h": meso_rows,
        "15m": micro_rows,
    }

    state, setup = advance_manipulation_state(
        "TEST", ohlcv_by_tf, None,
        now_ms=80 * 3_600_000.0,
        macro_tf="1d", meso_tf="1h", micro_tf="15m", family="C",
    )
    assert setup is None
    assert state.get("pattern") is None


def test_pattern_c_arms_even_when_price_far_below_prior_high():
    """Stage 0 must persist prior_high and advance to stage 1 even when current
    close is well below the level (no 95% proximity hard-reset)."""
    macro_rows = [
        [i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for i in range(100)
    ]
    # Same swing-high structure but price is at 90 (far below 111*0.95=105.45).
    meso_rows = []
    for i in range(80):
        ts = i * 3_600_000.0
        if i < 40:
            o, h, l, c, v = 90.0, 92.0, 88.0, 90.0, 100.0
        elif i < 50:
            p = 90.0 + (i - 39) * 2.0
            o, h, l, c, v = p - 0.3, p + 0.3, p - 0.5, p, 150.0
        elif i == 50:
            o, h, l, c, v = 109.5, 111.0, 109.0, 110.0, 200.0
        elif i < 65:
            p = 110.0 - (i - 50) * 0.67
            o, h, l, c, v = p - 0.3, p + 0.3, p - 0.5, p, 100.0
        else:
            o, h, l, c, v = 90.0, 91.0, 89.0, 90.0, 100.0
        meso_rows.append([ts, o, h, l, c, v])

    micro_rows = [
        [i * 900_000.0, 90.0, 91.0, 89.0, 90.0, 50.0]
        for i in range(30)
    ]

    ohlcv_by_tf = {
        "1d": macro_rows,
        "1h": meso_rows,
        "15m": micro_rows,
    }

    state, setup = advance_manipulation_state(
        "TEST", ohlcv_by_tf, None,
        now_ms=80 * 3_600_000.0,
        macro_tf="1d", meso_tf="1h", micro_tf="15m", family="C",
    )
    # Must arm (stage 0 → 1) instead of hard-resetting.
    assert setup is None
    assert state.get("pattern") == "C"
    assert state.get("stage") == 1
    assert "prior_high" in state.get("data", {})
