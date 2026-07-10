from __future__ import annotations

from hunt_core.errors import (
    CircuitBreaker,
    CircuitState,
    TradingCircuitBreakers,
    system_breakers,
)


def test_circuit_breaker_opens_after_failure_threshold():
    cb = CircuitBreaker(name="ws", failure_threshold=2, recovery_timeout=0.0, half_open_max=1)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert cb.failures == 1
    assert cb.can_execute() is True

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.failures == 2


def test_circuit_breaker_open_blocks_execution_before_timeout():
    cb = CircuitBreaker(name="ws", failure_threshold=2, recovery_timeout=60, half_open_max=1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False


def test_circuit_breaker_transitions_to_half_open_after_timeout():
    cb = CircuitBreaker(name="ws", failure_threshold=2, recovery_timeout=0.0, half_open_max=1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    assert cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN


def test_circuit_breaker_closes_after_half_open_success():
    cb = CircuitBreaker(name="ws", failure_threshold=2, recovery_timeout=0.0, half_open_max=1)
    cb.record_failure()
    cb.record_failure()
    cb.can_execute()
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failures == 0
    assert cb.half_open_attempts == 0
    assert cb.can_execute() is True


def test_circuit_breaker_reopens_on_half_open_failure():
    cb = CircuitBreaker(name="ws", failure_threshold=2, recovery_timeout=0.0, half_open_max=1)
    cb.record_failure()
    cb.record_failure()
    cb.can_execute()
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_record_success_decrements_failures_in_closed():
    cb = CircuitBreaker(name="ws", failure_threshold=5, recovery_timeout=0.0, half_open_max=1)
    cb.record_failure()
    cb.record_failure()
    assert cb.failures == 2
    assert cb.state == CircuitState.CLOSED

    cb.record_success()
    assert cb.failures == 1
    assert cb.state == CircuitState.CLOSED

    cb.record_success()
    assert cb.failures == 0
    assert cb.state == CircuitState.CLOSED


def test_trading_circuit_breakers_can_trade_when_all_closed():
    breakers = TradingCircuitBreakers()
    for cb in (breakers.ws, breakers.rest, breakers.execution, breakers.book_stale):
        cb.reset()

    can_trade, issues = breakers.can_trade()
    assert can_trade is True
    assert issues == []


def test_trading_circuit_breakers_blocks_when_any_open():
    breakers = TradingCircuitBreakers()
    for cb in (breakers.ws, breakers.rest, breakers.execution, breakers.book_stale):
        cb.reset()

    breakers.ws.record_failure()
    breakers.ws.record_failure()
    breakers.ws.record_failure()

    can_trade, issues = breakers.can_trade()
    assert can_trade is False
    assert len(issues) == 1
    assert "websocket" in issues[0]


def test_trading_circuit_breakers_all_closed_reflects_state():
    breakers = TradingCircuitBreakers()
    for cb in (breakers.ws, breakers.rest, breakers.execution, breakers.book_stale):
        cb.reset()
    assert breakers.all_closed() is True

    breakers.ws.record_failure()
    breakers.ws.record_failure()
    breakers.ws.record_failure()
    assert breakers.all_closed() is False


def test_system_breakers_returns_singleton():
    first = system_breakers()
    second = system_breakers()
    assert first is second
