"""Signal object — notification intent carrier for Deep + Scanner.

Architecturally, Signal is TRANSPORT, not domain.  The domain object is
Scenario (see hunt_core.domain.knowledge).  Signal carries the Scenario's
trade plan to the user via Telegram.

SignalState tracks the notification lifecycle, not the market model lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SignalModule = Literal[1, 2]
SignalState = Literal["forming", "signal", "activated", "tracking", "closed"]


@dataclass(slots=True)
class Signal:
    """Notification intent — carries a Scenario's trade plan to the user.

    This is NOT a domain object.  Domain logic lives in Scenario
    (hunt_core.domain.knowledge).  Signal is the transport envelope.

    ``ttl_seconds`` and ``expires_at`` bound the human opportunity window —
    a signal is stale if its opportunity has passed even if its lifecycle
    state is still "signal"/"activated".
    """
    symbol: str
    module: SignalModule
    direction: str
    setup_id: str
    thesis: str
    plan: dict[str, Any]
    state: SignalState
    created_at: str
    activated_at: str = ""
    as_of: str = ""
    ttl_seconds: int = 120
    expires_at: str = ""
    explanation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            from datetime import UTC, datetime
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return datetime.now(UTC) >= expiry
        except (TypeError, ValueError):
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "module": self.module,
            "direction": self.direction,
            "setup_id": self.setup_id,
            "thesis": self.thesis,
            "plan": self.plan,
            "state": self.state,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "as_of": self.as_of,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "explanation": self.explanation,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Signal:
        return cls(
            symbol=str(raw.get("symbol") or "").upper(),
            module=int(raw.get("module") or 1),  # type: ignore[arg-type]
            direction=str(raw.get("direction") or "").lower(),
            setup_id=str(raw.get("setup_id") or ""),
            thesis=str(raw.get("thesis") or ""),
            plan=dict(raw.get("plan") or {}),
            state=str(raw.get("state") or "forming"),  # type: ignore[arg-type]
            created_at=str(raw.get("created_at") or ""),
            activated_at=str(raw.get("activated_at") or ""),
            as_of=str(raw.get("as_of") or ""),
            ttl_seconds=int(raw.get("ttl_seconds") or 120),
            expires_at=str(raw.get("expires_at") or ""),
            explanation=dict(raw.get("explanation") or {}),
            provenance=dict(raw.get("provenance") or {}),
        )
