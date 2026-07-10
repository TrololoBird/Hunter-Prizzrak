from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

ModuleStatus = Literal["PASS", "FAIL", "CAUTION", "UNKNOWN"]
PipelineDirection = Literal["long", "short", "wait"]
GatingResult = Literal["SIGNAL", "REJECT", "CAUTION"]


class MarketRegime(Enum):
    NORMAL = "normal"
    HIGH_VOL = "high_vol"
    CRASH = "crash"
    ALT_SEASON = "alt_season"


@dataclass(frozen=True, slots=True)
class MacroContext:
    btc_above_ema200: bool | None = None
    btc_price: float | None = None
    btc_chg_24h: float | None = None
    btc_atr_pct: float | None = None
    btc_d: float | None = None
    btc_d_change_24h: float | None = None
    total3_cap: float | None = None
    total3_change_24h: float | None = None
    regime: MarketRegime = MarketRegime.NORMAL


@dataclass(frozen=True, slots=True)
class ModuleResult:
    status: ModuleStatus
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    # True when this module's data is a cached fallback served because a live
    # refresh failed/was rate-limited — not a normal "cache still fresh" hit.
    # Degradation must be visible, never silent (see feedback_zero_degradation).
    stale: bool = False


@dataclass(frozen=True, slots=True)
class RiskLevels:
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float | None = None
    tp3: float | None = None
    rr_tp1: float = 0.0
    rr_tp2: float | None = None
    rr_tp3: float | None = None
    atr_pct: float = 0.0
    sizing_modifier: float = 1.0
    ttl_hours: float = 6.0


@dataclass(frozen=True, slots=True)
class FiveModuleResult:
    macro: ModuleResult
    trend: ModuleResult
    structure: ModuleResult
    positioning: ModuleResult
    risk: ModuleResult

    gating: GatingResult
    direction: PipelineDirection
    reason: str
    regime: MarketRegime = MarketRegime.NORMAL
    macro_context: MacroContext | None = None

    risk_levels: RiskLevels | None = None

    # Wall-clock time the full pipeline took (network + compute), and whether
    # any module served a stale/degraded fallback — surfaced so a slow or
    # degraded read is visible in Telegram/logs, not just an internal delay.
    total_latency_s: float | None = None
    data_stale: bool = False

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "gating": self.gating,
            "direction": self.direction,
            "reason": self.reason,
            "regime": self.regime.value,
            "total_latency_s": self.total_latency_s,
            "data_stale": self.data_stale,
            "modules": {
                "macro": {"status": self.macro.status, "reason": self.macro.reason, "stale": self.macro.stale},
                "trend": {"status": self.trend.status, "reason": self.trend.reason, "stale": self.trend.stale},
                "structure": {"status": self.structure.status, "reason": self.structure.reason, "stale": self.structure.stale},
                "positioning": {"status": self.positioning.status, "reason": self.positioning.reason, "stale": self.positioning.stale},
                "risk": {"status": self.risk.status, "reason": self.risk.reason, "stale": self.risk.stale},
            },
        }
