"""One symbol raising an unhandled exception must not sink the whole tick.

_snapshot_one only catches TimeoutError + IncompleteReadError; anything else (e.g.
a Polars ComputeError) previously propagated through a plain asyncio.gather,
discarding every other symbol's already-computed row. gather now uses
return_exceptions=True and _settle_snapshot_results converts failures to error
rows while preserving successes; cancellation still propagates.
"""
from __future__ import annotations

import asyncio

import pytest

from hunt_core.runtime.cycle._cycle_tick import _settle_snapshot_results


def test_one_failure_does_not_drop_others() -> None:
    ordered = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    results = [
        ("BTCUSDT", {"symbol": "BTCUSDT", "price": 1.0}),
        ValueError("polars boom"),  # unhandled exception for ETH
        ("SOLUSDT", {"symbol": "SOLUSDT", "price": 2.0}),
    ]
    pairs = _settle_snapshot_results(ordered, results, now_iso="T", tier_for=lambda _s: "fast")
    by_sym = dict(pairs)
    assert by_sym["BTCUSDT"]["price"] == 1.0            # survivor kept
    assert by_sym["SOLUSDT"]["price"] == 2.0            # survivor kept
    assert by_sym["ETHUSDT"]["tick_path"] == "rest_error"  # failure → error row
    assert "polars boom" in by_sym["ETHUSDT"]["error"]


def test_cancellation_propagates() -> None:
    ordered = ["BTCUSDT"]
    results = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        _settle_snapshot_results(ordered, results, now_iso="T", tier_for=lambda _s: "fast")


def test_all_success_passthrough() -> None:
    ordered = ["BTCUSDT"]
    results = [("BTCUSDT", {"symbol": "BTCUSDT"})]
    assert _settle_snapshot_results(ordered, results, now_iso="T", tier_for=lambda _s: "full") == results
