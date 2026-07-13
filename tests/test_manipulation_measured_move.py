"""Regression: measured-move fallback target in _geometry.

Grounded on the «Owner of SHORT» channel: when the structural ladder has NO pool within
reach (only a far dead peak after a round-trip), the author still trades a MEASURED %-move
target (10–20 % чистого). The detector must project a frame-scaled measured target instead
of abstaining — but must NOT override a reachable structural target, and the R:R gate must
still apply. Verified: recovered UNI, VELVET, SAFE (real author longs) with no regressions.
"""
from __future__ import annotations

from hunt_core.deliver import manipulation_delivery as md
from hunt_core.scanner.detect.patterns import ManipulationSetup


def _setup(*, target, ladder=(), meso_tf="1h") -> ManipulationSetup:
    # Long, sweep_extreme=95 → stop 92.15 at buf 0.03 → risk 7.85 from price 100.
    return ManipulationSetup(
        direction="long", pattern_type="A", score=1.0, macro_tf="4h", meso_tf=meso_tf,
        micro_tf="15m", micro_confirmed=True,
        swept_level=96.0, sweep_extreme=95.0,  # depth ~1% clears the sweep-depth gate
        target=target, target_ladder=tuple(ladder), entry_ref=100.0,
        evidence=("regression",), steps_covered=5, total_steps=5,
    )


def test_projects_measured_move_when_only_far_dead_peak() -> None:
    # No reachable structural pool; setup.target is a far dead peak (400 % > 80 % cap on 1h).
    geo = md._geometry(_setup(target=500.0), price=100.0, stop_buffer=0.03)
    assert geo is not None, "should deliver via measured-move projection"
    assert geo["projected"] is True
    # 1h measured move = 18 % → target ~118
    assert abs(geo["primary_target"] - 118.0) < 0.5
    assert geo["rr_tp1"] >= md._MIN_RR


def test_reachable_structural_target_is_not_overridden() -> None:
    # setup.target = 105 (5 %, within cap) but cannot repay the wide stop → still rejected,
    # NOT rescued by a fabricated measured move.
    assert md._geometry(_setup(target=105.0), price=100.0, stop_buffer=0.03) is None


def test_reachable_structural_target_used_unchanged() -> None:
    geo = md._geometry(_setup(target=120.0), price=100.0, stop_buffer=0.03)
    assert geo is not None
    assert geo["projected"] is False
    assert abs(geo["primary_target"] - 120.0) < 1e-6


def test_measured_move_scales_by_frame() -> None:
    assert md._MEASURED_MOVE_BY_TF["15m"] == 0.12
    assert md._MEASURED_MOVE_BY_TF["1h"] == 0.18
    # A 15m frame projects a nearer target than 1h.
    g15 = md._geometry(_setup(target=500.0, meso_tf="15m"), price=100.0, stop_buffer=0.03)
    g1h = md._geometry(_setup(target=500.0, meso_tf="1h"), price=100.0, stop_buffer=0.03)
    assert g15 is not None and g1h is not None
    assert g15["primary_target"] < g1h["primary_target"]
