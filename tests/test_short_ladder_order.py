"""Interest-zone добор ladder must be nearest-first on BOTH sides.

_ladder sorted rungs by z["hi"] desc ("nearest first"), correct for LONG rungs
(below price → highest hi is nearest) but reversed for SHORT rungs (above price →
that put the FARTHEST rung as Д1). It now sorts by the side-aware `nearer`
function, so Д1 is the nearest limit for shorts too.
"""
from __future__ import annotations

from typing import Any

import pytest

from hunt_core.prizrak import orchestrator as orch
from hunt_core.prizrak.config import PrizrakConfig

_PRICE = 100.0


def _zone(lo: float, hi: float) -> dict[str, Any]:
    return {"lo": lo, "hi": hi, "touches": 5, "zone_volume": 1000.0, "width_pct": 1.0}


def _run(zones: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(orch, "find_accumulation_zones", lambda *a, **k: list(zones))
    # Bars must be WELL-FORMED even though this test only cares about rung order: the
    # zones now carry a стр.31 verdict, and computing it reads real OHLC. The old stub
    # ([{"t": 0}]) modelled a bar shape bars_from_ohlcv never emits, so it only "worked"
    # while nothing looked inside. Flat bars far from every zone ⇒ no reaction, no saw.
    monkeypatch.setattr(
        orch, "bars_from_ohlcv",
        lambda raw: [{"open": _PRICE, "high": _PRICE, "low": _PRICE,
                      "close": _PRICE, "volume": 1.0} for _ in raw],
    )
    cfg = PrizrakConfig.load()
    ohlcv = {"4h": [[0, 1, 1, 1, 1, 1]] * 130}
    return orch.compute_interest_zones(ohlcv, price=_PRICE, cfg=cfg, tf="4h")


def test_short_ladder_nearest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three short zones above price at increasing distance; Д1 must be the nearest.
    zones = [_zone(115.0, 116.0), _zone(105.0, 106.0), _zone(110.0, 111.0)]
    out = _run(zones, monkeypatch)
    ladder = out.get("short_ladder") or []
    assert len(ladder) == 3
    los = [z["lo"] for z in ladder]
    assert los == sorted(los)  # ascending → nearest (lowest) first
    assert los[0] == 105.0


def test_long_ladder_still_nearest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # Long zones below price; Д1 = nearest = highest hi (unchanged behavior).
    zones = [_zone(80.0, 81.0), _zone(94.0, 95.0), _zone(88.0, 89.0)]
    out = _run(zones, monkeypatch)
    ladder = out.get("long_ladder") or []
    assert len(ladder) == 3
    his = [z["hi"] for z in ladder]
    assert his == sorted(his, reverse=True)  # descending → nearest (highest) first
    assert his[0] == 95.0
