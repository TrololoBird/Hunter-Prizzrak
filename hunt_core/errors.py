from __future__ import annotations

import structlog

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Any, Optional

# BLE001-safe tuple for log-and-continue / degrade paths (not CancelledError - BaseException).
DEFENSIVE_EXC: tuple[type[BaseException], ...] = (
    OSError,
    ConnectionError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
    asyncio.TimeoutError,
)


def defensive_exc_types(*extra: type[BaseException]) -> tuple[type[BaseException], ...]:
    """Flatten DEFENSIVE_EXC with extra types for ``except`` clauses (never nest the tuple)."""
    return DEFENSIVE_EXC + extra


def finite_float_or_none(value: object) -> float | None:
    """Return a finite float or None — never substitute 0 for missing market data."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return default
        return numeric if math.isfinite(numeric) else default
    return default


__all__ = [
    "DEFENSIVE_EXC",
    "CircuitBreaker",
    "CircuitState",
    "TradingCircuitBreakers",
    "as_float",
    "defensive_exc_types",
    "finite_float_or_none",
    "system_breakers",
]


# ── Singleton system breakers ────────────────────────────────────────────────

_SYSTEM_BREAKERS: TradingCircuitBreakers | None = None


def system_breakers() -> TradingCircuitBreakers:
    """Lazy singleton — shared across all runtime modules.

    Usage::

        from hunt_core.errors import system_breakers
        if not system_breakers().rest.can_execute():
            return  # defer, REST is down
    """
    global _SYSTEM_BREAKERS
    if _SYSTEM_BREAKERS is None:
        _SYSTEM_BREAKERS = TradingCircuitBreakers()
    return _SYSTEM_BREAKERS


# ── Circuit Breaker ──────────────────────────────────────────────────────────


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


@dataclass
class CircuitBreaker:
    """Auto-disable a subsystem after repeated failures.

    State machine: CLOSED → OPEN (on threshold) → HALF_OPEN (after timeout) → CLOSED (on success).
    """
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max: int = 3

    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    failures: int = field(default=0, init=False)
    last_failure_time: Optional[datetime] = field(default=None, init=False)
    half_open_attempts: int = field(default=0, init=False)

    _LOG: Any = field(init=False)

    def __post_init__(self) -> None:
        self._LOG = structlog.get_logger(f"circuit.{self.name}")

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_attempts += 1
            if self.half_open_attempts >= self.half_open_max:
                self.state = CircuitState.CLOSED
                self.failures = 0
                self.half_open_attempts = 0
                self._LOG.info("CLOSED")
        elif self.state == CircuitState.CLOSED:
            self.failures = max(0, self.failures - 1)

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_time = datetime.now(UTC)
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self._LOG.warning("OPEN (half-open failed)")
        elif self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self._LOG.warning("OPEN", failures=self.failures)

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if (
                self.last_failure_time
                and (datetime.now(UTC) - self.last_failure_time).total_seconds()
                > self.recovery_timeout
            ):
                self.state = CircuitState.HALF_OPEN
                self.half_open_attempts = 0
                self._LOG.info("HALF_OPEN")
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            return self.half_open_attempts < self.half_open_max
        return False

    def reset(self) -> None:
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.last_failure_time = None
        self.half_open_attempts = 0
        self._LOG.info("RESET")


@dataclass
class TradingCircuitBreakers:
    """Aggregate breakers for WS, REST, execution, and book-staleness."""
    ws: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("websocket", failure_threshold=3, recovery_timeout=30))
    rest: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("rest_api", failure_threshold=5, recovery_timeout=60))
    execution: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("execution", failure_threshold=2, recovery_timeout=120))
    book_stale: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("book_stale", failure_threshold=10, recovery_timeout=300))

    def can_trade(self) -> tuple[bool, list[str]]:
        issues: list[str] = []
        for cb in (self.ws, self.rest, self.execution, self.book_stale):
            if not cb.can_execute():
                issues.append(f"{cb.name}: {cb.state.name}")
        return len(issues) == 0, issues

    def all_closed(self) -> bool:
        return all(cb.state == CircuitState.CLOSED for cb in (self.ws, self.rest, self.execution, self.book_stale))
