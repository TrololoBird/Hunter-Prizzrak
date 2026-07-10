"""Persistent per-symbol manipulation-tracking state.

Redesign rationale: the previous version re-derived the entire multi-step
pattern (impulse -> absorption -> bokovik -> sweep -> structure break) from a
single static OHLCV window on every scan call. That invites false positives
by construction — different steps get matched to different bars picked
independently out of the same rolling window, so a coincidental combination
of an old wick here and a fresh candle there can satisfy every check at
once, even when they aren't actually one causal sequence (backtested against
real manipulations: this is exactly how Pattern B fired short on genuine
long pumps).

The transcripts describe a trader's *process over time*: notice a setup
forming, wait — sometimes across many chart checks — for the next
confirming event, and only then act. This module makes that literal: once a
step is confirmed, its bar timestamp is frozen into the persisted state, and
every later step is searched only in bars strictly after that timestamp. A
step can never be satisfied by a bar that occurred before the previous step.

State is a plain JSON-serializable dict so it round-trips through
load_scanner_state/save_scanner_state (hunt_core/paths.SCANNER_STATE) with no
custom (de)serialization step, mirroring hunt_core/track/tracker.py's
tracker-state pattern.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

Direction = Literal["long", "short"]
PatternType = Literal["A", "A3", "B", "C"]

# If a step doesn't advance within this many hours of the previous one being
# confirmed, the tracked setup is stale — the trader's own rule ("если у нас
# нету полноценного закрепа... цена может пойти дальше вниз") is to abandon
# it rather than wait indefinitely.
STEP_TIMEOUT_HOURS = 48.0


def new_symbol_state() -> dict[str, Any]:
    """A symbol with no manipulation currently being tracked."""
    return {"pattern": None, "stage": 0, "anchor_ts": 0.0, "first_ts": 0.0, "data": {}}


def is_stale(state: dict[str, Any], *, now_ms: float) -> bool:
    anchor_ts = state.get("anchor_ts") or 0.0
    if not anchor_ts:
        return False
    age_hours = (now_ms - anchor_ts) / 3_600_000.0
    return age_hours > STEP_TIMEOUT_HOURS


def load_scanner_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_scanner_state(states: dict[str, dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, indent=2, default=str), encoding="utf-8")


__all__ = [
    "Direction", "PatternType", "STEP_TIMEOUT_HOURS",
    "new_symbol_state", "is_stale",
    "load_scanner_state", "save_scanner_state",
]
