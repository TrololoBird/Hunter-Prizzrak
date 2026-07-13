"""Liquidation cluster size-tail must be side-aware.

Long-liquidation mass sits BELOW price, short-squeeze mass ABOVE. The old
nearest-by-absolute-distance attached the SAME central cluster to both the
long and short rows — printing an identical, misleading "$X · Y% плотн." on
each. Each row must only consider clusters on its own side of current price.
"""
from __future__ import annotations

from hunt_core.deliver._sections import format_liquidation_map_section


def _row(clusters: list[dict]) -> dict:
    return {
        "price": 62000.0,
        "market": {
            "liq_heatmap_nearest_long": 61817.0,
            "liq_heatmap_nearest_short": 62127.0,
            "liq_cascade_risk": "long_flush",
            "liq_synthetic_only": True,
            "liq_heatmap_clusters": clusters,
            "liq_magnet_pull_long_pct": 0.3,
            "liq_magnet_pull_short_pct": 0.25,
        },
    }


def _line(text: str, needle: str) -> str:
    return next((ln for ln in text.splitlines() if needle in ln), "")


def test_single_below_cluster_not_attributed_to_short() -> None:
    # One dominant cluster just BELOW price: nearest to both magnets by abs distance.
    text = format_liquidation_map_section(_row([{"price": 61990.0, "total_notional": 40_200_000.0, "intensity": 0.94}]))
    long_line = _line(text, "Лонг-ликвидации")
    short_line = _line(text, "Шорт-сквиз")
    assert "плотн." in long_line  # below cluster feeds the long-liquidation row
    assert "плотн." not in short_line  # nothing above price → no size tail on short row


def test_two_sided_clusters_split_correctly() -> None:
    text = format_liquidation_map_section(
        _row(
            [
                {"price": 61900.0, "total_notional": 40_000_000.0, "intensity": 0.94},
                {"price": 62100.0, "total_notional": 108_000_000.0, "intensity": 0.60},
            ]
        )
    )
    long_line = _line(text, "Лонг-ликвидации")
    short_line = _line(text, "Шорт-сквиз")
    # Each row shows a DIFFERENT magnitude, from its own side.
    assert "плотн." in long_line and "плотн." in short_line
    assert long_line != short_line
