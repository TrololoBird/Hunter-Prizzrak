"""Tracker FSM — phase enum, coercion, and allowed transitions (Phase 9 split)."""
from __future__ import annotations

import structlog
from enum import Enum
from typing import Any

from hunt_core.track.events import record_phase_transition as _record_phase_transition

_LOG = structlog.get_logger(__name__)
class SignalPhase(str, Enum):
    REGISTERED = "registered"
    ARMED = "armed"
    TRIGGERED = "triggered"
    TP1_MANAGED = "tp1_managed"
    INVALIDATED = "invalidated"
    CLOSED = "closed"


_ACTIVE_PHASES = frozenset(
    {
        SignalPhase.REGISTERED,
        SignalPhase.ARMED,
        SignalPhase.TRIGGERED,
        SignalPhase.TP1_MANAGED,
    }
)
_ALLOWED_TRANSITIONS: dict[SignalPhase, frozenset[SignalPhase]] = {
    SignalPhase.REGISTERED: frozenset(
        {
            SignalPhase.ARMED,
            SignalPhase.TRIGGERED,
            SignalPhase.INVALIDATED,
            SignalPhase.CLOSED,
        }
    ),
    SignalPhase.ARMED: frozenset(
        {SignalPhase.TRIGGERED, SignalPhase.INVALIDATED, SignalPhase.CLOSED}
    ),
    SignalPhase.TRIGGERED: frozenset(
        {SignalPhase.TP1_MANAGED, SignalPhase.INVALIDATED, SignalPhase.CLOSED}
    ),
    SignalPhase.TP1_MANAGED: frozenset(
        {SignalPhase.INVALIDATED, SignalPhase.CLOSED}
    ),
    SignalPhase.INVALIDATED: frozenset({SignalPhase.CLOSED}),
    SignalPhase.CLOSED: frozenset(),
}
INVALIDATING_CLOSE_REASONS = frozenset(
    {
        "stop_hit",
        "trailing_stop_profit",
        "bounce_invalidate",
        "trend_exhaustion",
        "reclaim_invalidation",
        "support_lost",
        "bias_flip",
        "lifecycle_stale",
        "orphan_expired",
        "time_stall",
        "timeout",
    }
)


def coerce_signal_phase(signal: dict[str, Any]) -> SignalPhase:
    """Resolve tracker FSM phase; infer from legacy rows when ``phase`` is absent."""
    raw = signal.get("phase")
    if isinstance(raw, SignalPhase):
        return raw
    if isinstance(raw, str) and raw in SignalPhase._value2member_map_:
        return SignalPhase(raw)
    if signal.get("status") == "closed":
        return SignalPhase.CLOSED
    if signal.get("tp1_managed"):
        return SignalPhase.TP1_MANAGED
    if str(signal.get("delivery_tier") or "") == "armed":
        return SignalPhase.ARMED
    if signal.get("status") == "active":
        return SignalPhase.TRIGGERED
    return SignalPhase.REGISTERED


def sync_status_from_phase(signal: dict[str, Any]) -> None:
    phase = coerce_signal_phase(signal)
    if phase in _ACTIVE_PHASES:
        signal["status"] = "active"
    elif phase in {SignalPhase.INVALIDATED, SignalPhase.CLOSED}:
        signal["status"] = "closed"


def is_signal_active(signal: dict[str, Any]) -> bool:
    """Backward compat: ``status=='active'`` ⇔ phase ∈ {REGISTERED..TP1_MANAGED}."""
    return coerce_signal_phase(signal) in _ACTIVE_PHASES


def transition(
    signal: dict[str, Any],
    from_phase: SignalPhase | None,
    to_phase: SignalPhase,
    *,
    strict: bool = True,
) -> bool:
    current = coerce_signal_phase(signal)
    if from_phase is not None and current != from_phase:
        if strict:
            _LOG.debug(
                "phase transition rejected %s -> %s (current=%s)",
                from_phase.value,
                to_phase.value,
                current.value,
            )
            return False
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if to_phase not in allowed and to_phase != current:
        _LOG.debug(
            "phase transition not allowed %s -> %s",
            current.value,
            to_phase.value,
        )
        return False
    if to_phase == current:
        return True
    signal["phase"] = to_phase.value
    sync_status_from_phase(signal)
    sym = str(signal.get("symbol") or "")
    direction = str(signal.get("direction") or "")
    if sym and direction:
        try:
            _record_phase_transition(
                symbol=sym,
                direction=direction,
                from_phase=current.value,
                to_phase=to_phase.value,
            )
        except Exception:  # noqa: BLE001
            _LOG.debug(
                "phase_transition_record_failed sym=%s %s->%s",
                sym, current.value, to_phase.value, exc_info=True,
            )
    return True


def initial_signal_phase(setup: dict[str, Any]) -> SignalPhase:
    tier = str(setup.get("delivery_tier") or "triggered").lower()
    if tier == "armed":
        return SignalPhase.ARMED
    if tier == "triggered":
        return SignalPhase.TRIGGERED
    return SignalPhase.REGISTERED


# Backward-compat private aliases for in-package callers.
_coerce_signal_phase = coerce_signal_phase
_sync_status_from_phase = sync_status_from_phase
_is_signal_active = is_signal_active
_transition = transition
_initial_signal_phase = initial_signal_phase

__all__ = [
    "INVALIDATING_CLOSE_REASONS",
    "SignalPhase",
    "coerce_signal_phase",
    "initial_signal_phase",
    "is_signal_active",
    "sync_status_from_phase",
    "transition",
]
