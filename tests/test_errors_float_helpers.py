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


def test_require_finite_float_accepts_numeric_types():
    assert math.isclose(require_finite_float(1, "open"), 1.0)
    assert math.isclose(require_finite_float(1.5, "open"), 1.5)
    assert math.isclose(require_finite_float("2.25", "open"), 2.25)
    assert math.isclose(require_finite_float(-3, "open"), -3.0)
    assert math.isclose(require_finite_float(0, "open"), 0.0)


def test_require_finite_float_raises_on_missing():
    with pytest.raises(SignalDataMissing) as exc:
        require_finite_float(None, "price")
    assert exc.value.field == "price"


def test_require_finite_float_raises_on_non_numeric_str():
    with pytest.raises(SignalDataMissing) as exc:
        require_finite_float("not-a-number", "volume")
    assert exc.value.field == "volume"
    assert exc.value.detail == "not_numeric"


def test_require_finite_float_raises_on_non_finite():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(SignalDataMissing) as exc:
            require_finite_float(bad, "close")
        assert exc.value.field == "close"
        assert exc.value.detail == "non_finite"


def test_optional_finite_float_returns_none_for_missing():
    assert optional_finite_float(None) is None
    assert optional_finite_float("abc") is None


def test_optional_finite_float_returns_float_for_numeric():
    assert math.isclose(optional_finite_float(1), 1.0)
    assert math.isclose(optional_finite_float("2.5"), 2.5)
    assert math.isclose(optional_finite_float(3.5), 3.5)


def test_optional_finite_float_delegates_to_finite_float_or_none():
    assert math.isclose(optional_finite_float(7), finite_float_or_none(7))
    assert optional_finite_float(None) == finite_float_or_none(None)
    assert optional_finite_float(math.inf) == finite_float_or_none(math.inf)


def test_finite_float_or_none_none_passthrough():
    assert finite_float_or_none(None) is None


def test_finite_float_or_none_bool_guard():
    assert math.isclose(finite_float_or_none(True), 1.0)
    assert math.isclose(finite_float_or_none(False), 0.0)


def test_finite_float_or_none_non_finite_to_none():
    assert finite_float_or_none(math.nan) is None
    assert finite_float_or_none(math.inf) is None
    assert finite_float_or_none(-math.inf) is None


def test_finite_float_or_none_numeric_str():
    assert math.isclose(finite_float_or_none("4.5"), 4.5)
    assert math.isclose(finite_float_or_none("10"), 10.0)
    assert math.isclose(finite_float_or_none("-2.25"), -2.25)


def test_finite_float_or_none_non_numeric_to_none():
    assert finite_float_or_none("nope") is None
    assert finite_float_or_none(object()) is None


def test_as_float_default_fallback():
    assert math.isclose(as_float(None), 0.0)
    assert math.isclose(as_float(None, default=9.0), 9.0)
    assert math.isclose(as_float("garbage"), 0.0)
    assert math.isclose(as_float(object()), 0.0)


def test_as_float_bool_handling():
    assert math.isclose(as_float(True), 1.0)
    assert math.isclose(as_float(False), 0.0)


def test_as_float_numeric():
    assert math.isclose(as_float(1), 1.0)
    assert math.isclose(as_float(2.5), 2.5)
    assert math.isclose(as_float("3.75"), 3.75)
    assert math.isclose(as_float(math.inf), 0.0)
    assert math.isclose(as_float(math.nan), 0.0)


def test_as_int_default_fallback():
    assert as_int(None) == 0
    assert as_int(None, default=5) == 5
    assert as_int("garbage") == 0
    assert as_int(object()) == 0


def test_as_int_bool_handling():
    assert as_int(True) == 1
    assert as_int(False) == 0


def test_as_int_integer_string():
    assert as_int("42") == 42
    assert as_int("-7") == -7


def test_as_int_float_with_integer_value():
    assert as_int(3.0) == 3
    assert as_int(7.0) == 7


def test_as_int_non_integer_float_falls_back():
    assert as_int(3.5) == 0
    assert as_int(3.5, default=9) == 9


def test_row_float_default_for_non_dict():
    assert math.isclose(row_float(None, "price"), 0.0)
    assert math.isclose(row_float(None, "price", default=1.0), 1.0)
    assert math.isclose(row_float("not-a-dict", "price"), 0.0)
    assert math.isclose(row_float(object(), "price"), 0.0)


def test_row_float_dict_lookup_coercion():
    row = {"price": "12.5", "vol": 3, "bad": "nope", "missing": None}
    assert math.isclose(row_float(row, "price"), 12.5)
    assert math.isclose(row_float(row, "vol"), 3.0)
    assert math.isclose(row_float(row, "bad"), 0.0)
    assert math.isclose(row_float(row, "missing"), 0.0)
    assert math.isclose(row_float(row, "absent", default=5.0), 5.0)
