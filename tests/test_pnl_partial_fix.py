"""G-24: closing PnL must follow the method's money management — bank a partial at TP1,
move the stop to entry, let the runner ride. Marking the WHOLE position at the exit price
reported the classic "+20% on half, then trailed back to breakeven" trade as 0.00%,
erasing a real gain from the outcome ledger the calibration reads.
"""
from __future__ import annotations

from datetime import UTC, datetime

from hunt_core.track.tracker import close_signal

_OPENED = datetime(2026, 7, 14, tzinfo=UTC)
_CLOSED = datetime(2026, 7, 14, 6, tzinfo=UTC)


def _state(direction: str = "long", **over: object) -> tuple[dict, str]:
    sig: dict = {
        "symbol": "TESTUSDT",
        "direction": direction,
        "status": "active",
        "phase": "active",
        "entry_lo": 100.0,
        "entry_hi": 100.0,
        "opened_at": _OPENED.isoformat(),
        "tp1": 120.0 if direction == "long" else 80.0,
    }
    sig.update(over)
    key = f"TESTUSDT:{direction}"
    return {"signals": {key: sig}}, key


def test_partial_fix_then_breakeven_is_not_zero() -> None:
    # Banked 50% at TP1 (+20%), runner stopped out at breakeven (entry 100).
    state, key = _state(tp1_hit=True, partial_fixed_pct=50)
    close_signal(
        state, symbol="TESTUSDT", direction="long", reason="trailing_stop_profit",
        exit_price=100.0, now=_CLOSED, archive=False,
    )
    sig = state["signals"][key]
    assert sig["pnl_pct"] == 10.0  # 0.5*(+20%) + 0.5*(0%)
    assert sig["pnl_basis"] == "partial_fix_at_tp1"


def test_no_tp1_is_still_full_position() -> None:
    state, key = _state()
    close_signal(
        state, symbol="TESTUSDT", direction="long", reason="stop_hit",
        exit_price=90.0, now=_CLOSED, archive=False,
    )
    sig = state["signals"][key]
    assert sig["pnl_pct"] == -10.0
    assert sig["pnl_basis"] == "full_position"


def test_short_partial_fix() -> None:
    state, key = _state(direction="short", tp1_hit=True, partial_fixed_pct=50)
    close_signal(
        state, symbol="TESTUSDT", direction="short", reason="trailing_stop_profit",
        exit_price=100.0, now=_CLOSED, archive=False,
    )
    sig = state["signals"][key]
    assert sig["pnl_pct"] == 10.0  # 0.5*(+20% short leg) + 0.5*(0%)
