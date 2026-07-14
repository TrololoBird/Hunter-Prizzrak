"""G-26 (Pydantic half): the domain/value models the audit named are now BaseModel.

Pins the behaviour the conversion must preserve AND the new guarantees it adds:
construction still works, immutability holds where it held before, and — the actual
benefit — a wrong-typed field now fails LOUDLY at construction instead of flowing on as
bad data (the invariant-I-6 class this whole audit was about).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from hunt_core.maps.engine import MapBundle
from hunt_core.prizrak.engines.config import AnalystConfig, load_analyst_config
from hunt_core.scanner.prescan import HuntCandidate, PrescanHit, UniverseConfig


def test_all_are_pydantic_models() -> None:
    for cls in (AnalystConfig, MapBundle, UniverseConfig, PrescanHit, HuntCandidate):
        assert issubclass(cls, BaseModel), cls.__name__


def test_construction_and_attribute_access_unchanged() -> None:
    hit = PrescanHit(
        symbol="BTCUSDT", interval="5m", change_pct=3.1, threshold_pct=2.0,
        quote_volume=1e7, direction="long",
    )
    assert hit.symbol == "BTCUSDT" and hit.energy == 0.0  # default preserved
    assert UniverseConfig().max_hot_coins == 10
    assert AnalystConfig().signal_queue_top_n == 3


def test_frozen_value_objects_stay_immutable() -> None:
    hit = PrescanHit(
        symbol="ETHUSDT", interval="1h", change_pct=1.0, threshold_pct=0.5,
        quote_volume=1e7, direction="short",
    )
    with pytest.raises(ValidationError):
        hit.energy = 9.9  # was frozen=True as a dataclass; still frozen


def test_bad_field_now_fails_loud_at_construction() -> None:
    # The benefit: a non-numeric change_pct used to sail through a dataclass and blow up
    # (or silently coerce) somewhere downstream. Now it is rejected at the boundary.
    with pytest.raises(ValidationError):
        PrescanHit(
            symbol="X", interval="5m", change_pct="not-a-number", threshold_pct=2.0,
            quote_volume=1e7, direction="long",
        )


def test_mapbundle_holds_nested_dataclasses_and_serialises() -> None:
    # arbitrary_types_allowed: nested plain-dataclass maps are stored as-is.
    b = MapBundle(symbol="SOLUSDT", ts_ms=1_700_000_000_000, extra={"map_oi_z": 0.4})
    d = b.to_dict()
    assert d["symbol"] == "SOLUSDT" and d["orderbook"] is None
    assert d["extra"] == {"map_oi_z": 0.4}


def test_analyst_config_still_loads_from_toml() -> None:
    cfg = load_analyst_config()
    assert isinstance(cfg, AnalystConfig)
    assert 0 < cfg.signal_queue_ttl_hours < 100
