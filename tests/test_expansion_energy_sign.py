"""Expansion-readiness energy must reward surges, not droughts (SCAN-2).

`_z_component` fed vol_z / trade_z scored |z|, so a drought (z far below the
rolling mean) produced the same energy as an equal-magnitude surge — dead coins
read as "about to expand". Only positive z (activity above the mean) may add
energy now.
"""
from __future__ import annotations

from hunt_core.scanner.detect.expansion_readiness import _z_component


def test_surge_contributes_energy() -> None:
    assert _z_component(3.0, weight=0.35) > 0.0


def test_drought_contributes_nothing() -> None:
    # A large negative z (volume/trade far below average) must add zero energy.
    assert _z_component(-3.0, weight=0.35) == 0.0
    assert _z_component(-0.5, weight=0.20) == 0.0


def test_none_is_zero() -> None:
    assert _z_component(None) == 0.0


def test_symmetric_magnitude_no_longer_equal() -> None:
    # The old abs() bug made +z and -z equal; they must now diverge.
    assert _z_component(2.5, weight=0.35) != _z_component(-2.5, weight=0.35)
