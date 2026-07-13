"""The dump (short) forecast must report the NEAREST downward target as primary.

For downward targets min() is the deepest/farthest zone; the long builders use
min(upward) = nearest, so the short must use max(downward) = nearest to stay
symmetric. Using min made the short's expected_move overstate the drop (FCAST-1).
"""
from __future__ import annotations

import pytest

from hunt_core.toolkit import forecast


def test_primary_is_nearest_downward_target(monkeypatch: pytest.MonkeyPatch) -> None:
    # Targets below price 100: nearest = 90 (max), deepest = 80 (min).
    monkeypatch.setattr(forecast, "_collect_downward_targets", lambda row, price: ([80.0, 85.0, 90.0], ["f"]))
    out = forecast.build_dump_forecast({"price": 100.0})
    assert out is not None
    assert out["target_primary"] == 90.0                 # nearest, not the deepest 80
    assert out["expected_move_pct"] == pytest.approx(-10.0)  # to nearest, not -20
    # Zone bounds still span deepest→nearest.
    assert out["target_lo"] == 80.0 and out["target_hi"] == 90.0


def test_no_targets_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forecast, "_collect_downward_targets", lambda row, price: ([], []))
    assert forecast.build_dump_forecast({"price": 100.0}) is None
