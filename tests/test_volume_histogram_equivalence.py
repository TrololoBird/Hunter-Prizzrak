"""G-27/G-29: the maps volume histogram was rebuilt with an iter_rows loop (and preceded
by a volume_profile_levels() call whose result was thrown away). This pins the Polars
rewrite against a literal transcription of the ORIGINAL loop — same output, one pass.
"""
from __future__ import annotations

import polars as pl

from hunt_core.maps.volume_profile import _volume_histogram


def _reference_loop(work: pl.DataFrame, *, lookback: int | None, buckets: int) -> dict[int, float]:
    """The original implementation, verbatim, as the oracle."""
    if work.is_empty():
        return {}
    tail = work.tail(lookback) if lookback else work
    price_min = float(tail["low"].min())
    price_max = float(tail["high"].max())
    if price_max <= price_min:
        return {}
    bucket_size = (price_max - price_min) / max(1, buckets)
    bars = tail.select(
        pl.col("high").cast(pl.Float64).alias("hi"),
        pl.col("low").cast(pl.Float64).alias("lo"),
        pl.col("volume").cast(pl.Float64).fill_null(0.0).alias("vol"),
    ).filter(pl.col("vol") > 0)
    hist: dict[int, float] = {}
    for hi, lo, vol in bars.iter_rows():
        if hi is None or lo is None or vol <= 0:
            continue
        b_lo = max(0, int((min(lo, hi) - price_min) / bucket_size))
        b_hi = min(buckets - 1, int((max(lo, hi) - price_min) / bucket_size))
        share = vol / max(1, b_hi - b_lo + 1)
        for b in range(b_lo, b_hi + 1):
            hist[b] = hist.get(b, 0.0) + share
    return hist


def _frame() -> pl.DataFrame:
    rows = []
    px = 100.0
    for i in range(60):
        px += (1.7 if i % 3 else -2.3) * ((i % 7) + 1) / 5.0
        hi = px + 1.1 + (i % 4) * 0.4
        lo = px - 0.9 - (i % 5) * 0.3
        rows.append({"high": hi, "low": lo, "volume": 10.0 + (i % 9) * 3.0})
    rows.append({"high": 130.0, "low": 129.5, "volume": 0.0})  # zero-volume bar: dropped
    return pl.DataFrame(rows)


def test_matches_the_original_loop() -> None:
    df = _frame()
    for buckets in (12, 24, 60):
        for lookback in (None, 20, 60):
            got = _volume_histogram(df, lookback=lookback, buckets=buckets)
            want = _reference_loop(df, lookback=lookback, buckets=buckets)
            assert set(got) == set(want), f"buckets={buckets} lookback={lookback}"
            for b in want:
                assert abs(got[b] - want[b]) < 1e-9, f"bucket {b}"


def test_empty_and_degenerate() -> None:
    empty = pl.DataFrame({"high": [], "low": [], "volume": []})
    assert _volume_histogram(empty, lookback=None, buckets=10) == {}
    flat = pl.DataFrame({"high": [100.0, 100.0], "low": [100.0, 100.0], "volume": [5.0, 5.0]})
    assert _volume_histogram(flat, lookback=None, buckets=10) == {}
