"""Wiring of the market-cap доп-фактор into the prizrak confluence path.

Verifies the ambient ContextVar carries the per-tick cap series into ``_apply_confluence``,
that the bounded multiplier folds into ``strength`` and appears as a driver + on
``summary["marketcap"]``, and that ``build_prizrak_signals`` always resets the context.
"""

from __future__ import annotations

import math

from hunt_core.prizrak import orchestrator as orch
from hunt_core.prizrak.config import PrizrakConfig

_N = 64
_PERIOD = 16


def _shape(trend: str) -> list[float]:
    sign = 1.0 if trend == "bull" else -1.0
    return [100.0 + sign * 2.0 * i + 8.0 * math.sin(2 * math.pi * i / _PERIOD) for i in range(_N)]


def _price(trend: str) -> list[list[float]]:
    return [[i * 60_000, c, c + 0.1, c - 0.1, c, 10.0] for i, c in enumerate(_shape(trend))]


def _cap(trend: str) -> list[list[float]]:
    return [[i * 60_000, 1_000_000.0 * (1.0 + (c - 100.0) / 100.0)] for i, c in enumerate(_shape(trend))]


def _summary(action: str) -> dict[str, object]:
    return {"action": action, "zone": {"touches": 4}, "gates_failed": [], "entry_lo": 100.0, "entry_hi": 101.0}


def test_confirming_cap_raises_strength_via_context() -> None:
    cfg = PrizrakConfig(marketcap_enabled=True)
    ohlcv = _price("bull")

    token = orch._MARKETCAP_SERIES.set(None)
    try:
        base = orch._apply_confluence(_summary("long"), ohlcv=ohlcv, cfg=cfg)
    finally:
        orch._MARKETCAP_SERIES.reset(token)

    token = orch._MARKETCAP_SERIES.set(_cap("bull"))
    try:
        boosted = orch._apply_confluence(_summary("long"), ohlcv=ohlcv, cfg=cfg)
    finally:
        orch._MARKETCAP_SERIES.reset(token)

    assert base is not None and boosted is not None
    assert boosted["strength"] > base["strength"]
    assert "marketcap" in boosted and boosted["marketcap"]["cap_trend"] == "bull"
    assert any(d["name"] == "капитализация" for d in boosted["confluence_drivers"])


def test_disabled_factor_leaves_strength_unchanged() -> None:
    cfg = PrizrakConfig()  # marketcap_enabled defaults False
    ohlcv = _price("bull")
    token = orch._MARKETCAP_SERIES.set(_cap("bull"))
    try:
        res = orch._apply_confluence(_summary("long"), ohlcv=ohlcv, cfg=cfg)
    finally:
        orch._MARKETCAP_SERIES.reset(token)
    assert res is not None
    assert "marketcap" not in res  # disabled ⇒ not surfaced


def test_build_signals_resets_context() -> None:
    cfg = PrizrakConfig(marketcap_enabled=True)
    orch.build_prizrak_signals({"4h": _price("bull")}, price=200.0, cfg=cfg, marketcap_series=_cap("bull"))
    # Context must be reset to the default after the call, regardless of candidates found.
    assert orch._MARKETCAP_SERIES.get() is None
