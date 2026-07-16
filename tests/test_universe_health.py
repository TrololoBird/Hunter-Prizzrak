"""Universe data-plane health monitor — turns a silent mass blackout into a loud signal.

Models the 2026-07-11 incident: the SOCKS proxy died, every symbol started failing the
4h-staleness gate, and nothing surfaced it until the watchdog hard-killed a hung loop.
"""
from __future__ import annotations

from hunt_core.diagnostics.universe_health import (
    assess_universe_health,
    classify_row_health,
)


def _stale_row(sym: str) -> dict:
    """A real rejected-row shape (as written to hunt_scan-*.jsonl on the incident)."""
    v = f"klines.4h.stale.{sym}.40336224ms>36000000ms"
    return {
        "symbol": sym, "error": v, "no_signal_reason": v, "data_violations": [v],
        "data_integrity": {"complete": False, "violations": [v]},
    }


def _healthy_row(sym: str) -> dict:
    return {"symbol": sym, "price": 1.23, "structure": {}, "data_readiness": {"ready": True}}


def test_classify_normalises_stale_violation_to_a_stable_kind():
    assert classify_row_health(_stale_row("XMRUSDT")) == "klines.4h.stale"
    assert classify_row_health(_stale_row("XRPUSDT")) == "klines.4h.stale"  # symbol/ms stripped


def test_classify_fetch_failed_and_rows_shortfall():
    assert classify_row_health({"data_violations": ["klines.1m.fetch_failed"]}) == "klines.1m.fetch_failed"
    assert classify_row_health({"data_violations": ["klines.1m.rows=1<min_raw=300"]}) == "klines.1m.rows"


def _rest_error_row(sym: str, *, error: str) -> dict:
    """The exact row shape _cycle_tick produces for timeout / network-exception ticks."""
    return {
        "ts": "2026-07-11T12:00:00+00:00",
        "symbol": sym,
        "error": error,
        "tick_path": "rest_error",
        "snapshot_tier": "full",
    }


def test_rest_error_timeout_rows_are_blackout_failures():
    # 2026-07-11 dead-proxy signature: every symbol times out — must NOT be HEALTHY.
    assert classify_row_health(_rest_error_row("BTCUSDT", error="symbol_tick_timeout")) == "rest_error.timeout"
    assert classify_row_health(_rest_error_row("ETHUSDT", error="TimeoutError()")) == "rest_error.timeout"


def test_rest_error_exception_rows_are_blackout_failures():
    assert (
        classify_row_health(_rest_error_row("BTCUSDT", error="ClientProxyConnectionError('dead')"))
        == "rest_error.exception"
    )
    # even with an empty/odd error string, tick_path=rest_error is a failure by construction
    assert classify_row_health({"symbol": "X", "tick_path": "rest_error"}) == "rest_error.exception"


def test_universe_of_rest_errors_is_critical():
    rows = [_rest_error_row(s, error="symbol_tick_timeout") for s in ("A", "B", "C", "D", "E", "F")]
    h = assess_universe_health(rows)
    assert h.degraded and h.critical
    assert h.dominant_kind == "rest_error.timeout"


def test_healthy_row_is_not_a_failure():
    assert classify_row_health(_healthy_row("BTCUSDT")) is None
    # a legitimate no-signal tick (neutral phase, no data error) is NOT unhealthy
    assert classify_row_health({"symbol": "X", "no_signal_reason": "phase.neutral"}) is None


def test_total_blackout_is_critical():
    rows = [_stale_row(s) for s in ("A", "B", "C", "D", "E", "F", "G")]
    h = assess_universe_health(rows)
    assert h.degraded and h.critical
    assert h.failures == 7 and h.total == 7
    assert h.dominant_kind == "klines.4h.stale"
    assert "CRITICAL" in h.summary()


def test_partial_degradation_flags_degraded_not_critical():
    rows = [_stale_row(s) for s in ("A", "B", "C", "D")] + [_healthy_row(s) for s in ("E", "F", "G", "H")]
    h = assess_universe_health(rows)  # 50% failing
    assert h.degraded and not h.critical
    assert abs(h.failure_frac - 0.5) < 1e-9


def test_healthy_universe_not_degraded():
    rows = [_healthy_row(s) for s in ("A", "B", "C", "D", "E", "F")]
    h = assess_universe_health(rows)
    assert not h.degraded and not h.critical
    assert "OK" in h.summary()


def test_tiny_universe_never_flags_to_avoid_false_alarms():
    # 2 pinned symbols both failing must NOT trip a universe-wide alarm.
    rows = [_stale_row("BTCUSDT"), _stale_row("ETHUSDT")]
    h = assess_universe_health(rows, min_universe=5)
    assert not h.degraded and not h.critical


def test_telemetry_shape_is_logging_friendly():
    rows = [_stale_row(s) for s in ("A", "B", "C", "D", "E")]
    t = assess_universe_health(rows).telemetry()
    assert t["universe"] == 5 and t["failures"] == 5 and t["failure_pct"] == 100.0
    assert t["dominant_kind"] == "klines.4h.stale"
    assert t["critical"] is True
