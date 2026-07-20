"""Single emit path — lifecycle transitions → Telegram."""
from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

from hunt_core.signals.lifecycle import LifecycleTransition, SignalLifecycleStore, process_lifecycle_tick
from hunt_core.signals.model import Signal

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView

_LOG = structlog.get_logger(__name__)


class SignalEmitter:
    """Routes Deep rows through the lifecycle spine (typed :class:`NativeAnalystView`)."""

    def __init__(self, store: SignalLifecycleStore | None = None) -> None:
        self.store = store or SignalLifecycleStore.load()

    def preview_deep_row(self, native: NativeAnalystView) -> LifecycleTransition:
        return process_lifecycle_tick(native, module=1, store=self.store, commit=False)

    async def emit_deep(
        self,
        broadcaster: Any,
        native: NativeAnalystView,
        *,
        cycle_peers: list[NativeAnalystView] | None = None,
        transition: LifecycleTransition | None = None,
    ) -> bool:
        """Emit only when lifecycle advances — replaces fingerprint dedup."""
        from hunt_core.runtime.analyst_assembly import _compact_symbol, send_analyst_change_telegram

        transition = transition or process_lifecycle_tick(
            native, module=1, store=self.store, commit=True
        )
        if transition.event == "none":
            _LOG.info(
                "deep_lifecycle_suppressed symbol=%s reason=%s",
                _compact_symbol(native.view.symbol),
                transition.suppress_reason,
            )
            return False
        ok = await send_analyst_change_telegram(
            broadcaster,
            native,
            cycle_peers=cycle_peers,
            lifecycle_event=transition.event,
        )
        if ok and transition.signal is not None:
            self.store.record_emit(transition.signal, event=transition.event)
            self.store.save()
            _record_deep_outcome(transition.signal, native, event=transition.event)
            _register_tracker(transition.signal, native)
        return ok


def _ledger_setup_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Bridge deep-plan keys (entry_lo/entry_hi/rr_primary) to the ledger's
    geometry reader (entry/entry_zone/risk_reward) so delivered-signal rows
    stop recording null trade geometry."""
    from hunt_core.track.tracker import _entry_zone_from_plan

    setup = dict(plan)
    zone = _entry_zone_from_plan(setup)
    if zone is not None and setup.get("entry_zone") is None:
        setup["entry_zone"] = zone
        if setup.get("entry") is None:
            setup["entry"] = (zone[0] + zone[1]) / 2.0
    if setup.get("risk_reward") is None:
        setup["risk_reward"] = setup.get("rr_primary")
    return setup


def _record_deep_outcome(signal: Signal, native: NativeAnalystView, *, event: str) -> None:
    try:
        from hunt_core.runtime.cycle._cycle_ledger import record_outcome_ledger

        record_outcome_ledger(
            symbol=signal.symbol,
            direction=signal.direction,
            native=native,
            setup=_ledger_setup_from_plan(signal.plan),
            delivered=True,
            blockers=[],
            event="delivered" if event == "signal" else event,
        )
    except Exception:
        _LOG.exception("deep_outcome_ledger_failed symbol=%s", signal.symbol)


def _register_tracker(signal: Signal, native: NativeAnalystView) -> None:
    try:
        from datetime import UTC, datetime

        from hunt_core.track.tracker import (
            load_tracker_state as load_state,
            register_signal_open,
            save_tracker_state as save_state,
        )

        state = load_state()
        setup = dict(signal.plan)
        setup["direction"] = signal.direction
        setup["phase"] = signal.thesis
        # A deep plan is emitted long before the limit fills: the lifecycle spine emits event
        # "signal" for every activation that is not "in_entry_zone" (lifecycle.py), i.e. for
        # forward/deep zones whose entry sits percent away from spot. Registering those as
        # TRIGGERED told the tracker the position was already open: `extreme_hi/lo` seed at spot,
        # so MFE is instantly the whole unfilled distance — trailing fires, TP1 "hits", and
        # auto_resolve books a win for an order that never filled. ARMED is the state the tracker
        # already models for exactly this — `_maybe_armed_to_triggered` promotes it once price
        # enters the zone — but nothing ever set the tier, so the gate was unreachable.
        setup["delivery_tier"] = (
            "triggered" if signal.state == "activated" else "armed"
        )
        register_signal_open(
            state,
            symbol=signal.symbol,
            direction=signal.direction,
            price=float(native.view.last_price or 0),
            setup=setup,
            # No typed lifecycle model on the native deep lane yet (ADR-0004 gap) → empty.
            lifecycle={},
            now=datetime.now(UTC),
        )
        save_state(state)
    except Exception:
        _LOG.exception("signal_tracker_register_failed symbol=%s", signal.symbol)
