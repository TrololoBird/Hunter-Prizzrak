"""G-3: worst_entry_edge must return the LEAST-favorable fill so displayed R:R is
conservative — long → entry high (paid most), short → entry low (sold cheapest).
Both the contract helper and the tracker helper must agree on this convention.
"""
from __future__ import annotations

from hunt_core.contract import worst_entry_edge
from hunt_core.track._trailing import _worst_entry


def test_contract_long_worst_is_high() -> None:
    assert worst_entry_edge({"entry_zone": [100.0, 102.0]}, direction="long") == 102.0


def test_contract_short_worst_is_low() -> None:
    assert worst_entry_edge({"entry_zone": [100.0, 102.0]}, direction="short") == 100.0


def test_tracker_matches_contract_convention() -> None:
    active = {"entry_lo": 100.0, "entry_hi": 102.0}
    assert _worst_entry(active, direction="long") == 102.0
    assert _worst_entry(active, direction="short") == 100.0


def test_long_rr_is_conservative_vs_best_edge() -> None:
    # long zone [100,102], tp 110, stop 95. Worst edge (102): reward 8, risk 7 → R:R<best.
    edge = worst_entry_edge({"entry_zone": [100.0, 102.0]}, direction="long")
    assert edge is not None
    rr_worst = (110.0 - edge) / (edge - 95.0)
    rr_best = (110.0 - 100.0) / (100.0 - 95.0)
    assert rr_worst < rr_best
