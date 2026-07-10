from __future__ import annotations
from unittest.mock import patch

from hunt_core.scanner.detect.patterns import (
    advance_manipulation_state,
    _C_TOTAL_STEPS,
)


def _ascending_leg(rows, start_price, peak_price, trough_price, vol=100):
    """Append bars forming a single up-leg in an ascending channel.
    Peak must be a local swing high (2 following bars with lower high).
    Trough must be a higher low than previous trough.
    """
    I = 3_600_000.0
    ts = rows[-1][0] + I if rows else 0.0
    def bar(o, h, l, c, v=100):
        nonlocal ts
        rows.append([ts, float(o), float(h), float(l), float(c), float(v)])
        ts += I
    # Push up: 2 bars
    mid = (start_price + peak_price) / 2
    bar(mid, mid+1, mid-0.5, mid)
    bar(peak_price-0.5, peak_price, peak_price-1, peak_price-0.5, vol)  # peak
    # Pullback: 2 bars with lower highs
    bar(peak_price-0.5, peak_price-0.5, peak_price-1.5, peak_price-1)
    bar(peak_price-1, peak_price-1, trough_price, peak_price-1.5)
    # Trough: 1 bar then start next push
    return rows

def _descending_leg(rows, start_price, trough_price, vol=100):
    """Append bars forming a single down-leg (lower high, lower low)."""
    I = 3_600_000.0
    ts = rows[-1][0] + I if rows else 0.0
    def bar(o, h, l, c, v=100):
        nonlocal ts
        rows.append([ts, float(o), float(h), float(l), float(c), float(v)])
        ts += I
    # First bar of leg: lower high than previous peak
    leg_high = start_price - 0.5
    bar(leg_high-0.5, leg_high, leg_high-1, leg_high-0.5)
    bar(leg_high-1, leg_high-0.5, leg_high-1.5, leg_high-1)
    # Push down to trough
    bar(trough_price+0.5, trough_price+1, trough_price, trough_price+0.5, vol)  # swing low candidate
    bar(trough_price+0.5, trough_price+1, trough_price, trough_price+0.5)  # same
    # Bounce with 2 bars of higher lows
    bar(trough_price+1, trough_price+1.5, trough_price+0.5, trough_price+1)
    return rows


def test_pattern_c_emits_after_break_above_prior_high():
    """Pattern C (Type 2) must arm when prior_high is found and emit once
    price breaks above it, with channel context from is_ascending_channel
    and is_descending_channel (mocked for deterministic testing)."""

    macro_rows = [
        [i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for i in range(100)
    ]

    # 80 bars of 1h — flat except a clear swing-high peak at bar 50 (high=111).
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

    micro_rows = [
        [i * 900_000.0, 100.0, 101.0, 99.0, 100.0, 50.0]
        for i in range(30)
    ]

    ohlcv_by_tf = {
        "1d": macro_rows,
        "1h": meso_rows,
        "15m": micro_rows,
    }

    # Tick 1 — should arm (stage 0 → 1)
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

    # Tick 2 — break above prior_high, with channel context mocked to pass.
    base_ts_meso = 80 * 3_600_000.0
    new_meso = list(meso_rows)
    for i in range(10):
        ts = base_ts_meso + i * 3_600_000.0
        bp = prior_high + 1.0 + i * 0.1
        new_meso.append([ts, bp - 0.5, bp + 0.5, bp - 1.0, bp, 300.0])
    ohlcv_by_tf["1h"] = new_meso

    with (
        patch("hunt_core.scanner.detect.patterns.is_ascending_channel", return_value=True),
        patch("hunt_core.scanner.detect.patterns.is_descending_channel", return_value=True),
    ):
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
