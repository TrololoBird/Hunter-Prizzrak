"""G-17/G-18: the delivery layer must read the keys the maps producers actually write,
and a big deep wall must survive the detector's cap long enough to be rendered.
"""
from __future__ import annotations

from collections import deque

from hunt_core.deliver._sections import format_liquidity_heatmap_section
from hunt_core.maps.orderbook import _detect_sticky_walls


def _snap(levels: list[tuple[float, float]], side: str) -> dict:
    return {
        f"{side}_levels": [
            {"price": p, "qty": q, "notional_usd": p * q} for p, q in levels
        ]
    }


def test_big_deep_wall_beats_nearby_dust() -> None:
    # Seven tiny bids hugging price + one huge bid 2% below. The old detector kept the
    # six NEAREST and evicted the big one, so WO#6 had nothing deep to render.
    price = 100.0
    dust = [(99.9 - i * 0.05, 1.0) for i in range(7)]  # tiny, very close
    deep = [(98.0, 5000.0)]                             # the real defended level, -2%
    hist: deque = deque(_snap(dust + deep, "bid") for _ in range(5))
    walls = _detect_sticky_walls(hist, current_price=price, min_samples=3)
    prices = [w["price"] for w in walls if w["side"] == "bid"]
    assert any(abs(p - 98.0) < 0.2 for p in prices), "deep wall must survive the cap"


def test_heatmap_section_renders_producer_keys() -> None:
    # depth_heatmap_matrix uses price_center (not price); liquidity_voids use
    # price_center/distance_pct (not price_lo/direction). Both blocks used to render
    # nothing at all because they read keys no producer emits.
    row = {
        "price": 100.0,
        "maps": {
            "orderbook": {
                "sticky_walls": [],
                "spoof_flags": [],
                "depth_heatmap_matrix": [
                    {"sample": 0, "price_center": 101.5, "depth_usd": 9_000.0, "intensity": 0.9}
                ],
                "liquidity_voids": [
                    {"price_center": 103.0, "depth_usd": 10.0, "distance_pct": 3.0}
                ],
            }
        },
        "market": {},
    }
    out = format_liquidity_heatmap_section(row)
    assert "Depth bands" in out
    assert "101.5" in out
    assert "Разрежение" in out
    assert "103" in out
