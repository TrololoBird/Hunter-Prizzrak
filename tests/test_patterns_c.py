from __future__ import annotations
from unittest.mock import patch

from hunt_core.scanner.detect.patterns import (
    advance_manipulation_state,
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


def test_pattern_c_arms_at_stage0_before_zakrep():
    """With a prior swing high found but price NOT yet holding above it, Pattern C
    arms at stage 0 and persists prior_high — it does NOT emit on a mere approach."""

    macro_rows = [
        [i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for i in range(100)
    ]

    # 80 bars of 1h — flat except a clear swing-high peak at bar 50 (high=111),
    # price back at ~100 (below the level, no закреп).
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

    ohlcv_by_tf = {"1d": macro_rows, "1h": meso_rows, "15m": micro_rows}

    state, setup = advance_manipulation_state(
        "TEST", ohlcv_by_tf, None,
        now_ms=80 * 3_600_000.0,
        macro_tf="1d", meso_tf="1h", micro_tf="15m", family="C",
    )
    assert setup is None, "Pattern C must not emit without a held reclaim (закреп)"
    assert state.get("pattern") == "C"
    assert state.get("stage") == 0, "no закреп yet → stays armed at stage 0"
    assert state["data"]["prior_high"] > 0


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
    # Must arm at stage 0 (no закреп yet) and persist prior_high, not hard-reset.
    assert setup is None
    assert state.get("pattern") == "C"
    assert state.get("stage") == 0
    assert "prior_high" in state.get("data", {})


def _c_meso(zakrep_vol: float):
    """80 1h bars: peak 111 @bar50 → pull back → rise back → last 2 bars CLOSE and
    HOLD above 111 (закреп) with ``zakrep_vol``. Returns the meso rows."""
    meso = []
    for i in range(78):
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
            p = 100.0 + (i - 64) * 0.9  # rise back toward the reclaimed high
            o, h, l, c, v = p - 0.3, p + 0.3, p - 0.5, p, 100.0
        meso.append([ts, o, h, l, c, v])
    # закреп: 2 consecutive closes held above 111 with volume behind them
    meso.append([78 * 3_600_000.0, 112.0, 113.5, 111.8, 113.0, zakrep_vol])
    meso.append([79 * 3_600_000.0, 113.0, 114.5, 112.8, 114.0, zakrep_vol])
    return meso


def _emit_c(zakrep_vol: float = 400.0, htf_bias: str | None = None):
    """Run Pattern C once on a held-reclaim (закреп) frame. Returns (state, setup)."""
    macro_rows = [[i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0] for i in range(100)]
    meso_rows = _c_meso(zakrep_vol)
    micro_rows = [[i * 900_000.0, 100.0, 101.0, 99.0, 100.0, 50.0] for i in range(30)]
    ohlcv_by_tf = {"1d": macro_rows, "1h": meso_rows, "15m": micro_rows}
    with (
        patch("hunt_core.scanner.detect.patterns.is_ascending_channel", return_value=True),
        patch("hunt_core.scanner.detect.patterns.is_descending_channel", return_value=True),
    ):
        return advance_manipulation_state(
            "TEST", ohlcv_by_tf, None, now_ms=80 * 3_600_000.0,
            macro_tf="1d", meso_tf="1h", micro_tf="15m", family="C", htf_bias=htf_bias,
        )


def test_pattern_c_emits_on_held_reclaim_with_wide_stop():
    """Type 2 (MANIPULATION module): a HELD reclaim (закреп ≥2 closes) with volume is
    the entry event — enter on the закреп with a WIDE stop below the manipulation low,
    leaving room for добор + пересиживание. NOT a tight Prizrak-style retest entry."""
    state, setup = _emit_c()
    assert setup is not None, "held reclaim + volume must emit"
    assert setup.direction == "long"
    assert setup.pattern_type == "C"
    assert "zakrep_reclaim" in setup.evidence and "wide_stop_dobor" in setup.evidence
    # The stop anchor (sweep_extreme) must sit WELL BELOW the entry — a wide,
    # sit-through stop, not a ~2% retest stop under the reclaimed high.
    assert setup.sweep_extreme < setup.entry_ref
    assert (setup.entry_ref - setup.sweep_extreme) / setup.entry_ref > 0.05


def test_pattern_c_emits_under_bearish_htf():
    """Type 2 forms INSIDE a bearish structure — a bearish HTF must not veto it."""
    _, setup = _emit_c(htf_bias="bear")
    assert setup is not None, "bearish HTF must not veto Pattern C"
    assert setup.direction == "long"


def test_pattern_c_requires_bullish_volume_hard_gate():
    """«если нету полноценного закрепа, если нету бычьих объёмов — цена может пойти вниз».

    Without buyers behind the закреп it is a fakeout: hard gate at the reclaim stage.
    """
    state, setup = _emit_c(zakrep_vol=100.0)  # no volume spike
    assert setup is None
    assert state.get("pattern") is None, "закреп without bullish volume must reset, not arm"
