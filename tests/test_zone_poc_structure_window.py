"""POC must be profiled over the накопление's own bars, not the whole lookback.

Course, с. 26: «Натягивая профиль на структуру — важно захватить все свечи структуры».
A profile taken over the full tier lookback is a POC of the lookback, not of the zone.
"""

from __future__ import annotations

from hunt_core.prizrak.poc import zone_poc

# 30 bars of loud, high-volume trend far above the zone, then 20 quiet bars forming a
# tight накопление around 100. The zone's POC must come from the накопление bars.
_TREND = [[i, 200.0, 205.0, 195.0, 200.0, 10_000.0] for i in range(30)]
_BASE = [[30 + i, 100.0, 101.0, 99.0, 100.0, 50.0] for i in range(20)]
OHLCV = _TREND + _BASE

ZONE = {"lo": 99.0, "hi": 101.0, "first_touch_idx": 30, "last_touch_idx": 49}


def test_poc_is_taken_from_the_structure_bars() -> None:
    out = zone_poc(OHLCV, zone=ZONE)
    assert out["poc"] is not None
    assert 99.0 <= out["poc"] <= 101.0, f"POC {out['poc']} came from outside the structure"


def test_full_lookback_profile_would_miss_the_zone() -> None:
    """Guard the premise the module actually relies on: the FULL window profiles the loud trend.

    Это и есть цена ошибки — ПОК всего окна вместо ПОКа структуры. Раньше здесь стоял более
    слабый guard: «зона БЕЗ индексов span'а промахивается». Он держался лишь потому, что
    ``_structure_bars`` без индексов сдавался на всё окно, — а именно эта сдача и оказалась
    дефектом: на живом BTC 4h огибающая касаний занимала 79–96% окна, так что «индексы есть»
    от «индексов нет» не отличалось ничем. Теперь структура ищется по самой полосе, поэтому
    случай без индексов проверяется как ПОЛОЖИТЕЛЬНЫЙ (тест ниже), а промах окна — здесь.
    """
    out = zone_poc(OHLCV, zone=None)
    assert out["poc"] > 101.0


def test_zone_without_span_indices_still_profiles_its_structure() -> None:
    """Полоса сама задаёт структуру: непрерывная серия свечей, торгующих внутри неё."""
    out = zone_poc(OHLCV, zone={"lo": 99.0, "hi": 101.0})
    assert out["poc"] is not None
    assert 99.0 <= out["poc"] <= 101.0, f"POC {out['poc']} взят не со свечей структуры"


def test_position_in_zone_reported_when_poc_sits_inside() -> None:
    out = zone_poc(OHLCV, zone=ZONE)
    assert 0.0 <= out["poc_position_in_zone"] <= 1.0


def test_degenerate_span_falls_back_to_full_window() -> None:
    """Too few structure bars to profile — use what we have rather than return nothing."""
    tiny = {"lo": 99.0, "hi": 101.0, "first_touch_idx": 48, "last_touch_idx": 49}
    assert zone_poc(OHLCV, zone=tiny)["poc"] is not None


def test_out_of_range_indices_fall_back_to_full_window() -> None:
    bad = {"lo": 99.0, "hi": 101.0, "first_touch_idx": 10, "last_touch_idx": 9_999}
    assert zone_poc(OHLCV, zone=bad)["poc"] is not None
