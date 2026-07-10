"""Delivery-side display helpers that survived the legacy-gate removal.

These are the only pieces of the old ``deliver/dispatch.py`` "unified delivery
decision" module that live code still imports — the readiness label chain
(fuel → RU tier line), the invalidation-detail humanizer, and ``SniperConfig``
(a runtime delivery-window config). Everything else in dispatch.py (the
``evaluate_delivery`` gate pipeline, ``deliver/arbiter.py``, and the whole
``scanner/gate/`` stack it pulled in) was legacy Hunter-fusion machinery,
fully superseded by the persistent state machine in
``scanner/detect/patterns.py`` + ``deliver/manipulation_delivery.py``, and was
deleted. This module is what remained genuinely reachable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from hunt_core.deliver.geometry import (
    geometry_block_reason,
    resolve_min_rr,
)

LOG = logging.getLogger(__name__)


# ── SniperConfig — live TG delivery window config ──────────────────────────
@dataclass(frozen=True)
class SniperConfig:
    """Live TG delivery restricted to imminent pre-dump / pre-pump windows.

    Mid-leg ``dump_active`` / ``impulse_initiating`` are monitor-only. Deep
    analysis for pinned or user symbols uses the ``/signal`` query path.
    """

    enabled: bool = True
    live_phases_short: frozenset[str] = frozenset(
        {"exhaustion_at_high", "distribution", "dump_initiating", "pre_dump"}
    )
    live_phases_long: frozenset[str] = frozenset(
        {"accumulation", "breakout_arming", "post_dump_bounce", "recovery", "pre_pump"}
    )
    top_ls_max: float = 2.0
    require_top_ls: bool = True
    chase_tol: float = 0.002

    @property
    def live_phases(self) -> frozenset[str]:
        """Back-compat alias — short pre-dump phases only."""
        return self.live_phases_short

    @classmethod
    def from_env(cls) -> "SniperConfig":
        wide = os.environ.get("HUNT_WIDE_MODE", "0") not in {"0", "false", "False"}
        default_sniper = "0" if wide else "1"
        off = os.environ.get("HUNT_SNIPER_MODE", default_sniper) in {"0", "false", "False"}
        require_ls = os.environ.get("HUNT_SNIPER_REQUIRE_TOP_LS", "1") not in {"0", "false", "False"}
        return cls(
            enabled=not off,
            top_ls_max=float(os.environ.get("HUNT_SNIPER_TOP_LS_MAX", "2.0")),
            require_top_ls=require_ls,
            chase_tol=float(os.environ.get("HUNT_SNIPER_CHASE_TOL", "0.002")),
        )


# ── Invalidation detail humanizer ──────────────────────────────────────────
INVALIDATE_LABELS: dict[str, str] = {
    "stop_hit": "Стоп сработал",
    "target_hit": "Цель достигнута",
    "structure_broken": "Структура сломана",
    "stale": "Сетап устарел",
    "superseded": "Заменён свежим сигналом",
    "manual": "Отменён вручную",
}


def invalidate_detail_human(detail: str, *, reason: str = "") -> str:
    if reason and reason in INVALIDATE_LABELS:
        base = INVALIDATE_LABELS[reason]
        if detail and detail not in base:
            return f"{base} · {detail}"
        return base
    if detail:
        return detail
    return INVALIDATE_LABELS.get(reason, "Сигнал отменён")


# ── Readiness label chain ──────────────────────────────────────────────────
def readiness_score(setup: dict[str, Any], *, direction: str) -> float | None:
    score = setup.get("dump_score" if direction == "short" else "long_score")
    if score is not None:
        try:
            return float(score)
        except (TypeError, ValueError):
            LOG.debug("readiness_score_parse_failed score=%r", score)
    fusion = setup.get("fusion_score")
    if fusion is not None:
        try:
            return float(fusion)
        except (TypeError, ValueError):
            LOG.debug("readiness_score_parse_failed score=%r", fusion)
    return None


def readiness_tier(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 70:
        return "strong"
    if score >= 60:
        return "ready"
    if score >= 45:
        return "forming"
    return "watch"


def display_readiness_score(
    setup: dict[str, Any], *, direction: str, row: dict[str, Any] | None = None,
) -> float | None:
    """Fuel capped for display when geometry is not tradable."""
    min_rr = resolve_min_rr(setup, direction=direction)
    fuel = readiness_score(setup, direction=direction)
    if fuel is None:
        return None
    if geometry_block_reason(setup, min_rr=min_rr, row=row, direction=direction):
        return min(fuel, 59.0)
    return fuel


def readiness_label_ru(score: float | None) -> str:
    """User-facing tier — never say «fuel»."""
    if score is None:
        return "готовность н/д"
    tier = readiness_tier(score)
    s = f"{score:.0f}/100"
    if tier == "strong":
        return f"готовность {s} · сильный сетап"
    if tier == "ready":
        return f"готовность {s} · ждём confirm"
    if tier == "forming":
        return f"готовность {s} · формирование"
    return f"готовность {s} · только наблюдение"


def readiness_label_for_setup(
    setup: dict[str, Any], *, direction: str, row: dict[str, Any] | None = None,
) -> str:
    """Readiness line with optional geometry caveat (fuel vs tradability)."""
    min_rr = resolve_min_rr(setup, direction=direction)
    raw = readiness_score(setup, direction=direction)
    display = display_readiness_score(setup, direction=direction, row=row)
    if display is None:
        return "готовность н/д"
    base = readiness_label_ru(display)
    reason = geometry_block_reason(setup, min_rr=min_rr, row=row, direction=direction)
    if not reason:
        return base
    raw_note = f" (raw {raw:.0f})" if raw is not None and raw > display + 0.5 else ""
    return f"{base}{raw_note} · ⚠️ {reason}"


def readiness_short_ru(score: float | None) -> str:
    return readiness_label_ru(score).split("·", 1)[0].strip()


def readiness_short_for_setup(
    setup: dict[str, Any], *, direction: str, row: dict[str, Any] | None = None,
) -> str:
    return readiness_label_for_setup(setup, direction=direction, row=row).split("·", 1)[0].strip()


def confirm_gap_readiness(score: float | None) -> str:
    """Gap line for confirm checklist."""
    if score is None:
        return "готовность≥60 (score отсутствует)"
    if score >= 60:
        return f"готовность OK ({score:.0f}/100)"
    return f"готовность≥60 (сейчас {score:.0f}/100)"


__all__ = [
    "SniperConfig",
    "INVALIDATE_LABELS",
    "invalidate_detail_human",
    "readiness_score",
    "readiness_tier",
    "display_readiness_score",
    "readiness_label_ru",
    "readiness_label_for_setup",
    "readiness_short_ru",
    "readiness_short_for_setup",
    "confirm_gap_readiness",
]
