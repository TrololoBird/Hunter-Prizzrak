"""Pinned deep TG — change-only notifications (no startup burst)."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.runtime.analyst_assembly import (
    assemble_analyst_tick,
    material_deep_change,
    send_analyst_change_telegram,
)
from hunt_core.runtime.tick_state import deep_query_store

if TYPE_CHECKING:
    from hunt_core.maps.engine import MapTimeSeriesStore
    from hunt_core.view.runtime import MarketRuntime

LOG = structlog.get_logger("hunt.pinned_brief")


def pinned_startup_brief_enabled() -> bool:
    raw = os.getenv("HUNT_PINNED_STARTUP_BRIEF", "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


async def deliver_pinned_startup_brief(
    broadcaster: TelegramBroadcaster,
    *,
    rt: MarketRuntime | None = None,
    store: MapTimeSeriesStore | None = None,
) -> int:
    """Legacy startup burst — disabled by default; use ``analyst_pinned_loop`` instead."""
    if not pinned_startup_brief_enabled():
        LOG.info("pinned_startup_brief_skipped", reason="disabled")
        return 0

    from hunt_core.maps.engine import get_map_store
    from hunt_core.runtime.tick_state import live_market_runtime

    rt = rt or live_market_runtime()
    if rt is None:
        LOG.info("pinned_startup_brief_skipped", reason="engine_unavailable")
        return 0
    store = store or get_map_store()

    sent = 0
    for sym in PINNED_SYMBOLS:
        try:
            prev = deep_query_store().get(sym)
            native = await assemble_analyst_tick(sym, rt, store=store)
            if native is None:
                continue
            if material_deep_change(sym, native, prev=prev):
                if await send_analyst_change_telegram(broadcaster, native):
                    sent += 1
        except Exception as exc:
            LOG.warning("pinned_brief_probe_failed", symbol=sym, error=repr(exc))
    LOG.info("pinned_brief_complete", sent=sent, total=len(PINNED_SYMBOLS))
    return sent


__all__ = ["deliver_pinned_startup_brief", "pinned_startup_brief_enabled"]
