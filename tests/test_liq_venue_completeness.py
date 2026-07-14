"""Liquidation section states realized-tape completeness per venue (SLICE 1в-4).

Bybit streams the full 500ms tape; Binance/OKX/Bitget cap to the largest per ~1s.
The header must distinguish an estimate (Binance-OI-based) from real cross-venue
liquidations, and label each contributing venue's completeness.
"""
from __future__ import annotations

from hunt_core.deliver._sections import format_liquidation_map_section
from hunt_core.maps.liquidation import LiquidationHeatmap, heatmap_to_market_dict


def test_venue_completeness_mapping() -> None:
    hm = LiquidationHeatmap(
        clusters=(), density_zones=(),
        nearest_long_liquidation=None, nearest_short_liquidation=None,
        cascade_risk_direction=None, total_long_at_risk=0.0, total_short_at_risk=0.0,
        forward_confidence=0.5, venues=("bybit", "binance", "okx"), realized_event_count=5,
    )
    d = heatmap_to_market_dict(hm)
    assert d["liq_venue_completeness"] == {"bybit": "full", "binance": "capped_1s", "okx": "capped_1s"}


def test_realized_header_lists_venue_completeness() -> None:
    row = {
        "price": 62000.0,
        "market": {
            "liq_heatmap_nearest_long": 61900.0,
            "liq_synthetic_only": False,
            "liq_venue_completeness": {"bybit": "full", "binance": "capped_1s"},
            "liq_heatmap_clusters": [],
        },
    }
    text = format_liquidation_map_section(row)
    assert "реальные ликвидации" in text
    assert "bybit=full" in text and "binance=capped_1s" in text


def test_synthetic_header_is_honest_about_binance_oi() -> None:
    row = {
        "price": 62000.0,
        "market": {"liq_heatmap_nearest_long": 61900.0, "liq_synthetic_only": True,
                   "liq_heatmap_clusters": []},
    }
    text = format_liquidation_map_section(row)
    assert "оценка по leverage-tier (Binance OI)" in text
    assert "без реальных ликвидаций" in text
