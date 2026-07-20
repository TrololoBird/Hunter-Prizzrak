"""Outcome ledger recording extracted from _cycle_tick (debloat)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView

LOG = structlog.get_logger("hunt_core.runtime.cycle.ledger")


def record_outcome_ledger(
    *,
    symbol: str,
    direction: str,
    native: NativeAnalystView | None = None,
    setup: dict[str, Any] | None = None,
    delivered: bool = False,
    blockers: list[str] | None = None,
    event: str = "blocked",
) -> None:
    from hunt_core.deliver.lab import ledger_path_for_lane
    from hunt_core.track.outcome_ledger import append_ledger_event, build_ledger_record

    try:
        record = build_ledger_record(
            symbol=symbol,
            direction=direction,
            event=event,
            native=native,
            setup=setup,
            blockers=blockers,
            delivered=delivered,
        )
        # No legacy row on the native deep lane; lane routing keys off the setup only.
        append_ledger_event(record, path=ledger_path_for_lane(setup=setup, row=None))
    except Exception as exc:
        LOG.warning(
            "outcome_ledger_failed | symbol=%s direction=%s event=%s error=%s",
            symbol,
            direction,
            event,
            exc,
        )


__all__ = ["record_outcome_ledger"]
