"""
Window extraction — for each event, extract a fixed-size context window.

Reads events from reports/events.parquet, reads raw OHLCV from dataset_vN/,
writes windows to reports/windows.parquet.

Each row = one bar, relative to the event (relative_bar = -N ... +M).
This format enables fast cross-event comparison, clustering, and pattern matching.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import cache_path, get_active_version, report_path  # noqa: E402

# ── window config ───────────────────────────────────────────
BARS_BEFORE = 200   # bars before event start
BARS_AFTER = 100    # bars after event end


# ── windows schema ──────────────────────────────────────────
# Each row is a single bar relative to the event.
# relative_bar: -200 ... +100 (negative = before event, 0 = event start)
WINDOWS_SCHEMA = {
    "event_id": pl.Int64,
    "symbol": pl.Utf8,
    "timeframe": pl.Utf8,
    "direction": pl.Utf8,
    "magnitude_pct": pl.Float64,
    "relative_bar": pl.Int64,     # -BARS_BEFORE ... +BARS_AFTER
    "abs_ts": pl.Int64,           # absolute timestamp
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "body": pl.Float64,           # close - open
    "body_pct": pl.Float64,       # (close - open) / open * 100
    "upper_wick": pl.Float64,     # high - max(open, close)
    "lower_wick": pl.Float64,     # min(open, close) - low
    "range": pl.Float64,          # high - low
    "range_pct": pl.Float64,      # (high - low) / open * 100
}


def extract_window_for_event(
    event: dict,
    ohlcv_df: pl.DataFrame,
    bars_before: int = BARS_BEFORE,
    bars_after: int = BARS_AFTER,
) -> list[dict]:
    """
    Given a single event dict and the full OHLCV DataFrame for that (symbol, tf),
    extract the context window.
    """
    event_start_ts = event["event_start_ts"]
    event_id = event["event_id"]

    # find the bar index for event_start_ts. After cross-TF merge, the merged
    # event_start_ts is the MIN start across the merged raw events and may not
    # land exactly on this (primary) TF's bar grid — an exact-equality match
    # then silently returned no window (the "NO_DATA" events). Snap to the
    # nearest bar at/just before event_start_ts instead.
    ts_arr = ohlcv_df["timestamp"].to_numpy()
    if len(ts_arr) == 0:
        return []
    exact = ohlcv_df.select(pl.arange(len(ohlcv_df)).filter(ohlcv_df["timestamp"] == event_start_ts))
    if len(exact) > 0:
        start_idx = int(exact[0, 0])
    else:
        import numpy as np
        pos = int(np.searchsorted(ts_arr, event_start_ts, side="right")) - 1
        if pos < 0:
            pos = 0
        start_idx = pos

    # slice window
    win_start = max(0, start_idx - bars_before)
    win_end = min(len(ohlcv_df), start_idx + bars_after + 1)
    window_df = ohlcv_df.slice(win_start, win_end - win_start)

    rows = []
    for i, row in enumerate(window_df.iter_rows(named=True)):
        rel_bar = (win_start + i) - start_idx

        body = row["close"] - row["open"]
        body_pct = body / row["open"] * 100 if row["open"] != 0 else 0.0
        upper_wick = row["high"] - max(row["open"], row["close"])
        lower_wick = min(row["open"], row["close"]) - row["low"]
        bar_range = row["high"] - row["low"]
        range_pct = bar_range / row["open"] * 100 if row["open"] != 0 else 0.0

        rows.append({
            "event_id": event_id,
            "symbol": event["symbol"],
            "timeframe": event["primary_tf"],
            "direction": event["direction"],
            "magnitude_pct": event["magnitude_pct"],
            "relative_bar": rel_bar,
            "relative_timestamp": row["timestamp"] - event_start_ts,
            "abs_ts": row["timestamp"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "body": body,
            "body_pct": round(body_pct, 4),
            "upper_wick": upper_wick,
            "lower_wick": lower_wick,
            "range": bar_range,
            "range_pct": round(range_pct, 4),
        })

    return rows


def extract_all_windows(
    version: int | None = None,
    bars_before: int = BARS_BEFORE,
    bars_after: int = BARS_AFTER,
) -> pl.DataFrame:
    """Extract windows for all (deduplicated) events in events_merged.parquet."""
    if version is None:
        version = get_active_version()

    events_path = report_path("events_merged.parquet")
    if not events_path.exists():
        print(f"No merged events found at {events_path}. Run cross_timeframe_merge.py first.")
        return pl.DataFrame()

    events_df = pl.read_parquet(events_path)
    print(f"Loaded {len(events_df)} merged events")

    # group events by (symbol, primary_tf) for efficient OHLCV loading
    groups = events_df.group_by(["symbol", "primary_tf"]).agg(pl.len().alias("count"))
    all_rows: list[dict] = []

    for row in groups.iter_rows(named=True):
        sym = row["symbol"]
        tf = row["primary_tf"]
        n_events = row["count"]

        ohlcv_path = cache_path(sym, tf, version)
        if not ohlcv_path.exists():
            print(f"  {sym} {tf}: OHLCV not found, skipping {n_events} events")
            continue

        ohlcv_df = pl.read_parquet(ohlcv_path)
        sym_events = events_df.filter(
            (pl.col("symbol") == sym) & (pl.col("primary_tf") == tf)
        )

        print(f"  {sym} {tf}: extracting windows for {n_events} events ... ", end="", flush=True)

        for ev in sym_events.iter_rows(named=True):
            window_rows = extract_window_for_event(ev, ohlcv_df, bars_before, bars_after)
            all_rows.extend(window_rows)

        print(f"{len(all_rows)} rows so far")

    if not all_rows:
        print("No windows extracted.")
        return pl.DataFrame()

    windows_df = pl.DataFrame(all_rows)
    out = report_path("windows.parquet")
    windows_df.write_parquet(out)
    print(f"\nSaved {len(windows_df)} window rows → {out}")
    print(f"  {events_df['event_id'].n_unique()} events × "
          f"~{bars_before + bars_after + 1} bars ≈ {len(windows_df)} rows")

    return windows_df


if __name__ == "__main__":
    windows = extract_all_windows()
    if len(windows) > 0:
        print(f"\nWindow shape: {windows.shape}")
        print(f"Columns: {windows.columns}")
