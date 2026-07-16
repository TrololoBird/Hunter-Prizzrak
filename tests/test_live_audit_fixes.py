"""Regression pins for defects found by watching the live bot (2026-07-16).

Each test fails on the pre-fix code:

- forward/deep plans registered as FILLED positions (phantom MFE → phantom TP1);
- `auto_resolve_active_signals` full-closing every non-manipulation signal at
  TP1, killing the runner the delivered card promises;
- `_backfill_signal_geometry` anchoring R:R at the BEST fill;
- degenerate TP ladders whose rungs describe one zone several times.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hunt_core.prizrak.orchestrator import _build_tp_ladder
from hunt_core.track.tracker import (
    _backfill_signal_geometry,
    auto_resolve_active_signals,
    register_signal_open,
)

# ---------------------------------------------------------------------------
# TP ladder: min-gap collapses near-duplicate rungs
# ---------------------------------------------------------------------------


def test_tp_ladder_collapses_near_duplicate_rungs() -> None:
    """The live BTCUSDT ladder: TP3→TP4 were 4.3 apart (0.007%) on risk 2329."""
    entry = 59198.16
    risk = entry - 56869.4
    min_gap = max(risk * 0.25, entry * 0.0015)
    ladder = _build_tp_ladder(
        entry,
        "long",
        zone_targets=[64596.83, 64245.6],
        swing_levels=[63929.6, 64185.7, 64241.3],
        max_steps=5,
        min_gap=min_gap,
    )
    for a, b in zip(ladder, ladder[1:]):
        assert abs(b - a) >= min_gap, f"rungs {a} and {b} are one zone, not two"
    assert ladder[0] == 63929.6  # nearest real target survives
    assert len(ladder) < 5  # the 64185/64241/64245 cluster collapsed


def test_tp_ladder_without_min_gap_keeps_distinct_levels() -> None:
    ladder = _build_tp_ladder(
        100.0, "long", zone_targets=[130.0], swing_levels=[110.0, 120.0],
        max_steps=5, min_gap=1.0,
    )
    assert ladder == [110.0, 120.0, 130.0]


def test_tp_ladder_short_direction_min_gap() -> None:
    ladder = _build_tp_ladder(
        100.0, "short", zone_targets=[70.0], swing_levels=[90.0, 89.9, 80.0],
        max_steps=5, min_gap=5.0,
    )
    assert ladder == [90.0, 80.0, 70.0]  # 89.9 collapsed into 90.0
    assert all(b < a for a, b in zip(ladder, ladder[1:]))


# ---------------------------------------------------------------------------
# R:R backfill anchors at the worst fill
# ---------------------------------------------------------------------------


def test_backfill_rr_uses_worst_fill_short() -> None:
    """Short band 100–102, SL 105, TP1 92 → conservative 8/5 = 1.6 (not 3.33)."""
    sig: dict[str, Any] = {
        "entry_lo": 100.0, "entry_hi": 102.0,
        "stop_loss": 105.0, "tp1": 92.0, "direction": "short",
    }
    _backfill_signal_geometry(sig)
    assert sig["risk_reward"] == 1.6


def test_backfill_rr_uses_worst_fill_long() -> None:
    """Long band 100–102, SL 96, TP1 110 → worst fill 102 → 8/6 = 1.333."""
    sig: dict[str, Any] = {
        "entry_lo": 100.0, "entry_hi": 102.0,
        "stop_loss": 96.0, "tp1": 110.0, "direction": "long",
    }
    _backfill_signal_geometry(sig)
    assert sig["risk_reward"] == round(8.0 / 6.0, 3)


# ---------------------------------------------------------------------------
# ARMED plans are not managed as open positions
# ---------------------------------------------------------------------------


def _armed_state(now: datetime) -> dict[str, Any]:
    """The live BTCUSDT deep long: spot 64468, entry -8.9%, TP1 *below spot*."""
    state: dict[str, Any] = {"signals": {}, "followup_sent": {}}
    register_signal_open(
        state,
        symbol="BTCUSDT",
        direction="long",
        price=64468.75,
        setup={
            "direction": "long",
            "phase": "zone_target_deep_long",
            "delivery_tier": "armed",
            "entry_lo": 58961.84,
            "entry_hi": 59198.16,
            "stop_loss": 56869.4,
            "tp1": 64185.7,
            "tp2": 64596.83,
        },
        lifecycle={},
        now=now,
    )
    return state


def test_armed_forward_plan_is_not_resolved_at_registration_price() -> None:
    """TP1 sits below spot: a filled-position model books an instant fake win."""
    now = datetime.now(UTC)
    state = _armed_state(now)
    sig = state["signals"]["BTCUSDT:long"]
    assert sig["delivery_tier"] == "armed"

    closed = auto_resolve_active_signals(
        state,
        {"BTCUSDT": 64468.75},
        now=now + timedelta(hours=1),
    )
    assert closed == [], "an unfilled limit must not resolve"
    assert state["signals"]["BTCUSDT:long"].get("status") != "closed"


def test_evaluate_levels_skips_armed_signal() -> None:
    from hunt_core.track._evaluate_levels import evaluate_levels

    now = datetime.now(UTC)
    state = _armed_state(now)
    events = evaluate_levels(
        state,
        symbol="BTCUSDT",
        direction="long",
        price=64468.75,
        hi=64500.0,
        lo=64400.0,
        ts=now + timedelta(hours=1),
        row={},
    )
    assert events == []
    sig = state["signals"]["BTCUSDT:long"]
    assert not sig.get("tp1_hit"), "unfilled limit must not register a TP1 hit"


# ---------------------------------------------------------------------------
# Runner survives TP1 on the deep/prizrak lane
# ---------------------------------------------------------------------------


def _triggered_state(now: datetime, *, phase: str, direction: str = "long") -> dict[str, Any]:
    state: dict[str, Any] = {"signals": {}, "followup_sent": {}}
    register_signal_open(
        state,
        symbol="ETHUSDT",
        direction=direction,
        price=100.0,
        setup={
            "direction": direction,
            "phase": phase,
            "delivery_tier": "triggered",
            "entry_lo": 99.0,
            "entry_hi": 100.0,
            "stop_loss": 96.0,
            "tp1": 108.0,
            "tp2": 116.0,
        },
        lifecycle={},
        now=now,
    )
    return state


def test_prizrak_long_holds_runner_at_tp1() -> None:
    """Course стр.19: fix 50% at TP1, keep the runner — the card promises this."""
    now = datetime.now(UTC)
    state = _triggered_state(now, phase="pp_break_long")
    closed = auto_resolve_active_signals(
        state, {"ETHUSDT": 108.5}, now=now + timedelta(hours=1),
    )
    assert closed == [], "TP1 must be a partial fix, not a full close"
    sig = state["signals"]["ETHUSDT:long"]
    assert sig.get("tp1_hit") is True
    assert sig.get("status") != "closed"


def test_prizrak_long_closes_at_tp2() -> None:
    now = datetime.now(UTC)
    state = _triggered_state(now, phase="pp_break_long")
    closed = auto_resolve_active_signals(
        state, {"ETHUSDT": 116.5}, now=now + timedelta(hours=1),
    )
    assert closed == ["ETHUSDT:long"]
