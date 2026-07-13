"""Regression: the sweep-depth gate in ``_geometry`` (deliver/manipulation_delivery).

Grounded on real O/USDT bars (`scripts/ground_manip_geometry`): the scanner was
shipping Pattern-A longs whose «свип» pierced the level by only 0.07–0.34% — chart
noise, not a liquidity grab — while every genuine corpus winner swept ≥1%. A fixed
stop buffer bolted onto a sub-0.5% wiggle put the stop 15–45× the sweep depth below
entry. ``_geometry`` must abstain on such non-sweeps and still pass real ones.
"""
from __future__ import annotations

from hunt_core.deliver import manipulation_delivery as md
from hunt_core.scanner.detect.patterns import ManipulationSetup


def _long(swept_level: float, sweep_extreme: float, *, pattern: str = "A") -> ManipulationSetup:
    # Reachable structural pool (~+38–53%, within the 1h 80% cap) so only the
    # sweep-depth gate — not the target-distance gate — decides these cases.
    ladder = (swept_level * 1.38, swept_level * 1.45, swept_level * 1.53)
    return ManipulationSetup(
        direction="long",
        pattern_type=pattern,
        score=1.0,
        macro_tf="1h",
        meso_tf="1h",
        micro_tf="15m",
        micro_confirmed=True,
        swept_level=swept_level,
        sweep_extreme=sweep_extreme,
        target=ladder[0],
        target_ladder=ladder,
        entry_ref=swept_level,
        evidence=("regression",),
        steps_covered=5,
        total_steps=5,
    )


def test_shallow_sweep_rejected() -> None:
    # Real O/USDT junk: 0.07% "sweep" → not a liquidity grab → abstain.
    setup = _long(0.55300, 0.55260)  # depth 0.07%
    assert md._geometry(setup, price=0.55589, stop_buffer=0.03) is None


def test_borderline_shallow_sweep_rejected() -> None:
    setup = _long(0.55420, 0.55230)  # depth 0.34% < 0.5%
    assert md._geometry(setup, price=0.55420, stop_buffer=0.03) is None


def test_real_sweep_delivered() -> None:
    # Real ZEREBRO winner geometry: 1.16% sweep clears the gate.
    setup = _long(0.03713, 0.03670)  # depth 1.16%
    geo = md._geometry(setup, price=0.03713, stop_buffer=0.03)
    assert geo is not None
    assert geo["stop"] < setup.sweep_extreme  # long stop below the swept extreme


def test_a3_exempt_from_sweep_depth_gate() -> None:
    # Pattern A3 has no real sweep (synthetic ATR extreme); a shallow "depth" must
    # not gate it out — it is governed by the other geometry gates only.
    setup = _long(0.03569, 0.03560, pattern="A3")  # depth 0.25%, but A3
    geo = md._geometry(setup, price=0.03569, stop_buffer=0.03)
    assert geo is not None
