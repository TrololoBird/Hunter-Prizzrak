"""Fusion sub-checks read the REAL producer keys (R2 phantom-key fix, display/journal layer)."""
from __future__ import annotations

from typing import Any

from hunt_core.toolkit.manipulation_fusion import evaluate_manipulation_fusion


def _row(market: dict[str, Any] | None = None, structure: dict[str, Any] | None = None,
         phase: str = "accumulation") -> dict[str, Any]:
    return {
        "symbol": "TESTUSDT",
        "price": 100.0,
        "lifecycle": {"phase": phase},
        "market": market or {},
        "structure": structure or {},
    }


def test_obi_bid_reads_depth_imbalance() -> None:
    assert evaluate_manipulation_fusion(_row({"depth_imbalance": 0.25})).checks["obi_bid"] is True
    assert evaluate_manipulation_fusion(_row({"orderbook_imbalance": 0.25})).checks["obi_bid"] is False


def test_sweep_reclaim_reads_structure_events() -> None:
    a = evaluate_manipulation_fusion(_row(structure={"choch_detected": True}))
    assert a.checks["sweep_reclaim"] is True
    b = evaluate_manipulation_fusion(_row(structure={"bsl_sweep": True}))
    assert b.checks["sweep_reclaim"] is False  # old phantom key no longer honored


def test_above_vah_reads_map_vp_vah() -> None:
    a = evaluate_manipulation_fusion(_row({"map_vp_vah": 90.0}))  # price 100 > VAH 90
    assert a.checks["pos_near_high"] is True
    b = evaluate_manipulation_fusion(_row({"map_vah": 90.0}))
    assert b.checks["pos_near_high"] is False  # phantom key ignored


def test_squeeze_taker_reads_taker_5m() -> None:
    crowded = {
        "funding_rate": -0.0005,
        "taker_5m": 1.10,
        "map_accum_bid_absorption": True,
        "map_cvd_divergence": "bullish_div",
    }
    row = _row(crowded, phase="exhaustion_at_high")
    row["oi"] = {"regime": "squeeze"}
    a = evaluate_manipulation_fusion(row)
    # 4+ squeeze checks fire → anti_squeeze veto engages (False = squeeze blocks predump)
    assert a.checks["anti_squeeze"] is False
