from __future__ import annotations

from hunt_core.scanner.detect.events import (
    detect_absorption,
    detect_consecutive_impulse,
    detect_impulse,
    bullish_volume,
    ohlcv_to_df,
)


def _flat(n: int, price: float = 100.0, start_t: int = 0):
    return [[start_t + i, price, price + 0.5, price - 0.5, price, 10.0] for i in range(n)]


def test_absorption_false_when_price_never_retraces():
    # Green impulse then price holds high — nothing absorbs it. Must be False.
    # Regression: slicing from impulse_idx (inclusive) let the impulse bar's own
    # low count as a retrace, saturating the ratio near 1.0 (always True).
    rows = _flat(22)
    rows.append([22, 100.0, 112.0, 100.0, 112.0, 50.0])  # green impulse
    rows += [[23 + i, 112.0, 112.5, 111.5, 112.0, 10.0] for i in range(8)]  # holds high
    df = ohlcv_to_df(rows)
    ok, idx = detect_impulse(df, direction="up")
    assert ok and idx is not None
    assert detect_absorption(df, idx) is False


def test_absorption_true_when_price_retraces_back():
    # Green impulse then price slides all the way back down — real absorption.
    rows = _flat(22)
    rows.append([22, 100.0, 112.0, 100.0, 112.0, 50.0])
    rows += [
        [23 + i, 112.0 - 1.5 * i, 112.3 - 1.5 * i, 111.7 - 1.5 * i, 112.0 - 1.5 * i, 10.0]
        for i in range(9)
    ]
    df = ohlcv_to_df(rows)
    ok, idx = detect_impulse(df, direction="up")
    assert ok and idx is not None
    assert detect_absorption(df, idx) is True


def test_absorption_false_when_impulse_is_last_bar():
    # Impulse on the final bar: no subsequent bars, nothing has retraced yet.
    rows = _flat(22)
    rows.append([22, 100.0, 112.0, 100.0, 112.0, 50.0])
    df = ohlcv_to_df(rows)
    assert detect_absorption(df, len(df) - 1) is False


def test_consecutive_impulse_up_rejects_downtrend():
    # Four consecutive RED candles must NOT seed an up (long) run.
    rows = _flat(15)
    p = 100.0
    for i in range(4):
        rows.append([15 + i, p, p + 0.2, p - 3.0, p - 3.0, 20.0])
        p -= 3.0
    df = ohlcv_to_df(rows)
    assert detect_consecutive_impulse(df, min_count=3, direction="up") == (False, None)


def test_consecutive_impulse_up_accepts_uptrend():
    rows = _flat(15)
    p = 100.0
    for i in range(4):
        rows.append([15 + i, p, p + 3.0, p - 0.2, p + 3.0, 20.0])
        p += 3.0
    df = ohlcv_to_df(rows)
    ok, idx = detect_consecutive_impulse(df, min_count=3, direction="up")
    assert ok and idx is not None


def test_consecutive_impulse_down_rejects_uptrend():
    rows = _flat(15)
    p = 100.0
    for i in range(4):
        rows.append([15 + i, p, p + 3.0, p - 0.2, p + 3.0, 20.0])
        p += 3.0
    df = ohlcv_to_df(rows)
    assert detect_consecutive_impulse(df, min_count=3, direction="down") == (False, None)


def test_bullish_volume_above_threshold():
    rows = _flat(25, price=100.0)
    df = ohlcv_to_df(rows)
    assert bullish_volume(df) is False  # flat volume, no spike

    # Last bar: volume spike 3x vs steady 10.0, closing UP -> bullish volume.
    # [ts, open, high, low, close, volume]
    rows[-1] = [rows[-1][0], 100.0, 105.0, 99.5, 104.0, 50.0]
    df = ohlcv_to_df(rows)
    assert bullish_volume(df) is True


def test_bullish_volume_rejects_spike_on_a_down_bar():
    """A volume spike on a RED bar is distribution, not «бычьи объёмы».

    In a post-pump window the highest-volume bar is the candle that absorbed the
    pump; a direction-blind z-score reported it as bullish confirmation.
    """
    rows = _flat(25, price=100.0)
    rows[-1] = [rows[-1][0], 100.0, 100.5, 95.0, 96.0, 50.0]  # red dump bar, huge volume
    df = ohlcv_to_df(rows)
    assert bullish_volume(df) is False


def test_bullish_volume_below_threshold():
    rows = _flat(25, price=100.0)
    rows[-1][5] = 12.0  # only 20% above 10.0 avg
    df = ohlcv_to_df(rows)
    assert bullish_volume(df, min_z=2.0) is False


def test_bullish_volume_too_few_bars():
    df = ohlcv_to_df(_flat(5))
    assert bullish_volume(df) is False
