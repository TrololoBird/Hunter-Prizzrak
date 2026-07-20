"""Шаг 0 плана: при WAIT бот объясняет ПОЧЕМУ нет сделки, с числами — не молчит.

Доминирующий исход сверки с каналом = расхождение молчанием (канал дал сетап, бот не дал).
Раньше гейты вычисляли причину и выбрасывали (return None), а gates_failed был мёртвым
orphan-read (всегда []). Теперь причина с числами доходит до карточки (/signal) и до
calibration-ролл-апа. Каждый тест падает на дефектном (прежнем) коде.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.engines.calibration import aggregate_calibration
from hunt_core.prizrak.format_telegram import _abstain_reason_line
from hunt_core.prizrak.orchestrator import _geometry_from_zone, _ABSTAIN_SINK

CFG = PrizrakConfig.load()


def _bars(px: float, n: int = 60) -> list[list[float]]:
    return [[i * 3_600_000, px, px * 1.001, px * 0.999, px, 100.0] for i in range(n)]


def test_rr_below_floor_notes_reason_with_numbers() -> None:
    """RR-гейт отклоняет через None — но теперь пишет структурную причину с числами."""
    sink: list[dict[str, Any]] = []
    token = _ABSTAIN_SINK.set(sink)
    try:
        # зона с целью слишком близко → RR ниже пола: заставим _structural_targets вернуть
        # цель через swing_levels совсем рядом с entry.
        zone = {"tf": "4h", "lo": 100.0, "hi": 104.0}
        _geometry_from_zone(
            direction="long", entry=100.0, ohlcv_by_tf={"4h": _bars(100.0)},
            cfg=CFG, swing_levels=[100.5], min_tf="4h", zone=zone,
        )
    finally:
        _ABSTAIN_SINK.reset(token)
    rr = [r for r in sink if r["reason"] == "rr_below_floor"]
    assert rr, f"причина rr_below_floor не записана: {sink}"
    r = rr[0]
    assert r["min_rr"] == CFG.min_rr and "rr" in r and "stop" in r and "tp1" in r


def test_no_target_notes_reason() -> None:
    sink: list[dict[str, Any]] = []
    token = _ABSTAIN_SINK.set(sink)
    try:
        _geometry_from_zone(
            direction="long", entry=100.0, ohlcv_by_tf={"4h": _bars(100.0)},
            cfg=CFG, swing_levels=None, min_tf="4h", zone={"tf": "4h", "lo": 98.0, "hi": 102.0},
        )
    finally:
        _ABSTAIN_SINK.reset(token)
    assert any(r["reason"] == "no_structural_target" for r in sink), sink


def test_no_sink_installed_is_noop() -> None:
    """Без установленного sink точки abstain ничего не пишут (тесты/вызовы без трейса)."""
    # просто не должно падать
    _geometry_from_zone(
        direction="long", entry=100.0, ohlcv_by_tf={"4h": _bars(100.0)},
        cfg=CFG, swing_levels=None, min_tf="4h", zone={"tf": "4h", "lo": 98.0, "hi": 102.0},
    )


def test_reason_line_rr_is_human_readable() -> None:
    # _abstain_reason_line takes the abstain reasons list directly (PrizrakOutput.abstain).
    abstain = [{"reason": "rr_below_floor", "rr": 2.3, "min_rr": 3.0,
                "stop": 0.412, "tp1": 0.455, "buffer_pct": 1.8}]
    line = _abstain_reason_line(abstain)
    assert line is not None and "RR 2.3 < 3.0" in line and "TP1" in line


def test_reason_line_priority_rr_over_veto() -> None:
    """RR (почти прошёл) информативнее вето — печатается он."""
    abstain = [
        {"reason": "htf_counter_trend_no_slom", "htf_bias": "short"},
        {"reason": "rr_below_floor", "rr": 2.8, "min_rr": 3.0, "stop": 1.0, "tp1": 1.2, "buffer_pct": 2.0},
    ]
    assert "RR 2.8" in (_abstain_reason_line(abstain) or "")


def test_reason_line_empty_is_none() -> None:
    assert _abstain_reason_line([]) is None


def test_calibration_revives_from_wait_ticks() -> None:
    """Регрессия: WAIT-тики раньше отбрасывались (summary=None → continue), поэтому
    gate_failures всегда пуст. Теперь WAIT несёт abstain-причины как gates_failed."""
    summaries = [
        {"symbol": "BTC", "action": "long", "gates_failed": [], "strength": 0.6, "rr_primary": 2.5},
        {"symbol": "ETH", "action": "wait", "gates_failed": ["rr_below_floor"]},
        {"symbol": "SOL", "action": "wait", "gates_failed": ["rr_below_floor", "no_structural_target"]},
    ]
    rep = aggregate_calibration(summaries)
    assert rep["gate_failures"], "диагностика мертва — gate_failures пуст"
    assert rep["top_blockers"][0] == "rr_below_floor"


def test_load_deep_tick_summaries_includes_wait_ticks(tmp_path, monkeypatch) -> None:
    """Регрессия именно в load: WAIT-тик (prizrak_summary=None) раньше отбрасывался, из-за
    чего diagnostic никогда не видел причин. Теперь его abstain доходит как gates_failed."""
    import json
    import hunt_core.prizrak.engines.calibration as cal

    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        json.dumps({"ts": 1, "symbol": "eth", "prizrak_summary": None,
                    "prizrak_abstain": [{"reason": "rr_below_floor"}, {"reason": "rr_below_floor"},
                                        {"reason": "no_structural_target"}]}) + "\n"
        + json.dumps({"ts": 2, "symbol": "btc",
                      "prizrak_summary": {"action": "long", "strength": 0.6}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cal, "ANALYST_TICKS_JSONL", ticks)
    out = cal.load_deep_tick_summaries(limit=10)
    wait = [r for r in out if r.get("action") == "wait"]
    assert wait, "WAIT-тик отброшен — регрессия load"
    assert set(wait[0]["gates_failed"]) == {"rr_below_floor", "no_structural_target"}
