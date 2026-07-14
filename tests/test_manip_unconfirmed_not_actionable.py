"""An unconfirmed manipulation setup must NOT render as an actionable plan (WO #1).

micro_confirmed gates actionability, not just score. Before the LTF reversal is
found, the full «📍 Вход» plan reads as go-now → the trader enters an unconfirmed
continuation of the sweep (realized: ALLO −13.55%). Unconfirmed → disavow the entry.
"""
from __future__ import annotations

from hunt_core.deliver.manipulation_delivery import (
    _format_manipulation_signal,
    _geometry,
    _stop_buffer,
)
from hunt_core.scanner.detect.patterns import ManipulationSetup


def _render(confirmed: bool) -> str:
    setup = ManipulationSetup(
        direction="long", pattern_type="C", score=1.0 if confirmed else 0.7,
        macro_tf="4h", meso_tf="1h", micro_tf="15m", micro_confirmed=confirmed,
        swept_level=100.0, sweep_extreme=90.0, target=140.0, target_ladder=(140.0,),
        entry_ref=100.0, total_steps=2, steps_covered=2 if confirmed else 1,
    )
    geo = _geometry(setup, price=100.0, stop_buffer=_stop_buffer([[0, 0, 105, 88, 100, 1]] * 30))
    assert geo is not None
    return _format_manipulation_signal("BTCUSDT", setup, price=100.0, geo=geo)


def test_confirmed_setup_shows_actionable_entry() -> None:
    txt = _render(True)
    assert "📍 Вход" in txt


def test_unconfirmed_setup_disavows_entry() -> None:
    txt = _render(False)
    assert "📍 Вход" not in txt  # no go-now marker
    assert "ОЖИДАНИЕ подтверждения" in txt
    assert "НЕ вход" in txt
