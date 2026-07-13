"""Pattern A must re-locate its frozen impulse bar by timestamp, not raw index.

The meso OHLCV window is refetched each scan cycle as a fixed-length rolling
window, so a stored integer impulse index drifts one bar later every time a bar
closes (SCAN-1). _relocate_idx_by_ts recovers the true current index from the
bar's frozen open-time, honouring the state timestamp-freeze guarantee.
"""
from __future__ import annotations

import polars as pl

from hunt_core.scanner.detect.patterns import _relocate_idx_by_ts


def _df(ts_values: list[float]) -> pl.DataFrame:
    n = len(ts_values)
    return pl.DataFrame(
        {
            "ts": ts_values,
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "volume": [1.0] * n,
        }
    )


def test_index_follows_bar_after_window_rolls() -> None:
    # Impulse bar has ts=1005, at index 5 in the original window.
    orig = _df([1000.0 + i for i in range(10)])
    assert _relocate_idx_by_ts(orig, 1005.0) == 5
    # Two bars close: window rolls forward by 2 (oldest two drop, two new appear).
    rolled = _df([1002.0 + i for i in range(10)])
    # The SAME bar (ts=1005) is now at index 3 — a raw index of 5 would be wrong.
    assert _relocate_idx_by_ts(rolled, 1005.0) == 3


def test_none_when_bar_rolled_off() -> None:
    rolled = _df([2000.0 + i for i in range(10)])
    assert _relocate_idx_by_ts(rolled, 1005.0) is None


def test_none_on_missing_ts_or_column() -> None:
    assert _relocate_idx_by_ts(_df([1.0, 2.0]), None) is None
    assert _relocate_idx_by_ts(pl.DataFrame({"close": [1.0]}), 1.0) is None
