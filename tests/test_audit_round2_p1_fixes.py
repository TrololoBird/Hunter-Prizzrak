"""Pinning tests for the round-2 P1 correctness fixes (G-45, G-67, G-70)."""
from __future__ import annotations

from pathlib import Path

from hunt_core.track._trailing import apply_tp1_breakeven_trail, apply_tp1_management

_ROOT = Path(__file__).resolve().parents[1]


def test_g70_breakeven_stop_clamped_to_favorable_extreme_short() -> None:
    # Short, TP1 only 0.5% below entry → the 1% min buffer would place entry-buf (99.0)
    # BELOW the best price reached (extreme_lo=99.4), overstating the runner's locked
    # profit. The clamp must pin the stop at the realized extreme, not beyond it.
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 99.5,
        "stop_loss": 99.7, "extreme_lo": 99.4, "extreme_hi": 100.0,
    }
    ok = apply_tp1_breakeven_trail(active, direction="short", symbol="", row=None)
    assert ok is True
    assert active["stop_loss"] == 99.4  # clamped to extreme_lo, NOT 99.0


def test_g70_no_clamp_when_extreme_is_further_than_buffer_short() -> None:
    # Price ran well past TP1 (extreme_lo=98.0) → entry-buf (99.0) is above the extreme,
    # so the clamp does not bite and the normal breakeven stop stands.
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 99.5,
        "stop_loss": 99.7, "extreme_lo": 98.0, "extreme_hi": 100.0,
    }
    ok = apply_tp1_breakeven_trail(active, direction="short", symbol="", row=None)
    assert ok is True
    assert active["stop_loss"] == 99.0  # entry - 1% buffer, unclamped


def test_g70_breakeven_stop_clamped_long() -> None:
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 100.5,
        "stop_loss": 100.3, "extreme_lo": 100.0, "extreme_hi": 100.6,
    }
    ok = apply_tp1_breakeven_trail(active, direction="long", symbol="", row=None)
    assert ok is True
    assert active["stop_loss"] == 100.6  # clamped to extreme_hi, NOT 101.0


def test_gm3_post_tp1_stop_is_breakeven_long() -> None:
    # G-M3: the method + validated backtest move the stop to ENTRY after TP1, not to
    # entry + 0.5×(TP1−entry). No favorable extreme recorded → stop == entry exactly.
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 120.0, "stop_loss": 90.0,
    }
    ok = apply_tp1_management(active, direction="long", symbol="", row=None)
    assert ok is True
    assert active["stop_loss"] == 100.0  # BE, NOT 110.0 (the old half-gain lock)
    assert active["sl_at_breakeven"] is True
    assert active["tp1_managed"] is True


def test_gm3_post_tp1_stop_is_breakeven_short() -> None:
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 80.0, "stop_loss": 110.0,
    }
    ok = apply_tp1_management(active, direction="short", symbol="", row=None)
    assert ok is True
    assert active["stop_loss"] == 100.0  # BE, NOT 90.0


def test_gm3_breakeven_buffer_stays_g70_clamped() -> None:
    # With a recorded favorable extreme the existing BE+buf refinement stands (its
    # G-70 clamp is pinned above) — but never the 0.5×gain lock.
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 120.0, "stop_loss": 90.0,
        "extreme_hi": 120.0, "extreme_lo": 100.0,
    }
    apply_tp1_management(active, direction="long", symbol="", row=None)
    assert active["stop_loss"] == 101.0  # entry + 1% min buffer, NOT 110.0


def test_gm3_never_loosens_a_tighter_trailed_stop() -> None:
    active = {
        "entry_lo": 100.0, "entry_hi": 100.0, "tp1": 120.0, "stop_loss": 112.0,
        "extreme_hi": 125.0, "extreme_lo": 100.0,
    }
    apply_tp1_management(active, direction="long", symbol="", row=None)
    assert active["stop_loss"] == 112.0  # trailed stop already in profit stays


def test_g67_manipulation_delivery_does_not_double_record_burst() -> None:
    # register_signal_open records the confirm-burst once (tracker.py); the redundant
    # second call in manipulation_delivery was removed. Guard against its return.
    src = (_ROOT / "hunt_core/deliver/manipulation_delivery.py").read_text()
    assert "record_confirm_burst(" not in src


def test_g45_pinned_signal_prefers_global_ls_over_top() -> None:
    # The pinned /signal map bundle must read global_ls_1h FIRST (top-trader L/S is a
    # different population). Guard against re-inverting the fallback order.
    src = (_ROOT / "hunt_core/runtime/symbol_probe.py").read_text()
    assert 'market.get("global_ls_1h") or market.get("top_ls_1h")' in src
    assert 'market.get("top_ls_1h") or market.get("global_ls_1h")' not in src


def test_g43_g44_stats_filters_match_real_funnel_events() -> None:
    # The funnel producer (track/events.py) emits event="funnel_<stage>". The stats
    # filters must use those names — the old {prep,start,imminent}/"confirmed" matched
    # no producer, so the sections were always empty/zero.
    src = (_ROOT / "hunt_core/runtime/stats_report.py").read_text()
    assert '== "funnel_deliver"' in src  # G-43 confirmed-count
    assert '"funnel_prescan"' in src and '"funnel_dump_initiation"' in src  # G-44 early
    assert '{"prep", "start", "imminent"}' not in src
    assert 'ev.get("event") == "confirmed"' not in src


def test_g71_dead_pre_gate_fields_removed() -> None:
    # pre_gate is never written; pre_gate_open/energy were constant + read by nobody.
    from hunt_core.track.outcome_ledger import build_authority_snapshot

    row = build_authority_snapshot(setup={}, fusion={}, lifecycle={}, blockers=None, delivered=False)
    assert "pre_gate_open" not in row
    assert "pre_gate_energy" not in row
