"""Multi-horizon ПОК-anchored setups — the derivation behind the Prizrak-post format.

Pins the behaviours the rewrite exists for (razbor prizrak_bch_praktikum §7, methodology §7):
* 🟢 перезакуп anchors to the volume ПОК (стр.30), NOT the box top — the measured BCH 218→196-class
  fix (here: entry sits at the ПОК inside the base, never the box edge);
* 🔴 near resistance surfaces even when the whole range straddles price;
* «по факту» flags a counter-trend / worked zone instead of dropping it;
* nothing is fabricated — empty horizons when there is no qualifying accumulation.
"""
from __future__ import annotations

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.setups import build_symbol_setups

_CFG = PrizrakConfig.load()


def _bar(o: float, h: float, low: float, c: float, v: float = 100.0) -> list[float]:
    return [0.0, o, h, low, c, v]


def _flat_base(*, lo: float, hi: float, cycles: int, vol: float = 100.0) -> list[list[float]]:
    """A clean flat: price walks boundary→boundary, giving both clusters their pivots + volume."""
    bars: list[list[float]] = []
    mid = (lo + hi) / 2
    for _ in range(cycles):
        bars.append(_bar(mid, hi * 1.001, mid * 0.999, hi * 0.999, vol))
        bars.append(_bar(hi * 0.999, hi, mid, mid, vol))
        bars.append(_bar(mid, mid * 1.001, lo * 0.999, lo * 1.001, vol))
        bars.append(_bar(lo * 1.001, mid, lo, mid, vol))
    return bars


def test_empty_when_no_zones() -> None:
    """I-6: no accumulation → empty horizons, never a fabricated zone."""
    flat = [_bar(100.0 + i * 0.01, 100.0 + i * 0.01, 100.0 + i * 0.01, 100.0 + i * 0.01) for i in range(150)]
    out = build_symbol_setups({"4h": flat}, price=101.0, cfg=_CFG)
    assert out["horizons"] == {}


def test_perezakup_anchors_to_poc_not_box_top() -> None:
    """★ The core fix: перезакуп entry sits at the volume ПОК inside the base, not the box top."""
    # A base 100–110 well below price → its value area / ПОК become the re-buy anchor.
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10, vol=500.0)
    bars.append(_bar(110.0, 130.0, 110.0, 128.0))  # break up and away
    bars += [_bar(128.0, 129.0, 127.0, 128.0) for _ in range(4)]  # sit at 128, price above the base
    out = build_symbol_setups({"4h": bars}, price=128.0, cfg=_CFG)
    hz = out["horizons"].get("local")
    assert hz is not None, out
    pk = hz.get("perezakup")
    assert isinstance(pk, dict), f"expected a перезакуп below price: {hz}"
    # ПОК is set and the entry anchors to it (inside the base), NOT the box top edge.
    assert pk["poc"] is not None
    assert 100.0 <= pk["entry"] <= 110.0 + 1e-6
    assert pk["entry"] <= 128.0  # a re-buy lives below price
    assert abs(pk["entry"] - pk["poc"]) < 1e-6  # entry IS the ПОК anchor


def test_deep_base_surfaces_with_extended_lookback() -> None:
    """★ A deep-but-recent base (>120 bars back) still surfaces — the fix for «BCH 🟢196 clipped out
    of a 20-day window». A win=120 map would show nothing below price; the level-map window catches it."""
    base = _flat_base(lo=100.0, hi=110.0, cycles=10, vol=500.0)  # ~40 bars of clean base
    base.append(_bar(110.0, 130.0, 110.0, 128.0))  # break up and away
    # 200 bars oscillating well above the base → the base sits ~200 bars back: OUT of a 120-window,
    # inside the level-map window only.
    run = [_bar(128.0, 129.5, 126.5, 128.0) for _ in range(200)]
    out = build_symbol_setups({"4h": base + run}, price=128.0, cfg=_CFG)
    pk = out["horizons"].get("local", {}).get("perezakup")
    assert isinstance(pk, dict), "deep base must still surface with the extended level-map lookback"
    assert pk["poc"] is not None and 100.0 <= pk["poc"] <= 110.0 + 1e-6


def test_cotrend_perezakup_not_flagged_even_if_worked() -> None:
    """★ A co-trend ПОК re-buy is the author's PRIMARY limit — a prior reaction (worked) must NOT
    flag it «по факту» (video refines стр.31). Only counter-trend перезакуп is «по факту»."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10, vol=500.0)
    bars.append(_bar(110.0, 130.0, 110.0, 128.0))
    bars += [_bar(128.0, 129.0, 127.0, 128.0) for _ in range(4)]
    common = {"4h": bars}
    co = build_symbol_setups(common, price=128.0, cfg=_CFG, structure={"htf_bias": {"bias": "long"}})
    pk_co = co["horizons"]["local"]["perezakup"]
    assert pk_co["by_fact"] is False and pk_co["fact_reason"] == ""  # co-trend → clean limit
    against = build_symbol_setups(common, price=128.0, cfg=_CFG, structure={"htf_bias": {"bias": "short"}})
    pk_ag = against["horizons"]["local"]["perezakup"]
    assert pk_ag["by_fact"] is True and pk_ag["fact_reason"] == "против тренда"  # add longs «по факту»


def test_near_resistance_surfaces_and_by_fact_marks_counter_trend() -> None:
    """A base straddling price yields a 🔴 short at the ceiling; a counter-trend zone is «по факту»."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10)
    bars.append(_bar(105.0, 105.5, 104.5, 105.0))  # sit INSIDE → straddle
    out = build_symbol_setups(
        {"4h": bars}, price=105.0, cfg=_CFG,
        structure={"htf_bias": {"bias": "long"}},  # a short here is counter-trend
    )
    hz = out["horizons"].get("local")
    assert hz is not None
    shorts = hz.get("short") or []
    assert shorts, f"near resistance (ceiling ≈110) must surface: {hz}"
    assert min(abs(z["hi"] - 110.0) for z in shorts) < 1.5
    # A short against a long bias is «по факту», not a set-and-forget limit.
    assert all(z["by_fact"] for z in shorts)
