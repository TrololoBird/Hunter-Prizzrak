"""Active hunt signal tracking — invalidate, TP hit, phase change follow-ups."""
from __future__ import annotations



from hunt_core import clock
import json
import structlog
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

SignalEvent = Literal[
    "signal_open",
    "invalidate",
    "fix_profit_tp1",
    "fix_profit_tp2",
    "phase_change",
    "entry_triggered",
    "stop_warning",
    "trailing_updated",
    "avg_zone",
]

from hunt_core.params.store import tracker_thresholds
from hunt_core.features.prepare_columns import feature_vector_from_row
from hunt_core.paths import SIGNAL_STATE as STATE_PATH
def _is_structural_confirm_trigger(trigger: str) -> bool:
    t = str(trigger)
    if t.endswith("_score_only"):
        return False
    if "cascade" in t:
        return True
    return (
        "close_below_support" in t
        or "close_above_resistance" in t
        or t.startswith("bos_retest_")
        or t.startswith("prokol_reclaim_")
        or t in {"pp_short_break", "pp_long_break", "distribution_structure_confirm",
                 "peak_fade_confirm", "pre_dump_div_confirm"}
    )
from hunt_core.track.events import append_signal_event as _append_event
from hunt_core.track._tracker_fsm import (
    SignalPhase,
    INVALIDATING_CLOSE_REASONS as _INVALIDATING_CLOSE_REASONS,
    _ACTIVE_PHASES,  # private name — used in close_signal()
    coerce_signal_phase as _coerce_signal_phase,
    initial_signal_phase as _initial_signal_phase,
    is_signal_active as _is_signal_active,
    transition as _transition,
)
from hunt_core.track._cooldowns import (
    global_confirm_burst_cap_reached,
    recent_stop_hit_cooldown,
    record_confirm_burst,
    symbol_daily_tg_cap_reached,
    symbol_loss_streak_cooldown,
    symbol_repeat_loser_blocked,
)
from hunt_core.track._followups import evaluate_followups

_LOG = structlog.get_logger(__name__)
FOLLOWUP_COOLDOWN_MINUTES = 5
PHASE_CHANGE_GRACE_MIN = 20.0
RECLAIM_BUFFER = 1.001  # fallback; prefer tracker_thresholds().reclaim_buffer
# A hunt setup is a momentum trade — after this long without SL/TP it is stale.
SIGNAL_TIMEOUT_HOURS = 48.0
# Phase 4A: level test tracking — approach within 0.3%, expire after 1.5×ATR reaction.
_LEVEL_APPROACH_TOLERANCE = 0.003
_LEVEL_REACTION_ATR_MULT = 1.5
# Phase 5B: BB squeeze + open profit → 30% tighter trail (volatility compression).
# H-A "sniper" hold-to-target exit (Gate G2, edge-validated 2026-06-12): on the live
# short slice the soft `lifecycle_stale` close forfeits winners — backtest on
# dump_active short (n=37) shows 19% SL / 43% reach TP2 when held to target/SL.
# So in sniper mode short positions ride to SL/TP (evaluate_levels); the soft
# lifecycle_stale timeout is suppressed. (The old bounce `invalidate_short` arm was
# dead — no producer — and was removed, G-69.)
# The unit-tested `_stale_lifecycle_invalidate` itself is unchanged — gated at call site.
SNIPER_HOLD_TO_TARGET = os.environ.get("HUNT_SNIPER_MODE", "1") not in {"0", "false", "False"}
HUNT_EXIT_V2 = os.environ.get("HUNT_EXIT_V2", "").strip().lower() in {"1", "true", "yes"}
EXIT_V2_ACTIVE = HUNT_EXIT_V2 or SNIPER_HOLD_TO_TARGET
# Backward compat for logic_verify imports — runtime uses tracker_thresholds().
STALE_LC_TICKS_DEFAULT = 3
_SHORT_STALE_PHASES = frozenset(
    {
        "no_setup",
        "post_dump_bounce",
        "recovery",
        "accumulation",
        "breakout_arming",
        "impulse_initiating",
    },
)
_LONG_STALE_PHASES = frozenset(
    {"distribution", "exhaustion_at_high", "dump_active"},
)


@dataclass(frozen=True, slots=True)
class HuntFollowUp:
    event: SignalEvent
    symbol: str
    direction: str
    message_key: str
    detail: str
    price: float
    payload: dict[str, Any]


def _reclaim_buffer(symbol: str = "") -> float:
    return float(tracker_thresholds(symbol).get("reclaim_buffer", RECLAIM_BUFFER))


_DUMP_SHORT_ENTRY_PHASES = frozenset(
    {"dump_active", "distribution", "exhaustion_at_high"},
)
_BOUNCE_WITHIN_DUMP_PHASES = frozenset(
    {
        "impulse_initiating",
        "post_dump_bounce",
        "recovery",
        "accumulation",
        "breakout_arming",
    },
)


def _hold_short_through_dump_bounce(
    active: dict[str, Any],
    lifecycle: dict[str, Any],
    *,
    price: float,
    opened_bias: str,
    lc_bias: str,
    symbol: str = "",
) -> bool:
    """BEAT 2026-06-12: wait→long bounce closed a +EV short at 20m without entry reclaim."""
    if lc_bias != "long" or opened_bias not in {"wait", "short"}:
        return False
    entry_hi = float(active.get("entry_hi") or 0)
    if entry_hi <= 0 or price > entry_hi * _reclaim_buffer(symbol):
        return False
    opened_phase = str(active.get("entry_lifecycle_phase") or "")
    lc_phase = str(lifecycle.get("phase") or "")
    fall = float(lifecycle.get("fall_from_high_pct") or 0)
    dump_entry = opened_phase in _DUMP_SHORT_ENTRY_PHASES
    bounce_leg = lc_phase in _BOUNCE_WITHIN_DUMP_PHASES
    if dump_entry and bounce_leg and (fall >= 8.0 or opened_bias == "wait"):
        return True
    return opened_bias == "wait" and lc_bias == "long"


def _key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.lower()}"


def signal_confirm_announced(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
) -> bool:
    """True when confirm TG already shipped for an open tracked signal."""
    k = _key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or not _is_signal_active(active):
        return False
    return bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))


def has_active_signal(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str | None = None,
) -> bool:
    """True if an unresolved (not TP/SL-closed) signal is open for the symbol.

    Dedup guard for the emission path: a setup whose prior signal is still open
    must NOT re-fire — the opportunity has not resolved, so a second identical
    (or opposite) signal is either a duplicate or a self-contradiction, not a new
    trade. ``direction=None`` checks both sides (blocks long while short is open).
    """
    signals = state.get("signals") if isinstance(state, dict) else None
    if not isinstance(signals, dict):
        return False
    dirs = (direction.lower(),) if direction else ("long", "short")
    for d in dirs:
        active = signals.get(_key(symbol, d))
        if isinstance(active, dict) and _is_signal_active(active):
            return True
    return False


def _has_structural_trigger(setup: dict[str, Any]) -> bool:
    """True when confirm_hard includes a closed-bar structural break."""
    for raw in setup.get("confirm_hard") or []:
        if _is_structural_confirm_trigger(str(raw)):
            return True
    return False


def _entry_zone_from_plan(setup: dict[str, Any]) -> list[float] | None:
    """Deep (prizrak) plans carry entry_lo/entry_hi but no ``entry_zone`` key.
    Without this, the caller's ``[price, price]`` fallback would silently anchor the
    signal to spot while stop/TP stay on the plan's own entry — the two disagree and
    a losing exit reads back as a TP hit."""
    lo, hi = setup.get("entry_lo"), setup.get("entry_hi")
    if lo is None or hi is None:
        return None
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if lo_f <= 0 or hi_f <= 0:
        return None
    return [min(lo_f, hi_f), max(lo_f, hi_f)]


def _delivered_levels_snapshot(
    setup: dict[str, Any],
    *,
    direction: str,
    entry_lo: float,
    entry_hi: float,
) -> dict[str, Any]:
    """Immutable levels frozen at register_signal_open for audit/TG follow-ups."""
    entry = entry_hi if direction == "short" else entry_lo
    if entry <= 0:
        entry = entry_lo or entry_hi
    return {
        "entry": entry,
        "entry_lo": entry_lo,
        "entry_hi": entry_hi,
        "sl": setup.get("stop_loss"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "tp3": setup.get("tp3"),
        "rr": setup.get("risk_reward"),
    }


def _apply_early_breakeven_lock(
    active: dict[str, Any],
    *,
    direction: str,
    symbol: str,
) -> bool:
    """Lock SL near breakeven once MFE threshold met — only after TP1 or structural confirm."""
    if active.get("trailing_active") or active.get("sl_at_breakeven"):
        return False
    if not (
        active.get("tp1_hit")
        or active.get("structural_trigger")
    ):
        return False
    mfe = _mfe_pct(active, direction=direction)
    tr = tracker_thresholds(symbol)
    entry_phase = str(active.get("entry_lifecycle_phase") or "")
    threshold = float(tr.get("early_breakeven_mfe_pct", 99.0))
    if entry_phase == "dump_active":
        threshold = float(tr.get("dump_active_early_be_mfe_pct", 99.0))
    elif entry_phase == "dump_initiating":
        threshold = float(tr.get("dump_initiating_early_be_mfe_pct", 99.0))
    elif entry_phase == "exhaustion_at_high":
        threshold = float(tr.get("exhaustion_early_be_mfe_pct", 99.0))
    elif entry_phase in {"accumulation", "recovery"}:
        threshold = float(tr.get("accumulation_early_be_mfe_pct", 99.0))
    elif entry_phase == "distribution":
        threshold = float(tr.get("distribution_early_be_mfe_pct", 99.0))
    if mfe < threshold:
        return False
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return False
    buf_pct = float(tr.get("early_breakeven_buffer_pct", 0.12))
    cur = float(active.get("stop_loss") or 0)
    if direction == "short":
        new_stop = round(entry * (1.0 - buf_pct / 100.0), 6)
        if new_stop >= entry or (cur > 0 and new_stop >= cur):
            return False
    else:
        new_stop = round(entry * (1.0 + buf_pct / 100.0), 6)
        if new_stop <= entry or (cur > 0 and new_stop <= cur):
            return False
    active["stop_loss"] = new_stop
    active["sl_at_breakeven"] = True
    return True


def _backfill_signal_geometry(sig: dict[str, Any]) -> None:
    """Repair missing risk_reward / original_stop on legacy tracker rows."""
    if not isinstance(sig, dict) or sig.get("status") == "closed":
        return
    if not sig.get("original_stop_loss") and sig.get("stop_loss"):
        sig["original_stop_loss"] = sig.get("stop_loss")
    if sig.get("risk_reward"):
        return
    direction = str(sig.get("direction") or "short").lower()
    try:
        el = float(sig.get("entry_lo") or 0)
        eh = float(sig.get("entry_hi") or 0)
    except (TypeError, ValueError):
        return
    sl = float(sig.get("stop_loss") or 0)
    tp1 = float(sig.get("tp1") or 0)
    if sl <= 0 or tp1 <= 0 or eh <= 0:
        return
    worst = eh if direction == "short" else el
    risk = abs(worst - sl)
    reward = abs(worst - tp1)
    if risk > 0:
        sig["risk_reward"] = round(reward / risk, 3)


def load_tracker_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"signals": {}, "followup_sent": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "signals" in raw:
            for sig in (raw.get("signals") or {}).values():
                if isinstance(sig, dict):
                    _backfill_signal_geometry(sig)
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {"signals": {}, "followup_sent": {}}


def save_tracker_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def iter_active_tracker_symbols(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (symbol, direction) for each active tracked signal."""
    out: list[tuple[str, str]] = []
    for key, sig in (state.get("signals") or {}).items():
        if not isinstance(sig, dict) or not _is_signal_active(sig):
            continue
        sym = str(sig.get("symbol") or key.partition(":")[0]).upper()
        direction = str(sig.get("direction") or key.rsplit(":", 1)[-1]).lower()
        if sym and direction in ("short", "long"):
            out.append((sym, direction))
    return out


def reconcile_active_from_ticker(
    state: dict[str, Any],
    *,
    ticker_by_sym: dict[str, Any],
    now: datetime,
    only_symbols: set[str] | None = None,
    ws_feed: Any | None = None,
) -> list[HuntFollowUp]:
    """Apply live price to latched SL/TP when a symbol missed the tick batch."""
    from hunt_core.market.live_price import resolve_live_price

    events: list[HuntFollowUp] = []
    for sym, direction in iter_active_tracker_symbols(state):
        if only_symbols is not None and sym not in only_symbols:
            continue
        t = ticker_by_sym.get(sym) or {}
        px = float(t.get("last_price") or t.get("lastPrice") or 0)
        if ws_feed is not None:
            ws_px, _src = resolve_live_price(sym, ws_feed=ws_feed, fallback=px)
            if ws_px > 0:
                px = ws_px
        if px <= 0:
            continue
        k = _key(sym, direction)
        active = (state.get("signals") or {}).get(k)
        if not isinstance(active, dict) or not _is_signal_active(active):
            continue
        hi = max(float(active.get("extreme_hi") or px), px)
        lo = min(float(active.get("extreme_lo") or px), px)
        events.extend(
            reconcile_signal(
                state,
                symbol=sym,
                direction=direction,
                hi=hi,
                lo=lo,
                last_price=px,
                ts=now,
            )
        )
    return events


def _followup_allowed(state: dict[str, Any], message_key: str, *, now: datetime) -> bool:
    raw = (state.get("followup_sent") or {}).get(message_key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return now - last >= timedelta(minutes=FOLLOWUP_COOLDOWN_MINUTES)


def _mark_followup(state: dict[str, Any], message_key: str, *, now: datetime) -> None:
    sent = state.setdefault("followup_sent", {})
    sent[message_key] = now.isoformat()


def _close_already_notified(state: dict[str, Any], symbol: str, direction: str) -> bool:
    """True when terminal close/invalidate was already announced in Telegram."""
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    return isinstance(sig, dict) and bool(sig.get("close_notified"))


def mark_close_notified(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    message_key: str,
    now: datetime,
    remove_active: bool = False,
) -> None:
    """Latch terminal close TG — prevents re-close spam across ticks/processes."""
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    if not isinstance(sig, dict):
        return
    sig["close_notified"] = True
    sig["close_message_key"] = message_key
    sig["close_notified_at"] = now.isoformat()
    _mark_followup(state, message_key, now=now)
    if remove_active:
        state.setdefault("signals", {}).pop(k, None)


def register_signal_open(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    price: float,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    now: datetime,
    entry_message_id: int | None = None,
    features_open: dict[str, Any] | None = None,
    book_walls: dict[str, Any] | None = None,
) -> None:
    # The mission_delivery_block() call that stood here was a documented no-op — it
    # always returns None ("no veto beyond the setup's own confirmation": the persistent
    # state machine only emits once every stage has confirmed in order). Keeping it cost
    # track/ an import of scanner/, a spine→strategy inversion, for a branch that could
    # never be taken.
    if entry_message_id is not None:
        eid = int(entry_message_id)
        for sig in (state.get("signals") or {}).values():
            if isinstance(sig, dict) and sig.get("entry_message_id") == eid:
                return
        for rec in state.get("closed_history") or []:
            if isinstance(rec, dict) and rec.get("entry_message_id") == eid:
                return
    k = _key(symbol, direction)
    dir_l = direction.lower()
    score_key = "dump_score" if dir_l == "short" else "long_score"
    fuel_key = "dump_fuel" if dir_l == "short" else "long_fuel"

    # One direction per symbol: a fresh confirmed opposite setup supersedes
    # the stale one (simultaneous SYM:long + SYM:short is a contradiction).
    opposite = _key(symbol, "long" if direction.lower() == "short" else "short")
    opp_sig = (state.get("signals") or {}).get(opposite)
    if isinstance(opp_sig, dict) and _is_signal_active(opp_sig):
        close_signal(
            state,
            symbol=symbol,
            direction="long" if direction.lower() == "short" else "short",
            reason="opposite_signal",
            exit_price=price,
            now=now,
        )
    ez = setup.get("entry_zone") or _entry_zone_from_plan(setup) or [price, price]
    entry_lo = float(ez[0] if len(ez) > 0 else price)
    entry_hi = float(ez[1] if len(ez) > 1 else price)
    initial_phase = _initial_signal_phase(setup)
    orig_sl = setup.get("stop_loss")
    signal: dict[str, Any] = {
        "status": "active",
        "phase": initial_phase.value,
        "setup_phase": setup.get("phase"),
        "opened_at": now.isoformat(),
        "direction": direction,
        "entry_lo": entry_lo,
        "entry_hi": entry_hi,
        "stop_loss": orig_sl,
        "original_stop_loss": orig_sl,
        "delivered_levels_snapshot": _delivered_levels_snapshot(
            setup,
            direction=direction.lower(),
            entry_lo=entry_lo,
            entry_hi=entry_hi,
        ),
        "structural_trigger": _has_structural_trigger(setup),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "tp3": setup.get("tp3"),
        "risk_reward": setup.get("risk_reward"),
        "level_mode": setup.get("level_mode"),
        "entry_zone": list(ez) if isinstance(ez, (list, tuple)) else [price, price],
        "lifecycle_phase": (lifecycle or {}).get("phase") or setup.get("lifecycle_phase"),
        # immutable entry bucket — never updated by followups
        "entry_lifecycle_phase": (
            (lifecycle or {}).get("phase")
            or setup.get("lifecycle_phase")
            or setup.get("phase")
        ),
        "entry_lifecycle_bias": (lifecycle or {}).get("recommended_bias"),
        "lifecycle_bias": (lifecycle or {}).get("recommended_bias"),
        "score": setup.get(score_key),
        "fuel": setup.get(fuel_key),
        "delivery_tier": setup.get("delivery_tier") or "triggered",
        "support_break_level": setup.get("support_break_level"),
        "invalidation_above": setup.get("invalidation_above"),
        "resistance_break_level": setup.get("resistance_break_level"),
        "invalidation_below": setup.get("invalidation_below"),
        "telegram_sent": bool(setup.get("telegram_sent")),
        "entry_message_id": entry_message_id,
        "extreme_hi": price,
        "extreme_lo": price,
        "last_checked_at": now.isoformat(),
        "last_reconcile_ts": now.isoformat(),
        "close_notified": False,
        "level_test_count": 0,
        "level_reaction_max_pct": 0.0,
        "level_expired": False,
    }
    if isinstance(features_open, dict):
        signal["features_open"] = features_open
    if isinstance(book_walls, dict):
        signal["book_walls"] = book_walls
    signal["symbol"] = symbol.upper()
    state.setdefault("signals", {})[k] = signal
    record_confirm_burst(state, now=now)


def _tick_feature_latch(
    active: dict[str, Any],
    row: dict[str, Any],
    *,
    direction: str,
) -> None:
    """Update per-tick feature snapshots; latch peak when MFE improves."""
    active["features_last"] = feature_vector_from_row(row)
    cur_mfe = _mfe_pct(active, direction=direction)
    peak = float(active.get("peak_mfe_pct") or 0.0)
    if cur_mfe > peak + 0.001:
        active["peak_mfe_pct"] = round(cur_mfe, 2)
        active["features_peak"] = active["features_last"]


from hunt_core.track._trailing import (
    _mfe_pct,
    _worst_entry,
    apply_tp1_management,
)
from hunt_core.track._evaluate_levels import (
    evaluate_levels,
)


def on_tp1_reached(
    active: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    row: dict[str, Any] | None = None,
) -> bool:
    """TP1 touch: partial fix, ATR breakeven lock, arm trailing for runner."""
    return apply_tp1_management(
        active, direction=direction, symbol=symbol, row=row
    )


def _latched_levels_payload(active: dict[str, Any]) -> dict[str, Any]:
    """Current tracker levels for follow-up messages."""
    return {
        "stop_loss": active.get("stop_loss"),
        "tp1": active.get("tp1"),
        "tp2": active.get("tp2"),
        "entry_lo": active.get("entry_lo"),
        "entry_hi": active.get("entry_hi"),
        "opened_at": active.get("opened_at"),
        "entry_message_id": active.get("entry_message_id"),
        "score": active.get("score"),
    }


def _short_structure_invalidated(
    active: dict[str, Any],
    setup: dict[str, Any],
    *,
    price: float,
) -> tuple[bool, str]:
    """Latch: do not cancel on score flicker — only structural breaks."""
    stop = float(active.get("stop_loss") or 0)
    if stop > 0 and price >= stop:
        return True, "stop_hit"
    reclaim = float(
        active.get("invalidation_above")
        or setup.get("invalidation_above")
        or active.get("support_break_level")
        or setup.get("support_break_level")
        or 0
    )
    if reclaim > 0 and price > reclaim * RECLAIM_BUFFER:
        return True, "reclaim_invalidation"
    return False, ""


def _long_structure_invalidated(
    active: dict[str, Any],
    setup: dict[str, Any],
    *,
    price: float,
    symbol: str = "",
) -> tuple[bool, str]:
    stop = float(active.get("stop_loss") or 0)
    if stop > 0 and price <= stop:
        return True, "stop_hit"
    break_below = float(
        active.get("invalidation_below")
        or setup.get("invalidation_below")
        or active.get("resistance_break_level")
        or setup.get("resistance_break_level")
        or 0
    )
    if break_below > 0 and price < break_below / _reclaim_buffer(symbol):
        return True, "support_lost"
    return False, ""


def close_signal(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    reason: str = "manual",
    exit_price: float | None = None,
    now: datetime | None = None,
    archive: bool = True,
) -> None:
    """Terminal transition: always records outcome (reason / exit / pnl / duration).

    ``archive`` appends the closed record to the persistent ``signal_history.jsonl``.
    Tests pass ``archive=False`` so verify runs never pollute production data.
    """
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    if not isinstance(sig, dict) or _coerce_signal_phase(sig) == SignalPhase.CLOSED:
        return
    ts = now or clock.now_utc()
    cur = _coerce_signal_phase(sig)
    if reason in _INVALIDATING_CLOSE_REASONS and cur in _ACTIVE_PHASES:
        _transition(sig, cur, SignalPhase.INVALIDATED, strict=False)
        cur = _coerce_signal_phase(sig)
    _transition(sig, cur, SignalPhase.CLOSED, strict=False)
    sig["status"] = "closed"
    sig["closed_at"] = ts.isoformat()
    sig["close_reason"] = reason
    sig.setdefault("close_notified", False)
    sig["close_lifecycle_phase"] = sig.get("lifecycle_phase")
    if exit_price is not None and exit_price > 0:
        sig["exit_price"] = exit_price
        lo = float(sig.get("entry_lo") or 0)
        hi = float(sig.get("entry_hi") or 0)
        mid = (lo + hi) / 2.0 if lo > 0 and hi > 0 else (lo or hi)
        if mid > 0:

            def _leg(px: float) -> float:
                raw = (px - mid) / mid * 100.0
                return raw if direction == "long" else -raw

            # Faithful money-management PnL: the method banks a partial at TP1 and moves
            # the stop to entry (BE), so the runner is what rides on. Marking the WHOLE
            # position at the exit price (the old behaviour) reported a trade that took
            # +20% on half and then trailed back to BE as PnL 0.00% — erasing a real gain
            # and poisoning the outcome ledger / win-rate the calibration reads.
            tp1 = float(sig.get("tp1") or 0)
            fixed_pct = float(sig.get("partial_fixed_pct") or 0)
            if sig.get("tp1_hit") and tp1 > 0 and 0.0 < fixed_pct < 100.0:
                frac = fixed_pct / 100.0
                sig["pnl_pct"] = round(frac * _leg(tp1) + (1.0 - frac) * _leg(exit_price), 2)
                sig["pnl_basis"] = "partial_fix_at_tp1"
            else:
                sig["pnl_pct"] = round(_leg(exit_price), 2)
                sig["pnl_basis"] = "full_position"
    try:
        opened = datetime.fromisoformat(str(sig.get("opened_at")))
        sig["duration_min"] = round((ts - opened).total_seconds() / 60.0, 1)
    except (TypeError, ValueError):
        pass
    # Snapshot MFE and TP1 progress at close for history/backtest analysis
    mfe = _mfe_pct(sig, direction=direction)
    sig["mfe_pct"] = round(mfe, 2)
    if isinstance(sig.get("features_last"), dict):
        sig["features_close"] = sig["features_last"]
    sig.pop("features_last", None)
    tp1 = float(sig.get("tp1") or 0)
    entry_edge = _worst_entry(sig, direction=direction)
    if tp1 > 0 and entry_edge > 0:
        tp1_dist = abs(entry_edge - tp1)
        if tp1_dist > 0:
            sig["tp1_progress_pct"] = round(min(mfe / tp1_dist * entry_edge, 100.0), 1)
    # Archive to closed_history so repeat signals on the same key don't lose prior outcomes
    history: list = state.setdefault("closed_history", [])
    from hunt_core.track.outcomes import outcome_archive_key

    record = dict(sig)
    record.setdefault("symbol", symbol)
    record.setdefault("direction", direction)
    leg_key = outcome_archive_key(record)
    if leg_key is not None:
        for rec in reversed(history):
            if outcome_archive_key(rec) == leg_key:
                return
    history.append(record)
    if len(history) > 1000:
        del history[:len(history) - 1000]
    if archive:
        try:
            from hunt_core.track.outcomes import append_outcome_record, kpi_bucket
            from hunt_core.paths import SIGNAL_HISTORY

            append_outcome_record(SIGNAL_HISTORY, {**record, "kpi_bucket": kpi_bucket(record)})
        except Exception:  # noqa: BLE001
            _LOG.exception("outcome record append failed")
    try:
        _append_event(
            "close",
            symbol=symbol,
            direction=direction,
            detail=reason,
            payload={
                "close_reason": reason,
                "pnl_pct": sig.get("pnl_pct"),
                "duration_min": sig.get("duration_min"),
                "exit_price": sig.get("exit_price"),
                "close_lifecycle_phase": sig.get("close_lifecycle_phase"),
                "score": sig.get("score"),
                "fuel": sig.get("fuel"),
                "entry_lifecycle_phase": sig.get("entry_lifecycle_phase"),
                "entry_lifecycle_bias": sig.get("entry_lifecycle_bias"),
                "tp1_managed": sig.get("tp1_managed", False),
                "signal_phase": sig.get("phase"),
            },
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("close event append failed")
    # Task 7: auto-resolution — record outcome to ledger
    try:
        _ledger_event = {
            "tp1_hit": "tp1_hit", "tp2_hit": "tp2_hit",
            "stop_loss": "sl_hit", "stop_loss_slippage": "sl_hit",
            "orphan_expired": "timeout", "time_stall": "timeout",
        }.get(reason, "close")
        from hunt_core.track.outcome_ledger import append_ledger_event
        append_ledger_event({
            "symbol": symbol,
            "direction": direction,
            "event": _ledger_event,
            "delivered": True,
            "reason": reason,
            "exit_price": sig.get("exit_price"),
            "pnl_pct": sig.get("pnl_pct"),
            "duration_min": sig.get("duration_min"),
            "fusion_score": sig.get("fusion_score") or sig.get("score"),
            "entry_lifecycle_phase": sig.get("entry_lifecycle_phase"),
            "close_lifecycle_phase": sig.get("close_lifecycle_phase"),
            "entry_zone": [sig.get("entry_lo"), sig.get("entry_hi")],
            "stop_loss": sig.get("stop_loss"),
            "tp1": sig.get("tp1"),
            "tp2": sig.get("tp2"),
            "risk_reward": sig.get("risk_reward"),
        })
    except Exception:  # noqa: BLE001
        _LOG.exception("ledger event append failed")


# Auto-resolution threshold: close signals older than this (live price check).
AUTO_RESOLVE_TIMEOUT_HOURS = 48.0
# Manipulation runners hold to a structural pool, which on a 1d-scale setup is days
# away. They exit on TP2 or stop, not on the clock; this is a runaway guard only.
AUTO_RESOLVE_TIMEOUT_HOURS_LADDER = 24.0 * 14
AUTO_RESOLVE_GRACE_MINUTES = 5.0  # ignore TP1/SL hits within first N min (entry still filling)


def auto_resolve_active_signals(
    tracker_state: dict[str, Any],
    price_map: dict[str, float],
    *,
    now: datetime | None = None,
    timeout_hours: float = AUTO_RESOLVE_TIMEOUT_HOURS,
    grace_minutes: float = AUTO_RESOLVE_GRACE_MINUTES,
) -> list[str]:
    """Check all active signals against live price — TP1/SL/timeout resolution.

    Uses live WS ticker prices (not kline extremes) so resolution happens
    at tick granularity. Writes to outcome ledger via ``close_signal()``.

    Returns list of closed signal keys (e.g. ``["BTCUSDT:long", ...]``).
    """
    ts = now or clock.now_utc()
    signals = tracker_state.get("signals") or {}
    closed: list[str] = []

    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") in ("closed", "invalidated"):
            continue

        sym = sig.get("symbol") or ""
        direction = sig.get("direction") or ""
        if not sym or direction not in ("long", "short"):
            continue

        price = price_map.get(sym)
        if price is None or price <= 0:
            continue

        tp1 = float(sig.get("tp1") or 0)
        tp2 = float(sig.get("tp2") or 0)
        sl = float(sig.get("stop_loss") or 0)

        # A manipulation setup carries a pool ladder and is delivered as «тейки
        # частями … держим до цели/стопа»: TP1 is a PARTIAL fix, the runner rides to
        # the «среднесрочная цель». Closing the whole position on the first touch
        # contradicted _trailing.py, which already exempts these signals from the
        # trail for exactly that reason, and capped every manipulation trade at its
        # nearest pool. Fall through to TP2/stop instead.
        holds_ladder = str(sig.get("setup_phase") or "") == "manipulation" and tp2 > 0

        # Grace period: ignore TP1/SL hits during entry fill window
        age_min = _signal_age_min(sig, ts)
        if age_min < grace_minutes:
            continue
        # The method's own horizon is multi-day («в среднесроке эта сделка показала
        # 250%»); a 48h wall-clock timeout closed the runner before the structural
        # target could be reached.
        effective_timeout_h = AUTO_RESOLVE_TIMEOUT_HOURS_LADDER if holds_ladder else timeout_hours
        if age_min > effective_timeout_h * 60:
            close_signal(
                tracker_state,
                symbol=sym,
                direction=direction,
                reason="timeout",
                exit_price=price,
                now=ts,
            )
            closed.append(key)
            continue

        if direction == "long":
            if holds_ladder and tp2 > 0 and price >= tp2:
                close_signal(
                    tracker_state, symbol=sym, direction=direction,
                    reason="tp2_hit", exit_price=price, now=ts,
                )
                closed.append(key)
            elif holds_ladder and tp1 > 0 and price >= tp1:
                sig["tp1_hit"] = True  # partial fix; keep the runner open
            elif tp1 > 0 and price >= tp1:
                close_signal(
                    tracker_state,
                    symbol=sym,
                    direction=direction,
                    reason="tp1_hit",
                    exit_price=price,
                    now=ts,
                )
                closed.append(key)
            elif sl > 0 and price <= sl:
                close_signal(
                    tracker_state,
                    symbol=sym,
                    direction=direction,
                    # Canonical stop-out reason. "stop_loss" was recognised by NO
                    # consumer — the re-entry / loss-streak cooldowns (_cooldowns.py)
                    # and the loss classifiers (outcomes.LOSS_REASONS,
                    # stats_report._STOP_REASONS) all key on "stop_hit" — so this
                    # dominant per-tick auto-resolve stop path bypassed both
                    # cooldowns (instant re-entry into a just-stopped symbol) and
                    # misclassified the loss in win-rate stats (TRACK-1).
                    reason="stop_hit",
                    exit_price=price,
                    now=ts,
                )
                closed.append(key)
        elif direction == "short":
            if holds_ladder and tp2 > 0 and price <= tp2:
                close_signal(
                    tracker_state, symbol=sym, direction=direction,
                    reason="tp2_hit", exit_price=price, now=ts,
                )
                closed.append(key)
            elif holds_ladder and tp1 > 0 and price <= tp1:
                sig["tp1_hit"] = True  # partial fix; keep the runner open
            elif tp1 > 0 and price <= tp1:
                close_signal(
                    tracker_state,
                    symbol=sym,
                    direction=direction,
                    reason="tp1_hit",
                    exit_price=price,
                    now=ts,
                )
                closed.append(key)
            elif sl > 0 and price >= sl:
                close_signal(
                    tracker_state,
                    symbol=sym,
                    direction=direction,
                    # Canonical stop-out reason. "stop_loss" was recognised by NO
                    # consumer — the re-entry / loss-streak cooldowns (_cooldowns.py)
                    # and the loss classifiers (outcomes.LOSS_REASONS,
                    # stats_report._STOP_REASONS) all key on "stop_hit" — so this
                    # dominant per-tick auto-resolve stop path bypassed both
                    # cooldowns (instant re-entry into a just-stopped symbol) and
                    # misclassified the loss in win-rate stats (TRACK-1).
                    reason="stop_hit",
                    exit_price=price,
                    now=ts,
                )
                closed.append(key)

    return closed


# Minimum signal age before trusting wider bars for intrabar extremes: a live 5m
# candle may have opened BEFORE the signal did — its wick would falsely hit SL.


def _entry_mid(active: dict[str, Any]) -> float:
    lo = float(active.get("entry_lo") or 0)
    hi = float(active.get("entry_hi") or 0)
    if lo > 0 and hi > 0:
        return (lo + hi) / 2.0
    return lo or hi


def _pnl_at_price(active: dict[str, Any], direction: str, price: float) -> float:
    mid = _entry_mid(active)
    if mid <= 0 or price <= 0:
        return 0.0
    raw = (price - mid) / mid * 100.0
    return raw if direction == "long" else -raw


def _signal_age_min(active: dict[str, Any], ts: datetime) -> float:
    try:
        opened = datetime.fromisoformat(str(active.get("opened_at")))
    except (TypeError, ValueError):
        return 0.0
    return (ts - opened).total_seconds() / 60.0


def duration_minutes(
    opened_at: str | None,
    *,
    now: datetime | None = None,
    end_at: str | None = None,
) -> float | None:
    """Minutes elapsed since ``opened_at`` (or until ``end_at`` when set)."""
    if not opened_at:
        return None
    try:
        start = datetime.fromisoformat(str(opened_at).replace(" ", "T"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end_at:
            end = datetime.fromisoformat(str(end_at).replace(" ", "T"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
        else:
            end = now or clock.now_utc()
        return round((end - start).total_seconds() / 60.0, 1)
    except (TypeError, ValueError):
        return None


def _followup_trade_metrics(
    active: dict[str, Any],
    *,
    direction: str,
    price: float,
    ts: datetime,
) -> dict[str, Any]:
    """PnL % and duration for Telegram follow-ups."""
    return {
        "duration_min": duration_minutes(active.get("opened_at"), now=ts),
        "pnl_pct": round(_pnl_at_price(active, direction, price), 2),
    }



def latch_setup_if_active(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
) -> dict[str, Any]:
    """Keep confirmed=True while TG-active signal open — no demote next poll."""
    k = _key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or not _is_signal_active(active):
        return setup
    if not (active.get("telegram_sent") or active.get("entry_message_id")):
        return setup
    out = dict(setup)
    out["impulse_confirmed"] = True
    out["confirm_latched"] = True
    out["phase"] = "long_confirmed" if direction == "long" else "dump_confirmed"
    if active.get("level_expired"):
        out["level_expired"] = True
        out["level_test_count"] = active.get("level_test_count")
        out["level_reaction_max_pct"] = active.get("level_reaction_max_pct")
    return out


def latch_row_setups(state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Apply confirm latch to both setup sides on a watch row."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return row
    for direction, key in (("short", "dump"), ("long", "long")):
        setup = row.get(key)
        if isinstance(setup, dict):
            row[key] = latch_setup_if_active(
                state, symbol=sym, direction=direction, setup=setup
            )
    return row


def reconcile_signal(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    hi: float,
    lo: float,
    last_price: float,
    ts: datetime,
) -> list[HuntFollowUp]:
    """Orphan reconciliation: apply kline extremes fetched outside the watch loop.

    Used for active signals whose symbol is no longer in the watchlist —
    without this they never close (PLAYUSDT post-mortem: TP2 hit, stayed
    active for 18h).
    """
    active = (state.get("signals") or {}).get(_key(symbol, direction))
    if isinstance(active, dict) and _is_signal_active(active):
        active["extreme_hi"] = max(float(active.get("extreme_hi") or last_price), hi)
        active["extreme_lo"] = min(float(active.get("extreme_lo") or last_price), lo)
    events = evaluate_levels(
        state, symbol=symbol, direction=direction,
        price=last_price, hi=hi, lo=lo, ts=ts,
    )
    active = (state.get("signals") or {}).get(_key(symbol, direction))
    if isinstance(active, dict):
        active["last_checked_at"] = ts.isoformat()
        if _is_signal_active(active):
            active["last_reconcile_ts"] = ts.isoformat()
    return events


def mark_followups_sent(
    state: dict[str, Any], events: list[HuntFollowUp], *, now: datetime
) -> None:
    for ev in events:
        _mark_followup(state, ev.message_key, now=now)


def reconcile_orphan(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    hi: float,
    lo: float,
    last_price: float,
    ts: datetime,
) -> list[HuntFollowUp]:
    """Reconcile one active signal against kline extremes."""
    return reconcile_signal(
        state,
        symbol=symbol,
        direction=direction,
        hi=hi,
        lo=lo,
        last_price=last_price,
        ts=ts,
    )


def _fmt(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.3f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


__all__ = [
    "FOLLOWUP_COOLDOWN_MINUTES",
    "HuntFollowUp",
    "SignalEvent",
    "SignalPhase",
    "evaluate_followups",
    "global_confirm_burst_cap_reached",
    "iter_active_tracker_symbols",
    "latch_row_setups",
    "load_tracker_state",
    "mark_close_notified",
    "mark_followups_sent",
    "reconcile_active_from_ticker",
    "reconcile_orphan",
    "reconcile_signal",
    "recent_stop_hit_cooldown",
    "record_confirm_burst",
    "register_signal_open",
    "signal_confirm_announced",
    "symbol_daily_tg_cap_reached",
    "symbol_loss_streak_cooldown",
    "symbol_repeat_loser_blocked",
]
