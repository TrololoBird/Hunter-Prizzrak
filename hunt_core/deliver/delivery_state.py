"""Persisted cross-channel cooldown for Module 2 Scanner production Telegram."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hunt_core.paths import DELIVERY_STATE, LAB_OUTCOME_LEDGER

STATE_PATH = DELIVERY_STATE
LAB_LEDGER_PATH = LAB_OUTCOME_LEDGER
DEFAULT_COOLDOWN_MINUTES = 45
# Cross-path per-symbol cooldown (direction-agnostic) — prevents conflicting signals
# from intra-bar, deep analyst, and scanner paths firing within minutes of each other.
CROSS_PATH_COOLDOWN_MINUTES = 15


def _cross_key(symbol: str, direction: str) -> str:
    return f"xchan:{symbol.upper()}:{direction.lower()}"


def load_delivery_state(path: Path | None = None) -> dict[str, str]:
    p = path or STATE_PATH
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_delivery_state(state: dict[str, str], path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def production_cooldown_ok(
    state: dict[str, str],
    *,
    symbol: str,
    direction: str,
    now: datetime | None = None,
    minutes: int | None = None,
    horizon=None,
) -> bool:
    """False when any Module-2 production channel sent sym+dir inside the window."""
    if minutes is None and horizon is not None:
        from hunt_core.domain.signal_horizon import cooldown_minutes_for_horizon

        minutes = cooldown_minutes_for_horizon(horizon)
    mins = minutes if minutes is not None else DEFAULT_COOLDOWN_MINUTES
    now = now or datetime.now(UTC)
    raw = state.get(_cross_key(symbol, direction))
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return now - last >= timedelta(minutes=mins)


def mark_cross_channel_sent(
    state: dict[str, str],
    *,
    symbol: str,
    direction: str,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    state[_cross_key(symbol, direction)] = now.isoformat()
