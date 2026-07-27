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

# Здесь стояли `_MIN_RR_FLOOR` / `_MIN_QUOTE_VOL_24H` / `_MIN_OI_USD` — сняты вместе с
# единственными читавшими их функциями. Живой порог ликвидности вселенной задаёт
# `[hunter] min_quote_volume_usd` / `min_open_interest_usd` (`scanner/prescan.py`), и это
# ЕДИНСТВЕННЫЙ такой порог; вторая копия здесь молча расходилась бы с ним.


@dataclass(frozen=True)
class GateResult:
    """Delivery decision result (the fusion ``confirmed`` flag is authoritative)."""

    ok: bool
    code: str = ""
    message: str = ""


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


def disabled_phase_pairs(*_a: Any, **_k: Any) -> dict[tuple[str, str], Any]:
    return {}


# --- report helpers: surface the fusion gate reason -------------------------
def evaluate_alert_gate(setup: dict[str, Any], **_k: Any) -> GateResult:
    """A confirmed fusion setup is alert-worthy; otherwise blocked by gate_reason.

    Ключ подтверждения ОДИН — ``impulse_confirmed`` (пишет ``track/tracker.py``). Стоявший рядом
    ``intrabar_confirmed`` не писал никто: это хвост правки, доведённой до конца в
    ``runtime/query_service.py``, но не здесь. Живой эффект — счётчик «n re-alert» в сводке
    ``/signals`` (``runtime/signals_report.py``) решался и решается одним ``impulse_confirmed``.
    """
    if setup.get("impulse_confirmed"):
        return GateResult(ok=True)
    return GateResult(ok=False, code=str(setup.get("gate_reason") or "not_confirmed"))


def collect_report_blockers(setup: dict[str, Any] | None = None, **_k: Any) -> list[GateResult]:
    if isinstance(setup, dict) and not setup.get("impulse_confirmed"):
        reason = str(setup.get("gate_reason") or "not_confirmed")
        return [GateResult(ok=False, code=reason, message=reason)]
    return []


# Снято 2026-07-26 как неисполнявшееся (ни одного вызывающего вне этого файла):
# `liquidity_skip_reason` (продюсер его ключа `liquidity_skip` ушёл с легаси-транспортом),
# `effective_min_rr_for_delivery`, `delivery_freshness_block`, `delivery_hard_block`,
# `run_gate_pipeline`, `evaluate_formation`. `__all__` их держал, и ровно поэтому vulture их не
# считал находкой — проверять надо ДОСТИЖИМОСТЬ, а не наличие экспорта.
__all__ = [
    "BOUNCE_MIN_RISK_REWARD",
    "GateResult",
    "MID_DUMP_LC_PHASES",
    "REPORT_BLOCK_PRIORITY",
    "collect_report_blockers",
    "disabled_phase_pairs",
    "evaluate_alert_gate",
    "mission_delivery_block",
    "price_in_entry_zone",
]
