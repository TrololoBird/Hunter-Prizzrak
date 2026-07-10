"""Signal horizon taxonomy — drives TTL and cooldown."""
from __future__ import annotations

from enum import Enum
from typing import Any


class SignalHorizon(str, Enum):
    SCALP = "scalp"
    INTRADAY = "intraday"
    SWING = "swing"
    POSITION = "position"


_HORIZON_TTL_MIN = {
    SignalHorizon.SCALP: 120,
    SignalHorizon.INTRADAY: 360,
    SignalHorizon.SWING: 2880,
    SignalHorizon.POSITION: 10080,
}

_HORIZON_COOLDOWN_MIN = {
    SignalHorizon.SCALP: 45,
    SignalHorizon.INTRADAY: 90,
    SignalHorizon.SWING: 360,
    SignalHorizon.POSITION: 1440,
}


def derive_signal_horizon(
    setup: dict[str, Any] | None,
    *,
    atr_pct: float | None = None,
) -> SignalHorizon:
    setup = setup or {}
    source_tf = str(setup.get("source_tf") or setup.get("trigger_tf") or "15m").lower()
    target_tf = str(setup.get("target_tf") or setup.get("pattern_tf") or source_tf).lower()
    if source_tf in {"1m", "3m", "5m"} and target_tf in {"1m", "5m", "15m"}:
        return SignalHorizon.SCALP
    if source_tf in {"15m", "5m"} and target_tf in {"1h", "4h"}:
        return SignalHorizon.INTRADAY
    if target_tf in {"4h", "1d"}:
        return SignalHorizon.SWING
    if atr_pct is not None and atr_pct >= 8.0:
        return SignalHorizon.INTRADAY
    return SignalHorizon.SWING


def ttl_minutes_for_horizon(horizon: SignalHorizon) -> int:
    return _HORIZON_TTL_MIN.get(horizon, 360)


def cooldown_minutes_for_horizon(horizon: SignalHorizon) -> int:
    return _HORIZON_COOLDOWN_MIN.get(horizon, 45)


__all__ = [
    "SignalHorizon",
    "cooldown_minutes_for_horizon",
    "derive_signal_horizon",
    "ttl_minutes_for_horizon",
]
