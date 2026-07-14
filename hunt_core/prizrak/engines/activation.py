"""Activation state — how close price is to a signal's entry/catalyst (R4).

Trimmed (2026-07): ``plan_lifecycle_from_activation``, ``recompute_plan_on_activation``,
``activation_event`` were removed — their only caller was ``serialize.py``'s
``attach_verdict_v2_to_row`` (deleted along with the L0-L5 ``ScenarioVerdict``
authority). Only ``assess_activation`` survives — it's generic (plain dict in, plain
dict out) and is still called by ``signal_queue.py``, agnostic to which engine
produced the summary.
"""
from __future__ import annotations

import logging

from typing import Any, Literal

from hunt_core.prizrak.engines._helpers import safe_float

LOG = logging.getLogger(__name__)

ActivationState = Literal["idle", "near_entry", "in_entry_zone", "near_catalyst", "at_catalyst"]


def assess_activation(row: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return {"state": "idle", "dist_catalyst_pct": None, "dist_entry_pct": None, "detail": ""}

    state: ActivationState = "idle"
    dist_cat: float | None = None
    dist_entry: float | None = None

    cat = summary.get("catalyst_level")
    if cat is not None:
        try:
            cp = float(cat)
            dist_cat = abs(price - cp) / price * 100.0
            if dist_cat <= 0.15:
                state = "at_catalyst"
            elif dist_cat <= 0.55 and state == "idle":
                state = "near_catalyst"
        except (TypeError, ValueError):
            LOG.debug("catalyst_level float conversion failed", exc_info=True)
            pass

    lo = summary.get("entry_lo")
    hi = summary.get("entry_hi")
    try:
        el, eh = safe_float(lo), safe_float(hi)
        if el > 0 and eh > 0:
            # A "вход по факту реакции, не по касанию" downgrade used to live here, gated
            # on entry_type == "pullback_limit". It was doubly broken: `entry_type` is
            # produced by NOTHING (so the branch never ran, and with it the whole
            # at_resistance computation), and the test itself was direction-blind —
            # `price > zone_mid` is the WORSE fill for a long but the BETTER one for a
            # short, and at_resistance confirms a short rather than blocking it. Wiring it
            # as written would have shipped that bug. The reaction-vs-touch gate is a real
            # method concept, but it belongs in the orchestrator with direction awareness,
            # not as a decoy reading a phantom key. Touching the zone = in_entry_zone.
            if el <= price <= eh:
                state = "in_entry_zone"
            else:
                dist_entry = min(abs(price - el), abs(price - eh)) / price * 100.0
                if dist_entry <= 0.35 and state in {"idle", "near_catalyst"}:
                    state = "near_entry"
    except (TypeError, ValueError):
        LOG.debug("entry_lo/entry_hi zone assessment failed", exc_info=True)
        pass

    detail = ""
    if state == "in_entry_zone":
        detail = "active"
    elif state in {"near_entry", "at_catalyst", "near_catalyst"}:
        detail = "armed"

    return {
        "state": state,
        "dist_catalyst_pct": round(dist_cat, 3) if dist_cat is not None else None,
        "dist_entry_pct": round(dist_entry, 3) if dist_entry is not None else None,
        "detail": detail,
    }


__all__ = ["ActivationState", "assess_activation"]
