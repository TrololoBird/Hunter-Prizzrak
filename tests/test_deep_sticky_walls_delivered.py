"""Deep sticky walls (±1.5-3%) must reach the delivery text, not just the nearest (WO #6).

_detect_sticky_walls tracks walls out to several % with notional; the old render showed
only the nearest per side, hiding a large wall a couple % away. Now the top-N by
notional within ±4% per side are surfaced.
"""
from __future__ import annotations

from hunt_core.deliver._sections import format_liquidity_heatmap_section


def _row(walls: list[dict]) -> dict:
    return {"maps": {"orderbook": {"sticky_walls": walls, "spoof_flags": [],
                                   "depth_heatmap_matrix": [], "liquidity_voids": []}}}


def test_deep_wall_is_delivered() -> None:
    # A large bid wall 2.4% below price must appear even though a smaller one sits nearer.
    text = format_liquidity_heatmap_section(_row([
        {"side": "bid", "price": 61980.0, "notional_usd": 5_000_000.0, "distance_pct": 0.2},
        {"side": "bid", "price": 61000.0, "notional_usd": 40_000_000.0, "distance_pct": 2.4},
    ]))
    assert "61000" in text  # the deep, larger wall reaches the text
    assert "61980" in text  # nearer one still shown


def test_biggest_notional_wall_ordered_first() -> None:
    text = format_liquidity_heatmap_section(_row([
        {"side": "ask", "price": 62100.0, "notional_usd": 3_000_000.0, "distance_pct": 0.3},
        {"side": "ask", "price": 63500.0, "notional_usd": 90_000_000.0, "distance_pct": 2.4},
    ]))
    assert text.index("63500") < text.index("62100")  # biggest wall first


def test_wall_beyond_range_excluded() -> None:
    text = format_liquidity_heatmap_section(_row([
        {"side": "bid", "price": 61980.0, "notional_usd": 5_000_000.0, "distance_pct": 0.2},
        {"side": "bid", "price": 55000.0, "notional_usd": 80_000_000.0, "distance_pct": 11.0},
    ]))
    assert "55000" not in text  # >4% off → not a near-actionable wall
