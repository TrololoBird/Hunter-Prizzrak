"""Opportunity — quality assessment of a potential signal, before delivery.

``Opportunity`` separates **signal strength** (how strong is the move) from
**opportunity quality** (is this worth a human's attention right now).

Architecture::

    Scorers → Composite → OpportunityFilter → Signal Lifecycle → Telegram
                              ↑
                         Opportunity instantiated here

``OpportunityFilter`` computes this for every Detection that survives the
factor pipeline.  Low-opportunity signals are suppressed **before** entering
the full gate pipeline — saving CPU and reducing false-positive Telegram noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


# ── Dynamic TTL by factor type (seconds) ──────────────────────────────────────
# How fast does each signal source decay?  Book imbalance is seconds,
# funding is minutes.
FACTOR_TTL_MAP: dict[str, int] = {
    "book": 30,
    "flow": 45,
    "structure": 90,
    "funding": 300,
    "oi_pressure": 120,
    "volume_anomaly": 60,
    "compression": 120,
    "oi_acceleration": 90,
    "funding_velocity": 180,
    "poc_migration": 120,
    "va_contraction": 120,
    "liquidity_void_path": 60,
    "market_maker_trap": 45,
    "whale_activity": 60,
}
_DEFAULT_TTL_S = 120


def resolve_ttl(active_factors: tuple[str, ...]) -> int:
    """Pick the shortest TTL among active factors — signal decays at the fastest clock."""
    if not active_factors:
        return _DEFAULT_TTL_S
    return min(FACTOR_TTL_MAP.get(f, _DEFAULT_TTL_S) for f in active_factors)


@dataclass(frozen=True, slots=True)
class Opportunity:
    """Quality assessment of a signal opportunity.

    Separates "how strong is this setup" (score) from
    "is it worth alerting a human right now" (opportunity).

    Fields are filled by ``OpportunityFilter.evaluate()``.
    """

    symbol: str
    direction: str

    # ── Core dimensions ─────────────────────────────────────────────────────
    confidence: float       # P(win) — calibrated probability in [0, 1]
    evidence: float         # independent confirmation count (normalised) in [0, 1]
    score: float            # raw fusion magnitude in [0, 1] (strength, not probability)
    opportunity: float      # combined quality in [0, 1] — the single decision metric

    # ── Why this opportunity exists ──────────────────────────────────────────
    reasons: tuple[str, ...] = field(default_factory=tuple)
    confluence_count: int = 0

    # ── Timing ──────────────────────────────────────────────────────────────
    generated_at: str = ""
    ttl_seconds: int = _DEFAULT_TTL_S    # dynamic: shortest decay among active factors
    expires_at: str = ""

    # ── Market context snapshot ──────────────────────────────────────────────
    spread_bps: float | None = None
    volume_24h_usd: float | None = None
    oi_usd: float | None = None
    funding_rate: float | None = None

    # ── Factor breakdown ─────────────────────────────────────────────────────
    active_factors: tuple[str, ...] = field(default_factory=tuple)
    factor_scores: dict[str, float] = field(default_factory=dict)
    factor_details: dict[str, str] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return datetime.now(UTC) >= expiry
        except (TypeError, ValueError):
            return False

    @property
    def should_deliver(self) -> bool:
        """True when this opportunity passes the quality bar for human delivery."""
        return self.opportunity >= 0.70 and not self.expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "score": self.score,
            "opportunity": self.opportunity,
            "reasons": list(self.reasons),
            "confluence_count": self.confluence_count,
            "generated_at": self.generated_at,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "spread_bps": self.spread_bps,
            "volume_24h_usd": self.volume_24h_usd,
            "oi_usd": self.oi_usd,
            "funding_rate": self.funding_rate,
            "active_factors": list(self.active_factors),
            "factor_scores": dict(self.factor_scores),
            "factor_details": dict(self.factor_details),
        }


__all__ = ["Opportunity"]
