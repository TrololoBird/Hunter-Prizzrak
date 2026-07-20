"""One symbol raising an unhandled exception must not sink the whole tick.

``_native_one`` only catches TimeoutError + IncompleteReadError; anything else (e.g. a Polars
ComputeError) would propagate through a plain ``asyncio.gather`` and discard every other symbol's
already-assembled view. gather uses ``return_exceptions=True`` and ``_settle_native_results`` folds
failures into per-symbol error rows while preserving successes; cancellation still propagates.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from hunt_core.runtime.cycle._cycle_tick import _settle_native_results

_NOW = datetime(2026, 7, 19, tzinfo=UTC)


def test_one_failure_does_not_drop_others() -> None:
    ordered = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    # A successful gather item is the (sym, err_row|None, nav) triple _native_one returns; here nav is
    # stubbed by a sentinel object since _settle only routes it, never reads it.
    btc_nav = object()
    sol_nav = object()
    results = [
        ("BTCUSDT", None, btc_nav),
        ValueError("polars boom"),  # unhandled exception for ETH
        ("SOLUSDT", None, sol_nav),
    ]
    settled = _settle_native_results(ordered, results, now=_NOW)
    assert settled["BTCUSDT"] == (None, btc_nav)          # survivor kept
    assert settled["SOLUSDT"] == (None, sol_nav)          # survivor kept
    eth_err, eth_nav = settled["ETHUSDT"]
    assert eth_nav is None
    assert eth_err is not None
    assert eth_err["tick_path"] == "rest_error"           # failure → error row
    assert "polars boom" in eth_err["error"]


def test_cancellation_propagates() -> None:
    ordered = ["BTCUSDT"]
    results: list[object] = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        _settle_native_results(ordered, results, now=_NOW)


def test_all_success_passthrough() -> None:
    ordered = ["BTCUSDT"]
    nav = object()
    settled = _settle_native_results(ordered, [("BTCUSDT", None, nav)], now=_NOW)
    assert settled == {"BTCUSDT": (None, nav)}
