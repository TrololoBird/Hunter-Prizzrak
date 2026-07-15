"""Outcome ledger recording extracted from _cycle_tick (debloat)."""
from __future__ import annotations

from typing import Any

import structlog

LOG = structlog.get_logger("hunt_core.runtime.cycle.ledger")


def record_outcome_ledger(
    *,
    symbol: str,
    direction: str,
    row: dict[str, Any],
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
            row=row,
            setup=setup,
            blockers=blockers,
            delivered=delivered,
        )
        append_ledger_event(record, path=ledger_path_for_lane(setup=setup, row=row))
    except Exception as exc:
        LOG.warning(
            "outcome_ledger_failed | symbol=%s direction=%s event=%s error=%s",
            symbol,
            direction,
            event,
            exc,
        )


__all__ = ["record_outcome_ledger"]
