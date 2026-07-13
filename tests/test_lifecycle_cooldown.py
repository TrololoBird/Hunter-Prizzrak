"""The per-setup 4h cooldown must actually suppress re-emission.

The guard was `event=="signal" and prev_state=="signal"` — unreachable, because
event=="signal" is only set when prev_state is NOT "signal" (SIG-1). With the dead
conjunct removed, a setup that emitted a signal and then oscillated back into the
near-entry state within 4h is suppressed; the first emission (no prior emit) and
emissions after the cooldown window still pass.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
            "activation": "near_entry",  # → a fresh "signal" event from a non-signal prev_state
            "catalyst_level": 100.0,
            "path": "retest",
            "entry_lo": 99.0,
            "entry_hi": 101.0,
        },
    }


def _store_with_emit(*, age: timedelta) -> SignalLifecycleStore:
    store = SignalLifecycleStore(entries={})
    setup_id = compute_setup_id(thesis_kind="retest", anchor_level=100.0, direction="long")
    store.entries[setup_id] = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "state": "forming",  # NOT in {signal,activated,tracking} → event="signal" fires
        "last_emit_at": (datetime.now(UTC) - age).isoformat(),
    }
    return store


def test_recent_emit_suppressed_by_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hunt_core.signals.price_sanity.price_sanity_check", lambda row, **k: (True, "ok"))
    store = _store_with_emit(age=timedelta(hours=1))  # emitted 1h ago (<4h)
    t = process_lifecycle_tick(_row(), store=store, commit=False)
    assert t.event == "none"
    assert t.suppress_reason == "setup_cooldown"


def test_old_emit_passes_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hunt_core.signals.price_sanity.price_sanity_check", lambda row, **k: (True, "ok"))
    store = _store_with_emit(age=timedelta(hours=5))  # >4h → cooldown clear
    t = process_lifecycle_tick(_row(), store=store, commit=False)
    assert t.event == "signal"
