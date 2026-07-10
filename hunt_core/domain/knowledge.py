"""Epistemological domain types — Hunter Next Generation.

Levels of knowledge (see docs/EPISTEMOLOGY.md):
    Observation → Evidence → Belief → Hypothesis → FalsificationCriteria
    → TradeAssessment → TradePlan

These replace the untyped setup dict and consolidate multiple competing
FSMs into a single domain model with clear ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Level 1: Observation — raw verifiable fact
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Observation:
    """Single market data point — price, volume, OI, funding, etc."""
    symbol: str
    timestamp_ms: int
    source: str  # e.g. "ccxt_ws", "ccxt_rest", "polars_feature"
    kind: str  # e.g. "price", "oi", "funding", "liquidation", "volume"
    value: float
    timeframe: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 2: Evidence — observation + context + minimal interpretation
# ---------------------------------------------------------------------------

EvidenceKind = Literal[
    "liquidity_sweep",
    "oi_expansion",
    "oi_contraction",
    "funding_flush",
    "funding_divergence",
    "choch",
    "bos",
    "delta_absorption",
    "volume_spike",
    "compression",
    "expansion_start",
    "failed_breakout",
    "failed_breakdown",
    "poc_reclaim",
    "poc_loss",
    "vwap_reclaim",
    "level_test",
    "whale_activity",
    "supply_exhaustion",
    "demand_exhaustion",
    "liquidation_cascade",
    "market_maker_trap",
    "trend_maturity",
    "structure_break",
    "other",
]


@dataclass(frozen=True, slots=True)
class Evidence:
    """Interpreted observation — fact + context, no trading decision."""
    kind: EvidenceKind
    description: str
    confidence: float  # 0..1 — how certain we are this event occurred
    source_observations: tuple[str, ...] = ()  # references to supporting data
    timeframe: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 3: Belief — most non-contradictory model explaining evidence
# ---------------------------------------------------------------------------

MarketModelKind = Literal[
    "accumulation",
    "distribution",
    "trend_continuation_up",
    "trend_continuation_down",
    "compression",
    "expansion",
    "mean_reversion",
    "short_covering",
    "long_unwinding",
    "failed_auction",
    "liquidation_cascade",
    "range_bound",
    "breakout",
    "breakdown",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class CompetingModel:
    """One of potentially several market models explaining observations."""
    kind: MarketModelKind
    explanatory_power: float  # 0..1 — how well this model fits evidence
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Belief:
    """Current best model of market state — NOT a fact about the market.

    Hunter never says "market is in accumulation".
    Hunter says "observations best explained by accumulation model".
    """
    primary_model: CompetingModel
    alternatives: tuple[CompetingModel, ...] = ()
    model_spread: float = 0.0  # gap between primary and best alternative
    ambiguous: bool = False  # True when alternatives are too close to call


# ---------------------------------------------------------------------------
# Level 4: Hypothesis — testable prediction from Belief
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Hypothesis:
    """Testable prediction born from Belief — a scientific experiment on the model.

    Example: "If accumulation is correct, after return to POC we should see
    absorption + OI growth. If this doesn't happen, model is rejected."
    """
    direction: Literal["long", "short"]
    thesis: str  # human-readable description
    expected_observations: tuple[str, ...] = ()  # what we should see if correct
    belief_basis: str = ""  # which Belief supports this


# ---------------------------------------------------------------------------
# Level 5: Falsification Criteria
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FalsificationCriteria:
    """What observations would disprove our market model.

    SL is a CONSEQUENCE of these criteria, not the other way around.
    Falsification can happen before SL (model disproved, exit early).
    SL can trigger without falsification (false breakout, model still valid).
    """
    conditions: tuple[str, ...]  # e.g. ("close below VAL", "OI growth + no absorption")
    invalidation_price: float  # price level where model is most likely wrong
    invalidation_reason: str  # human-readable reason
    structural_anchor: str = ""  # which structural level anchors this


# ---------------------------------------------------------------------------
# Level 6: Trade Assessment
# ---------------------------------------------------------------------------

TradeAssessmentVerdict = Literal["tradeable", "not_tradeable"]


@dataclass(frozen=True, slots=True)
class TradeAssessment:
    """Judgment: is this situation tradeable?

    Can conclude "valid Hypothesis but not tradeable" — this is a
    full-fledged result, not a failure.
    """
    verdict: TradeAssessmentVerdict
    reason: str
    rr_sufficient: bool = False
    falsification_definable: bool = False
    lead_time_positive: bool = False
    gates_failed: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Level 7: Scenario — the live domain object
# ---------------------------------------------------------------------------

ScenarioLifecycleState = Literal[
    "forming",  # hypothesis exists, plan being built
    "armed",    # plan ready, waiting for entry zone
    "active",   # price in entry zone, plan executable
    "tracking", # notification sent, monitoring outcome
    "terminal", # outcome recorded (TP/SL/Expiry/Falsified/Cancelled)
]


@dataclass(slots=True)
class Scenario:
    """Live domain object — the central entity of Hunter.

    Unlike ScenarioVerdict (frozen snapshot), Scenario is mutable:
    it can change lifecycle state, update probability, revise targets.

    Scenario is NOT a Signal. Signal is transport.
    Scenario is NOT a ScenarioVerdict. ScenarioVerdict is a snapshot.
    """
    symbol: str
    direction: Literal["long", "short"]
    hypothesis: Hypothesis
    falsification: FalsificationCriteria
    assessment: TradeAssessment
    lifecycle: ScenarioLifecycleState = "forming"

    # Trade plan fields (present only when assessment.verdict == "tradeable")
    entry_zone: tuple[float, float] | None = None
    stop_loss: float | None = None  # derived from falsification.invalidation_price
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    entry_type: str = ""  # "market", "pullback_limit", "breakout"
    rr_primary: float = 0.0

    # Provenance — full justification chain
    belief: Belief | None = None
    evidence_chain: tuple[Evidence, ...] = ()
    created_at: str = ""
    setup_id: str = ""

    # Outcome (filled when lifecycle reaches terminal)
    outcome: str = ""  # "tp1", "tp2", "tp3", "sl", "expiry", "falsified", "cancelled"
    outcome_reason: str = ""

    def has_trade_plan(self) -> bool:
        return (
            self.assessment.verdict == "tradeable"
            and self.entry_zone is not None
            and self.stop_loss is not None
            and self.take_profit_1 is not None
        )

    def is_actionable(self) -> bool:
        return self.lifecycle in ("armed", "active") and self.has_trade_plan()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "lifecycle": self.lifecycle,
            "thesis": self.hypothesis.thesis,
            "falsification_reason": self.falsification.invalidation_reason,
            "falsification_price": self.falsification.invalidation_price,
            "assessment_verdict": self.assessment.verdict,
            "assessment_reason": self.assessment.reason,
            "entry_zone": list(self.entry_zone) if self.entry_zone else None,
            "stop_loss": self.stop_loss,
            "tp1": self.take_profit_1,
            "tp2": self.take_profit_2,
            "tp3": self.take_profit_3,
            "entry_type": self.entry_type,
            "rr_primary": self.rr_primary,
            "setup_id": self.setup_id,
            "created_at": self.created_at,
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "belief_model": self.belief.primary_model.kind if self.belief else "",
            "belief_ambiguous": self.belief.ambiguous if self.belief else False,
            "evidence_count": len(self.evidence_chain),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Scenario | None:
        """Extract Scenario from a tick row (created by serialize.py bridge)."""
        sc = row.get("scenario")
        return sc if isinstance(sc, cls) else None


# ---------------------------------------------------------------------------
# Execution Decision — infrastructure, not domain
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """Should we notify the user? Infrastructure object, not domain."""
    approved: bool
    reason: str
    cooldown_active: bool = False
    risk_budget_exceeded: bool = False


# ---------------------------------------------------------------------------
# Outcome — Learning World feedback
# ---------------------------------------------------------------------------

OutcomeKind = Literal[
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
    "sl_hit",
    "falsified",  # model disproved before SL
    "expired",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class Outcome:
    """Final result — records what happened after notification."""
    kind: OutcomeKind
    reason: str
    pnl_pct: float = 0.0
    duration_minutes: float = 0.0
    falsification_triggered: bool = False  # True if model was disproved


# ---------------------------------------------------------------------------
# Scanner domain types: Event and Opportunity (Law 13)
# ---------------------------------------------------------------------------

EventKind = Literal[
    "expansion_started",
    "compression_detected",
    "liq_vacuum",
    "failed_breakout",
    "failed_breakdown",
    "oi_divergence",
    "funding_flush",
    "volume_anomaly",
    "whale_activity",
    "liquidation_cascade",
    "structure_break",
    "accumulation_signal",
    "distribution_signal",
    "range_expansion",
    "delta_absorption",
    "other",
]

UrgencyLevel = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """Scanner's primary output — a detected market event, NOT a direction.

    Per Constitution Law 13: Scanner finds events, not long/short.
    Direction is Deep's conclusion from Event + full analysis.
    """
    kind: EventKind
    symbol: str
    timestamp_ms: int
    description: str
    confidence: float  # 0..1
    urgency: UrgencyLevel = "medium"
    lead_time_estimate_min: float = 0.0
    supporting_evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Opportunity:
    """What a MarketEvent means for a trader — actionability assessment.

    Event ↔ Opportunity is many-to-many:
    - One event can create multiple opportunities
    - One opportunity can be supported by multiple events
    """
    symbol: str
    event_kinds: tuple[EventKind, ...]
    lead_time_min: float
    urgency: UrgencyLevel
    confidence: float  # 0..1
    description: str
    requires_deep_analysis: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "Belief",
    "CompetingModel",
    "EventKind",
    "Evidence",
    "EvidenceKind",
    "ExecutionDecision",
    "FalsificationCriteria",
    "Hypothesis",
    "MarketEvent",
    "MarketModelKind",
    "Observation",
    "Opportunity",
    "Outcome",
    "OutcomeKind",
    "Scenario",
    "ScenarioLifecycleState",
    "TradeAssessment",
    "TradeAssessmentVerdict",
    "UrgencyLevel",
]
