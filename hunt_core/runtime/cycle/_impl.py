"""Hunter per-tick cycle — run_loop / run_tick (H-B rewrite)."""
from __future__ import annotations

import structlog


import asyncio
import json
import os
from typing import Any

from hunt_core.domain.config import SYMBOL_TICK_TIMEOUT_S

logger = structlog.get_logger(__name__)

from hunt_core.market import HuntCcxtStreams

from hunt_core.data.lake import (
    buffer_cooldown_state,
)
from hunt_core.runtime.state import (
    SNIPER_CONFIG,
    STATE_PATH,
)


HUNT_SNIPER_MODE = SNIPER_CONFIG.enabled
HUNT_SNIPER_LIVE_PHASES = SNIPER_CONFIG.live_phases
HUNT_SNIPER_TOP_LS_MAX = SNIPER_CONFIG.top_ls_max
HUNT_SNIPER_REQUIRE_TOP_LS = SNIPER_CONFIG.require_top_ls
HUNT_SNIPER_CHASE_TOL = SNIPER_CONFIG.chase_tol
HUNT_SNAPSHOT_PARALLEL = max(1, int(os.getenv("HUNT_SNAPSHOT_PARALLEL", "6")))

_TICK_LOCK = asyncio.Lock()


def _overlay_ws_tickers(
    ticker_by_sym: dict[str, dict[str, Any]],
    symbols: tuple[str, ...] | list[str],
    ws_feed: HuntCcxtStreams | None,
) -> None:
    """Prefer WS last over batch REST ticker for snapshot price seed.

    Only updates symbols already present from the REST ticker batch — never
    creates new partial entries (which would lack quote_volume and trigger
    ticker_field_missing downstream).
    """
    if ws_feed is None:
        return
    for sym in symbols:
        base = ticker_by_sym.get(sym)
        if base is None:
            continue  # no REST ticker — don't synthesise an incomplete entry
        lt = ws_feed.live_ticker(sym)
        if not lt:
            continue
        last = float(lt.get("last") or 0)
        if last <= 0:
            continue
        base = dict(base)
        base["last_price"] = last
        ticker_by_sym[sym] = base


def _load_state() -> dict[str, str]:
    state: dict[str, str] = {}
    if STATE_PATH.exists():
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update({str(k): str(v) for k, v in raw.items()})
        except json.JSONDecodeError:
            pass
    try:
        from hunt_core.deliver.delivery_state import load_delivery_state

        ds = load_delivery_state()
        if isinstance(ds, dict):
            state.update({str(k): str(v) for k, v in ds.items()})
    except Exception:
        logger.exception("load delivery state failed")
    return state


def _save_state(state: dict[str, str]) -> None:
    buffer_cooldown_state(state, STATE_PATH)
    try:
        from hunt_core.deliver.delivery_state import save_delivery_state

        save_delivery_state(state)
    except Exception:
        logger.exception("save delivery state failed")


from hunt_core.runtime.cycle._cycle_tick import run_tick


from hunt_core.runtime.cycle._cycle_loop import run_loop


__all__ = ["SYMBOL_TICK_TIMEOUT_S", "run_tick", "run_loop"]
