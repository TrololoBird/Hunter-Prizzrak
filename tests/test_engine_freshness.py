"""Engine transport logic — drop-forming (I-5), the two-axis freshness gate, jittered backoff,
and whole-feed silence detection. All pure, deterministic (no live WS)."""
from __future__ import annotations

from hunt_core.engine import freshness, params
from hunt_core.engine.health import feed_silence_s
from hunt_core.engine.ingest import backoff_delay_s


def _bar(open_ms: int, close: float = 1.0) -> list[float]:
    return [float(open_ms), close, close, close, close, 1.0]


def test_closed_bars_drops_forming_candle() -> None:
    cache = [_bar(0), _bar(900_000), _bar(1_800_000)]  # last is the forming candle over WS
    closed = freshness.closed_bars(cache)
    assert closed == cache[:-1]
    assert freshness.newest_closed(cache) == cache[-2]


def test_newest_closed_none_when_only_forming() -> None:
    assert freshness.newest_closed([_bar(0)]) is None
    assert freshness.newest_closed([]) is None


def test_ws_frame_trustworthy_two_axis_gate() -> None:
    interval = 900_000  # 15m
    # now sits inside the bar that opened at 1_800_000; last CLOSED bar opened at 900_000.
    now = 1_800_000 + 100_000
    cache = [_bar(0), _bar(900_000), _bar(1_800_000)]  # closed = [0, 900k]; newest closed open=900k
    # content ok (reached last closed) AND ticked recently → trust
    assert freshness.ws_frame_trustworthy(
        cache, interval_ms=interval, now_ms=now, last_ws_refresh_ms=now - 10_000
    )
    # wall-clock fail: last refresh before the half-candle mark (1_350_000) → distrust (frozen)
    assert not freshness.ws_frame_trustworthy(
        cache, interval_ms=interval, now_ms=now, last_ws_refresh_ms=1_300_000
    )
    # content fail: WS buffer stuck a bar behind → distrust
    stale_cache = [_bar(0), _bar(900_000)]  # closed = [0]; newest closed open=0 < 900k
    assert not freshness.ws_frame_trustworthy(
        stale_cache, interval_ms=interval, now_ms=now, last_ws_refresh_ms=now
    )


def test_backoff_is_bounded_and_jittered() -> None:
    # attempt 1 → ceil = 2^1-1 = 1 → delay ∈ [1, 2]; never zero (hot-loop guard)
    for _ in range(200):
        d1 = backoff_delay_s(1)
        assert 1.0 <= d1 <= 2.0
    # grows but never exceeds cap+1, even for a large attempt
    d_big = backoff_delay_s(40)
    assert 1.0 <= d_big <= params.BACKOFF_CAP_S + 1.0


def test_feed_silence_none_during_warmup_and_measures_whole_feed() -> None:
    assert feed_silence_s({}, now_ms=1_000) is None  # warm-up: not a stall
    # one recent stream keeps the whole feed alive (event-driven silence on others is fine)
    last = {"BTC:trades": 100_000, "BTC:book": 199_500}
    assert feed_silence_s(last, now_ms=200_000) == 0.5
    # whole feed silent → the watchdog would force-reconnect
    assert feed_silence_s({"a": 0, "b": 0}, now_ms=200_000) == 200.0
