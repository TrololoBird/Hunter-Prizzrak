"""Exchange-anchored wall clock.

The local OS clock cannot be trusted for comparisons against exchange
timestamps: sandbox clocks drift (observed 14h behind real UTC), which makes a
freshly-fetched kline look "incomplete/in the future" and silently discards
most of the history. We sync a constant offset from the exchange server time
once the market plane is up and expose a corrected ``now``.

Relative durations (cooldowns, signal ages, staleness) are unaffected by a
constant offset and keep working whether or not the offset is set — only
*cross-clock* comparisons (local now vs exchange bar timestamps, REST fetch
windows) require the correction, which is why those paths use this module.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

_offset_ms: float = 0.0
_synced: bool = False


def set_offset_ms(offset_ms: float) -> None:
    """Record ``exchange_server_ms - local_ms`` (call after a server-time fetch)."""
    global _offset_ms, _synced
    _offset_ms = float(offset_ms)
    _synced = True


def offset_ms() -> float:
    return _offset_ms


def is_synced() -> bool:
    return _synced


def now_ms() -> float:
    """Exchange-corrected epoch milliseconds."""
    return time.time() * 1000.0 + _offset_ms


def now_utc() -> datetime:
    """Exchange-corrected timezone-aware UTC ``datetime``."""
    return datetime.now(UTC) + timedelta(milliseconds=_offset_ms)


__all__ = [
    "set_offset_ms",
    "offset_ms",
    "is_synced",
    "now_ms",
    "now_utc",
]
