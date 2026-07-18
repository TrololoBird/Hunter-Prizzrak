"""F2 (стр.33): стоп несёт ОБОСНОВАНИЕ — за что спрятан + буфер + безопасный/рисковый,
не голую цену. Трейдер сверяет это с формулировкой канала («стоп за структуру 1-3%»)."""
from __future__ import annotations

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import _geometry_from_zone, _structural_stop

CFG = PrizrakConfig.load()


def test_structural_stop_returns_anchor_kind() -> None:
    zone = {"lo": 100.0, "hi": 104.0}
    _, kind = _structural_stop("long", entry=102.0, zone=zone, buffer_pct=0.02)
    assert kind == "structure"


def test_wick_anchor_named_on_3plus_prokol() -> None:
    zone = {"lo": 100.0, "hi": 104.0, "ext_lo": 98.5, "lo_touches": 3}
    stop, kind = _structural_stop("long", entry=102.0, zone=zone, buffer_pct=0.02)
    assert kind == "wick" and stop == 98.5 * 0.98


def test_fallback_anchor_is_risky() -> None:
    _, kind = _structural_stop("long", entry=100.0, zone=None, buffer_pct=0.02)
    assert kind == "entry_fallback"


def test_geometry_propagates_anchor_and_buffer() -> None:
    """Якорь и буфер доходят до result (а значит до summary → карточки)."""
    zone = {"tf": "4h", "lo": 100.0, "hi": 104.0}
    bars = [[i * 3_600_000, 100.0, 100.1, 99.9, 100.0, 100.0] for i in range(60)]
    geo = _geometry_from_zone(
        direction="long", entry=100.0, ohlcv_by_tf={"4h": bars}, cfg=CFG,
        swing_levels=[130.0], min_tf="4h", zone=zone,
    )
    # либо сетап (geo с якорем), либо abstain — но если geo есть, якорь и буфер обязаны быть
    if geo is not None:
        assert geo.get("stop_anchor") in ("structure", "wick", "neighbor", "entry_fallback")
        assert geo.get("stop_buffer_pct") == round(CFG.stop_buffer_pct * 100, 2)
