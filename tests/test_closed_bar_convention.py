"""Closed-bar convention pins (TF audit F1/F2).

Frames reaching the snapshot layer are CLOSED-BARS-ONLY — `finalize_kline_frame`
(= `_drop_incomplete_ohlcv_tail`) runs on the REST path, the WS cache and the
resampler, and the WS overlay carries a `_ClosedKlineBar`. So row -1 is the
NEWEST CLOSED bar.

The old `idx = -2 if closed` was a leftover from when frames carried a forming
bar; post-finalize it silently served the PREVIOUS closed bar to every
require_closed consumer — one full interval stale (up to 2h on 1h, 8h on 4h).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.features.snapshot import (
    _bar_close_time_ms,
    merge_ws_kline_closed,
    tf_snapshot_lite,
)


def _frame() -> pl.DataFrame:
    """Three closed 1h bars; the newest closed bar closes at 17:00Z."""
    return pl.DataFrame(
        {
            "time": [
                1_752_600_000_000,  # 15:00
                1_752_603_600_000,  # 16:00
                1_752_607_200_000,  # 17:00  <- newest closed
            ],
            "close_time": [
                1_752_603_599_999,  # 15:59:59.999
                1_752_607_199_999,  # 16:59:59.999
                1_752_610_799_999,  # 17:59:59.999
            ],
            "open": [100.0, 200.0, 300.0],
            "high": [101.0, 201.0, 301.0],
            "low": [99.0, 199.0, 299.0],
            "close": [100.5, 200.5, 300.5],
            "volume": [1.0, 2.0, 3.0],
        }
    )


def test_snapshot_reports_newest_closed_bar() -> None:
    """closed=True must NOT hand back the second-newest closed bar."""
    df = _frame()
    snap = tf_snapshot_lite(df, idx=-1)
    assert snap["close"] == 300.5, "must read the newest closed bar, not bar N-1"


def test_close_time_is_the_newest_closed_bar() -> None:
    df = _frame()
    # closed=True used to report the 16:59:59 close — one interval in the past,
    # which is exactly what the staleness/sync checks compared against.
    assert _bar_close_time_ms(df, closed=True) == 1_752_610_799_999
    assert _bar_close_time_ms(df, closed=False) == 1_752_610_799_999


def test_closed_and_live_describe_the_same_bar() -> None:
    """Post-finalize there is no forming bar, so the pair is one bar."""
    df = _frame()
    assert _bar_close_time_ms(df, closed=True) == _bar_close_time_ms(df, closed=False)


# ---------------------------------------------------------------------------
# WS overlay stale guard (was dead: compared keys neither side emits)
# ---------------------------------------------------------------------------


class _WsFeed:
    def __init__(self, overlay: dict[str, Any]) -> None:
        self._overlay = overlay

    def closed_kline_overlay(self, symbol: str, *, interval: str) -> dict[str, Any]:
        return self._overlay


def test_stale_ws_overlay_cannot_clobber_fresher_rest_bar() -> None:
    """A 40-min WS stall must not overwrite an advanced REST bar."""
    # REST base: 15m bar closing at 17:00Z
    tf: dict[str, Any] = {
        "15m_closed": {"close": 300.5, "close_time_ms": 1_752_610_799_999},
    }
    # WS overlay still holding a pre-stall bar opening at 15:45Z (closes 16:00Z)
    stale = {"close": 100.5, "closed_bar": True, "ws_open_ms": 1_752_602_700_000}
    merge_ws_kline_closed(tf, "BTCUSDT", _WsFeed(stale), tf_key="15m_closed")
    assert tf["15m_closed"]["close"] == 300.5, "stale WS bar must be rejected"


def test_fresh_ws_overlay_is_applied() -> None:
    tf: dict[str, Any] = {
        "15m_closed": {"close": 300.5, "close_time_ms": 1_752_607_199_999},
    }
    # WS bar opening 17:00Z → closes 17:15Z, newer than the base's 17:00Z close
    fresh = {"close": 400.5, "closed_bar": True, "ws_open_ms": 1_752_607_200_000}
    merge_ws_kline_closed(tf, "BTCUSDT", _WsFeed(fresh), tf_key="15m_closed")
    assert tf["15m_closed"]["close"] == 400.5


def test_overlay_seeds_empty_base() -> None:
    tf: dict[str, Any] = {"15m_closed": {"status": "empty"}}
    fresh = {"close": 400.5, "closed_bar": True, "ws_open_ms": 1_752_607_200_000}
    merge_ws_kline_closed(tf, "BTCUSDT", _WsFeed(fresh), tf_key="15m_closed")
    assert tf["15m_closed"]["close"] == 400.5


# 4h WS overlay — REST-independent freshening of the pinned 4h bar (klines.4h.stale fix).
# Same machinery as 15m; proves the 4h_closed→4h interval map + TF_MS["4h"] stale guard.
def test_fresh_ws_4h_overlay_is_applied() -> None:
    tf: dict[str, Any] = {
        "4h_closed": {"close": 300.5, "close_time_ms": 1_752_595_199_999},
    }
    # WS 4h bar opening when the base closed → overlay_ts = open + 4h > base close.
    fresh = {"close": 400.5, "closed_bar": True, "ws_open_ms": 1_752_595_200_000}
    merge_ws_kline_closed(tf, "BTCUSDT", _WsFeed(fresh), tf_key="4h_closed")
    assert tf["4h_closed"]["close"] == 400.5


def test_stale_ws_4h_overlay_cannot_clobber_fresher_rest_bar() -> None:
    tf: dict[str, Any] = {
        "4h_closed": {"close": 300.5, "close_time_ms": 1_752_609_599_999},
    }
    # WS bar from a prior 4h period: overlay_ts = old_open + 4h < advanced REST close.
    stale = {"close": 100.5, "closed_bar": True, "ws_open_ms": 1_752_580_800_000}
    merge_ws_kline_closed(tf, "BTCUSDT", _WsFeed(stale), tf_key="4h_closed")
    assert tf["4h_closed"]["close"] == 300.5, "stale WS 4h bar must be rejected"
