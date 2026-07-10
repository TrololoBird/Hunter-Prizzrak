from __future__ import annotations

import logging
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


_NETWORK_ERROR_NAMES = {
    "aiohttperror",
    "clienterror",
    "clientconnectorerror",
    "clientpayloaderror",
    "socketerror",
    "timeout",
    "timeouterror",
    "connectionerror",
    "oserror",
}

_SCHEMA_ERROR_NAMES = {
    "msgspecerror",
    "validationerror",
    "typeerror",
    "keyerror",
    "attributeerror",
    "columnnotfounderror",  # polars missing column
    "invalidoperationerror",  # polars schema mismatch
    "schemamismatcherror",
}

_DATA_ERROR_NAMES = {
    "indexerror",
    "zerodivisionerror",
}


def classify_runtime_error(exc: BaseException) -> str:
    """Return a coarse runtime error class for live-path telemetry."""
    try:
        import ccxt

        if isinstance(exc, ccxt.DDoSProtection):
            return "ip_ban" if "418" in str(exc) else "rate_limit"
        if isinstance(exc, (ccxt.RateLimitExceeded,)):
            return "rate_limit"
        if isinstance(exc, ccxt.ExchangeNotAvailable):
            text = str(exc).lower()
            if "418" in text or "ban" in text:
                return "ip_ban"
            if "429" in text or "rate limit" in text:
                return "rate_limit"
            return "network"
        if isinstance(exc, ccxt.NetworkError):
            return "network"
    except Exception:
        logging.getLogger(__name__).exception("ccxt error classification failed")
    name = exc.__class__.__name__.lower()

    if isinstance(exc, asyncio.TimeoutError) or name in _NETWORK_ERROR_NAMES:
        return "network"
    if name in _SCHEMA_ERROR_NAMES:
        return "schema"
    if name in _DATA_ERROR_NAMES:
        return "data"
    return "bug"


def build_runtime_error_payload(
    *,
    component: str,
    exc: BaseException,
    setup_id: str | None = None,
    symbol: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "component": component,
        "error_class": classify_runtime_error(exc),
        "exception_type": exc.__class__.__name__,
        "error": str(exc),
    }
    if setup_id:
        payload["setup_id"] = setup_id
    if symbol:
        payload["symbol"] = symbol
    if extra:
        payload.update(extra)
    return payload


class SignalDataMissing(Exception):
    """Required signal-path field absent or non-finite."""

    def __init__(self, field: str, *, detail: str = "") -> None:
        self.field = field
        self.detail = detail
        msg = f"signal_data_missing:{field}"
        if detail:
            msg = f"{msg}:{detail}"
        super().__init__(msg)


def require_finite_float(value: Any, field: str) -> float:
    """Coerce *value* to a finite float, raising ``SignalDataMissing`` on failure.

    Raises with ``detail="not_numeric"`` when the value cannot be parsed and
    ``detail="non_finite"`` for NaN/inf.
    """
    if value is None:
        raise SignalDataMissing(field)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise SignalDataMissing(field, detail="not_numeric") from exc
    if not math.isfinite(numeric):
        raise SignalDataMissing(field, detail="non_finite")
    return numeric


def optional_finite_float(value: Any) -> float | None:
    """Return a finite float or ``None`` (alias of :func:`finite_float_or_none`)."""
    return finite_float_or_none(value)


def require_mark_price(
    price: Any,
    market: dict[str, Any] | None,
    *,
    field: str = "price",
) -> float:
    if isinstance(price, bool):
        raise SignalDataMissing(field, detail="not_numeric")
    mkt = market or {}
    for mark_key in ("mark_price", "markPrice", "live_mark_price"):
        candidate = mkt.get(mark_key)
        if isinstance(candidate, bool):
            continue
        val = optional_finite_float(candidate)
        if val is not None and val > 0:
            return val
    candidate = mkt.get("last_price")
    if not isinstance(candidate, bool):
        val = optional_finite_float(candidate)
        if val is not None and val > 0:
            return val
    if not isinstance(price, bool):
        val = optional_finite_float(price)
        if val is not None and val > 0:
            return val
    raise SignalDataMissing(field)


def require_level(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise SignalDataMissing(field, detail="not_numeric")
    val = optional_finite_float(value)
    if val is None or val <= 0:
        raise SignalDataMissing(field)
    return val


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


def as_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def row_float(row: object, key: str, default: float = 0.0) -> float:
    if not isinstance(row, dict):
        return default
    return as_float(row.get(key), default=default)


__all__ = [
    "DEFENSIVE_EXC",
    "CircuitBreaker",
    "CircuitState",
    "DeterminismHash",
    "SignalDataMissing",
    "TradingCircuitBreakers",
    "as_float",
    "as_int",
    "build_runtime_error_payload",
    "classify_runtime_error",
    "defensive_exc_types",
    "finite_float_or_none",
    "optional_finite_float",
    "require_finite_float",
    "require_level",
    "require_mark_price",
    "row_float",
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

    _LOG: logging.Logger = field(init=False)

    def __post_init__(self) -> None:
        self._LOG = logging.getLogger(f"circuit.{self.name}")

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
            self._LOG.warning("OPEN (%d failures)", self.failures)

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


# ── Determinism hash (tick-level digest) ──────────────────────────────────────


@dataclass
class DeterminismHash:
    """Accumulate a determinstic hex digest of the tick's key decision fields.

    Use to compare two runs: if both produce the same hexdigest at the same
    tick, they made the same decisions regardless of wall-clock timing quirks.

    Not a replay verifier — just a fast smoke-test for unintended divergence.
    """
    _parts: list[str] = field(default_factory=list)

    def update(self, *values: str | int | float | None) -> None:
        clean = "|".join(
            str(v) if v is not None else ""
            for v in values
        )
        self._parts.append(clean)

    def hexdigest(self) -> str:
        import hashlib
        raw = "||".join(self._parts)
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()

    def reset(self) -> None:
        self._parts.clear()
