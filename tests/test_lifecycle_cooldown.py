"""The lifecycle state machine itself is the re-emission dedup.

Audit R2 (chunk 7) removed the per-setup 4h cooldown branch: it was unreachable in
production — event=="signal" only fires when the store has NO entry for the setup_id
(record_emit always stores state "signal"/"activated", so any existing entry already
suppresses via prev_state), and a missing entry always passed the cooldown check.
These tests pin the property the cooldown pretended to provide: once a setup emitted,
the same setup does not re-emit "signal"; a fresh setup emits.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hunt_core.signals.lifecycle import (
    SignalLifecycleStore,
    compute_setup_id,
    process_lifecycle_tick,
)


def _row() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "as_of": datetime.now(UTC).isoformat(),
        "prizrak_summary": {
            "action": "long",
            "activation": "near_entry",
            "catalyst_level": 100.0,
            "path": "retest",
            "entry_lo": 99.0,
            "entry_hi": 101.0,
        },
    }


def _store_after_emit() -> SignalLifecycleStore:
    """Store state as record_emit actually leaves it (state='signal')."""
    store = SignalLifecycleStore(entries={})
    setup_id = compute_setup_id(thesis_kind="retest", anchor_level=100.0, direction="long")
    store.entries[setup_id] = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "state": "signal",
        "last_emit_at": datetime.now(UTC).isoformat(),
    }
    return store


def test_first_emission_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hunt_core.signals.price_sanity.price_sanity_check", lambda row, **k: (True, "ok"))
    t = process_lifecycle_tick(_row(), store=SignalLifecycleStore(entries={}), commit=False)
    assert t.event == "signal"
    assert t.signal is not None and t.signal.symbol == "BTCUSDT"


def test_already_emitted_setup_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hunt_core.signals.price_sanity.price_sanity_check", lambda row, **k: (True, "ok"))
    t = process_lifecycle_tick(_row(), store=_store_after_emit(), commit=False)
    assert t.event == "none"
    assert t.suppress_reason == "no_state_advance"


def test_activation_advance_still_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hunt_core.signals.price_sanity.price_sanity_check", lambda row, **k: (True, "ok"))
    row = _row()
    summary = row["prizrak_summary"]
    assert isinstance(summary, dict)
    summary["activation"] = "in_entry_zone"
    t = process_lifecycle_tick(row, store=_store_after_emit(), commit=False)
    assert t.event == "activated"
