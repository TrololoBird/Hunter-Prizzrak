"""Fusion sub-checks read the REAL producer keys (R2 phantom-key fix, display/journal layer)."""
from __future__ import annotations

from typing import Any

from hunt_core.toolkit.manipulation_fusion import evaluate_manipulation_fusion


def _row(market: dict[str, Any] | None = None, phase: str = "accumulation") -> dict[str, Any]:
    return {
        "symbol": "TESTUSDT",
        "price": 100.0,
        "lifecycle": {"phase": phase},
        "market": market or {},
    }


def test_obi_bid_reads_depth_imbalance() -> None:
    assert evaluate_manipulation_fusion(_row({"depth_imbalance": 0.25})).checks["obi_bid"] is True
    assert evaluate_manipulation_fusion(_row({"orderbook_imbalance": 0.25})).checks["obi_bid"] is False


def test_sweep_reclaim_check_is_gone_not_silently_false() -> None:
    """Проверка `sweep_reclaim` снята вместе с ключами, которых никто не писал.

    Прежний тест строил ``structure={"choch_detected": True}`` РУКАМИ и был зелёным, пока
    продакшн держал вечное False: единственный вызывающий не передаёт ``structure=`` вовсе.
    Ровно тот случай, о котором предупреждает CLAUDE.md — фикстура, где ключ есть, зелёная
    по построению и слепа к тому, ради чего написана. Теперь фиксируем ОТСУТСТВИЕ проверки:
    её возврат обязан прийти вместе с настоящим продюсером структуры, а не с фикстурой.
    """
    assessment = evaluate_manipulation_fusion(_row())
    assert "sweep_reclaim" not in assessment.checks
    assert "leg_gain" not in assessment.checks


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
