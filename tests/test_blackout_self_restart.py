"""Sustained critical data blackout → supervised self-restart (2026-07-13 incident).

A stalled WS mux froze 15m klines universe-wide while the bot kept ticking, so the
progress watchdog never fired and the alert-only path left the bot blind for ~2h.
should_self_restart_on_blackout gates the auto-recovery: supervised + critical +
persistent, but NEVER on an IP ban (self-heals; a respawn re-hits the banned IP).
"""
from __future__ import annotations

from hunt_core.diagnostics.universe_health import should_self_restart_on_blackout

_K = 10


def _call(**kw) -> bool:
    base = dict(critical=True, degraded_streak=_K, supervised=True, is_ban=False, streak_threshold=_K)
    base.update(kw)
    return should_self_restart_on_blackout(**base)


def test_restarts_on_sustained_critical_supervised_blackout() -> None:
    assert _call() is True


def test_never_restarts_on_ip_ban() -> None:
    # A ban self-heals; restarting just re-hits the banned IP → thrash.
    assert _call(is_ban=True) is False


def test_never_restarts_when_unsupervised() -> None:
    # Exiting without a supervisor = a dead bot.
    assert _call(supervised=False) is False


def test_waits_for_the_streak_threshold() -> None:
    assert _call(degraded_streak=_K - 1) is False
    assert _call(degraded_streak=_K) is True


def test_non_critical_degradation_does_not_restart() -> None:
    # Partial degradation (degraded but not critical) alerts but must not restart.
    assert _call(critical=False) is False
