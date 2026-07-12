"""Level strength is ranked by traded VOLUME, not touch count.

Course, с. 22: «Сила уровня определяется ТФ и объёмом (смотрим по VRVP), иногда в
маленькой часовой наторговке может быть объём больше, чем в 4ч-1д накоплении — от таких
структур стараемся брать». Touch count is only the structure-validity gate (4+ points);
among valid bases the stronger one is the one more was traded through.
"""

from __future__ import annotations

from hunt_core.prizrak.accumulation import find_accumulation_zone, find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.structure import bars_from_ohlcv

CFG = PrizrakConfig.load()


def _box(hi_px: float, lo_px: float, cycles: int, ts0: int, vol: float) -> list[list[float]]:
    """Oscillating bars forming a detectable накопление, each bar carrying `vol`."""
    mid = (hi_px + lo_px) / 2
    pattern = [lo_px, mid, hi_px, mid]
    step = 60 * 60_000
    return [
        [ts0 + i * step, c, c + 0.05, c - 0.05, c, vol]
        for i, c in enumerate(pattern[j % 4] for j in range(cycles * 4))
    ]


def test_higher_volume_fewer_touches_zone_wins() -> None:
    """Two valid bases: a many-touch low-volume one, and a fewer-touch high-volume one."""
    # Base A: lots of touches, thin volume, lower price band.
    base_a = _box(100.0, 98.0, 12, 0, vol=10.0)      # 48 bars, ~24 touches, vol 10/bar
    # Base B: fewer touches, heavy volume, higher price band.
    base_b = _box(120.0, 118.0, 6, 48 * 60 * 60_000, vol=500.0)  # 24 bars, ~12 touches, vol 500/bar
    bars = bars_from_ohlcv(base_a + base_b)
    zones = find_accumulation_zones(bars, tf="1h", cfg=CFG, max_zones=8)
    assert len(zones) >= 2
    # Every zone must carry a volume figure.
    assert all("zone_volume" in z for z in zones)
    top = find_accumulation_zone(bars, tf="1h", cfg=CFG)
    # The heavy-volume base B (118-120) must be chosen over the many-touch base A.
    assert 117.0 <= top["lo"] and top["hi"] <= 121.0, f"low-volume base won: {top}"
    # And it is genuinely the max-volume zone, not merely the most-touched.
    assert top["zone_volume"] == max(z["zone_volume"] for z in zones)
    a = next(z for z in zones if z["hi"] <= 101.0)
    assert top["zone_volume"] > a["zone_volume"]
    assert top["touches"] < a["touches"]  # fewer touches, yet stronger — the с.22 point
