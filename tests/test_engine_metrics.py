"""Engine Prometheus metrics — the silent-blackout guard (library-adoption #1).

Asserts the emit helpers move the right series and that cardinality stays coarse (venue / plane TYPE,
never per-symbol)."""
from __future__ import annotations

from hunt_core.engine import metrics


def test_feed_silence_gauge_tracks_seconds() -> None:
    metrics.set_feed_silence("binance", 3.5)
    assert metrics.FEED_SILENCE.labels(venue="binance")._value.get() == 3.5
    metrics.set_feed_silence("binance", 120.0)  # a blackout climbing unbounded
    assert metrics.FEED_SILENCE.labels(venue="binance")._value.get() == 120.0


def test_reconnect_counter_increments_by_reason() -> None:
    before = metrics.WS_RECONNECTS.labels(venue="okx", reason="silence")._value.get()
    metrics.record_reconnect("okx", "silence")
    assert metrics.WS_RECONNECTS.labels(venue="okx", reason="silence")._value.get() == before + 1


def test_staleness_reject_coarsens_plane_to_type() -> None:
    # "kline.4h" and "kline.1m" must land on the SAME series ("kline") — no per-tf cardinality blowup.
    before = metrics.STALENESS_REJECTS.labels(plane="kline")._value.get()
    metrics.record_staleness_reject("kline.4h")
    metrics.record_staleness_reject("kline.1m")
    assert metrics.STALENESS_REJECTS.labels(plane="kline")._value.get() == before + 2


def test_healthy_symbols_gauge() -> None:
    metrics.set_healthy_symbols("binance", 7)
    assert metrics.HEALTHY_SYMBOLS.labels(venue="binance")._value.get() == 7.0
