"""G-23: the anti-fantasy-RR cap in signal_queue was dead.

`compute_opportunity_score` caps an inflated rr_primary against rr_conservative
(`rr > rr_cons * 1.8 → rr = rr_cons`), but NOTHING produced rr_conservative, so it was
always 0 and the cap never fired: a setup whose R:R was flattered by a wide entry band
took the full rr_norm weight and crowded honest setups out of TOP-3.
"""
from __future__ import annotations

from hunt_core.prizrak.engines.signal_queue import compute_opportunity_score
from hunt_core.prizrak.orchestrator import _rr_conservative


def test_rr_conservative_prices_the_worst_fill() -> None:
    # Long band 100–110, stop 95, TP1 140. Worst fill is the TOP of the band (110):
    # reward 30, risk 15 → 2.0, versus 8.0 measured from the bottom edge.
    rr = _rr_conservative(direction="long", entry_lo=100.0, entry_hi=110.0, stop=95.0, tp1=140.0)
    assert rr == 2.0


def test_short_conservative_uses_low_edge() -> None:
    # Short band 100–110, stop 115, TP1 70. Worst fill is the BOTTOM (100):
    # reward 30, risk 15 → 2.0.
    rr = _rr_conservative(direction="short", entry_lo=100.0, entry_hi=110.0, stop=115.0, tp1=70.0)
    assert rr == 2.0


def test_incomplete_geometry_is_none_not_zero() -> None:
    assert _rr_conservative(direction="long", entry_lo=100.0, entry_hi=110.0, stop=None, tp1=140.0) is None


def test_cap_now_fires_on_inflated_rr() -> None:
    # rr_primary 8.0 is >1.8x the conservative 2.0 → the score must be built on 2.0.
    base = {"action": "long", "strength": 0.6, "path": "zone_long", "fragility": 0.5,
            "trade_quality": "marginal", "geometry_confidence": 0.5}
    inflated = compute_opportunity_score({**base, "rr_primary": 8.0, "rr_conservative": 2.0})
    honest = compute_opportunity_score({**base, "rr_primary": 2.0, "rr_conservative": 2.0})
    assert inflated == honest, "an inflated rr_primary must not outscore the honest one"

    # Without the (previously missing) field the cap cannot fire — the old behaviour.
    uncapped = compute_opportunity_score({**base, "rr_primary": 8.0})
    assert uncapped > honest
