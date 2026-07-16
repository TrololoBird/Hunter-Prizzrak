"""Trailing stop, MFE helpers, TP1 management (Phase 8 split)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from hunt_core.params.store import tp1_partial_fix_pct as _tp1_pct
from hunt_core.params.store import tracker_thresholds

_SQUEEZE_TRAIL_TIGHTEN = 0.70

def _worst_entry(active: dict[str, Any], *, direction: str) -> float:
    """Worst-case (least-favorable) fill edge for R:R, MFE and breakeven SL.

    Long → entry HIGH (paid the most), short → entry LOW (sold cheapest). Kept in
    lockstep with contract.worst_entry_edge; the old inverted convention over-stated
    MFE and moved BE too early.
    """
    if direction == "short":
        return float(active.get("entry_lo") or 0)
    return float(active.get("entry_hi") or 0)


def _mfe_pct(active: dict[str, Any], *, direction: str) -> float:
    """Max favorable excursion % from latched entry."""
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return 0.0
    if direction == "short":
        best = float(active.get("extreme_lo") or entry)
        return max(0.0, (entry - best) / entry * 100.0)
    best = float(active.get("extreme_hi") or entry)
    return max(0.0, (best - entry) / entry * 100.0)


def _squeeze_on_1h(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    tf = row.get("timeframes") or {}
    if not isinstance(tf, dict):
        return False
    block = tf.get("1h") or tf.get("1h_closed") or {}
    return bool(isinstance(block, dict) and block.get("squeeze_on"))


def _initial_risk_distance(active: dict[str, Any], *, direction: str) -> float:
    entry = _worst_entry(active, direction=direction)
    orig = float(active.get("original_stop_loss") or active.get("stop_loss") or 0)
    if entry <= 0 or orig <= 0:
        return 0.0
    if direction == "short" and orig > entry:
        return orig - entry
    if direction == "long" and orig < entry:
        return entry - orig
    return 0.0


def _stop_in_profit_zone(
    active: dict[str, Any], *, direction: str, stop: float
) -> bool:
    """True when SL sits beyond entry in the favorable direction (BE / trail lock)."""
    entry = _worst_entry(active, direction=direction)
    if entry <= 0 or stop <= 0:
        return False
    if direction == "short":
        return stop < entry
    return stop > entry


def _update_trailing_stop(
    active: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None,
    symbol: str,
    ts: datetime | None = None,
) -> tuple[bool, float]:
    """Trail SL behind peak MFE; squeeze_on + MFE>0 tightens room by 30% (Phase 5B).

    Returns ``(updated, previous_stop)`` for same-tick guards and TG notifications.

    Манипуляционные сигналы (reversal) не используют трейлинг — держим до цели/стопа.
    """
    # register_signal_open stores the manipulation marker as active["setup_phase"]
    # = setup["phase"] == "manipulation" (NOT entry_type, which isn't persisted, and
    # NOT "phase", which becomes the lifecycle value "dump_confirmed"). The old guard
    # matched entry_type=="manipulation_reversal" and so NEVER fired — the runner got
    # trailed out of big moves on temporary pullbacks. Manipulation reversals hold to
    # structural targets + averaging, no trail.
    if str(active.get("setup_phase") or "") == "manipulation":
        return False, float(active.get("stop_loss") or 0)
    cur_stop = float(active.get("stop_loss") or 0)
    mfe = _mfe_pct(active, direction=direction)
    if mfe <= 0:
        return False, cur_stop
    tr = tracker_thresholds(symbol)
    min_trail_mfe = float(tr.get("min_trail_mfe_pct", 2.5))
    if mfe <= min_trail_mfe:
        return False, cur_stop
    initial_r = _initial_risk_distance(active, direction=direction)
    if initial_r <= 0:
        return False, cur_stop
    trail_frac = float(tr.get("atr_trail_risk_fraction", tr.get("breakeven_risk_fraction", 0.25)))
    entry = _worst_entry(active, direction=direction)
    atr_pct = _closed_atr1h_pct(row) if row else 0.0
    if atr_pct > 0 and entry > 0:
        trail_dist = entry * (atr_pct / 100.0) * trail_frac
    else:
        trail_dist = initial_r * trail_frac
    if _squeeze_on_1h(row):
        trail_dist *= _SQUEEZE_TRAIL_TIGHTEN
    min_ratchet = (
        entry * (atr_pct / 100.0) * float(tr.get("trail_min_atr_move", 0.15))
        if atr_pct > 0 and entry > 0
        else initial_r * 0.05
    )
    if direction == "short":
        best = float(active.get("extreme_lo") or 0)
        if best <= 0:
            return False, cur_stop
        new_stop = best + trail_dist
        if new_stop >= entry or (cur_stop > 0 and new_stop >= cur_stop):
            return False, cur_stop
    else:
        best = float(active.get("extreme_hi") or 0)
        if best <= 0:
            return False, cur_stop
        new_stop = best - trail_dist
        if new_stop <= entry or (cur_stop > 0 and new_stop <= cur_stop):
            return False, cur_stop
    if cur_stop > 0 and abs(new_stop - cur_stop) < min_ratchet:
        return False, cur_stop
    active["stop_loss"] = round(new_stop, 6)
    active["trailing_active"] = True
    # Once trailing SL is in profit territory, suppress bias_flip exits.
    if direction == "short" and new_stop < entry:
        active["sl_at_breakeven"] = True
    elif direction == "long" and new_stop > entry:
        active["sl_at_breakeven"] = True
    return True, cur_stop



def apply_tp1_breakeven_trail(
    active: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    row: dict[str, Any] | None = None,
) -> bool:
    """After TP1: lock breakeven using ATR-relative buffer and arm trailing."""
    if active.get("tp1_breakeven_active"):
        return False
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return False
    tr = tracker_thresholds(symbol)
    atr_pct = _closed_atr1h_pct(row) if row else 0.0
    atr_frac = float(
        tr.get("tp1_breakeven_atr_fraction", tr.get("breakeven_risk_fraction", 0.25))
    )
    buf_pct = float(tr.get("breakeven_buffer_pct", 0.15))
    min_buf_pct = float(tr.get("breakeven_buffer_min_pct", 1.0))
    if atr_pct > 0:
        buf = entry * (atr_pct / 100.0) * atr_frac
    else:
        buf = entry * (buf_pct / 100.0)
    buf = max(buf, entry * (min_buf_pct / 100.0))
    cur = float(active.get("stop_loss") or 0)
    if direction == "short":
        # Clamp to the realized favorable extreme: when TP1 sits closer than the
        # min-buffer floor (~1%), entry-buf could land BELOW the lowest price reached,
        # placing the runner stop beyond the best and stopping it out at an over-stated
        # profit. Never lock more than price actually gave.
        ext_lo = float(active.get("extreme_lo") or entry)
        new_stop = round(max(entry - buf, ext_lo), 6)
        if new_stop >= entry or (cur > 0 and new_stop >= cur):
            return False
    else:
        ext_hi = float(active.get("extreme_hi") or entry)
        new_stop = round(min(entry + buf, ext_hi), 6)
        if new_stop <= entry or (cur > 0 and new_stop <= cur):
            return False
    active["stop_loss"] = new_stop
    active["sl_at_breakeven"] = True
    active["tp1_breakeven_active"] = True
    return True



def apply_tp1_management(
    active: dict[str, Any], *, direction: str, symbol: str = "", row: dict[str, Any] | None = None
) -> bool:
    """After TP1: partial fix (50% normal / 80% hot) + lock the runner in profit.

    The post-TP1 stop must NEVER sit in the loss zone. The old logic placed it
    ``entry*(1+buf)`` with a 1% floor — i.e. >=1% *beyond entry on the adverse
    side* — which turned TP1 winners into ~1.5% losses (EPIC/UBU 2026-06-12)
    and clobbered an already profit-trailed stop (a +9%-locked trail reset to
    -1%). Instead lock a fraction of the realised TP1 distance: the stop sits
    between entry and TP1 — in profit, yet far enough from the entry-noise band
    that 1m wicks cannot reach it — and we never loosen a tighter trailed stop.
    """
    if active.get("tp1_managed"):
        return False
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return False
    pct = _tp1_pct(symbol)
    tp1 = float(active.get("tp1") or 0)
    lock_frac = float(tracker_thresholds(symbol).get("tp1_profit_lock_fraction", 0.5))
    cur = float(active.get("stop_loss") or 0)
    if direction == "short":
        gain = entry - tp1 if (0.0 < tp1 < entry) else 0.0
        lock_stop = min(entry - lock_frac * gain, entry)  # at/below entry = BE/profit
        if cur > 0:
            lock_stop = min(lock_stop, cur)  # never loosen a tighter trailed stop
    else:
        gain = tp1 - entry if tp1 > entry else 0.0
        lock_stop = max(entry + lock_frac * gain, entry)
        if cur > 0:
            lock_stop = max(lock_stop, cur)
    active["stop_loss"] = round(lock_stop, 6)
    active["partial_fixed_pct"] = pct
    active["sl_at_breakeven"] = True
    active["tp1_managed"] = True
    apply_tp1_breakeven_trail(
        active, direction=direction, symbol=symbol, row=row
    )
    from hunt_core.track._tracker_fsm import SignalPhase, coerce_signal_phase, transition

    transition(
        active,
        coerce_signal_phase(active),
        SignalPhase.TP1_MANAGED,
        strict=False,
    )
    return True


def _closed_atr1h_pct(row: dict[str, Any]) -> float:
    tf = row.get("timeframes") or {}
    block = tf.get("1h_closed") or tf.get("1h") or {}
    try:
        val = float(block.get("atr_pct") or 0)
    except (TypeError, ValueError):
        return 0.0
    return val if val > 0 else 0.0


__all__ = [
    "apply_tp1_breakeven_trail",
    "apply_tp1_management",
    "_closed_atr1h_pct",
    "_mfe_pct",
    "_stop_in_profit_zone",
    "_update_trailing_stop",
    "_worst_entry",
]
