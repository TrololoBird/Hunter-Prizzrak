"""Trade-plan datatypes — surviving fragment of the deleted Verdict V2 (ScenarioVerdict)
dataclass family.

The entire ``ScenarioVerdict`` tree (EngineOutput, HorizonForecast, HorizonTopology,
DisagreementState, MarketDriver, PatternCandidate/Confidence, ExpectedPath,
ScenarioCatalyst, ScenarioFragility, SignalStrength, TradeQuality, DataQualityReport,
MaturityFeatures, SignalDecision, ScenarioVerdict itself) was removed: it was never
instantiated anywhere once ``deep/engines/orchestrator.py`` (L0-L5) was deleted —
confirmed by grep before removal, not assumed. Only ``TradePlan``/``PlanLifecycle``/
``EntryType`` survive because ``activation.py::assess_activation`` (called by the
still-alive ``signal_queue.py``) type-hints against them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EntryType = Literal["market", "pullback_limit", "breakout"]
PlanLifecycle = Literal["forming", "armed", "active"]


@dataclass(frozen=True, slots=True)
class TradePlan:
    direction: Literal["long", "short"]
    entry_type: EntryType
    entry_zone: tuple[float, float]
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    take_profit_3: float | None
    rr_tp1: float
    rr_tp2: float | None
    rr_tp3: float | None
    rr_primary: float
    invalidation_reason: str
    level_sources: list[str] = field(default_factory=list)
    entry_reference: float = 0.0
    rr_conservative_tp1: float = 0.0
    rr_conservative_tp2: float | None = None
    rr_conservative_tp3: float | None = None
    rr_base_label: str = "≈R:R (от края зоны)"
    plan_lifecycle: PlanLifecycle = "forming"


__all__ = ["EntryType", "PlanLifecycle", "TradePlan"]
