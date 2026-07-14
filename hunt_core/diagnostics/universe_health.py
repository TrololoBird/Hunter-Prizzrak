"""Universe-level data-plane health — the missing operator signal.

Per-symbol readiness (``data_readiness``) and per-symbol audits already exist, but nothing
aggregated the whole universe per tick. On 2026-07-11 the SOCKS proxy died, EVERY symbol
started failing the 4h-staleness gate (`klines.4h.stale.*`), no signal could form, and the
degradation was SILENT until the event loop hung and the watchdog hard-killed the process
hours later. This module turns that silent mass-failure into a loud, structured signal:

    health = assess_universe_health(rows)
    if health.degraded:
        LOG.warning("hunt_universe_degraded", **health.telemetry())
        # ... and (caller's choice) fire an ops alert

It is a PURE function of the tick rows — no I/O, no exchange calls — so it is trivially
unit-tested and cannot itself hang. Wiring lives in the tick loop (see _cycle_loop.py).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# A row is a data-plane FAILURE when its data could not be assembled/validated — NOT
# merely when it produced no signal (a healthy "neutral" tick is fine). We detect that
# from any of: an explicit data-shaped `error`, non-empty `data_violations`, or
# `data_integrity.complete == False`.
_DATA_ERROR_RE = re.compile(r"^(klines?|book|ticker|funding|oi|data)\b|stale|fetch_failed|staleness")

# Normalise a violation string to a stable KIND for bucketing:
#   "klines.4h.stale.XMRUSDT.40336224ms>36000000ms" -> "klines.4h.stale"
#   "klines.1m.rows=1<min_raw=300"                   -> "klines.1m.rows"
#   "klines.1m.fetch_failed"                          -> "klines.1m.fetch_failed"
_NUM_TAIL_RE = re.compile(r"[.=<>].*$")


def _violation_kind(v: str) -> str:
    v = str(v)
    # klines.<tf>.<kind>[.symbol.numbers] -> keep the first 3 dotted segments, strip numbers
    parts = v.split(".")
    if len(parts) >= 3 and parts[0].startswith("kline"):
        kind = parts[2]
        kind = _NUM_TAIL_RE.sub("", kind)  # rows=1<... -> rows
        return f"{parts[0]}.{parts[1]}.{kind}"
    # generic: keep up to the first numeric/operator token
    return _NUM_TAIL_RE.sub("", v.split(" ")[0])


def classify_row_health(row: Mapping[str, Any]) -> str | None:
    """Return a normalised failure KIND if this row is a data-plane failure, else None.

    Robust to both row shapes (processed candidate rows vs rejected rows) and to missing
    keys — anything it cannot positively classify as a failure is treated as healthy."""
    if not isinstance(row, Mapping):
        return None
    violations = row.get("data_violations")
    if isinstance(violations, (list, tuple)) and violations:
        return _violation_kind(violations[0])
    integ = row.get("data_integrity")
    if isinstance(integ, Mapping) and integ.get("complete") is False:
        vs = integ.get("violations")
        if isinstance(vs, (list, tuple)) and vs:
            return _violation_kind(vs[0])
        return "data.incomplete"
    err = row.get("error")
    if isinstance(err, str) and err and _DATA_ERROR_RE.search(err):
        return _violation_kind(err)
    return None


@dataclass(frozen=True)
class UniverseHealth:
    total: int
    failures: int
    kinds: Counter = field(default_factory=Counter)
    degraded: bool = False
    critical: bool = False
    threshold: float = 0.5
    critical_threshold: float = 0.9

    @property
    def failure_frac(self) -> float:
        return (self.failures / self.total) if self.total else 0.0

    @property
    def dominant_kind(self) -> str | None:
        return self.kinds.most_common(1)[0][0] if self.kinds else None

    def telemetry(self) -> dict[str, Any]:
        return {
            "universe": self.total,
            "failures": self.failures,
            "failure_pct": round(self.failure_frac * 100.0, 1),
            "dominant_kind": self.dominant_kind,
            "top_kinds": dict(self.kinds.most_common(5)),
            "degraded": self.degraded,
            "critical": self.critical,
        }

    def summary(self) -> str:
        if not self.degraded:
            return f"universe OK ({self.total - self.failures}/{self.total} healthy)"
        sev = "CRITICAL" if self.critical else "DEGRADED"
        return (
            f"universe {sev}: {self.failures}/{self.total} "
            f"({self.failure_frac * 100:.0f}%) failing data — "
            f"dominant: {self.dominant_kind}"
        )


def assess_universe_health(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    threshold: float = 0.5,
    critical_threshold: float = 0.9,
    min_universe: int = 5,
) -> UniverseHealth:
    """Aggregate per-tick data-plane health across the whole universe.

    ``degraded`` when ≥ ``threshold`` of a non-trivial universe (≥ ``min_universe``) failed
    data assembly this tick; ``critical`` at ≥ ``critical_threshold`` (near-total blackout —
    the proxy-death signature). Below ``min_universe`` symbols the fraction is too noisy to
    act on, so it never flags (avoids false alarms on a tiny pinned-only tick)."""
    rows = rows or []
    total = len(rows)
    kinds: Counter = Counter()
    for r in rows:
        kind = classify_row_health(r)
        if kind is not None:
            kinds[kind] += 1
    failures = sum(kinds.values())
    frac = (failures / total) if total else 0.0
    big_enough = total >= min_universe
    degraded = big_enough and frac >= threshold
    critical = big_enough and frac >= critical_threshold
    return UniverseHealth(
        total=total, failures=failures, kinds=kinds,
        degraded=degraded, critical=critical,
        threshold=threshold, critical_threshold=critical_threshold,
    )


def should_self_restart_on_blackout(
    *,
    critical: bool,
    degraded_streak: int,
    supervised: bool,
    is_ban: bool,
    streak_threshold: int,
) -> bool:
    """Whether a sustained critical data blackout warrants a supervised self-restart.

    A data blackout (bot ticking, but the data universe-wide stale) does NOT trip the
    progress watchdog — the alert fires but nothing recovers, so the bot can sit blind
    until an operator notices (2026-07-13 incident: a stalled 15m WS mux froze the whole
    universe ~2h while 1m/5m kept flowing). When the blackout is critical and persists,
    exit for a clean supervised respawn (cheap now — HTF frames are persisted/reloaded,
    so no warmup blackout).

    Guards: only when SUPERVISED (else exit = a dead bot), and NOT on an IP ban (a ban
    self-heals when it lifts; restarting just re-hits the same banned IP and thrashes).
    Pure predicate — the os._exit lives in the tick loop.
    """
    if not supervised or is_ban:
        return False
    return bool(critical) and degraded_streak >= streak_threshold


__all__ = [
    "assess_universe_health",
    "classify_row_health",
    "should_self_restart_on_blackout",
    "UniverseHealth",
]
