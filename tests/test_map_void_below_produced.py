"""`map_void_below` must be PRODUCED, not just read.

``toolkit.targets.collect_downward_targets`` reads ``map_void_below`` and appends a
"void_path_down" target, but no producer ever emitted the key:
``derive_ob_accumulation_features`` computed voids on both sides and published only
``map_void_above``. Downward void targets could therefore never fire while upward
ones could — a directional asymmetry rather than a design choice.
"""
from __future__ import annotations

from hunt_core.maps.orderbook import OrderbookMap, derive_ob_accumulation_features
from hunt_core.toolkit.targets import collect_downward_targets

_PRICE = 62_000.0


def _ob(voids: list[dict[str, float]]) -> OrderbookMap:
    return OrderbookMap(symbol="BTCUSDT", current_price=_PRICE, liquidity_voids=voids)


def test_void_below_is_emitted() -> None:
    out = derive_ob_accumulation_features(
        _ob([{"price_center": 61_500.0, "distance_pct": 0.8}]), current_price=_PRICE
    )
    assert out["map_void_below"] == 61_500.0
    assert out["map_void_below_pct"] == 0.8


def test_void_above_still_emitted_unchanged() -> None:
    out = derive_ob_accumulation_features(
        _ob([{"price_center": 62_600.0, "distance_pct": 1.0}]), current_price=_PRICE
    )
    assert out["map_void_above"] == 62_600.0
    assert out["map_void_above_pct"] == 1.0
    assert "map_void_below" not in out  # nothing below price → key stays absent


def test_both_sides_emitted_and_nearest_wins_per_side() -> None:
    out = derive_ob_accumulation_features(
        _ob(
            [
                {"price_center": 61_500.0, "distance_pct": 0.8},
                {"price_center": 60_000.0, "distance_pct": 3.2},   # farther below
                {"price_center": 62_600.0, "distance_pct": 1.0},
                {"price_center": 64_000.0, "distance_pct": 3.2},   # farther above
            ]
        ),
        current_price=_PRICE,
    )
    assert out["map_void_below"] == 61_500.0  # nearest by distance_pct, own side only
    assert out["map_void_above"] == 62_600.0


def test_void_above_is_never_booked_as_below() -> None:
    # Side filtering must be by price, not by absolute distance.
    out = derive_ob_accumulation_features(
        _ob([{"price_center": 62_600.0, "distance_pct": 1.0}]), current_price=_PRICE
    )
    assert "map_void_below" not in out


def test_downward_void_target_now_fires() -> None:
    # The phantom key's consumer: previously unreachable in production.
    row = {"market": {"map_void_below": 61_500.0}}
    targets, factors = collect_downward_targets(row, _PRICE)
    assert "void_path_down" in factors
    assert 61_500.0 in targets
