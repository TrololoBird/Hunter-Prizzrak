"""Manipulation trades must ride the pool ladder, not die at the nearest pool.

Delivered manipulation signals promise «тейки частями по лестнице, держим до
цели/стопа», and ``_trailing.py`` already exempts them from the trail for that
reason. Two things contradicted it:

* ``manipulation_delivery`` handed the tracker ``tp1`` only (the nearest pool) and
  no ``tp2``, so the «среднесрочная цель» was never tracked;
* ``auto_resolve_active_signals`` closed the whole position on the first TP1 touch,
  and a 48h wall-clock timeout killed multi-day runners.

Median delivered manipulation trade lasted 16 minutes for a method whose own
numbers are 100-400% over days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hunt_core.track import tracker as T


def _state(*, phase: str, tp1: float, tp2: float | None, sl: float,
           direction: str = "long", age_min: float = 60.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "signals": {
            f"X:{direction}": {
                "symbol": "X", "direction": direction, "status": "active",
                "tp1": tp1, "tp2": tp2, "stop_loss": sl, "setup_phase": phase,
                "opened_at": (now - timedelta(minutes=age_min)).isoformat(),
                "entry_lo": 100.0, "entry_hi": 100.0,
            }
        }
    }


def _resolve(state: dict, price: float, direction: str = "long"):
    closed = T.auto_resolve_active_signals(state, {"X": price})
    return closed, state["signals"][f"X:{direction}"]


def test_tp1_is_a_partial_fix_for_a_manipulation_runner():
    state = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0)
    closed, sig = _resolve(state, 111.0)
    assert closed == []
    assert sig["status"] == "active"
    assert sig["tp1_hit"] is True


def test_runner_closes_at_the_mid_term_target():
    state = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0)
    _resolve(state, 111.0)
    closed, sig = _resolve(state, 151.0)
    assert closed == ["X:long"]
    assert sig["status"] == "closed"


def test_runner_still_honours_the_stop_after_tp1():
    state = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0)
    _resolve(state, 111.0)
    closed, sig = _resolve(state, 89.0)
    assert closed == ["X:long"]
    assert sig["status"] == "closed"


def test_short_runner_is_symmetric():
    state = _state(phase="manipulation", tp1=90.0, tp2=50.0, sl=110.0, direction="short")
    closed, sig = _resolve(state, 89.0, direction="short")
    assert closed == [] and sig["tp1_hit"] is True
    closed, sig = _resolve(state, 49.0, direction="short")
    assert closed == ["X:short"] and sig["status"] == "closed"


def test_non_manipulation_signals_keep_closing_at_tp1():
    """Blast radius: only the ladder-carrying manipulation signals change."""
    state = _state(phase="pre", tp1=110.0, tp2=150.0, sl=90.0)
    closed, sig = _resolve(state, 111.0)
    assert closed == ["X:long"]
    assert sig["status"] == "closed"


def test_manipulation_without_tp2_keeps_closing_at_tp1():
    """No mid-term target to ride to -> old behaviour, never an unbounded hold."""
    state = _state(phase="manipulation", tp1=110.0, tp2=None, sl=90.0)
    closed, sig = _resolve(state, 111.0)
    assert closed == ["X:long"]
    assert sig["status"] == "closed"


def test_runner_outlives_the_48h_timeout_but_not_the_runaway_guard():
    held = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0, age_min=72 * 60)
    closed, sig = _resolve(held, 105.0)
    assert closed == [] and sig["status"] == "active"

    stale = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0,
                   age_min=(T.AUTO_RESOLVE_TIMEOUT_HOURS_LADDER + 1) * 60)
    closed, _ = _resolve(stale, 105.0)
    assert closed == ["X:long"]


def test_plain_signal_still_times_out_at_48h():
    state = _state(phase="pre", tp1=110.0, tp2=150.0, sl=90.0,
                   age_min=(T.AUTO_RESOLVE_TIMEOUT_HOURS + 1) * 60)
    closed, _ = _resolve(state, 105.0)
    assert closed == ["X:long"]
