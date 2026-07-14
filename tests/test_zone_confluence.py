"""Zone confluence fuses independent maps into a conviction score (SLICE 1b).

A limit/добор zone is high-conviction when multiple maps corroborate it at once —
POC + liquidation magnet + wall + funding regime. Only same-side evidence counts.
"""
from __future__ import annotations

from hunt_core.deliver.zone_confluence import score_zone_confluence


def test_full_confluence_long_support() -> None:
    # Support zone 59900–60100 under price 62000, corroborated by 4 maps.
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={
            "liq_heatmap_nearest_long": 60000.0,      # long-liq magnet in zone
            "liq_heatmap_clusters": [{"price": 60050.0}],
            "funding": -0.0005,                        # shorts crowded → favors long
        },
        maps={
            "volume_profile": {"poc": 60000.0, "hvn_nodes": [{"price": 59950.0}]},
            "orderbook": {"sticky_walls": [{"price": 60020.0}]},
        },
    )
    assert conf["score"] >= 4
    assert "POC" in conf["factors"] and "магнит ликв." in conf["factors"]
    assert "стена" in conf["factors"]
    assert conf["label"] == "сильный"


def test_wrong_side_magnet_not_counted() -> None:
    # A SHORT-squeeze magnet (above price) must NOT corroborate a LONG support zone.
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={
            "liq_heatmap_nearest_short": 60000.0,      # wrong side
            "liq_heatmap_clusters": [{"price": 63000.0}],  # above price → short side
            "funding": 0.0,
        },
        maps={},
    )
    assert "магнит ликв." not in conf["factors"]


def test_funding_alignment_is_side_aware() -> None:
    # Positive funding (crowded long) corroborates a SHORT zone, not a long one.
    short = score_zone_confluence(
        lo=64000.0, hi=64200.0, side="short", price=62000.0,
        market={"funding": 0.0005}, maps={},
    )
    assert any("funding" in f for f in short["factors"])
    long_ = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0,
        market={"funding": 0.0005}, maps={},
    )
    assert not any("funding" in f for f in long_["factors"])


def test_no_confluence_is_empty() -> None:
    conf = score_zone_confluence(
        lo=59900.0, hi=60100.0, side="long", price=62000.0, market={}, maps={},
    )
    assert conf["score"] == 0 and conf["factors"] == () and conf["label"] == ""
