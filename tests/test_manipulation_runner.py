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
from hunt_core.track._evaluate_levels import (
    SIGNAL_TIMEOUT_HOURS,
    SIGNAL_TIMEOUT_HOURS_LONG,
)


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


def test_deep_lane_signals_also_hold_the_runner_at_tp1():
    """The partial-fix rule is the COURSE's, not the manipulation lane's.

    This test used to pin the opposite ("only manipulation signals change") as
    the blast radius of the G-M1 fix. That scope turned out to be the bug: the
    runner was gated on ``setup_phase == "manipulation"``, but Prizrak rows carry
    setup_phase = signal.thesis ("pp_break_long", …) and scanner rows carry
    "dump_confirmed"/"long_confirmed" — so every deep signal fell through to the
    full-close branch while its delivered card promised «На TP1: фиксировать 50%,
    не 100% — приоритет по тренду (стр.19)» and ``evaluate_levels`` had already
    banked 50% and moved SL→BE in the same tick.
    """
    state = _state(phase="pp_break_long", tp1=110.0, tp2=150.0, sl=90.0)
    closed, sig = _resolve(state, 111.0)
    assert closed == []
    assert sig["status"] == "active"
    assert sig["tp1_hit"] is True


def test_single_target_short_still_closes_at_first_target():
    """Unchanged exception: метод фиксирует первую цель у шорта (no tp2 ladder)."""
    state = _state(phase="pp_break_short", tp1=90.0, tp2=None, sl=110.0, direction="short")
    closed, sig = _resolve(state, 89.0, direction="short")
    assert closed == ["X:short"]
    assert sig["status"] == "closed"


def test_single_target_long_manipulation_holds_the_runner():
    """G-M1: Pattern C longs carry a single-pool ladder (tp2=None). TP1 is a
    partial fix + stop -> BE; the runner rides until stall/stop/timeout, it must
    NOT full-close on the first touch."""
    state = _state(phase="manipulation", tp1=110.0, tp2=None, sl=90.0)
    closed, sig = _resolve(state, 111.0)
    assert closed == []
    assert sig["status"] == "active"
    assert sig["tp1_hit"] is True
    assert sig["tp1_managed"] is True
    assert sig["stop_loss"] == 100.0  # BE = entry
    from hunt_core.params.store import tp1_partial_fix_pct

    assert sig["partial_fixed_pct"] == tp1_partial_fix_pct("X")  # regime fraction unchanged


def test_single_target_short_manipulation_still_closes_at_tp1():
    """Pattern B shorts keep the full close at their first target (метод:
    фиксируем первую цель у шорта)."""
    state = _state(phase="manipulation", tp1=90.0, tp2=None, sl=110.0, direction="short")
    closed, sig = _resolve(state, 89.0, direction="short")
    assert closed == ["X:short"]
    assert sig["status"] == "closed"


def test_runner_partial_fix_moves_stop_to_breakeven():
    """G-M3: after TP1 the ladder runner's stop is BE (entry), not a profit lock."""
    state = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0)
    closed, sig = _resolve(state, 111.0)
    assert closed == []
    assert sig["stop_loss"] == 100.0
    closed, sig = _resolve(state, 99.0)
    assert closed == ["X:long"] and sig["status"] == "closed"


def test_runner_outlives_the_48h_timeout_but_not_the_runaway_guard():
    held = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0, age_min=72 * 60)
    closed, sig = _resolve(held, 105.0)
    assert closed == [] and sig["status"] == "active"

    stale = _state(phase="manipulation", tp1=110.0, tp2=150.0, sl=90.0,
                   age_min=(SIGNAL_TIMEOUT_HOURS_LONG + 1) * 60)
    closed, _ = _resolve(stale, 105.0)
    assert closed == ["X:long"]


def test_plain_short_still_times_out_at_48h():
    state = _state(phase="pre", tp1=90.0, tp2=50.0, sl=110.0, direction="short",
                   age_min=(SIGNAL_TIMEOUT_HOURS + 1) * 60)
    closed, _ = _resolve(state, 105.0, direction="short")
    assert closed == ["X:short"]


def test_plain_long_gets_the_medium_term_timeout():
    """G-M1: longs are medium-term (21d, dataset_v10-validated) — a 49h-old long
    must survive the old flat 48h wall."""
    young = _state(phase="pre", tp1=110.0, tp2=150.0, sl=90.0, age_min=49 * 60)
    closed, sig = _resolve(young, 105.0)
    assert closed == [] and sig["status"] == "active"

    stale = _state(phase="pre", tp1=110.0, tp2=150.0, sl=90.0,
                   age_min=(SIGNAL_TIMEOUT_HOURS_LONG + 1) * 60)
    closed, _ = _resolve(stale, 105.0)
    assert closed == ["X:long"]
