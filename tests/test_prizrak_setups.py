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


def test_cotrend_perezakup_survives_and_counter_trend_one_is_dropped() -> None:
    """★ Со-трендовый перезакуп — ПЕРВИЧНЫЙ лимит автора: прошлая реакция его НЕ дисквалифицирует
    (видео уточняет стр.31), он обязан остаться в карте чистым. Контр-трендовый — наоборот,
    из карты УБИРАЕТСЯ: помечать его «по факту» и тут же предлагать в плане значило печатать
    «бери» и «не бери» в одном сообщении (замерено на живом BTC 2026-07-26).

    Этот тест и держит границу: фильтр обязан быть ИЗБИРАТЕЛЬНЫМ, а не сплошным."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10, vol=500.0)
    bars.append(_bar(110.0, 130.0, 110.0, 128.0))
    bars += [_bar(128.0, 129.0, 127.0, 128.0) for _ in range(4)]
    common = {"4h": bars}
    co = build_symbol_setups(common, price=128.0, cfg=_CFG, structure={"htf_bias": {"bias": "long"}})
    pk_co = co["horizons"]["local"]["perezakup"]
    assert pk_co["by_fact"] is False and pk_co["fact_reason"] == ""  # co-trend → clean limit
    against = build_symbol_setups(common, price=128.0, cfg=_CFG, structure={"htf_bias": {"bias": "short"}})
    assert "perezakup" not in (against["horizons"].get("local") or {})
    assert (against.get("dropped_by_fact") or {}).get("против тренда", 0) >= 1


def test_near_resistance_surfaces_and_counter_trend_is_dropped() -> None:
    """Бокс вокруг цены даёт шорт у потолка, но отработанный уровень убирается из карты со счётом.

    Избирательность фильтра держит ``test_cotrend_perezakup_survives_and_counter_trend_one_is_dropped``:
    со-трендовый перезакуп в той же машинерии обязан ОСТАТЬСЯ."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10)
    bars.append(_bar(105.0, 105.5, 104.5, 105.0))  # sit INSIDE → straddle
    out = build_symbol_setups(
        {"4h": bars}, price=105.0, cfg=_CFG,
        structure={"htf_bias": {"bias": "long"}},  # a short here is counter-trend
    )
    # Потолок этого бокса цена трогала десять раз, то есть уровень ОТРАБОТАН — лимитом он больше
    # не торгуется (стр.31). В карту он не попадает, но и не исчезает молча: причина сосчитана,
    # и карточка скажет «чистых зон нет — отработан: N» вместо того, чтобы просто замолчать (I-6).
    assert not (out["horizons"].get("local") or {}).get("short")
    assert sum((out.get("dropped_by_fact") or {}).values()) >= 1


def test_perezakup_never_reports_a_poc_outside_its_own_zone() -> None:
    """★ I-6: the rendered ПОК must lie INSIDE the зона it labels — never a foreign number.

    Regression (measured on ARPA, 2026-07-25): the base's box sat below price, but the structure
    bars it spans wicked above, so VRVP returned VAL/VAH/ПОК above price. ``_perezakup_view`` clipped
    the entry but printed the raw ПОК and then re-ordered the edges with min/max, yielding
    «перезакуп 0.008320–0.008686 (ПОК 0.01036)» — a buy zone at/above spot, anchored on a ПОК 19%
    outside it. The sibling ``_zone_view`` had always guarded this; only this path did not.
    """
    checked = 0
    for price in (105.0, 109.0, 111.0, 128.0):
        bars = _flat_base(lo=100.0, hi=110.0, cycles=10, vol=500.0)
        bars.append(_bar(price, price * 1.002, price * 0.998, price))
        hz = build_symbol_setups({"4h": bars}, price=price, cfg=_CFG)["horizons"].get("local")
        pk = (hz or {}).get("perezakup")
        if pk is None:
            continue  # no re-buy band left below price — a legal, fail-loud outcome
        checked += 1
        lo, hi = float(pk["lo"]), float(pk["hi"])
        assert lo < hi, f"degenerate зона at price={price}: {pk}"
        assert hi <= price + 1e-9, f"перезакуп must sit BELOW price ({price}): {pk}"
        if pk["poc"] is not None:
            assert lo <= float(pk["poc"]) <= hi, f"ПОК outside its зона at price={price}: {pk}"
        assert lo <= float(pk["entry"]) <= hi, f"entry outside its зона at price={price}: {pk}"
    assert checked, "vacuous test — no fixture produced a перезакуп to check"
