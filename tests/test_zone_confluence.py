"""Zone confluence = number of INDEPENDENT sources that corroborate the zone.

POC/HVN/naked-POC are one source (volume profile); wall/absorption one source
(order book) — they must not be counted as separate votes (that inflates
conviction). Funding is a global directional modifier, not a per-zone vote.
Only same-side evidence counts. (Sprint-1b + review fix.)
"""
from __future__ import annotations

from hunt_core.deliver.zone_confluence import score_zone_confluence


def test_three_independent_sources_is_strong() -> None:
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={
            "liq_heatmap_nearest_long": 60000.0,        # source: liquidation
            "liq_heatmap_clusters": [{"price": 60050.0}],
        },
        maps={
            "volume_profile": {"poc": 60000.0, "hvn_nodes": [{"price": 59950.0}]},  # source: VP
            "orderbook": {"sticky_walls": [{"price": 60020.0}]},                     # source: OB
        },
    )
    assert conf["score"] == 3  # three independent sources
    assert set(conf["sources"]) == {"volume_profile", "liquidation", "orderbook"}
    assert conf["label"] == "сильный"


def test_correlated_vp_features_count_as_one_source() -> None:
    # POC + naked POC + HVN all come from the volume profile → ONE source, not three.
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={},
        maps={"volume_profile": {"poc": 60000.0, "naked_poc": 60010.0,
                                 "hvn_nodes": [{"price": 59950.0}]}},
    )
    assert conf["score"] == 1
    assert conf["sources"] == ("volume_profile",)
    assert set(conf["factors"]) == {"POC", "naked POC", "HVN"}  # hits still listed


def test_funding_is_modifier_not_a_source() -> None:
    # Funding alone must not create confluence score — it is a background regime.
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={"funding": -0.0005}, maps={},
    )
    assert conf["score"] == 0
    assert conf["funding_regime"] == "шорты перегреты"
    assert conf["factors"] == ()


def test_funding_regime_is_side_aware() -> None:
    short = score_zone_confluence(lo=64000.0, hi=64200.0, side="short", price=62000.0,
                                  market={"funding": 0.0005}, maps={})
    assert short["funding_regime"] == "лонги перегреты"
    long_ = score_zone_confluence(lo=59900.0, hi=60100.0, side="long", price=62000.0,
                                  market={"funding": 0.0005}, maps={})
    assert long_["funding_regime"] is None  # crowded-long doesn't favor a long entry


def test_wrong_side_liquidation_not_counted() -> None:
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={"liq_heatmap_nearest_short": 60000.0,
                "liq_heatmap_clusters": [{"price": 63000.0}]},  # above price → short side
        maps={},
    )
    assert "liquidation" not in conf["sources"]
