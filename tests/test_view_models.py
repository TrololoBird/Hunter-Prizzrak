"""MarketView typed core (ADR-0004) — proves the row-dict defect family is now impossible.

Each test corresponds to a row in the ADR-0004 defect-elimination table: the untyped-dict defect
becomes a construction/validation error on the typed model."""
from __future__ import annotations

import polars as pl
import pytest
from pydantic import ValidationError

from hunt_core.engine.state import NotReady
from hunt_core.view.models import Derivs, Klines, MarketView


def _view(**kw: object) -> MarketView:
    base: dict[str, object] = {"symbol": "BTC/USDT:USDT", "now_ms": 1, "last_price": 64000.0, "price_source": "mark"}
    base.update(kw)
    return MarketView(**base)  # type: ignore[arg-type]


def test_unknown_key_rejected_at_construction() -> None:
    # phantom-key / orphan-field family: an unknown key is a hard error, not a silent dead branch.
    with pytest.raises(ValidationError):
        _view(not_a_market_view_field=1.0)  # unknown key → hard error (extra="forbid")
    with pytest.raises(ValidationError):
        Derivs(phantom=1.0)  # type: ignore[call-arg]


def test_frozen_cannot_mutate() -> None:
    v = _view()
    with pytest.raises(ValidationError):
        v.last_price = 1.0  # type: ignore[misc]


def test_strict_rejects_string_zero_but_keeps_real_zero() -> None:
    # falsy-zero family: "0" must NOT coerce to 0 (I-6), while a genuine 0.0 funding rate is valid data.
    with pytest.raises(ValidationError):
        Derivs(funding="0")  # type: ignore[arg-type]
    assert Derivs(funding=0.0).funding == 0.0  # real zero passes through, never nulled
    assert Derivs(funding=0).funding == 0.0  # int→float allowed in strict (lossless)


def test_absent_field_is_none_never_fabricated() -> None:
    d = Derivs()
    assert d.funding is None and d.oi is None and d.funding_zscore is None  # presence⟺fresh


def test_klines_require_raises_notready_when_absent() -> None:
    k = Klines(h4=pl.DataFrame({"c": [1.0, 2.0]}))
    assert k.require("4h").height == 2
    assert k.get("1m") is None
    with pytest.raises(NotReady):
        k.require("1m")


def test_marketview_defaults_are_empty_submodels() -> None:
    v = _view()
    assert v.klines.get("4h") is None
    assert v.derivs.mark is None
    assert v.cross.funding == {}
    assert v.not_ready == ()
