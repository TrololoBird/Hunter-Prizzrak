"""HTF-bias score reproduction — the МТФ header now CLAIMS «(1w·1d·4h·1h): score»,
so that claim must be reproducible from the REAL _htf_bias compute on exactly
those four TFs (the #1 fix must not overclaim — same defect class it fixes).

Weights (config.defaults): 1w=0.35, 1d=0.30, 4h=0.25, 1h=0.10 (Σ=1.00),
threshold 0.30 — MONOTONE by seniority (1w ≥ 1d ≥ 4h ≥ 1h), per the method's
senior-TF-dominant read. score = net / weight_available, where weight_available
sums the weights of every TF that HAS structure (neutral included) and net is the
signed sum of the directional ones. This is why the score does NOT match a flat
{−1,0} average over 4 TFs — the weights are unequal, and this test pins that the
real formula yields the printed numbers.
"""
from __future__ import annotations

from typing import Any

import pytest

from hunt_core.prizrak import orchestrator as orch
from hunt_core.prizrak.config import PrizrakConfig

# _tier_trend reduces: ll/lh → bear, hh/hl → bull, non-empty w/o those → neutral.
_BEAR = {"ll": True}
_BULL = {"hh": True}
_NEUTRAL = {"ranging": True}  # non-empty → counts toward weight_available


def _run_bias(votes: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Drive the REAL _htf_bias, stubbing only structure DETECTION (not scoring)."""
    def _fake_tier_structure(_ohlcv: Any, tier: Any, *, cfg: Any) -> dict[str, Any]:
        tf = tier.timeframes[0]
        return dict(votes.get(tf, _NEUTRAL))

    monkeypatch.setattr(orch, "_tier_structure", _fake_tier_structure)
    cfg = PrizrakConfig.load()
    ohlcv_by_tf = {tf: [[0, 1, 1, 1, 1, 1]] for tf in ("1w", "1d", "4h", "1h")}
    return orch._htf_bias({}, cfg=cfg, ohlcv_by_tf=ohlcv_by_tf)


def test_score_from_1w_1d_bear(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1w↓ 1d↓ 4h→ 1h→ : net = -(0.35+0.30) = -0.65, weight_available = 1.00.
    out = _run_bias({"1w": _BEAR, "1d": _BEAR, "4h": _NEUTRAL, "1h": _NEUTRAL}, monkeypatch)
    assert out["score"] == pytest.approx(-0.65, abs=1e-9)
    assert out["bias"] == "short"
    assert out["weight_available"] == pytest.approx(1.0, abs=1e-9)


def test_bias_dict_carries_cfg_weights_for_render(monkeypatch: pytest.MonkeyPatch) -> None:
    # #5: the render surfaces per-TF weights so -0.60 reads as weighted, not a flat
    # average. Those weights must come from cfg (no drift) and match the score math.
    cfg = PrizrakConfig.load()
    out = _run_bias({"1w": _BEAR, "1d": _BEAR, "4h": _NEUTRAL, "1h": _NEUTRAL}, monkeypatch)
    weights = out["weights"]
    assert weights == {
        "1w": round(cfg.htf_1w_weight, 2),
        "1d": round(cfg.htf_1d_weight, 2),
        "4h": round(cfg.htf_4h_weight, 2),
        "1h": round(cfg.htf_1h_weight, 2),
    }
    # The rendered weights must explain the score: -(1w+1d) = -0.65.
    assert -(weights["1w"] + weights["1d"]) == pytest.approx(out["score"], abs=1e-9)


def test_score_from_1w_1d_1h_bear(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1w↓ 1d↓ 4h→ 1h↓ : net = -(0.35+0.30+0.10) = -0.75.
    out = _run_bias({"1w": _BEAR, "1d": _BEAR, "4h": _NEUTRAL, "1h": _BEAR}, monkeypatch)
    assert out["score"] == pytest.approx(-0.75, abs=1e-9)
    assert out["bias"] == "short"


def test_equal_weight_assumption_would_have_been_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guards the reconciliation itself: a flat {−1,0} average over 4 TFs predicts
    # −0.50 for the 1w+1d-bear state; the REAL score is −0.65. If these ever
    # coincide, the weights were flattened and the header's TF list is misleading.
    out = _run_bias({"1w": _BEAR, "1d": _BEAR, "4h": _NEUTRAL, "1h": _NEUTRAL}, monkeypatch)
    flat_average = -2.0 / 4.0  # −0.50
    assert out["score"] != pytest.approx(flat_average, abs=1e-9)


def test_htf_weights_monotone_by_seniority() -> None:
    # G-14: senior TFs must dominate (method: "анализ со старших ТФ к младшим").
    # A non-monotone set (4h > 1d) let the medium TF outvote the higher one.
    cfg = PrizrakConfig.load()
    assert cfg.htf_1w_weight >= cfg.htf_1d_weight >= cfg.htf_4h_weight >= cfg.htf_1h_weight


def test_accumulation_shortcircuits_to_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    # 4h↑ vs 1w/1d↓ = accumulation → neutral (no weighted vote), per methodology.
    out = _run_bias({"1w": _BEAR, "1d": _BEAR, "4h": _BULL, "1h": _NEUTRAL}, monkeypatch)
    assert out["bias"] == "neutral"
    assert out["score"] == pytest.approx(0.0, abs=1e-9)
    # The early (accumulation) return must still carry weights + struct_by_tf so the
    # МТФ render's per-TF weight suffixes don't vanish on this path (dead-render gap).
    assert out.get("weights") and "struct_by_tf" in out
