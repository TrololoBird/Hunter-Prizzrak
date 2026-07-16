"""Synthetic liq magnets must not shift scores, votes, forecasts or targets.

``heatmap_to_market_dict`` publishes ``liq_heatmap_nearest_long/short`` and
``liq_heatmap_clusters`` UNCONDITIONALLY — including the forward-only /
entry-anchored leverage-tier ESTIMATE produced when there are no realized events.
The same module already gates ``magnet_pull_*`` and the squeeze-fuel at-risk
notional on ``realized_event_count > 0``, with an explicit comment that synthetic
values must not leak into score-shifting fields that consumers read without
checking ``liq_synthetic_only``.

Four consumers did exactly that. ``prizrak/liq_reconcile.py`` gates correctly and is
the reference pattern. These tests pin the rule at the producer's accessors and at
each score-shifting consumer.
"""
from __future__ import annotations

from typing import Any

from hunt_core.deliver._context_lines import format_liq_magnet_line
from hunt_core.deliver.zone_confluence import score_zone_confluence
from hunt_core.maps.liquidation import (
    liq_is_synthetic,
    realized_liq_clusters,
    realized_liq_magnet,
)
from hunt_core.toolkit.targets import collect_downward_targets, collect_upward_targets

_PRICE = 62_000.0


def _market(*, synthetic: bool) -> dict[str, Any]:
    return {
        "liq_heatmap_nearest_long": 61_900.0,
        "liq_heatmap_nearest_short": 62_100.0,
        "liq_synthetic_only": synthetic,
        "liq_heatmap_clusters": [
            {"price": 61_900.0, "total_notional": 40_000_000.0, "intensity": 0.9},
            {"price": 62_100.0, "total_notional": 40_000_000.0, "intensity": 0.9},
        ],
    }


# --- producer-side accessors ----------------------------------------------------

def test_accessor_hides_synthetic_magnet_and_clusters() -> None:
    m = _market(synthetic=True)
    assert liq_is_synthetic(m) is True
    assert realized_liq_magnet(m, side="long") is None
    assert realized_liq_magnet(m, side="short") is None
    assert realized_liq_clusters(m) == []


def test_accessor_passes_realized_magnet_and_clusters() -> None:
    m = _market(synthetic=False)
    assert liq_is_synthetic(m) is False
    assert realized_liq_magnet(m, side="long") == 61_900.0
    assert realized_liq_magnet(m, side="short") == 62_100.0
    assert len(realized_liq_clusters(m)) == 2


def test_accessor_rejects_unusable_values() -> None:
    assert realized_liq_magnet({"liq_heatmap_nearest_long": None}, side="long") is None
    assert realized_liq_magnet({"liq_heatmap_nearest_long": 0.0}, side="long") is None
    assert realized_liq_magnet({"liq_heatmap_nearest_long": "junk"}, side="long") is None
    assert realized_liq_magnet(None, side="long") is None


def test_accessor_rejects_bad_side() -> None:
    import pytest

    with pytest.raises(ValueError):
        realized_liq_magnet(_market(synthetic=False), side="up")


# --- consumer: toolkit/targets.py (forecast targets) ----------------------------

def test_synthetic_magnet_is_not_an_upward_target() -> None:
    targets, factors = collect_upward_targets({"market": _market(synthetic=True)}, _PRICE)
    assert "short_liq_magnet" not in factors
    assert 62_100.0 not in targets


def test_realized_magnet_still_is_an_upward_target() -> None:
    targets, factors = collect_upward_targets({"market": _market(synthetic=False)}, _PRICE)
    assert "short_liq_magnet" in factors
    assert 62_100.0 in targets


def test_synthetic_magnet_is_not_a_downward_target() -> None:
    targets, factors = collect_downward_targets({"market": _market(synthetic=True)}, _PRICE)
    assert "long_liq_magnet" not in factors
    assert 61_900.0 not in targets


def test_realized_magnet_still_is_a_downward_target() -> None:
    targets, factors = collect_downward_targets({"market": _market(synthetic=False)}, _PRICE)
    assert "long_liq_magnet" in factors
    assert 61_900.0 in targets


# --- consumer: deliver/zone_confluence.py (confluence votes) --------------------

def _confluence_factors(*, synthetic: bool) -> list[str]:
    conf = score_zone_confluence(
        lo=61_850.0, hi=61_950.0, side="long",
        market=_market(synthetic=synthetic), maps={}, price=_PRICE,
    )
    return list(conf["factors"])


def test_synthetic_magnet_casts_no_confluence_vote() -> None:
    assert not any("ликв" in f for f in _confluence_factors(synthetic=True))


def test_realized_magnet_still_casts_a_confluence_vote() -> None:
    assert any("ликв" in f for f in _confluence_factors(synthetic=False))


# --- consumer: toolkit/manipulation_fusion.py (ignition factor, display-only) ---

def _ignition(*, synthetic: bool) -> float:
    from hunt_core.toolkit.manipulation_fusion import evaluate_manipulation_fusion

    row = {"price": _PRICE, "market": _market(synthetic=synthetic)}
    return evaluate_manipulation_fusion(row).score_ignition


def test_synthetic_magnet_adds_no_ignition_weight() -> None:
    assert _ignition(synthetic=True) < _ignition(synthetic=False)


def test_fusion_short_liq_check_is_false_when_synthetic() -> None:
    from hunt_core.toolkit.manipulation_fusion import evaluate_manipulation_fusion

    row = {"price": _PRICE, "market": _market(synthetic=True)}
    assert evaluate_manipulation_fusion(row).checks["short_liq_above"] is False


# --- display-only consumer keeps showing the estimate, but LABELS it -------------

def test_context_line_labels_synthetic_estimate() -> None:
    row = {"price": _PRICE, "market": _market(synthetic=True)}
    line = format_liq_magnet_line(row, direction="long", price=_PRICE)
    assert "62" in line          # the estimate is still shown...
    assert "оценка" in line      # ...but explicitly labelled as one


def test_context_line_has_no_label_when_realized() -> None:
    row = {"price": _PRICE, "market": _market(synthetic=False)}
    line = format_liq_magnet_line(row, direction="long", price=_PRICE)
    assert "оценка" not in line
