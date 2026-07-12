"""Target ladder must not fall back to ТФ-2 accumulation zones.

Course, с. 24: a target should be the comparable-TF or nearest-higher level; «Уровни ТФ-1
... могут быть взяты как промежуточные цели», but «Уровни ТФ-2 (15м и ниже) обычно не
берутся в расчёт, т.к. на старшем ТФ их вообще "нет"». The old all-TF fallback pulled a
tiny 15m/5m zone as TP1 for a 4h/1d trade; the fallback now floors at ТФ-1.
"""

from __future__ import annotations

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import _structural_targets

CFG = PrizrakConfig.load()


def _box(hi_px: float, lo_px: float, cycles: int, ts0: int, step_min: int) -> list[list[float]]:
    """Oscillating bars that form a detectable накопление between lo_px and hi_px."""
    mid = (hi_px + lo_px) / 2
    pattern = [lo_px, mid, hi_px, mid]  # trough → up → peak → down (period 4)
    step = step_min * 60_000
    out: list[list[float]] = []
    for i in range(cycles * len(pattern)):
        c = pattern[i % len(pattern)]
        out.append([ts0 + i * step, c, c + 0.05, c - 0.05, c, 100.0])
    return out


def _flat(price: float, n: int, step_min: int) -> list[list[float]]:
    step = step_min * 60_000
    return [[i * step, price, price + 0.05, price - 0.05, price, 100.0] for i in range(n)]


def test_long_does_not_take_a_tf2_zone_as_target() -> None:
    """4h long entered at 110; the only structure ahead is a 15m box at ~150. Ignore it."""
    entry = 110.0
    ohlcv_by_tf = {
        "4h": _flat(100.0, 60, 240),   # no zone ahead of entry
        "1d": _flat(100.0, 150, 1440),
        "15m": _box(152.0, 148.0, 12, 0, 15),  # ТФ-2 box far above — must not leak in
    }
    targets = _structural_targets(ohlcv_by_tf, cfg=CFG, direction="long", entry=entry, min_tf="4h")
    assert all(t < 140.0 for t in targets), f"a ТФ-2 (15m) zone leaked in as a target: {targets}"


def test_tf1_zone_ahead_is_allowed_as_fallback_target() -> None:
    """A ТФ-1 (1h) box ahead is a permitted intermediate target."""
    entry = 110.0
    ohlcv_by_tf = {
        "4h": _flat(100.0, 60, 240),
        "1h": _box(131.0, 129.0, 12, 0, 60),  # 1h box ahead at ~130
    }
    targets = _structural_targets(ohlcv_by_tf, cfg=CFG, direction="long", entry=entry, min_tf="4h")
    assert targets, "a ТФ-1 target ahead should be usable as fallback"
    assert all(t > entry for t in targets)
    assert any(125.0 < t < 135.0 for t in targets), f"expected the 1h box near 130: {targets}"
