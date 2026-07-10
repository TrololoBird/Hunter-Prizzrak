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

This module also provides a Clock ABC (LiveClock/ReplayClock/SimulatedClock)
for testability and replay determinism.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Optional

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


# --- Clock ABC hierarchy ---


class Clock(ABC):
    """Abstract clock — execution code never calls datetime.utcnow() directly."""

    @abstractmethod
    def now(self) -> datetime: ...

    @abstractmethod
    def now_ms(self) -> int: ...

    @abstractmethod
    async def sleep(self, seconds: float) -> None: ...

    @abstractmethod
    def is_replay(self) -> bool: ...


class LiveClock(Clock):
    """Live wall clock using exchange-corrected time from this module."""

    def now(self) -> datetime:
        return now_utc()

    def now_ms(self) -> int:
        return int(now_ms())

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def is_replay(self) -> bool:
        return False


class ReplayClock(Clock):
    """Time advances by event order, not wall clock. Supports speed scaling."""

    def __init__(self, start: Optional[datetime] = None, speed: float = 1.0) -> None:
        self._current = start or datetime(2024, 1, 1, tzinfo=UTC)
        self._speed = speed
        self._event_count = 0

    def now(self) -> datetime:
        return self._current

    def now_ms(self) -> int:
        return int(self._current.timestamp() * 1000)

    def advance(self, event_time: datetime) -> None:
        self._current = event_time
        self._event_count += 1

    async def sleep(self, seconds: float) -> None:
        if self._speed == float("inf"):
            return
        await asyncio.sleep(seconds / self._speed)

    def is_replay(self) -> bool:
        return True

    @property
    def event_count(self) -> int:
        return self._event_count


class SimulatedClock(Clock):
    """For tests — manual time control via tick()."""

    def __init__(self, start: Optional[datetime] = None) -> None:
        self._current = start or datetime(2024, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def now_ms(self) -> int:
        return int(self._current.timestamp() * 1000)

    def tick(self, ms: int) -> None:
        self._current += timedelta(milliseconds=ms)

    def advance_to(self, dt: datetime) -> None:
        self._current = dt

    async def sleep(self, seconds: float) -> None:
        self.tick(int(seconds * 1000))

    def is_replay(self) -> bool:
        return True


__all__ = [
    "set_offset_ms",
    "offset_ms",
    "is_synced",
    "now_ms",
    "now_utc",
    "Clock",
    "LiveClock",
    "ReplayClock",
    "SimulatedClock",
]
