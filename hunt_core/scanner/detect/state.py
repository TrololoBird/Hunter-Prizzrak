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

from pathlib import Path
from typing import Any, Literal

from hunt_core import serde

Direction = Literal["long", "short"]
PatternType = Literal["A", "A3", "B", "C"]

# If a step doesn't advance within this many hours of the previous one being
# confirmed, the tracked setup is stale — the trader's own rule ("если у нас
# нету полноценного закрепа... цена может пойти дальше вниз") is to abandon
# it rather than wait indefinitely. Floor, used by the fast ladders.
STEP_TIMEOUT_HOURS = 48.0

_MESO_BAR_HOURS: dict[str, float] = {
    "1w": 168.0, "1d": 24.0, "4h": 4.0, "1h": 1.0, "15m": 0.25, "5m": 1.0 / 12.0,
}
# A stage must be given time measured in the meso frame's OWN bars, not in wall
# clock. detect_bokovik scans _BOKOVIK_WINDOW (30) bars, so a consolidation takes
# ~5 days to form on a 4h frame and ~1 month on a 1d frame. A flat 48h timeout
# reset those states long before the pattern could complete, so only 1h/15m-scale
# setups ever emitted — which is why delivered manipulation trades had a
# 16-minute median duration while the method targets multi-day moves.
_STEP_TIMEOUT_BARS = 40


def step_timeout_hours(meso_tf: str | None) -> float:
    """Stage timeout for a ladder whose detection frame is ``meso_tf``."""
    bar_hours = _MESO_BAR_HOURS.get(str(meso_tf or ""), 0.0)
    return max(STEP_TIMEOUT_HOURS, bar_hours * _STEP_TIMEOUT_BARS)


def new_symbol_state() -> dict[str, Any]:
    """A symbol with no manipulation currently being tracked."""
    return {"pattern": None, "stage": 0, "anchor_ts": 0.0, "first_ts": 0.0, "data": {}}


def is_stale(state: dict[str, Any], *, now_ms: float) -> bool:
    anchor_ts = state.get("anchor_ts") or 0.0
    if not anchor_ts:
        return False
    age_hours = (now_ms - anchor_ts) / 3_600_000.0
    return age_hours > step_timeout_hours(state.get("meso_tf"))


def load_scanner_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = serde.loads(path.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_scanner_state(states: dict[str, dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serde.dumps_str(states, indent=True), encoding="utf-8")


__all__ = [
    "Direction", "PatternType", "STEP_TIMEOUT_HOURS", "step_timeout_hours",
    "new_symbol_state", "is_stale",
    "load_scanner_state", "save_scanner_state",
]
