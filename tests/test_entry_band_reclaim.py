"""G-13 (WO#3): Pattern C is «закреп ВЫШЕ предыдущего максимума» — an entry band whose
lower edge dips below the reclaimed level would be buying the continuation of the sweep
(the reclaim is gone), not averaging in. Doboры downward stay legal for the other
patterns (пересиживание under a wide stop), so only C is clamped; band width is
FLAGGED, never suppressed.
"""
from __future__ import annotations

from hunt_core.deliver.manipulation_delivery import _geometry
from hunt_core.scanner.detect.patterns import ManipulationSetup


def _c_setup(*, prior_high: float, struct_low: float, target: float) -> ManipulationSetup:
    return ManipulationSetup(
        direction="long",
        pattern_type="C",
        score=1.0,
        meso_tf="4h",
        swept_level=prior_high,     # the reclaimed level
        sweep_extreme=struct_low,   # wide stop anchor below the manipulation low
        target=target,
        target_ladder=(target,),
        micro_confirmed=True,
    )


def test_pattern_c_entry_band_never_below_reclaim() -> None:
    # Reclaim close at 105 over a prior high of 100; wide stop anchored at 80.
    setup = _c_setup(prior_high=100.0, struct_low=80.0, target=140.0)
    geo = _geometry(setup, price=105.0, stop_buffer=0.03)
    assert geo is not None
    assert geo["entry_lo"] >= 100.0, "entry must not reach below the reclaimed level"
    assert geo["reclaim_clamped"] is True
    # Доборы below the reclaim are dropped for the same reason.
    assert all(d >= 100.0 for d in geo["dobor_ladder"])


def test_non_reclaim_pattern_keeps_downward_dobor() -> None:
    # Pattern A floor long: averaging DOWN toward the wide stop is the method.
    setup = ManipulationSetup(
        direction="long",
        pattern_type="A",
        score=1.0,
        meso_tf="4h",
        swept_level=100.0,
        sweep_extreme=90.0,
        target=140.0,
        target_ladder=(140.0,),
        micro_confirmed=True,
    )
    geo = _geometry(setup, price=105.0, stop_buffer=0.03)
    assert geo is not None
    assert geo["entry_lo"] < 105.0  # band still extends toward the stop
    assert geo["reclaim_clamped"] is False


def test_wide_band_is_flagged_not_suppressed() -> None:
    setup = _c_setup(prior_high=100.0, struct_low=80.0, target=140.0)
    geo = _geometry(setup, price=105.0, stop_buffer=0.03)
    assert geo is not None  # never suppressed
    assert "band_wide" in geo and "band_width_pct" in geo
