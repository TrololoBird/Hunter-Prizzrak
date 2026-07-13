"""Counter-bias flip detection must anchor on the immutable ENTRY bias (TRACK-3).

When entry_lifecycle_bias was empty at signal creation, the old code fell back
to the mutable lifecycle_bias — overwritten to the current tick each pass — so
the counter-flip check compared against the previous tick, not the entry, and a
slow entry→counter drift was never flagged. _entry_bias_latch latches the first
observed bias and keeps it fixed.
"""
from __future__ import annotations

from hunt_core.track._followups import _entry_bias_latch


def test_existing_entry_bias_is_returned_unchanged() -> None:
    active = {"entry_lifecycle_bias": "long"}
    assert _entry_bias_latch(active, "short") == "long"
    assert active["entry_lifecycle_bias"] == "long"  # not overwritten by current bias


def test_empty_entry_bias_latches_first_observed() -> None:
    active: dict[str, object] = {}
    assert _entry_bias_latch(active, "long") == "long"
    assert active["entry_lifecycle_bias"] == "long"


def test_latched_bias_survives_subsequent_ticks() -> None:
    # Entry bias unset; first tick sees "long" and latches it.
    active: dict[str, object] = {"lifecycle_bias": "long"}
    assert _entry_bias_latch(active, "long") == "long"
    # A later tick flips to "short": opened bias must STILL read the latched entry.
    active["lifecycle_bias"] = "short"  # mutable field drifts...
    assert _entry_bias_latch(active, "short") == "long"  # ...but entry stays "long"


def test_empty_bias_when_nothing_observed_yet() -> None:
    active: dict[str, object] = {}
    assert _entry_bias_latch(active, "") == ""
    assert "entry_lifecycle_bias" not in active
