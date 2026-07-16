"""Pinning tests: the list-OHLCV cache must never hold a still-forming kline.

``fetch_ohlcv_list_cached`` stores the RAW ccxt window, which includes Binance's
in-progress last candle. Consumers used to drop that candle at READ time
(``bars[-1][0] + interval_ms > now_ms``), but every kline TTL is >= one full
interval (``klines_1h`` = 3900s), so the check goes STALE inside the cache
lifetime:

    10:00  1h bar opens
    10:05  scan caches the window — last entry is a 5-minute-old PARTIAL bar
    11:04  scan re-reads the cache (inside the 3900s TTL); the read-time check
           computes 10:00 + 1h = 11:00 <= 11:04 → "closed" → the partial
           snapshot reaches the detectors as a CLOSED candle.

``bos_up``/``choch_*`` key off ``close[-1]``, so LTF "confirmation" — the gate
separating a tracked position from an advisory, hard-required by shorts — could
fire on a close that never existed. These tests pin the fix at the producer: the
cache stores only bars closed as of the FETCH instant.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from hunt_core.market.client import _CACHE_TTL

_HOUR_MS = 3_600_000


class _FakeExchange:
    """Minimal ccxt stand-in — ``drop_unclosed_ohlcv_tail`` only needs timeframes."""

    id = "binance"
    _TF_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

    def parse_timeframe(self, tf: str) -> int:
        return self._TF_S[tf]


class _StubClient:
    """Exercises the real ``fetch_ohlcv_list_cached`` body with a stubbed fetch.

    Binds the production coroutine onto a stub so the cache/TTL/single-flight
    logic under test is the real one, without a live exchange or REST gate.
    """

    def __init__(self, rows: list[list[Any]]) -> None:
        from hunt_core.market.client import HuntCcxtClient

        self._rows = rows
        self._ex = _FakeExchange()
        self._ohlcv_list_cache: dict[tuple[str, str, int], tuple[float, list[list[Any]]]] = {}
        self._ohlcv_list_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self.fetch_calls = 0
        self.fetch_ohlcv_list_cached = (
            HuntCcxtClient.fetch_ohlcv_list_cached.__get__(self, _StubClient)
        )

    def _bin_sym(self, symbol: str) -> str:
        return symbol

    async def fetch_ohlcv_list(
        self, symbol: str, interval: str, *, limit: int = 500, **_: Any
    ) -> list[list[Any]]:
        self.fetch_calls += 1
        return list(self._rows)


def _window_ending_at(open_ms: int, n: int = 5) -> list[list[Any]]:
    """``n`` ascending 1h bars whose LAST bar opens at ``open_ms``."""
    return [
        [float(open_ms - (n - 1 - i) * _HOUR_MS), 100.0, 101.0, 99.0, 100.0, 10.0]
        for i in range(n)
    ]


def test_cache_write_drops_the_forming_bar() -> None:
    """The cached window must exclude the bar still forming at fetch time."""
    now_ms = int(time.time() * 1000)
    forming_open = now_ms - 5 * 60 * 1000  # opened 5 minutes ago → NOT closed
    rows = _window_ending_at(forming_open)
    client = _StubClient(rows)

    got = asyncio.run(client.fetch_ohlcv_list_cached("BTCUSDT", "1h", limit=5))

    assert len(got) == len(rows) - 1
    assert int(got[-1][0]) == forming_open - _HOUR_MS
    assert int(got[-1][0]) + _HOUR_MS <= now_ms  # every served bar is closed


def test_stale_cache_never_serves_a_partial_bar_as_closed() -> None:
    """The concrete failure: partial bar cached at 10:05, re-read at 11:04.

    Fails before the fix — the read-time drop check passes on the stale entry
    (10:00 + 1h <= 11:04) and the 5-minute partial is served as a closed bar.
    """
    fetch_ms = int(time.time() * 1000)
    forming_open = fetch_ms - 5 * 60 * 1000  # the 10:00 bar, 5 min old at 10:05
    client = _StubClient(_window_ending_at(forming_open))

    asyncio.run(client.fetch_ohlcv_list_cached("BTCUSDT", "1h", limit=5))

    # Re-read at 11:04 — 59 min later, still inside the 3900s klines_1h TTL.
    key = ("BTCUSDT", "1h", 5)
    cached_at, cached_rows = client._ohlcv_list_cache[key]
    assert _CACHE_TTL["klines_1h"] > 59 * 60, "TTL premise of this test changed"
    client._ohlcv_list_cache[key] = (cached_at - 59 * 60, cached_rows)

    got = asyncio.run(client.fetch_ohlcv_list_cached("BTCUSDT", "1h", limit=5))

    assert client.fetch_calls == 1, "TTL should still be live — this is the stale-read path"
    # The forming bar must be absent, so a consumer's read-time check cannot
    # mistake it for closed.
    assert int(got[-1][0]) != forming_open
    assert int(got[-1][0]) + _HOUR_MS <= fetch_ms


def test_closed_window_is_served_intact() -> None:
    """A window whose last bar already closed at fetch time loses nothing."""
    now_ms = int(time.time() * 1000)
    closed_open = now_ms - 2 * _HOUR_MS  # closed an hour ago
    rows = _window_ending_at(closed_open)
    client = _StubClient(rows)

    got = asyncio.run(client.fetch_ohlcv_list_cached("BTCUSDT", "1h", limit=5))

    assert len(got) == len(rows)
    assert int(got[-1][0]) == closed_open
