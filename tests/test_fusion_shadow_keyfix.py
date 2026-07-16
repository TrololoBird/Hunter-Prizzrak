"""R2 shadow key-fix telemetry: emission unchanged, flips logged (manipulation_fusion)."""
from __future__ import annotations

from typing import Any

from hunt_core.toolkit.manipulation_fusion import evaluate_manipulation_fusion


def _ignition_row(**market_extra: Any) -> dict[str, Any]:
    market = {
        "funding_rate": -0.0005,
        "liq_heatmap_nearest_short": 110.0,
        "map_cvd_divergence": "bullish_div",
        **market_extra,
    }
    return {
        "symbol": "TESTUSDT",
        "price": 100.0,
        "lifecycle": {"phase": "accumulation"},
        "market": market,
        "structure": {},
    }


def test_shadow_never_alters_assessment() -> None:
    """depth_imbalance (real key) present, orderbook_imbalance (phantom) absent:
    the returned assessment must still be computed off the phantom key (obi_bid False)."""
    row = _ignition_row(depth_imbalance=0.25)
    a = evaluate_manipulation_fusion(row)
    assert a.checks["obi_bid"] is False  # phantom key absent → sub-check inert, as before


def test_shadow_flip_logged_without_crash(caplog) -> None:
    """A row where the real-key variant flips obi_bid must evaluate cleanly (telemetry
    path exercised); assessment identical to the phantom-key baseline."""
    row_plain = _ignition_row()
    row_with_di = _ignition_row(depth_imbalance=0.25)
    a_plain = evaluate_manipulation_fusion(row_plain)
    a_di = evaluate_manipulation_fusion(row_with_di)
    assert a_plain.archetype == a_di.archetype
    assert a_plain.pass_count == a_di.pass_count
