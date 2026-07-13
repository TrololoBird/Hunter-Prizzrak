"""Delivery-completeness key tuples must have no duplicate keys (DATA-2).

basis_5m / oi_z / gls_z were listed in both _FAST and the _FULL extension, so the
per-key audit loop checked them twice and appended each violation string twice.
"""
from __future__ import annotations

from hunt_core.data.completeness import (
    DELIVERY_MARKET_KEYS_FAST,
    DELIVERY_MARKET_KEYS_FULL,
    audit_market_derivatives,
)


def test_full_keys_unique() -> None:
    keys = DELIVERY_MARKET_KEYS_FULL
    dups = {k for k in keys if keys.count(k) > 1}
    assert not dups, f"duplicate delivery keys: {dups}"


def test_fast_keys_unique() -> None:
    keys = DELIVERY_MARKET_KEYS_FAST
    dups = {k for k in keys if keys.count(k) > 1}
    assert not dups, f"duplicate delivery keys: {dups}"


def test_full_superset_of_fast() -> None:
    assert set(DELIVERY_MARKET_KEYS_FAST).issubset(set(DELIVERY_MARKET_KEYS_FULL))


def test_missing_market_reports_each_violation_once() -> None:
    # Empty market → every key missing; each violation string must appear once.
    violations = audit_market_derivatives({}, tier="full")
    assert len(violations) == len(set(violations)), f"duplicated violations: {violations}"
