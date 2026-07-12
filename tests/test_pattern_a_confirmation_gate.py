"""Type-1 / Type-3 long-floor entries must wait for the LTF слом, not fire on the
bare sweep/floor.

Backtest (research/backtest_scanner.py over dataset_v8+v9) showed every Pattern A and
A3 emission was ltf_pending — by construction the 15m up-break cannot exist on the same
bar the floor low is swept — and they went 0 win / 44 loss / 30 timeout. The author's
own reliable floor long is «когда у нас будет подтверждение слома структуры нисходящей».
So the sweep/floor now ARMS a wait-stage and emission is gated on bos_up/choch_bull.
This locks in that no A/A3 setup is emitted without micro confirmation.
"""
from __future__ import annotations

from unittest.mock import patch

from hunt_core.scanner.detect import patterns as P


def _flat_micro(n: int = 40) -> list[list[float]]:
    # No up-swing → bos_up/choch_bull both False.
    return [[i * 900_000.0, 100.0, 100.5, 99.5, 100.0, 50.0] for i in range(n)]


def test_pattern_a_does_not_emit_on_bare_sweep_without_ltf_slom():
    """Advance Pattern A to the swept-floor stage, then tick with a flat micro frame:
    it must ARM (stage 4) and NOT emit until the LTF слом appears."""
    # Drive the A state machine directly through its stages with mocks so the test is
    # about the confirmation GATE, not the (separately tested) detection of each stage.
    state = {"pattern": "A", "stage": 2, "meso_tf": "1h",
             "data": {"impulse_idx": 5, "absorption_confirmed": True}}
    # A minimal meso frame with a detectable bokovik low that then gets swept.
    meso = [[i * 3_600_000.0, 100.0, 101.0, 99.0, 100.0, 100.0] for i in range(40)]
    meso += [[(40 + i) * 3_600_000.0, 100.0, 100.5, 95.0, 96.0, 120.0] for i in range(5)]  # sweep low
    macro = [[i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0] for i in range(100)]
    ohlcv = {"1d": macro, "1h": meso, "15m": _flat_micro()}

    with patch("hunt_core.scanner.detect.patterns.detect_bokovik",
               return_value={"lo": 99.0, "hi": 101.0, "poc": 100.0}), \
         patch("hunt_core.scanner.detect.patterns.detect_sweep_low",
               return_value=(True, 95.0, 0)):
        # stage 2 → 3 (bokovik), 3 → 4 (sweep arms), 4 stays pending (flat micro).
        for _ in range(3):
            state, setup = P.advance_manipulation_state(
                "T", ohlcv, state, now_ms=50 * 3_600_000.0,
                macro_tf="1d", meso_tf="1h", micro_tf="15m", family="A",
            )
            assert setup is None, "no Type-1 floor entry without the LTF слом"
        assert state.get("stage") == 4, "swept floor must ARM and wait for confirmation"


def test_pattern_a3_does_not_emit_on_bare_floor_without_ltf_slom():
    """A3 at the accumulation floor must arm (stage 2) and wait for the LTF слом."""
    state = {"pattern": "A3", "stage": 1, "meso_tf": "1h",
             "data": {"bokovik": {"lo": 100.0, "hi": 110.0, "poc": 105.0}}}
    # Price sitting in the lower half of the range, flat micro (no слом).
    meso = [[i * 3_600_000.0, 101.0, 102.0, 100.5, 101.0, 100.0] for i in range(40)]
    macro = [[i * 86_400_000.0, 100.0, 101.0, 99.0, 100.0, 1000.0] for i in range(100)]
    ohlcv = {"1d": macro, "1h": meso, "15m": _flat_micro()}
    # stage 1 → 2 (arm), stage 2 stays pending under a flat micro frame.
    for _ in range(2):
        state, setup = P.advance_manipulation_state(
            "T", ohlcv, state, now_ms=50 * 3_600_000.0,
            macro_tf="1d", meso_tf="1h", micro_tf="15m", family="A",
        )
        assert setup is None, "no A3 floor entry without the LTF слом"
    assert state.get("stage") == 2, "floor must ARM and wait for confirmation"
