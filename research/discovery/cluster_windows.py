"""
Basic event grouping — simple magnitude bins for initial pattern discovery.

Not real clustering yet. Just 4 buckets:
  small    (3-5%)
  medium   (5-8%)
  large    (8-15%)
  extreme  (15%+)

Real clustering comes later, once we have enough events.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import report_path  # noqa: E402

MAGNITUDE_BINS = [
    (0, 5, "small"),
    (5, 8, "medium"),
    (8, 15, "large"),
    (15, 9999, "extreme"),
]


def classify_magnitude(pct: float) -> str:
    for lo, hi, label in MAGNITUDE_BINS:
        if lo <= pct < hi:
            return label
    return "unknown"


def group_events() -> pl.DataFrame:
    """Read events_merged.parquet (deduplicated events) and add magnitude_group column."""
    events_path = report_path("events_merged.parquet")
    if not events_path.exists():
        print(f"No merged events found at {events_path}. Run cross_timeframe_merge.py first.")
        return pl.DataFrame()

    events = pl.read_parquet(events_path)
    print(f"Loaded {len(events)} events")

    # add magnitude group
    events = events.with_columns(
        pl.col("magnitude_pct").map_elements(classify_magnitude, return_dtype=pl.Utf8).alias("magnitude_group")
    )

    # summary by group
    print("\nMAGNITUDE GROUPS:")
    for group in ["small", "medium", "large", "extreme"]:
        subset = events.filter(pl.col("magnitude_group") == group)
        if len(subset) == 0:
            continue
        n_long = len(subset.filter(pl.col("direction") == "long"))
        n_short = len(subset.filter(pl.col("direction") == "short"))
        avg_mag = subset["magnitude_pct"].mean()
        print(f"  {group:<10} {len(subset):>5} events  (long={n_long}, short={n_short}, avg={avg_mag:.1f}%)")

    # summary by group × symbol
    print("\nBY GROUP × SYMBOL:")
    pivot = events.group_by(["magnitude_group", "symbol"]).agg(pl.len().alias("n")).sort(["magnitude_group", "symbol"])
    for row in pivot.iter_rows(named=True):
        print(f"  {row['magnitude_group']:<10} {row['symbol']:<20} {row['n']:>5}")

    # summary by group × timeframe
    print("\nBY GROUP × TIMEFRAME:")
    pivot = events.group_by(["magnitude_group", "primary_tf"]).agg(pl.len().alias("n")).sort(["magnitude_group", "primary_tf"])
    for row in pivot.iter_rows(named=True):
        print(f"  {row['magnitude_group']:<10} {row['primary_tf']:<6} {row['n']:>5}")

    # save with group column
    out = report_path("events_grouped.parquet")
    events.write_parquet(out)
    print(f"\nSaved → {out}")

    return events


if __name__ == "__main__":
    group_events()
