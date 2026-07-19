"""ScannerFeed (ADR-0004 S7) — the native engine-fed detection-frame source for the scanner.

The scanner cutover swaps the data SOURCE (client → engine), not the detection logic, so the
manipulations backtest (which feeds its own OHLCV) can't see it. These tests are the real gate:
they pin the properties the swap must preserve — closed-only frames, per-TF staleness reject, and
fail-loud funding — plus the new bar-close-driven cache that replaces the old interval-TTL cache
(and by construction kills the HTF-cache-TTL-drift blackout family).
"""
from __future__ import annotations

import asyncio

from hunt_core.scanner.feed import (
    _MAX_STALE_MS_BY_TF,
    _TIMEFRAMES,
    EngineScannerFeed,
    LegacyScannerFeed,
)

_H1_MS = 3_600_000  # 1h interval in ms
_TF_SECONDS = {"1w": 604800, "1d": 86400, "4h": 14400, "1h": 3600, "15m": 900, "5m": 300}


def test_staleness_bound_is_exactly_two_intervals_for_every_tf():
    """The 'a cache hit is fresh by construction' guarantee rests on staleness_bound == reuse
    horizon == 2·interval. Pin it so an edit to _MAX_STALE_MS_BY_TF can never silently decouple
    the two and re-open the HTF-cache-TTL-drift blackout family."""
    for tf in _TIMEFRAMES:
        interval_ms = _TF_SECONDS[tf] * 1000
        assert _MAX_STALE_MS_BY_TF[tf] == 2 * interval_ms, (
            f"{tf}: staleness bound {_MAX_STALE_MS_BY_TF[tf]} != 2·interval {2 * interval_ms}"
        )


class _FakeExchange:
    """Minimal ccxt-shaped stub: parse_timeframe + fetch_ohlcv (raw rows) + funding history."""

    def __init__(self, rows: list[list[float]], funding: list[dict] | None = None) -> None:
        self._rows = rows
        self._funding = funding or []
        self.ohlcv_calls = 0
        self.raise_ohlcv = False
        self.id = "fake"

    def parse_timeframe(self, tf: str) -> float:
        return float(_TF_SECONDS[tf])

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
        self.ohlcv_calls += 1
        if self.raise_ohlcv:
            raise RuntimeError("boom")
        return [list(r) for r in self._rows]

    async def fetch_funding_rate_history(self, symbol, limit=16):
        return list(self._funding)


class _FakeEngine:
    def __init__(self, exchange: _FakeExchange) -> None:
        self.exchange = exchange


def _rows(newest_closed_open: int, n_closed: int = 5, interval_ms: int = _H1_MS) -> list[list[float]]:
    """n_closed closed bars ending at ``newest_closed_open`` + 1 trailing FORMING bar.

    ``rest.seed_ohlcv`` drops the last (forming) bar, so after seeding the newest closed bar opens
    exactly at ``newest_closed_open`` — the value the cache/staleness math keys on.
    """
    start = newest_closed_open - (n_closed - 1) * interval_ms
    return [[start + i * interval_ms, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(n_closed + 1)]


def _feed(rows, funding=None, tfs=("1h",)) -> tuple[EngineScannerFeed, _FakeExchange]:
    ex = _FakeExchange(rows, funding=funding)
    return EngineScannerFeed(_FakeEngine(ex), timeframes=tfs), ex


def test_closed_only_frame_drops_forming_bar():
    """The feed serves only closed bars (I-5) — the forming tail from fetch_ohlcv is dropped."""
    t = 1_000 * _H1_MS
    feed, ex = _feed(_rows(t, n_closed=5))
    _, ohlcv, _ = asyncio.run(feed.detection_data("X/USDT:USDT", now_ms=t + _H1_MS + 5))
    bars = ohlcv["1h"]
    assert len(bars) == 5  # 6 raw (5 closed + 1 forming) → 5 after the drop
    assert int(bars[-1][0]) == t  # newest is the closed bar, not the forming one


def test_cache_reuses_frame_until_next_bar_is_due():
    """A cached frame is reused while no new bar has closed — no second REST call (bar-close cache)."""
    t = 1_000 * _H1_MS
    feed, ex = _feed(_rows(t))
    now1 = t + _H1_MS + 5  # just after the newest bar closed; next bar forming
    asyncio.run(feed.detection_data("X", now_ms=now1))
    assert ex.ohlcv_calls == 1
    now2 = t + _H1_MS + int(0.4 * _H1_MS)  # = t + 1.4·interval, still before t + 2·interval → reuse
    asyncio.run(feed.detection_data("X", now_ms=now2))
    assert ex.ohlcv_calls == 1  # reused, not refetched


def test_cache_refetches_once_a_new_bar_has_closed():
    """Past ``last_open + 2·interval`` a fresher closed bar exists → refetch."""
    t = 1_000 * _H1_MS
    feed, ex = _feed(_rows(t))
    asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 5))
    assert ex.ohlcv_calls == 1
    now2 = t + 2 * _H1_MS + 10  # a new bar has closed since
    asyncio.run(feed.detection_data("X", now_ms=now2))
    assert ex.ohlcv_calls == 2


def test_staleness_reject_yields_no_data_not_a_stale_frame():
    """A frame whose freshest closed bar is older than the 2·interval bound → omitted (I-6)."""
    t = 1_000 * _H1_MS
    feed, ex = _feed(_rows(t))
    now = t + 3 * _H1_MS  # now - last_open = 3·interval > stale bound (2·interval)
    _, ohlcv, _ = asyncio.run(feed.detection_data("X", now_ms=now))
    assert "1h" not in ohlcv  # nothing served
    assert ex.ohlcv_calls == 1  # it did refetch, then rejected on staleness (no stale entry cached)


def test_reused_frame_is_never_one_staleness_would_reject():
    """Reuse horizon == staleness bound (both 2·interval): a cache hit is fresh by construction."""
    t = 1_000 * _H1_MS
    feed, ex = _feed(_rows(t))
    asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 5))
    # Any now that yields a reuse (< t + 2·interval) also passes staleness (now - t < 2·interval):
    _, ohlcv, _ = asyncio.run(feed.detection_data("X", now_ms=t + 2 * _H1_MS - 1))
    assert ohlcv["1h"] and ex.ohlcv_calls == 1


def test_fetch_failure_is_fail_loud_none_not_cached():
    """A raised fetch → no frame this cycle, and nothing stale is cached (retries next cycle)."""
    t = 1_000 * _H1_MS
    feed, ex = _feed(_rows(t))
    ex.raise_ohlcv = True
    _, ohlcv, _ = asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 5))
    assert "1h" not in ohlcv
    ex.raise_ohlcv = False
    _, ohlcv2, _ = asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 6))
    assert ohlcv2["1h"]  # recovered — the failure left no poisoned cache entry


def test_funding_context_skips_unparseable_records():
    """Missing/garbage fundingRate is skipped (I-6), never fabricated as 0.0 into rate/peak."""
    t = 1_000 * _H1_MS
    funding = [
        {"fundingRate": "0.001"},
        {"fundingRate": None},       # missing → skip (legacy would fabricate 0.0)
        {"fundingRate": "garbage"},  # unparseable → skip
        {"fundingRate": 0.002},
    ]
    feed, _ = _feed(_rows(t), funding=funding)
    _, _, ctx = asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 5))
    assert ctx == {"rate": 0.002, "peak": 0.002}  # only the two finite rates counted


def test_funding_empty_history_is_none():
    t = 1_000 * _H1_MS
    feed, _ = _feed(_rows(t), funding=[])
    _, _, ctx = asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 5))
    assert ctx is None  # нет данных, never a fabricated context


# ── LegacyScannerFeed: the coexistence-OFF path must stay byte-identical to the old client path ──


class _FakeClient:
    def __init__(self, rows: list[list[float]], funding: list[dict]) -> None:
        self._rows = rows
        self._funding = funding

    async def fetch_ohlcv_list_cached(self, symbol, tf, limit):
        return [list(r) for r in self._rows]

    async def fetch_funding_rate_history(self, symbol, limit=10):
        return list(self._funding)


def test_legacy_feed_drops_forming_and_keeps_client_semantics():
    """LegacyScannerFeed reproduces the pre-cutover fetch: conditional forming-drop + old funding."""
    t = 1_000 * _H1_MS
    rows = _rows(t, n_closed=5)  # last row is forming (open = t + interval)
    client = _FakeClient(rows, funding=[{"fundingRate": 0.001}, {"fundingRate": None}])
    feed = LegacyScannerFeed(client, timeframes=("1h",))
    _, ohlcv, ctx = asyncio.run(feed.detection_data("X", now_ms=t + _H1_MS + 5))
    assert len(ohlcv["1h"]) == 5 and int(ohlcv["1h"][-1][0]) == t
    # Legacy `or 0.0` semantics preserved (None → 0.0) — this is the OLD path, kept byte-identical:
    assert ctx == {"rate": 0.0, "peak": 0.001}
