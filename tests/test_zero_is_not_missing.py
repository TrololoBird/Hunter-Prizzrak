"""G-21: a measured 0.0 is DATA, not "missing".

The codebase repeatedly used `a or b` to pick a field, which silently discards a
legitimate zero — a flat funding rate, balanced taker flow, an unchanged OI or price —
and falls through to another (often staler, or differently-windowed) source. This pins
the two places where that zero actually changes a decision.
"""
from __future__ import annotations

from hunt_core.maps.oi import oi_regime_from_row
from hunt_core.scanner.detect.expansion_readiness import _safe_float


def test_flat_oi_and_price_is_coiling_not_unknown() -> None:
    # OI flat AND price flat is the textbook "coiling" state. The `or` chain skipped both
    # zeros, fell through to a 24h change from a DIFFERENT window, and mislabelled it.
    row = {
        "market": {"oi_change_pct": 0.0, "price_change_pct": 0.0},
        "chg_24h_pct": 6.0,  # different window — must NOT be borrowed
    }
    assert oi_regime_from_row(row) == "coiling"


def test_missing_oi_is_unknown_not_zero() -> None:
    assert oi_regime_from_row({"market": {}}) == "unknown"


def test_measured_zero_flow_counts_as_measured() -> None:
    # A delta_ratio of 0.0 means balanced flow — a real measurement. It must not read as
    # "no flow data", or the fake-energy veto (which requires flow to be KNOWN) misfires.
    assert _safe_float(0.0) == 0.0
    assert _safe_float(None) is None
