"""Pinning tests for the closed-bar / no-lookahead sweep (audit task B).

Covers the two confirmed defects:

1. CONFIRMED-LEAK — the raw list-path OHLCV (``fetch_ohlcv_list*``) bypasses
   ``finalize_kline_frame``'s incomplete-tail drop, so Binance's in-progress
   candle reached the Prizrak detectors via ``runtime/analyst_assembly.py``
   (manipulation_delivery already dropped it; analyst_assembly did not).
   ``drop_unclosed_ohlcv_tail`` is the shared guard: a forming bar — however
   egregious — must never survive into a detector window.

2. Backtest forward-window boundary — ``research/backtest_scanner._simulate``
   skipped the first forward bar (the one opening exactly at decision time),
   so the first hour of post-signal price action never reached the
   stop/target simulation.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from hunt_core.market.factory import drop_unclosed_ohlcv_tail

_MIN_15M = 900_000
_MIN_1W = 604_800_000


class _FakeExchange:
    _TF_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}

    def parse_timeframe(self, tf: str) -> int:
        return self._TF_S[tf]


def _bar(open_ms: int, o: float, h: float, l: float, c: float, v: float = 1.0) -> list[float]:
    return [float(open_ms), o, h, l, c, v]


def test_forming_15m_bar_is_dropped() -> None:
    """The forming tail bar (egregiously different) must be removed; closed bars kept."""
    now = 10 * _MIN_15M + 450_000  # mid-way through the bar opening at 10*15m
    closed = [_bar(i * _MIN_15M, 100, 101, 99, 100) for i in range(10)]
    forming = _bar(10 * _MIN_15M, 100, 1000, 100, 990)  # blatant forming spike
    out = drop_unclosed_ohlcv_tail(closed + [forming], "15m", exchange=_FakeExchange(), now_ms=now)
    assert out == closed  # forming bar gone, history untouched


def test_closed_15m_window_is_untouched() -> None:
    now = 11 * _MIN_15M  # bar opening at 10*15m closed exactly now
    rows = [_bar(i * _MIN_15M, 100, 101, 99, 100) for i in range(11)]
    out = drop_unclosed_ohlcv_tail(rows, "15m", exchange=_FakeExchange(), now_ms=now)
    assert out == rows


def test_weekly_bar_closed_only_after_full_week() -> None:
    """A 1w bar is 'closed' only once open+7d <= now — 3 days in it is still forming."""
    week0 = 0
    week1 = _MIN_1W
    rows = [_bar(week0, 1, 2, 0.5, 1.5), _bar(week1, 1.5, 10, 1.4, 9.0)]
    ex = _FakeExchange()
    mid_week = week1 + 3 * 86_400_000  # Thursday of the second week
    assert drop_unclosed_ohlcv_tail(rows, "1w", exchange=ex, now_ms=mid_week) == rows[:1]
    at_close = week1 + _MIN_1W  # Monday 00:00 — the bar just closed
    assert drop_unclosed_ohlcv_tail(rows, "1w", exchange=ex, now_ms=at_close) == rows


def _load_backtest_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "research" / "backtest_scanner.py"
    spec = importlib.util.spec_from_file_location("_bt_scanner_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bt_scanner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_backtest_closed_upto_weekly_no_lookahead() -> None:
    """_closed_upto: a 1w bar contributes only once its CLOSE time (open+7d) <= T."""
    bt = _load_backtest_module()
    rows = [_bar(0, 1, 2, 0.5, 1.5), _bar(_MIN_1W, 1.5, 10, 1.4, 9.0)]
    # Mid-second-week scan tick: the in-progress weekly bar must NOT contribute.
    t_mid = _MIN_1W + 3 * 86_400_000
    assert bt._closed_upto(rows, "1w", t_mid) == rows[:1]
    # Exactly at the weekly close both bars are visible.
    assert bt._closed_upto(rows, "1w", 2 * _MIN_1W) == rows


def test_backtest_simulate_includes_first_forward_bar() -> None:
    """The bar opening exactly at decision time t0 belongs to the forward path.

    Setup: long, entry=100, stop=90, single TP=deep=120. Only the FIRST forward
    bar (open == t0) reaches 125; everything after is dead-flat at 100. If the
    simulation skips that bar (the old `t0 < r[0]` boundary), the campaign can
    only time out; with the correct boundary it wins on the first bar.
    """
    bt = _load_backtest_module()
    hour = 3_600_000
    t0 = 100 * hour
    fine = [_bar(t0, 100, 125, 99, 100)] + [
        _bar(t0 + i * hour, 100, 100, 100, 100) for i in range(1, 30)
    ]
    outcome, total_r, _mae, _legs = bt._simulate(
        fine, t0, "long", 100.0, 90.0, [120.0], [], 120.0, 48 * hour
    )
    assert outcome == "win"
    assert total_r > 0
