"""SL/TP intrabar evaluation and lifecycle stale invalidation (Phase 8 split)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from hunt_core.params.store import tracker_thresholds, tp1_partial_fix_pct as _tp1_pct
from hunt_core.track._trailing import (
    _mfe_pct,
    _stop_in_profit_zone,
    _update_trailing_stop,
    _worst_entry,
)

if TYPE_CHECKING:
    from hunt_core.track.tracker import HuntFollowUp

_LOG = logging.getLogger(__name__)

# Stale lifecycle phase sets (mirrored from tracker)
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

def _tracker_ref():
    from hunt_core.track import tracker as tr
    return tr

SNIPER_HOLD_TO_TARGET = __import__("os").environ.get("HUNT_SNIPER_MODE", "1") not in {"0", "false", "False"}
SIGNAL_TIMEOUT_HOURS = 48.0  # default / SHORT: pump-absorption is fast (hours–2-3 days)
# The LONG manipulation types are MEDIUM-TERM (accumulation → 100-400% over WEEKS; the
# trader "пересиживает" and holds — research/manipulations_corpus/long_manip_3types). A
# 48h close cut those winners before they ran; the dataset_v10 backtest is net-NEGATIVE
# with a 4-5 d horizon on longs but +0.54R/trade with a 21 d horizon. So give longs a
# medium-term leash. Shorts keep the fast timeout.
SIGNAL_TIMEOUT_HOURS_LONG = float(__import__("os").environ.get("HUNT_LONG_TIMEOUT_H", "504") or 504)  # 21 d


def _signal_timeout_hours(direction: str) -> float:
    return SIGNAL_TIMEOUT_HOURS_LONG if direction == "long" else SIGNAL_TIMEOUT_HOURS

_BAR_MIN_AGE_MIN = {"1m": 0.0, "1m_closed": 2.0, "5m": 6.0, "5m_closed": 11.0}
def _bar_extremes(
    row: dict[str, Any], active: dict[str, Any], *, price: float, ts: datetime
) -> tuple[float, float]:
    """Intrabar hi/lo since roughly the last poll — wicks must hit SL/TP, not only ticks."""
    trk = _tracker_ref()
    hi = lo = price
    age = trk._signal_age_min(active, ts)
    timeframes = row.get("timeframes") or {}
    for tf_key, min_age in _BAR_MIN_AGE_MIN.items():
        if age < min_age:
            continue
        candle = (timeframes.get(tf_key) or {}).get("candle") or {}
        try:
            c_hi = float(candle.get("high") or 0)
            c_lo = float(candle.get("low") or 0)
        except (TypeError, ValueError):
            continue
        if c_hi > 0:
            hi = max(hi, c_hi)
        if c_lo > 0:
            lo = min(lo, c_lo)
    # Cumulative extremes across polls (kline reconcile also writes these).
    try:
        hi = max(hi, float(active.get("extreme_hi") or price))
        lo = min(lo, float(active.get("extreme_lo") or price))
    except (TypeError, ValueError):
        pass
    active["extreme_hi"] = hi
    active["extreme_lo"] = lo
    return hi, lo


def _stale_lifecycle_invalidate(
    state: dict[str, Any],
    active: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    price: float,
    ts: datetime,
    announced: bool,
    archive: bool = True,
) -> HuntFollowUp | None:
    trk = _tracker_ref()
    """Close tracker position when lifecycle structurally contradicts the open thesis.

    ``archive`` is threaded to the terminal ``close_signal`` so verify/test callers
    (``archive=False``) never append rows to the production ``signal_history.jsonl``.
    """
    if trk._close_already_notified(state, symbol, direction):
        return None
    k = trk._key(symbol, direction)
    lc_phase = str(lifecycle.get("phase") or "")
    lc_bias = str(lifecycle.get("recommended_bias") or "")
    session = row.get("session") or {}
    pos = float(session.get("pos_in_range") or 0.5)

    contra = False
    tr = tracker_thresholds(symbol)
    ticks_needed = int(tr.get("stale_lc_ticks_default", 3))
    near_tp1_ticks = int(tr.get("stale_lc_ticks_near_tp1", 8))
    near_tp1_pct = float(tr.get("near_tp1_remaining_pct", 3.0))
    detail = ""

    if direction == "short":
        opened_phase = str(
            active.get("entry_lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )
        # Phase unchanged since entry — not a lifecycle transition (SPACEUSDT post-mortem:
        # short opened in impulse_initiating, stale fired 3 ticks later on same phase).
        if opened_phase and lc_phase == opened_phase:
            active["stale_lc_ticks"] = 0
            return None
        if active.get("tp1_managed") or active.get("tp1_hit") or active.get("sl_at_breakeven"):
            active["stale_lc_ticks"] = 0
            return None
        if lc_phase in _SHORT_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "post_dump_bounce" and active.get("tp1_hit"):
                ticks_needed = 1
        elif lc_bias == "long":
            contra = True
            detail = f"lifecycle_stale:bias_long:{lc_phase}"
    else:
        opened_phase = str(
            active.get("entry_lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )
        if opened_phase and lc_phase == opened_phase:
            active["stale_lc_ticks"] = 0
            return None
        if active.get("tp1_managed") or active.get("tp1_hit") or active.get("sl_at_breakeven"):
            active["stale_lc_ticks"] = 0
            return None
        if lc_phase in _LONG_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "distribution" and pos >= 0.82:
                ticks_needed = 2

    if not contra:
        active["stale_lc_ticks"] = 0
        return None

    mfe = _mfe_pct(active, direction=direction)
    if SNIPER_HOLD_TO_TARGET and (
        active.get("trailing_active")
        or mfe >= float(tr.get("sniper_hold_min_mfe_pct", 2.0))
    ):
        active["stale_lc_ticks"] = 0
        active["hold_reason"] = "sniper_hold"
        return None

    # Near-TP1 grace: if MFE is within 3% of TP1 distance, hold 8 ticks instead
    # of closing early. HUSDT/ARMUSDT were 1-2% from TP1 when stale fired at 3 ticks.
    if ticks_needed == int(tr.get("stale_lc_ticks_default", 3)) and not active.get("tp1_hit"):
        tp1 = float(active.get("tp1") or 0)
        entry_lo = float(active.get("entry_lo") or 0)
        entry_hi = float(active.get("entry_hi") or 0)
        entry_mid = (entry_lo + entry_hi) / 2.0 if entry_lo and entry_hi else (entry_lo or entry_hi)
        if tp1 > 0 and entry_mid > 0:
            if direction == "short":
                tp1_dist = (entry_mid - tp1) / entry_mid * 100.0
                mfe = (entry_mid - float(active.get("extreme_lo") or entry_mid)) / entry_mid * 100.0
            else:
                tp1_dist = (tp1 - entry_mid) / entry_mid * 100.0
                mfe = (float(active.get("extreme_hi") or entry_mid) - entry_mid) / entry_mid * 100.0
            remaining = tp1_dist - mfe
            if 0 < remaining <= near_tp1_pct:
                ticks_needed = near_tp1_ticks

    n = int(active.get("stale_lc_ticks") or 0) + 1
    active["stale_lc_ticks"] = n
    if n < ticks_needed:
        return None

    trk.close_signal(
        state,
        symbol=symbol,
        direction=direction,
        reason="lifecycle_stale",
        exit_price=price,
        now=ts,
        archive=archive,
    )
    msg_key = f"{k}:invalidate:lifecycle_stale:{lc_phase}"
    if not trk._followup_allowed(state, msg_key, now=ts):
        return None
    return trk.HuntFollowUp(
        event="invalidate",
        symbol=symbol,
        direction=direction,
        message_key=msg_key,
        detail=detail,
        price=price,
        payload={
            **trk._latched_levels_payload(active),
            "announced": announced,
            "reason": "lifecycle_stale",
            "phase": lc_phase,
            "stale_ticks": n,
            "pos_in_range": round(pos, 3),
            **trk._followup_trade_metrics(active, direction=direction, price=price, ts=ts),
        },
    )


def evaluate_levels(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    price: float,
    hi: float,
    lo: float,
    ts: datetime,
    row: dict[str, Any] | None = None,
) -> list[HuntFollowUp]:
    trk = _tracker_ref()
    """Latched SL/TP state machine against intrabar extremes.

    State transitions ALWAYS happen; the followup cooldown only dedupes
    messages. Transport flags (telegram_sent / entry_message_id) never gate
    state — they only mark events as announced for the sender.
    """
    events: list[HuntFollowUp] = []
    k = trk._key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or not trk._is_signal_active(active):
        return events
    if trk._close_already_notified(state, symbol, direction):
        return events
    announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))

    tr = tracker_thresholds(symbol)
    base_orphan_ttl_h = float(tr.get("orphan_ttl_hours", 24.0))
    orphan_ttl_h = 12.0 if direction == "short" else max(base_orphan_ttl_h * 2.0, 48.0)
    # Extend TTL if price has moved >50% toward TP1
    tp1 = float(active.get("tp1") or active.get("take_profit_1") or 0)
    entry = float(active.get("entry_price") or active.get("entry_reference") or 0)
    price = float(price or 0)
    if tp1 > 0 and entry > 0 and price > 0:
        if direction == "long" and price > entry and tp1 > entry:
            progress = (price - entry) / (tp1 - entry)
            if progress > 0.5:
                orphan_ttl_h = max(orphan_ttl_h, orphan_ttl_h + 12.0)
        elif direction == "short" and price < entry and tp1 < entry:
            progress = (entry - price) / (entry - tp1)
            if progress > 0.5:
                orphan_ttl_h = max(orphan_ttl_h, orphan_ttl_h + 12.0)
    last_rec_raw = active.get("last_reconcile_ts") or active.get("opened_at")
    try:
        last_rec = datetime.fromisoformat(str(last_rec_raw))
        if last_rec.tzinfo is None:
            last_rec = last_rec.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        last_rec = ts
    orphan_age_h = (ts - last_rec).total_seconds() / 3600.0
    if orphan_age_h >= orphan_ttl_h:
        _LOG.warning(
            "orphan_expired %s:%s — last reconcile %.1fh ago (ttl=%.0fh)",
            symbol,
            direction,
            orphan_age_h,
            orphan_ttl_h,
        )
        trk.close_signal(
            state,
            symbol=symbol,
            direction=direction,
            reason="orphan_expired",
            exit_price=price,
            now=ts,
        )
        msg_key = f"{k}:invalidate:orphan_expired"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"orphan TTL {orphan_ttl_h:.0f}h · "
                        f"last reconcile {orphan_age_h:.1f}h ago"
                    ),
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "reason": "orphan_expired",
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    # Longs accumulate and legitimately sit flat/red for days before the pump
    # ("пересидеть") — the 8h/1%-MFE stall was a short-trade tuning that killed medium-
    # term longs early. Scale the stall window for longs (env HUNT_LONG_STALL_H).
    _stall_default = float(tr.get("mfe_stall_hours", 8.0))
    if direction == "long":
        stall_h = float(__import__("os").environ.get("HUNT_LONG_STALL_H", "120") or 120)  # 5 d
    else:
        stall_h = _stall_default
    stall_min_mfe = float(tr.get("mfe_stall_min_pct", 1.0))
    signal_timeout_h = _signal_timeout_hours(direction)
    age_min = trk._signal_age_min(active, ts)
    if (
        not active.get("tp1_hit")
        and age_min >= stall_h * 60.0
        and age_min < signal_timeout_h * 60.0
        and _mfe_pct(active, direction=direction) < stall_min_mfe
    ):
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason="time_stall", exit_price=price, now=ts,
        )
        msg_key = f"{k}:invalidate:time_stall"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"time stall {stall_h:.0f}h · MFE "
                        f"{_mfe_pct(active, direction=direction):.1f}% < {stall_min_mfe:.1f}%"
                    ),
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "reason": "time_stall",
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    if age_min >= signal_timeout_h * 60.0:
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason="timeout", exit_price=price, now=ts,
        )
        msg_key = f"{k}:invalidate:timeout"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=f"timeout {signal_timeout_h:.0f}h без SL/TP",
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "reason": "timeout",
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    be_locked = trk._apply_early_breakeven_lock(active, direction=direction, symbol=symbol)
    if be_locked:
        stop = float(active.get("stop_loss") or 0)
        mfe = _mfe_pct(active, direction=direction)
        phase = str(active.get("entry_lifecycle_phase") or "")
        msg_key = f"{k}:early_be:{stop:.6f}"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="early_breakeven",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"Early BE · MFE {mfe:.1f}% · SL → {trk._fmt(stop)} "
                        f"({phase or '—'})"
                    ),
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "sl_at_breakeven": True,
                        "entry_lifecycle_phase": phase,
                        "mfe_pct": round(mfe, 2),
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
    trail_updated, prev_stop = _update_trailing_stop(
        active, direction=direction, row=row, symbol=symbol, ts=ts
    )

    tp1 = float(active.get("tp1") or 0)
    tp2 = float(active.get("tp2") or 0)
    stop = float(active.get("stop_loss") or 0)
    latch = trk._latched_levels_payload(active)
    latch["announced"] = announced

    if trail_updated and stop > 0:
        protected = round(trk._pnl_at_price(active, direction, stop), 2)
        msg_key = f"{k}:trailing:{stop:.6f}"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="trailing_updated",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"Trailing SL → {trk._fmt(stop)} · защита ~{protected:.1f}%"
                    ),
                    price=price,
                    payload={
                        **latch,
                        "stop_loss": stop,
                        "prev_stop": prev_stop,
                        "protected_pnl_pct": protected,
                        "trailing_active": True,
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )

    if active.get("tp1_hit") and not active.get("tp1_managed"):
        trk.on_tp1_reached(active, direction=direction, symbol=symbol, row=row)
        latch = trk._latched_levels_payload(active)
        latch["announced"] = announced
        stop = float(active.get("stop_loss") or 0)

    if direction == "short":
        stop_hit = stop > 0 and hi >= stop
        tp1_touch = tp1 > 0 and lo <= tp1
        tp2_touch = tp2 > 0 and lo <= tp2
        near_stop = stop > 0 and hi >= stop * 0.998
    else:
        stop_hit = stop > 0 and lo <= stop
        tp1_touch = tp1 > 0 and hi >= tp1
        tp2_touch = tp2 > 0 and hi >= tp2
        near_stop = stop > 0 and lo <= stop * 1.002

    # Same-tick guard: trailing into profit zone must not instant-close on stale hi/lo.
    if (
        stop_hit
        and trail_updated
        and _stop_in_profit_zone(active, direction=direction, stop=stop)
    ):
        stop_hit = False

    # Stop first: a wick through SL ends the signal even if TP printed later.
    if stop_hit:
        pnl_at_stop = trk._pnl_at_price(active, direction, stop)
        if active.get("trailing_active") and pnl_at_stop > 0:
            close_reason = "trailing_stop_profit"
            detail_msg = (
                f"Trailing SL {trk._fmt(stop)} · фиксация +{pnl_at_stop:.1f}%"
            )
        else:
            close_reason = "stop_hit"
            detail_msg = f"SL {trk._fmt(stop)} пробит (intrabar)"
        trk._transition(
            active,
            trk._coerce_signal_phase(active),
            trk.SignalPhase.INVALIDATED,
            strict=False,
        )
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason=close_reason, exit_price=stop, now=ts,
        )
        msg_key = f"{k}:invalidate:{close_reason}"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=detail_msg,
                    price=price,
                    payload={
                        **latch,
                        "reason": close_reason,
                        "trailing_active": bool(active.get("trailing_active")),
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=stop, ts=ts
                        ),
                    },
                )
            )
        return events

    if tp2_touch:
        skipped = not active.get("tp1_hit")
        active["tp1_hit"] = True
        active["tp2_hit"] = True
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason="tp2", exit_price=tp2, now=ts,
        )
        msg_key = f"{k}:tp2"
        if trk._followup_allowed(state, msg_key, now=ts):
            detail = f"TP1+TP2 (пролёт) · TP2 {trk._fmt(tp2)}" if skipped else f"TP2 {trk._fmt(tp2)}"
            events.append(
                trk.HuntFollowUp(
                    event="fix_profit_tp2",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=detail,
                    price=price,
                    payload={**latch, "tp2": tp2, "tp1_skipped": skipped},
                )
            )
        return events

    if tp1_touch and not active.get("tp1_hit"):
        active["tp1_hit"] = True
        trk.on_tp1_reached(active, direction=direction, symbol=symbol, row=row)
        latch = {**trk._latched_levels_payload(active), "announced": announced, "tp1": tp1}
        _worst_entry(active, direction=direction)
        fix_pct = int(active.get("partial_fixed_pct") or _tp1_pct(symbol))
        msg_key = f"{k}:tp1"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="fix_profit_tp1",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"TP1 {trk._fmt(tp1)} · зафиксируй {fix_pct}% · "
                        f"SL → {trk._fmt(active.get('stop_loss'))} (BE+buf)"
                    ),
                    price=price,
                    payload={
                        **latch,
                        "partial_fixed_pct": fix_pct,
                        "sl_at_breakeven": True,
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=tp1, ts=ts
                        ),
                    },
                )
            )

    if near_stop and not active.get("stop_warned"):
        active["stop_warned"] = True
        msg_key = f"{k}:stop_warn"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="stop_warning",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=f"near SL {trk._fmt(stop)}",
                    price=price,
                    payload={**latch, "stop": stop},
                )
            )
    return events


__all__ = ["evaluate_levels", "_stale_lifecycle_invalidate", "_bar_extremes"]
