"""HTF kline frames persist across a restart so the cold cache has a fresh-enough
fallback instead of a stale bootstrap seed (collapses the post-restart blackout).

Only 1h/4h/1d are persisted; a frame older than the TF fallback max-age is skipped
on load so genuinely-stale data is never reloaded.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from hunt_core.data.frame_cache import SymbolFrameCache


def _kline_frame(*, tf_minutes: int, bars: int, newest_age_h: float) -> pl.DataFrame:
    """Build a minimal kline frame whose newest close_time is newest_age_h ago."""
    step = timedelta(minutes=tf_minutes)
    newest_close = datetime.now(UTC) - timedelta(hours=newest_age_h)
    close_times = [newest_close - step * (bars - 1 - i) for i in range(bars)]
    return pl.DataFrame(
        {
            "time": [ct - step for ct in close_times],
            "close_time": close_times,
            "open": [100.0] * bars,
            "high": [101.0] * bars,
            "low": [99.0] * bars,
            "close": [100.5] * bars,
            "volume": [10.0] * bars,
            "num_trades": [5] * bars,
        }
    )


def test_fresh_htf_frames_round_trip(tmp_path) -> None:
    src = SymbolFrameCache()
    src.seed_klines(
        "BTCUSDT",
        {
            "4h": _kline_frame(tf_minutes=240, bars=30, newest_age_h=1.0),
            "1h": _kline_frame(tf_minutes=60, bars=48, newest_age_h=0.5),
            "5m": _kline_frame(tf_minutes=5, bars=60, newest_age_h=0.1),  # NOT persisted
        },
    )
    written = src.persist_htf_frames(tmp_path)
    assert written == 2  # 4h + 1h only; 5m excluded

    dst = SymbolFrameCache()
    loaded = dst.load_htf_frames(tmp_path)
    assert loaded == 2
    # The reloaded 4h frame is servable via the fallback path.
    frame = dst.get_kline_frame("BTCUSDT", "4h")
    assert frame is not None and frame.height == 30
    assert "5m" not in dst.kline_map("BTCUSDT")  # low TF never persisted


def test_stale_frame_skipped_on_load(tmp_path) -> None:
    # 4h fallback max-age is 8h; a frame 12h old must be skipped on reload.
    # Persist "happened" 11h ago (frame was fresh then) — the process then died
    # and stayed down, so only the load-time staleness gate can reject it.
    src = SymbolFrameCache()
    src.seed_klines("ETHUSDT", {"4h": _kline_frame(tf_minutes=240, bars=30, newest_age_h=12.0)})
    written_at_ms = int((datetime.now(UTC) - timedelta(hours=11)).timestamp() * 1000)
    assert src.persist_htf_frames(tmp_path, now_ms=written_at_ms) == 1

    dst = SymbolFrameCache()
    assert dst.load_htf_frames(tmp_path) == 0
    assert dst.get_kline_frame("ETHUSDT", "4h") is None


def test_load_missing_dir_is_safe(tmp_path) -> None:
    dst = SymbolFrameCache()
    assert dst.load_htf_frames(tmp_path / "does_not_exist") == 0


def test_load_corrupt_file_is_safe(tmp_path) -> None:
    (tmp_path / "htf_4h.parquet").write_bytes(b"not a parquet file")
    dst = SymbolFrameCache()
    assert dst.load_htf_frames(tmp_path) == 0  # skipped, no raise


def test_persist_empty_cache_writes_nothing(tmp_path) -> None:
    assert SymbolFrameCache().persist_htf_frames(tmp_path) == 0


def test_unclosed_tail_never_persisted(tmp_path) -> None:
    # A still-forming bar (close_time in the future) must not survive persist —
    # a reload could otherwise repaint (closed-bar discipline).
    frame = _kline_frame(tf_minutes=60, bars=48, newest_age_h=0.5)
    forming = frame.tail(1).with_columns(
        (pl.col("time") + timedelta(hours=1)).alias("time"),
        (pl.col("close_time") + timedelta(hours=1)).alias("close_time"),
    )
    src = SymbolFrameCache()
    src.seed_klines("BTCUSDT", {"1h": pl.concat([frame, forming])})
    assert src.persist_htf_frames(tmp_path) == 1

    dst = SymbolFrameCache()
    dst.load_htf_frames(tmp_path)
    reloaded = dst.get_kline_frame("BTCUSDT", "1h")
    assert reloaded is not None and reloaded.height == 48  # forming row dropped
    assert reloaded["close_time"].max() == frame["close_time"].max()


def test_persist_is_atomic_no_tmp_leftover(tmp_path) -> None:
    src = SymbolFrameCache()
    src.seed_klines("BTCUSDT", {"1h": _kline_frame(tf_minutes=60, bars=48, newest_age_h=0.5)})
    assert src.persist_htf_frames(tmp_path) == 1
    assert (tmp_path / "htf_1h.parquet").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_stale_frames_aged_out_of_file(tmp_path) -> None:
    # Housekeeping: a symbol whose frame went stale (left the universe) is not
    # re-persisted, and a TF file with nothing fresh left is removed entirely.
    src = SymbolFrameCache()
    src.seed_klines("DEADUSDT", {"1h": _kline_frame(tf_minutes=60, bars=48, newest_age_h=0.5)})
    assert src.persist_htf_frames(tmp_path) == 1
    assert (tmp_path / "htf_1h.parquet").exists()

    # Same in-memory frame, but "now" has moved past the 1h max-age (2h).
    future_ms = int((datetime.now(UTC) + timedelta(hours=6)).timestamp() * 1000)
    assert src.persist_htf_frames(tmp_path, now_ms=future_ms) == 0
    assert not (tmp_path / "htf_1h.parquet").exists()


def test_1w_persisted_for_pinned(tmp_path) -> None:
    src = SymbolFrameCache()
    src.seed_klines(
        "BTCUSDT", {"1w": _kline_frame(tf_minutes=7 * 24 * 60, bars=220, newest_age_h=24.0)}
    )
    assert src.persist_htf_frames(tmp_path) == 1

    dst = SymbolFrameCache()
    assert dst.load_htf_frames(tmp_path) == 1
    assert dst.get_kline_frame("BTCUSDT", "1w") is not None


class _StubExchange:
    @staticmethod
    def parse_timeframe(interval: str) -> int:
        return {"1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}[interval]


def test_reloaded_current_frame_serves_instead_of_rest(tmp_path) -> None:
    # Restart simulation: persist → reload into a fresh cache → the tick path's
    # cache-first gate accepts the frame (its newest closed bar is the latest
    # possible one), so no full REST re-download happens until the bar rolls.
    from hunt_core.data.collect import htf_cache_frame_serves

    src = SymbolFrameCache()
    src.seed_klines("BTCUSDT", {"1h": _kline_frame(tf_minutes=60, bars=48, newest_age_h=0.5)})
    assert src.persist_htf_frames(tmp_path) == 1

    dst = SymbolFrameCache()
    assert dst.load_htf_frames(tmp_path) == 1
    frame = dst.get_kline_frame("BTCUSDT", "1h")
    assert frame is not None
    ex = _StubExchange()
    assert htf_cache_frame_serves(frame, "1h", 48, exchange=ex)
    # Bar rolled over → frame is no longer current → REST tops up the tail.
    later_ms = int((datetime.now(UTC) + timedelta(hours=2)).timestamp() * 1000)
    assert not htf_cache_frame_serves(frame, "1h", 48, exchange=ex, now_ms=later_ms)
    # Coverage gate: not enough bars for structure depth → REST.
    assert not htf_cache_frame_serves(frame, "1h", 500, exchange=ex)
    # No exchange (cannot compute the step) → REST.
    assert not htf_cache_frame_serves(frame, "1h", 48, exchange=None)


def test_oddball_schema_dropped_not_fatal(tmp_path) -> None:
    # Two symbols share the finalize schema; a third has a dropped column. concat
    # needs identical columns, so the oddball is skipped — the rest still persist.
    src = SymbolFrameCache()
    good = _kline_frame(tf_minutes=240, bars=30, newest_age_h=1.0)
    src.seed_klines("BTCUSDT", {"4h": good})
    src.seed_klines("ETHUSDT", {"4h": good})
    src._frames["ODDUSDT"] = {"4h": good.drop("num_trades")}

    written = src.persist_htf_frames(tmp_path)
    assert written == 2  # BTC + ETH; ODD dropped, no exception

    dst = SymbolFrameCache()
    dst.load_htf_frames(tmp_path)
    assert dst.get_kline_frame("BTCUSDT", "4h") is not None
    assert dst.get_kline_frame("ETHUSDT", "4h") is not None
    assert dst.get_kline_frame("ODDUSDT", "4h") is None
