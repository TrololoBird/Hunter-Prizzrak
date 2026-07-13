"""frame_cache WS merge (ADR-0001 pillar 4, safe part).

update_ohlcv used to REPLACE the REST-seeded frame with the ~200-bar WS window,
so the REST-outage fallback served a stub exactly when it mattered (the
2026-07-12 418 ban blackout). It must MERGE (dedup by open time, keep-last,
capped) and stamp freshness so get_kline_frame keeps serving mid-outage.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.data.frame_cache import get_frame_cache


class _Ex:
    """Minimal exchange stub for interval parsing."""

    def parse_timeframe(self, interval: str) -> int:
        return 60  # 1m


def _rest_frame(start_min: int, n: int) -> pl.DataFrame:
    from hunt_core.market.factory import ccxt_ohlcv_to_frame

    rows = [
        [
            (start_min + i) * 60_000,
            100.0, 101.0, 99.0, 100.5, 10.0,
        ]
        for i in range(n)
    ]
    return ccxt_ohlcv_to_frame(rows, "1m", exchange=_Ex())


def _ws_rows(start_min: int, n: int) -> list[list[Any]]:
    return [
        [(start_min + i) * 60_000, 100.0, 102.0, 99.5, 101.0, 12.0]
        for i in range(n)
    ]


def test_ws_update_merges_into_rest_history_instead_of_replacing() -> None:
    cache = get_frame_cache()
    sym = "MERGETESTUSDT"
    base = _rest_frame(1000, 500)  # REST seed: bars 1000..1499
    cache.seed_klines(sym, {"1m": base})

    # WS window overlaps the tail and extends 10 new bars (1490..1509).
    cache.update_ohlcv(sym, "1m", _ws_rows(1490, 20), exchange=_Ex())

    merged = cache.get_kline_frame(sym, "1m")
    assert merged is not None
    assert merged.height == 510  # 500 + 10 new, overlap deduped
    # Keep-last on the overlap: bar 1490 now carries the WS close.
    row_1490 = merged.filter(pl.col("time").dt.epoch(time_unit="ms") == 1490 * 60_000)
    assert row_1490["close"].item() == 101.0
    # Sorted, no duplicates.
    times = merged["time"].dt.epoch(time_unit="ms").to_list()
    assert times == sorted(set(times))


def test_ws_update_refreshes_staleness_stamp() -> None:
    cache = get_frame_cache()
    sym = "MERGETEST2USDT"
    cache.seed_klines(sym, {"1m": _rest_frame(1000, 50)})
    # Simulate an old seed: age the frame far beyond 1m max-age (120s).
    cache._frame_ts[sym]["1m"] -= 10_000.0
    assert cache.get_kline_frame(sym, "1m") is None  # aged out
    cache.update_ohlcv(sym, "1m", _ws_rows(1050, 5), exchange=_Ex())
    assert cache.get_kline_frame(sym, "1m") is not None  # WS update = fresh again


def test_merge_is_capped() -> None:
    cache = get_frame_cache()
    sym = "MERGETEST3USDT"
    cache.seed_klines(sym, {"1m": _rest_frame(1000, 1995)})
    cache.update_ohlcv(sym, "1m", _ws_rows(2995, 50), exchange=_Ex())
    merged = cache.get_kline_frame(sym, "1m")
    assert merged is not None
    assert merged.height == 2000  # _KLINE_MERGE_CAP
    # The cap trims the OLDEST bars, never the fresh tail.
    last_ms = merged["time"].dt.epoch(time_unit="ms").to_list()[-1]
    assert last_ms == 3044 * 60_000
