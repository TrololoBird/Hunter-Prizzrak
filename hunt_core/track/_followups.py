"""Lifecycle follow-ups — level tests, armed→triggered, bias-flip invalidation."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from hunt_core.track.tracker import HuntFollowUp

from hunt_core import clock
from hunt_core.params.store import tracker_thresholds
from hunt_core.track._evaluate_levels import (
    _bar_extremes,
    _stale_lifecycle_invalidate,
    evaluate_levels,
)
from hunt_core.track._trailing import _closed_atr1h_pct

_LEVEL_APPROACH_TOLERANCE = 0.003
_LEVEL_REACTION_ATR_MULT = 1.5
PHASE_CHANGE_GRACE_MIN = 20.0
EXIT_V2_ACTIVE = (
    os.environ.get("HUNT_EXIT_V2", "").strip().lower() in {"1", "true", "yes"}
    or os.environ.get("HUNT_SNIPER_MODE", "1") not in {"0", "false", "False"}
)


def _tracker_ref():
    from hunt_core.track import tracker as tr
    return tr


def _entry_bias_latch(active: dict[str, Any], lc_bias: str) -> str:
    """Immutable entry-bias latch for counter-flip detection (TRACK-3).

    Counter-bias TG alerts must compare the current lifecycle bias against the
    bias the trade was OPENED with. ``entry_lifecycle_bias`` can be empty when
    the lifecycle had no ``recommended_bias`` at creation (tracker.py:532). The
    old code then fell back to the MUTABLE ``lifecycle_bias`` — which is
    overwritten to the current tick every pass — so the "opened" bias became
    "the previous tick's bias" and a slow entry→counter drift never registered.
    Instead, latch the first non-empty bias observed here and keep it fixed.

    Returns the opened (entry) bias, latching it into ``active`` in place if it
    was previously unset.
    """
    opened = str(active.get("entry_lifecycle_bias") or "")
    if not opened and lc_bias:
        active["entry_lifecycle_bias"] = lc_bias
        opened = lc_bias
    return opened


def _closed_adx_1h(row: dict[str, Any]) -> float | None:
    tf = row.get("timeframes") or {}
    block = tf.get("1h_closed") or tf.get("1h") or {}
    try:
        val = float(block.get("adx14") or 0)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None



def _tracked_level(
    active: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
) -> float:
    latched = float(active.get("track_level") or 0)
    if latched > 0:
        return latched
    if direction == "short":
        return float(
            active.get("entry_lo")
            or setup.get("invalidation_above")
            or 0
        )
    return float(
        active.get("entry_hi")
        or setup.get("invalidation_below")
        or 0
    )


def _update_level_test_tracking(
    active: dict[str, Any],
    setup: dict[str, Any],
    row: dict[str, Any],
    *,
    direction: str,
    price: float,
) -> None:
    """Detect approach+bounce at latched level; expire after reaction >= 1.5×ATR."""
    if active.get("level_expired") or price <= 0:
        return
    level = _tracked_level(active, setup, direction=direction)
    if level <= 0:
        return
    active["track_level"] = level

    tol = level * _LEVEL_APPROACH_TOLERANCE
    dist = abs(price - level)
    in_zone = dist <= tol
    atr_pct = _closed_atr1h_pct(row)
    reaction_floor = max(0.5, _LEVEL_REACTION_ATR_MULT * max(atr_pct, 0.5))

    approaching = bool(active.get("_level_approaching"))
    if in_zone and not approaching:
        active["_level_approaching"] = True
        active["_level_touch_extreme"] = price
        return

    if approaching:
        extreme = float(active.get("_level_touch_extreme") or price)
        if direction == "short":
            extreme = min(extreme, price)
            reaction_pct = max(0.0, (price - extreme) / level * 100.0)
        else:
            extreme = max(extreme, price)
            reaction_pct = max(0.0, (extreme - price) / level * 100.0)
        active["_level_touch_extreme"] = extreme

        peak = float(active.get("level_reaction_max_pct") or 0.0)
        if reaction_pct > peak:
            active["level_reaction_max_pct"] = round(reaction_pct, 3)

        if reaction_pct >= reaction_floor:
            active["level_expired"] = True
            active.pop("_level_approaching", None)
            active.pop("_level_touch_extreme", None)
            return

        if not in_zone and reaction_pct >= reaction_floor * 0.35:
            active["level_test_count"] = int(active.get("level_test_count") or 0) + 1
            active["_level_approaching"] = False
            active.pop("_level_touch_extreme", None)
    elif not in_zone:
        active.pop("_level_approaching", None)
        active.pop("_level_touch_extreme", None)


def _maybe_armed_to_triggered(
    state: dict[str, Any],
    active: dict[str, Any],
    *,
    setup: dict[str, Any],
    symbol: str,
    direction: str,
    price: float,
    ts: datetime,
    announced: bool,
) -> Any | None:
    """ARMED setup → TRIGGERED when price enters the latched entry zone."""
    trk = _tracker_ref()
    if active.get("delivery_tier") != "armed":
        return None
    from hunt_core.contract import price_in_entry_zone  # noqa: PLC0415

    if not price_in_entry_zone(
        {
            "entry_zone": [active.get("entry_lo"), active.get("entry_hi")],
        },
        price=price,
        direction=direction,
        strict_upper=True,
    ):
        return None
    k = trk._key(symbol, direction)
    msg_key = f"{k}:entry_triggered"
    if not trk._followup_allowed(state, msg_key, now=ts):
        return None
    active["delivery_tier"] = "triggered"
    # The fill starts the trade's price history: re-seed the extremes at the
    # fill price. Anything recorded while ARMED is pre-entry market movement —
    # carrying it forward would hand the freshly-opened position an MFE/MAE it
    # never experienced (and could instantly satisfy TP1 or the trailing gate).
    active["extreme_hi"] = price
    active["extreme_lo"] = price
    active["opened_at"] = ts.isoformat()
    trk._transition(active, trk.SignalPhase.ARMED, trk.SignalPhase.TRIGGERED, strict=False)
    return trk.HuntFollowUp(
        event="entry_triggered",
        symbol=symbol,
        direction=direction,
        message_key=msg_key,
        detail="price_in_entry_zone",
        price=price,
        payload={
            **trk._latched_levels_payload(active),
            "announced": announced,
            "reason": "entry_triggered",
        },
    )


def evaluate_followups(
    state: dict[str, Any],
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[Any]:
    """Compare tick vs active signals; emit follow-up events (no entry cooldown)."""
    trk = _tracker_ref()
    ts = now or clock.now_utc()
    events: list[HuntFollowUp] = []
    symbol = str(row.get("symbol") or "").upper()
    price = float(row.get("price") or 0)
    if not symbol or price <= 0:
        return events

    lifecycle = row.get("lifecycle") or {}
    lc_phase = str(lifecycle.get("phase") or "")
    lc_bias = str(lifecycle.get("recommended_bias") or "")

    for direction, setup_key in (("short", "dump"), ("long", "long")):
        setup = row.get(setup_key) or {}
        k = trk._key(symbol, direction)
        active = (state.get("signals") or {}).get(k)
        if not active or not trk._is_signal_active(active):
            continue

        announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))
        opened_phase = str(
            active.get("lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )

        # 1) SL/TP against intrabar extremes — ALWAYS first, never skipped by
        # lifecycle branches and never gated by transport flags.
        hi, lo = _bar_extremes(row, active, price=price, ts=ts)
        trk._tick_feature_latch(active, row, direction=direction)
        _update_level_test_tracking(
            active, setup, row, direction=direction, price=price
        )
        events.extend(
            evaluate_levels(
                state, symbol=symbol, direction=direction,
                price=price, hi=hi, lo=lo, ts=ts, row=row,
            )
        )
        active["last_checked_at"] = ts.isoformat()
        if trk._is_signal_active(active):
            active["last_reconcile_ts"] = ts.isoformat()
        if not trk._is_signal_active(active):
            continue

        armed_fu = _maybe_armed_to_triggered(
            state,
            active,
            setup=setup,
            symbol=symbol,
            direction=direction,
            price=price,
            ts=ts,
            announced=announced,
        )
        if armed_fu is not None:
            events.append(armed_fu)

        stale_fu = None
        if not (EXIT_V2_ACTIVE and direction == "short"):
            stale_fu = _stale_lifecycle_invalidate(
                state,
                active,
                symbol=symbol,
                direction=direction,
                lifecycle=lifecycle,
                row=row,
                price=price,
                ts=ts,
                announced=announced,
            )
        if stale_fu is not None:
            events.append(stale_fu)
            continue

        # 2) Lifecycle invalidation — trend exhaustion for longs. The short
        # bounce-invalidate arm was dead code: lifecycle["invalidate_short"] has no
        # producer anywhere, so bounce_invalidate was always False (G-69, deleted).
        if (
            direction == "long"
            and lc_phase
            in {
                "exhaustion_at_high",
                "distribution",
            }
            and opened_phase in {
                "post_dump_bounce",
                "accumulation",
                "recovery",
                "breakout_arming",
                "impulse_initiating",
            }
        ):
            trk.close_signal(
                state, symbol=symbol, direction=direction,
                reason="trend_exhaustion", exit_price=price, now=ts,
            )
            msg_key = f"{k}:invalidate"
            if trk._followup_allowed(state, msg_key, now=ts):
                events.append(
                    trk.HuntFollowUp(
                        event="invalidate",
                        symbol=symbol,
                        direction=direction,
                        message_key=msg_key,
                        detail=f"phase={lc_phase}",
                        price=price,
                        payload={
                            **trk._latched_levels_payload(active),
                            "announced": announced,
                            "reason": "trend_exhaustion",
                            "phase": lc_phase,
                            **trk._followup_trade_metrics(
                                active, direction=direction, price=price, ts=ts
                            ),
                        },
                    )
                )

        else:
            if direction == "short":
                struct_bad, struct_reason = trk._short_structure_invalidated(
                    active,
                    setup,
                    price=price,
                )
            else:
                struct_bad, struct_reason = trk._long_structure_invalidated(
                    active,
                    setup,
                    price=price,
                    symbol=symbol,
                )
            if struct_bad:
                trk.close_signal(
                    state, symbol=symbol, direction=direction,
                    reason=struct_reason, exit_price=price, now=ts,
                )
                msg_key = f"{k}:invalidate:{struct_reason}"
                if trk._followup_allowed(state, msg_key, now=ts):
                    events.append(
                        trk.HuntFollowUp(
                            event="invalidate",
                            symbol=symbol,
                            direction=direction,
                            message_key=msg_key,
                            detail=struct_reason,
                            price=price,
                            payload={
                                **trk._latched_levels_payload(active),
                                "announced": announced,
                                "reason": struct_reason,
                                "phase": setup.get("phase"),
                                **trk._followup_trade_metrics(
                                    active, direction=direction, price=price, ts=ts
                                ),
                            },
                        )
                    )

        # Bias change while active — TG only on COUNTER-bias flips (long→short etc).
        # wait↔long ping-pong and accumulation↔impulse renames stay silent;
        # material long thesis break uses stale_lc (dump_active × N ticks).
        opened_bias = _entry_bias_latch(active, lc_bias)
        if trk._is_signal_active(active) and lc_phase:
            active["lifecycle_phase"] = lc_phase
            if lc_bias:
                active["lifecycle_bias"] = lc_bias

        if (
            trk._is_signal_active(active)
            and lc_bias
            and opened_bias
            and lc_bias != opened_bias
            and trk._signal_age_min(active, ts) >= PHASE_CHANGE_GRACE_MIN
        ):
            counter_bias = "long" if direction == "short" else "short"
            if lc_bias == counter_bias:
                if direction == "short" and trk._hold_short_through_dump_bounce(
                    active,
                    lifecycle,
                    price=price,
                    opened_bias=opened_bias,
                    lc_bias=lc_bias,
                    symbol=symbol,
                ):
                    active["lifecycle_bias"] = lc_bias
                    continue
                # HUSDT post-mortem: bias→long closed a losing short at -12.8%
                # while SL was never touched (wick missed between 60s polls).
                # Only crystallize on profitable flip or when SL is in play.
                stop = float(active.get("stop_loss") or 0)
                pnl_est = trk._pnl_at_price(active, direction, price)
                sl_in_play = (
                    direction == "short"
                    and stop > 0
                    and price >= stop * 0.998
                ) or (
                    direction == "long"
                    and stop > 0
                    and price <= stop * 1.002
                )
                if pnl_est < 0 and not sl_in_play:
                    msg_key = f"{k}:bias_warn:{lc_bias}"
                    if trk._followup_allowed(state, msg_key, now=ts):
                        events.append(
                            trk.HuntFollowUp(
                                event="phase_change",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=(
                                    f"counter-bias {lc_bias} при PnL {pnl_est:+.1f}% "
                                    f"— держим до SL/TP ({opened_phase} → {lc_phase})"
                                ),
                                price=price,
                                payload={
                                    "from": opened_phase,
                                    "to": lc_phase,
                                    "bias_from": opened_bias,
                                    "bias_to": lc_bias,
                                    "announced": announced,
                                    "pnl_est": round(pnl_est, 2),
                                },
                            )
                        )
                    active["lifecycle_bias"] = lc_bias
                    continue
                if active.get("tp1_managed") or active.get("sl_at_breakeven"):
                    active["lifecycle_bias"] = lc_bias
                    continue
                # Q13: in chop (ADX<20) prefer fixed SL over bias-flip on winners.
                tr = tracker_thresholds(symbol)
                chop_adx = float(tr.get("bias_flip_chop_adx_max", 20.0))
                adx1h = _closed_adx_1h(row)
                if (
                    pnl_est >= 0
                    and adx1h is not None
                    and adx1h < chop_adx
                    and not sl_in_play
                ):
                    active["lifecycle_bias"] = lc_bias
                    continue
                trk.close_signal(
                    state, symbol=symbol, direction=direction,
                    reason="bias_flip", exit_price=price, now=ts,
                )
                msg_key = f"{k}:invalidate:bias_flip"
                if trk._followup_allowed(state, msg_key, now=ts):
                    events.append(
                        trk.HuntFollowUp(
                            event="invalidate",
                            symbol=symbol,
                            direction=direction,
                            message_key=msg_key,
                            detail=(
                                f"bias {opened_bias or '—'} → {lc_bias} "
                                f"({opened_phase} → {lc_phase})"
                            ),
                            price=price,
                            payload={
                                **trk._latched_levels_payload(active),
                                "announced": announced,
                                "reason": "bias_flip",
                                "bias_to": lc_bias,
                                **trk._followup_trade_metrics(
                                    active, direction=direction, price=price, ts=ts
                                ),
                            },
                        )
                    )
                continue

    return events



__all__ = ["evaluate_followups", "PHASE_CHANGE_GRACE_MIN"]
