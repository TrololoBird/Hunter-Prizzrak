"""Lab vs production delivery lane routing (E1)."""
from __future__ import annotations

import os
from typing import Any


def lab_chat_id() -> str:
    return (
        os.environ.get("TELEGRAM_LAB_CHAT_ID", "").strip()
        or os.environ.get("HUNT_LAB_CHAT_ID", "").strip()
    )


def is_lab_delivery(*, setup: dict[str, Any], row: dict[str, Any] | None = None) -> bool:
    """Exploratory lane: EV bootstrap, catalog EV-primary, expansion advisory."""
    if os.environ.get("HUNT_EV_BOOTSTRAP", "0").strip().lower() in {"1", "true", "yes"}:
        if setup.get("ev_primary") or setup.get("ev_bootstrap"):
            return True
    if setup.get("long_ramp_reason"):
        return True
    if setup.get("delivery_lane") == "lab":
        return True
    row = row or {}
    if str(row.get("delivery_lane") or "") == "lab":
        return True
    exp = row.get("expansion")
    if isinstance(exp, dict) and exp.get("lab_alert"):
        return True
    return False


def route_delivery_lane(
    *,
    setup: dict[str, Any],
    row: dict[str, Any] | None = None,
) -> str:
    return "lab" if is_lab_delivery(setup=setup, row=row) else "production"


def ledger_path_for_lane(*, setup: dict[str, Any] | None = None, row: dict[str, Any] | None = None):
    from hunt_core.deliver.delivery_state import LAB_LEDGER_PATH
    from hunt_core.track.outcome_ledger import LEDGER_PATH

    if route_delivery_lane(setup=setup or {}, row=row) == "lab":
        return LAB_LEDGER_PATH
    return LEDGER_PATH


async def send_lane_html(
    broadcaster: Any,
    text: str,
    *,
    setup: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Route Telegram HTML to lab or production chat."""
    chat = lab_chat_id() if route_delivery_lane(setup=setup or {}, row=row) == "lab" else ""
    if chat:
        from hunt_core.deliver.telegram import TelegramBroadcaster

        lab_bc = TelegramBroadcaster(broadcaster.token, chat)
        return await lab_bc.send_html(text, **kwargs)
    return await broadcaster.send_html(text, **kwargs)
