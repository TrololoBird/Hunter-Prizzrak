"""Scoped smoke test for the finite-float helper dedup (PR #1 surface).

NOTE: The full suite is currently uncollectable because the foundational
`hunt_core.data` package is absent from this repository snapshot (it is
imported transitively by every test module). This test intentionally only
exercises `hunt_core.errors` / `hunt_core.data_readiness`, which import
cleanly, so CI can verify the dedup change without the missing package.
"""
from __future__ import annotations

import math

import pytest

from hunt_core.errors import (
    SignalDataMissing,
    as_float,
    as_int,
    finite_float_or_none,
    optional_finite_float,
    require_finite_float,
    row_float,
)


def test_require_finite_float_ok() -> None:
    assert math.isclose(require_finite_float(1, "x"), 1.0)
    assert math.isclose(require_finite_float(2.5, "x"), 2.5)
    assert math.isclose(require_finite_float("3.5", "x"), 3.5)


@pytest.mark.parametrize(
    "bad",
    [None, "abc", float("nan"), float("inf"), float("-inf")],
)
def test_require_finite_float_raises(bad: object) -> None:
    with pytest.raises(SignalDataMissing) as exc:
        require_finite_float(bad, "price")
    assert exc.value.field == "price"


def test_optional_delegates_to_finite_float_or_none() -> None:
    assert optional_finite_float(None) is None
    assert optional_finite_float("abc") is None
    assert math.isclose(optional_finite_float(1), 1.0)
    assert optional_finite_float(float("nan")) is None


def test_finite_float_or_none() -> None:
    assert finite_float_or_none(None) is None
    assert math.isclose(finite_float_or_none(True), 1.0)
    assert math.isclose(finite_float_or_none(False), 0.0)
    assert math.isclose(finite_float_or_none("2.5"), 2.5)
    assert finite_float_or_none(float("inf")) is None


def test_as_float_as_int() -> None:
    assert as_float(None) == 0.0
    assert math.isclose(as_float(True), 1.0)
    assert math.isclose(as_float("x", default=-1.0), -1.0)
    assert as_int(None) == 0
    assert as_int(True) == 1
    assert as_int("7") == 7
    assert as_int(5.0) == 5


def test_row_float() -> None:
    assert row_float(None, "k") == 0.0
    assert math.isclose(row_float({"k": "9"}, "k"), 9.0)
    assert math.isclose(row_float({"k": "bad"}, "k", default=-1.0), -1.0)


def test_math_finite_sanity() -> None:
    assert not math.isfinite(float("nan"))
