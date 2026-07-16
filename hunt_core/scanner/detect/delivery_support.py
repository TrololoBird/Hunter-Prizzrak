"""Delivery transport support — the small set of real helpers the delivery, tracking,
and report paths still need after the legacy gate/filter stack was removed.

The fusion engine's ``confirmed`` flag is the single delivery decision, so the former
multi-stage filter pipeline (mission veto, freshness/hard blocks, EV/family gates) is
gone. What remains here is genuine transport logic: a data-quality liquidity floor, an
entry-zone geometry test, a fixed R:R floor, and report helpers that surface the fusion
``gate_reason``. Functions that replaced a deleted *filter* embody the new design — "no
extra veto beyond the fusion gate" — and are documented as such.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Spine-owned now (levels/ and track/ were importing these FROM the scanner — a
# spine→strategy inversion). Re-exported here so this module's own callers are unchanged.
from hunt_core.contract import price_in_entry_zone  # noqa: F401
from hunt_core.signals.lifecycle import MID_DUMP_LC_PHASES  # noqa: F401
REPORT_BLOCK_PRIORITY: tuple[str, ...] = ("not_confirmed", "below_calibrated_gate", "cold_start")
BOUNCE_MIN_RISK_REWARD = 1.05
_MIN_RR_FLOOR = 1.6

# Data-quality liquidity floors (skip illiquid symbols — not a strategy filter).
_MIN_QUOTE_VOL_24H = 3_000_000.0
_MIN_OI_USD = 250_000.0


@dataclass(frozen=True)
class GateResult:
    """Delivery decision result (the fusion ``confirmed`` flag is authoritative)."""

    ok: bool
    code: str = ""
    message: str = ""


def liquidity_skip_reason(
    *,
    quote_volume: float | None,
    oi: float | None = None,
    last_price: float | None = None,
    symbol: str = "",
) -> str | None:
    """Skip symbols too illiquid to trade cleanly (data quality, public-only)."""
    if quote_volume is not None:
        try:
            qv = float(quote_volume)
            if qv < _MIN_QUOTE_VOL_24H:
                return f"liquidity_quote_vol_low:{qv:.0f}"
        except (TypeError, ValueError):
            return "liquidity_quote_vol_invalid"
    if oi is not None and last_price is not None:
        try:
            oi_usd = float(oi) * float(last_price)
            if 0 < oi_usd < _MIN_OI_USD:
                return f"liquidity_oi_low:{oi_usd:.0f}"
        except (TypeError, ValueError):
            return "liquidity_oi_invalid"
    return None


def effective_min_rr_for_delivery(setup: dict[str, Any], *_a: Any, **_k: Any) -> float:
    """Fixed R:R floor; structural geometry from levels.py already enforces R:R."""
    return _MIN_RR_FLOOR


# --- former filters: no veto beyond the fusion gate -------------------------
def mission_delivery_block(
    *,
    direction: str,
    lifecycle: dict[str, Any] | None = None,
    setup: dict[str, Any] | None = None,
    symbol: str = "",
    row: dict[str, Any] | None = None,
    **_k: Any,
) -> GateResult | None:
    """No veto beyond the setup's own confirmation.

    The legacy PRE/MID mission gate (scanner/gate/_mission.py) was deleted:
    the Hunter's persistent state machine (scanner/detect/patterns.py) only
    emits a setup once every stage has confirmed in order, so a separate
    mid-leg-chase veto at registration time is redundant.
    """
    return None


def delivery_freshness_block(*_a: Any, **_k: Any) -> None:
    return None


def delivery_hard_block(*_a: Any, **_k: Any) -> None:
    return None


def run_gate_pipeline(*_a: Any, **_k: Any) -> GateResult:
    return GateResult(ok=True)


def disabled_phase_pairs(*_a: Any, **_k: Any) -> dict[tuple[str, str], Any]:
    return {}


# --- report helpers: surface the fusion gate reason -------------------------
def evaluate_alert_gate(setup: dict[str, Any], **_k: Any) -> GateResult:
    """A confirmed fusion setup is alert-worthy; otherwise blocked by gate_reason."""
    if setup.get("impulse_confirmed") or setup.get("intrabar_confirmed"):
        return GateResult(ok=True)
    return GateResult(ok=False, code=str(setup.get("gate_reason") or "not_confirmed"))


def evaluate_formation(setup: dict[str, Any], **_k: Any) -> GateResult:
    confirmed = bool(setup.get("impulse_confirmed") or setup.get("intrabar_confirmed"))
    if confirmed:
        return GateResult(ok=True, code="confirmed")
    reason = str(setup.get("gate_reason") or "not_confirmed")
    return GateResult(ok=False, code=reason, message=reason)


def collect_report_blockers(setup: dict[str, Any] | None = None, **_k: Any) -> list[GateResult]:
    if isinstance(setup, dict) and not setup.get("impulse_confirmed"):
        reason = str(setup.get("gate_reason") or "not_confirmed")
        return [GateResult(ok=False, code=reason, message=reason)]
    return []


__all__ = [
    "BOUNCE_MIN_RISK_REWARD",
    "GateResult",
    "MID_DUMP_LC_PHASES",
    "REPORT_BLOCK_PRIORITY",
    "collect_report_blockers",
    "delivery_freshness_block",
    "delivery_hard_block",
    "disabled_phase_pairs",
    "effective_min_rr_for_delivery",
    "evaluate_alert_gate",
    "evaluate_formation",
    "liquidity_skip_reason",
    "mission_delivery_block",
    "price_in_entry_zone",
    "run_gate_pipeline",
]
