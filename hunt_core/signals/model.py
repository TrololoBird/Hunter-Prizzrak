"""Signal object — notification intent carrier for Deep + Scanner.

Architecturally, Signal is TRANSPORT, not domain.  The domain object is
Scenario (see hunt_core.domain.knowledge).  Signal carries the Scenario's
trade plan to the user via Telegram.

SignalState tracks the notification lifecycle, not the market model lifecycle.

NB (audit R2, chunk 7): the TTL / "human opportunity window" machinery
(ttl_seconds / expires_at / expired) and the to_dict/from_dict serialization
that used to live here were never consumed by any live code path — no gate
ever checked expiry, and Signal is never serialized or reconstructed. They
were deleted as dead code; enforcing a TTL gate later is an emission change
and goes through the backtest gate.
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
    """
    symbol: str
    module: SignalModule
    direction: str
    setup_id: str
    thesis: str
    plan: dict[str, Any]
    state: SignalState
    provenance: dict[str, Any] = field(default_factory=dict)
