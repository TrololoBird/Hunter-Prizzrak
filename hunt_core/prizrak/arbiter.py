"""Module 1 Deep arbiter — pinned change + verdict queue cooldowns."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

_DEEP_COOLDOWN: dict[str, datetime] = {}
DEFAULT_STALE_HOURS = 4.0


def _cooldown_hours() -> float:
    raw = os.getenv("HUNT_DEEP_COOLDOWN_HOURS", "").strip()
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return max(0.5, DEFAULT_STALE_HOURS / 8.0)


def deep_cooldown_ok(symbol: str, *, now: datetime | None = None, hours: float | None = None) -> bool:
    now = now or datetime.now(UTC)
    last = _DEEP_COOLDOWN.get(symbol.upper())
    if last is None:
        return True
    window = _cooldown_hours() if hours is None else max(0.05, float(hours))
    return now - last >= timedelta(hours=window)


def mark_deep_sent(symbol: str, *, now: datetime | None = None) -> None:
    _DEEP_COOLDOWN[symbol.upper()] = now or datetime.now(UTC)


def evaluate_deep_delivery(*, symbol: str, verdict: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not deep_cooldown_ok(symbol):
        blockers.append("deep_cooldown")
    action = str(
        verdict.get("action") or verdict.get("decision") or verdict.get("signal_decision") or "wait"
    ).lower()
    if action not in {"long", "short"}:
        blockers.append("decision_wait")
    # This gate previously only checked cooldown + non-wait — nothing stopped a
    # geometrically broken trade (e.g. R:R 0.22, "risking more than it can gain") from
    # shipping to Telegram. `_geometry_from_zone`'s min_rr floor now rejects those at the
    # source, but a low-strength counter-trend-with-slom candidate can still slip through
    # as "poor" quality — course discipline: "не нравится — не торгую".
    if str(verdict.get("trade_quality") or "") == "poor":
        blockers.append("trade_quality_poor")
    return len(blockers) == 0, blockers


__all__ = [
    "DEFAULT_STALE_HOURS",
    "deep_cooldown_ok",
    "evaluate_deep_delivery",
    "mark_deep_sent",
]
