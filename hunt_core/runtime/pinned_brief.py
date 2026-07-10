"""Pinned deep TG — change-only notifications (no startup burst)."""
from __future__ import annotations

import os
from typing import Any

import structlog

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.runtime.analyst_assembly import (
    assemble_analyst_tick,
    material_deep_change,
    send_analyst_change_telegram,
)
from hunt_core.runtime.tick_state import deep_query_store

LOG = structlog.get_logger("hunt.pinned_brief")


def pinned_startup_brief_enabled() -> bool:
    raw = os.getenv("HUNT_PINNED_STARTUP_BRIEF", "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


async def deliver_pinned_startup_brief(
    broadcaster: TelegramBroadcaster,
    *,
    client: Any,
    stagger_ms: int = 250,
) -> int:
    """Legacy startup burst — disabled by default; use analyst_pinned_loop instead."""
    if not pinned_startup_brief_enabled():
        LOG.info("pinned_startup_brief_skipped", reason="disabled")
        return 0
    sent = 0
    for sym in PINNED_SYMBOLS:
        try:
            prev = deep_query_store().get(sym)
            row = await assemble_analyst_tick(sym, client, stagger_ms=stagger_ms)
            if row.get("error"):
                continue
            if material_deep_change(sym, row, prev=prev):
                if await send_analyst_change_telegram(broadcaster, row):
                    sent += 1
        except Exception as exc:
            LOG.warning("pinned_brief_probe_failed", symbol=sym, error=repr(exc))
    LOG.info("pinned_brief_complete", sent=sent, total=len(PINNED_SYMBOLS))
    return sent


__all__ = ["deliver_pinned_startup_brief", "pinned_startup_brief_enabled"]
