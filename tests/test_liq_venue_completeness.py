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


def test_live_but_quiet_venue_shown_with_zero_events() -> None:
    # A live Bybit feeder with 0 events must be VISIBLE (0ev), so "quiet market" is
    # distinguishable from "Bybit feeder died".
    row = {
        "price": 62000.0,
        "market": {
            "liq_heatmap_nearest_long": 61900.0,
            "liq_synthetic_only": True,
            "liq_venue_events": {"bybit": 0, "binance": 5, "okx": 0},
            "liq_venue_completeness": {"bybit": "full", "binance": "capped_1s", "okx": "capped_1s"},
            "liq_heatmap_clusters": [],
        },
    }
    text = format_liquidation_map_section(row)
    assert "bybit=full·0ev" in text   # live but quiet — NOT hidden
    assert "binance=capped_1s·5ev" in text
    assert "okx=capped_1s·0ev" in text


def test_venue_events_exposed_in_map_dict() -> None:
    from hunt_core.maps.liquidation import LiquidationHeatmap, LiquidationMap

    hm = LiquidationHeatmap(
        clusters=(), density_zones=(),
        nearest_long_liquidation=None, nearest_short_liquidation=None,
        cascade_risk_direction=None, total_long_at_risk=0.0, total_short_at_risk=0.0,
        forward_confidence=0.5, venues=("binance",), realized_event_count=3,
    )
    m = LiquidationMap(heatmap=hm, forward_zones=[], realized_zones=[],
                       venue_events={"bybit": 0, "binance": 3, "okx": 0, "bitget": 0})
    d = m.to_dict()
    assert d["liq_venue_events"] == {"bybit": 0, "binance": 3, "okx": 0, "bitget": 0}
    # completeness now covers ALL live venues, not just the ones with events.
    assert set(d["liq_venue_completeness"]) == {"bybit", "binance", "okx", "bitget"}
    assert d["liq_venue_completeness"]["bybit"] == "full"


def test_synthetic_header_is_honest_about_binance_oi() -> None:
    row = {
        "price": 62000.0,
        "market": {"liq_heatmap_nearest_long": 61900.0, "liq_synthetic_only": True,
                   "liq_heatmap_clusters": []},
    }
    text = format_liquidation_map_section(row)
    assert "оценка по leverage-tier (Binance OI)" in text
    assert "без реальных ликвидаций" in text
