"""Activation state — how close price is to a signal's entry/catalyst (R4).

Trimmed (2026-07): ``plan_lifecycle_from_activation``, ``recompute_plan_on_activation``,
``activation_event`` were removed — their only caller was ``serialize.py``'s
``attach_verdict_v2_to_row`` (deleted along with the L0-L5 ``ScenarioVerdict``
authority). Only ``assess_activation`` survives — it's generic (plain dict in, plain
dict out) and is still called by ``signal_queue.py``, agnostic to which engine
produced the summary.
"""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.prizrak.engines._helpers import safe_float

ActivationState = Literal["idle", "near_entry", "in_entry_zone", "near_catalyst", "at_catalyst"]


def assess_activation(
    row: dict[str, Any],
    summary: dict[str, Any],
    *,
    entry_type: str | None = None,
) -> dict[str, Any]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return {"state": "idle", "dist_catalyst_pct": None, "dist_entry_pct": None, "detail": ""}

    state: ActivationState = "idle"
    dist_cat: float | None = None
    dist_entry: float | None = None
    et = str(entry_type or summary.get("entry_type") or "")

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
            pass

    lo = summary.get("entry_lo")
    hi = summary.get("entry_hi")
    try:
        el, eh = float(lo), float(hi)
        if el > 0 and eh > 0:
            zone_mid = (el + eh) / 2.0
            at_resistance = False
            struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
            kl = struct.get("key_levels") if isinstance(struct.get("key_levels"), dict) else {}
            resist = safe_float(kl.get("resistance") or kl.get("last_swing_high"))
            if resist > 0 and price >= resist * 0.997:
                at_resistance = True
            if el <= price <= eh:
                if et == "pullback_limit" and (price > zone_mid or at_resistance):
                    state = "near_entry"
                    dist_entry = min(abs(price - el), abs(price - eh)) / price * 100.0
                else:
                    state = "in_entry_zone"
            else:
                dist_entry = min(abs(price - el), abs(price - eh)) / price * 100.0
                if dist_entry <= 0.35 and state in {"idle", "near_catalyst"}:
                    state = "near_entry"
    except (TypeError, ValueError):
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
