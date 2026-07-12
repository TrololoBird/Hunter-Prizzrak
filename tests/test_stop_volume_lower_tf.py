"""Стоповый объём is detected on ТФ-1, not the move's own TF.

Course, с. 34: «это такое же накопление (база), но на более мелком ТФ, чем основное
движение актива, и обычно более плотное». Sliding a window over the move's own TF
collapses that base into a couple of candles; the lower TF is where it is visible.
"""

from __future__ import annotations

from hunt_core.prizrak.orchestrator import _LOWER_TF, _stop_volume_bars


def _row(ts: int, price: float) -> list[float]:
    return [ts, price, price + 1, price - 1, price, 100.0]


# 4h zone spanning ts 300..600, indices 3..6 in the 4h series.
OHLCV_4H = [_row(ts, 100.0) for ts in range(0, 1000, 100)]
ZONE = {"lo": 99.0, "hi": 101.0, "first_touch_idx": 3, "last_touch_idx": 6}
# 1h bars every 25 units; those inside [300, 600] should be selected.
OHLCV_1H = [_row(ts, 100.0) for ts in range(0, 1000, 25)]


def test_selects_lower_tf_within_zone_time_window() -> None:
    bars = _stop_volume_bars(ZONE, tf="4h", ohlcv=OHLCV_4H, ohlcv_by_tf={"1h": OHLCV_1H})
    assert bars is not OHLCV_4H
    assert all(300 <= r[0] <= 600 for r in bars)
    assert len(bars) >= 8


def test_falls_back_when_lower_tf_absent() -> None:
    bars = _stop_volume_bars(ZONE, tf="4h", ohlcv=OHLCV_4H, ohlcv_by_tf={})
    assert bars is OHLCV_4H


def test_falls_back_when_zone_span_missing() -> None:
    zone = {"lo": 99.0, "hi": 101.0}
    bars = _stop_volume_bars(zone, tf="4h", ohlcv=OHLCV_4H, ohlcv_by_tf={"1h": OHLCV_1H})
    assert bars is OHLCV_4H


def test_falls_back_when_lower_tf_window_too_thin() -> None:
    sparse_1h = [_row(ts, 100.0) for ts in (310, 320, 330)]  # only 3 bars in window
    bars = _stop_volume_bars(ZONE, tf="4h", ohlcv=OHLCV_4H, ohlcv_by_tf={"1h": sparse_1h})
    assert bars is OHLCV_4H


def test_ladder_maps_each_tf_one_step_down() -> None:
    assert _LOWER_TF == {"15m": "5m", "1h": "15m", "4h": "1h", "1d": "4h", "1w": "1d"}
    assert "5m" not in _LOWER_TF  # nothing below the lowest fetched TF
