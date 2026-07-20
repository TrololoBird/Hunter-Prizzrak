"""Hunter per-tick cycle — run_loop / run_tick (H-B rewrite)."""
from __future__ import annotations

import structlog


import asyncio
import os

from hunt_core import serde
from hunt_core.domain.config import SYMBOL_TICK_TIMEOUT_S

logger = structlog.get_logger(__name__)

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


def _load_state() -> dict[str, str]:
    state: dict[str, str] = {}
    if STATE_PATH.exists():
        try:
            raw = serde.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update({str(k): str(v) for k, v in raw.items()})
        except serde.JSONDecodeError:
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
