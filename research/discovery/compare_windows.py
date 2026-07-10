"""
Cross-event window comparison + control sample generation.

Reads windows.parquet and event_features.parquet.
Generates random "non-event" windows as control samples.
Computes feature comparison between events and controls.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import cache_path, get_active_version, report_path  # noqa: E402

BARS_BEFORE = 200
BARS_AFTER = 100
WINDOW_SIZE = BARS_BEFORE + BARS_AFTER + 1

# number of control samples per event (1:1 ratio)
CONTROL_RATIO = 1


def load_events() -> pl.DataFrame:
    path = report_path("events_merged.parquet")
    if not path.exists():
        return pl.DataFrame()
    return pl.read_parquet(path)


def load_windows() -> pl.DataFrame:
    path = report_path("windows.parquet")
    if not path.exists():
        return pl.DataFrame()
    return pl.read_parquet(path)


def generate_control_windows(
    events_df: pl.DataFrame,
    version: int | None = None,
    n_per_event: int = CONTROL_RATIO,
) -> pl.DataFrame:
    """
    Generate volatility-matched non-event windows for each (symbol, timeframe).

    For each event:
    1. Compute ATR of the event's pre-window.
    2. Find random windows with similar ATR (±30%) but no event overlap.
    This ensures controls have comparable market conditions.
    """
    import numpy as np

    if version is None:
        version = get_active_version()

    if len(events_df) == 0:
        return pl.DataFrame()

    # collect event timestamps per (symbol, tf)
    event_ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in events_df.iter_rows(named=True):
        key = (row["symbol"], row["primary_tf"])
        if key not in event_ranges:
            event_ranges[key] = []
        event_ranges[key].append((row["event_start_ts"], row["event_end_ts"]))

    all_controls: list[dict] = []
    control_id = 0

    for (sym, tf), ranges in event_ranges.items():
        ohlcv_path = cache_path(sym, tf, version)
        if not ohlcv_path.exists():
            continue

        ohlcv_df = pl.read_parquet(ohlcv_path)
        if len(ohlcv_df) < WINDOW_SIZE + 50:
            continue

        ts_arr = ohlcv_df["timestamp"].to_numpy().astype(np.int64)
        close_arr = ohlcv_df["close"].to_numpy().astype(np.float64)
        high_arr = ohlcv_df["high"].to_numpy().astype(np.float64)
        low_arr = ohlcv_df["low"].to_numpy().astype(np.float64)

        # compute rolling ATR (14-period)
        n = len(close_arr)
        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(high_arr[i] - low_arr[i],
                        abs(high_arr[i] - close_arr[i - 1]),
                        abs(low_arr[i] - close_arr[i - 1]))
        atr = np.zeros(n)
        atr[14:] = np.convolve(tr, np.ones(14) / 14, mode="full")[14:n + 14][:n - 14]
        atr[:14] = tr[:14]

        # Build busy index set from each event's own extraction window
        # (start_idx ± BARS_BEFORE/BARS_AFTER), not its raw start→end range.
        # After cross-TF merging, event_start_ts/event_end_ts can be a union
        # spanning far more bars than the window actually used downstream
        # (e.g. a daily-TF event merged with an hourly one) — using the raw
        # range to exclude "busy" bars would mark almost the whole dataset
        # as unusable for controls on short-history timeframes.
        busy_idx = set()
        for start_ts, _end_ts in ranges:
            start_mask = ts_arr == start_ts
            start_hits = np.where(start_mask)[0]
            if len(start_hits) == 0:
                continue
            s_idx = int(start_hits[0])
            lo = max(0, s_idx - BARS_BEFORE)
            hi = min(n, s_idx + BARS_AFTER + 1)
            busy_idx.update(range(lo, hi))

        # for each event, find volatility-matched control windows
        for start_ts, _end_ts in ranges:
            # find event's bar index (by its own start, not the possibly
            # merge-expanded start→end range)
            start_hits = np.where(ts_arr == start_ts)[0]
            if len(start_hits) == 0:
                continue
            ev_start_idx = int(start_hits[0])

            # too close to the start of history to have a volatility reference
            if ev_start_idx < 14:
                continue

            # compute event's pre-window ATR (before event start)
            pre_start = max(0, ev_start_idx - BARS_BEFORE)
            pre_atr = float(np.mean(atr[pre_start:ev_start_idx]))
            if pre_atr <= 0:
                continue

            # find valid candidate indices
            candidates = []
            for i in range(BARS_BEFORE + 14, n - BARS_AFTER):
                # no overlap with any event
                if any(busy_idx.intersection(range(i - BARS_BEFORE, i + BARS_AFTER + 1))):
                    continue
                # check ATR similarity
                cand_pre_start = max(0, i - BARS_BEFORE)
                cand_atr = float(np.mean(atr[cand_pre_start:i])) if i > cand_pre_start else 0
                if cand_atr <= 0:
                    continue
                # within ±50% of event's ATR
                ratio = cand_atr / pre_atr if pre_atr > 0 else 1
                if 0.5 <= ratio <= 1.5:
                    candidates.append((i, ratio))

            if not candidates:
                continue

            # sample from candidates
            sampled = random.sample(candidates, min(1, len(candidates)))

            for start_idx, _ in sampled:
                win_start = start_idx - BARS_BEFORE
                win_end = start_idx + BARS_AFTER + 1
                window_df = ohlcv_df.slice(win_start, win_end - win_start)

                for i, row in enumerate(window_df.iter_rows(named=True)):
                    rel_bar = (win_start + i) - start_idx
                    body = row["close"] - row["open"]
                    body_pct = body / row["open"] * 100 if row["open"] != 0 else 0.0
                    upper_wick = row["high"] - max(row["open"], row["close"])
                    lower_wick = min(row["open"], row["close"]) - row["low"]
                    bar_range = row["high"] - row["low"]
                    range_pct = bar_range / row["open"] * 100 if row["open"] != 0 else 0.0

                    all_controls.append({
                        "event_id": -(control_id + 1),
                        "symbol": sym,
                        "timeframe": tf,
                        "direction": "control",
                        "magnitude_pct": 0.0,
                        "relative_bar": rel_bar,
                        "relative_timestamp": row["timestamp"] - ts_arr[start_idx],
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

                control_id += 1

    if not all_controls:
        print("No control windows generated.")
        return pl.DataFrame()

    controls_df = pl.DataFrame(all_controls)
    out = report_path("controls.parquet")
    controls_df.write_parquet(out)
    print(f"Generated {control_id} volatility-matched control windows ({len(controls_df)} rows) → {out}")

    return controls_df


def compute_event_features(windows: pl.DataFrame) -> pl.DataFrame:
    """Aggregate per-event features from window rows."""
    features = windows.group_by("event_id").agg([
        pl.col("symbol").first(),
        pl.col("timeframe").first(),
        pl.col("direction").first(),
        pl.col("magnitude_pct").first(),

        # pre-event window (relative_bar < 0)
        pl.col("body_pct").filter(pl.col("relative_bar") < 0).mean().alias("avg_body_pct_before"),
        pl.col("range_pct").filter(pl.col("relative_bar") < 0).mean().alias("avg_range_pct_before"),
        pl.col("volume").filter(pl.col("relative_bar") < 0).mean().alias("avg_volume_before"),
        pl.col("body_pct").filter(pl.col("relative_bar") < 0).std().alias("std_body_pct_before"),
        pl.col("range_pct").filter(pl.col("relative_bar") < 0).std().alias("std_range_pct_before"),

        # during event (relative_bar >= 0)
        pl.col("body_pct").filter(pl.col("relative_bar") >= 0).mean().alias("avg_body_pct_during"),
        pl.col("range_pct").filter(pl.col("relative_bar") >= 0).mean().alias("avg_range_pct_during"),
        pl.col("volume").filter(pl.col("relative_bar") >= 0).mean().alias("avg_volume_during"),
        pl.col("upper_wick").filter(pl.col("relative_bar") >= 0).mean().alias("avg_upper_wick_during"),
        pl.col("lower_wick").filter(pl.col("relative_bar") >= 0).mean().alias("avg_lower_wick_during"),

        # volume ratio
        (
            pl.col("volume").filter(pl.col("relative_bar") >= 0).mean()
            / pl.col("volume").filter(pl.col("relative_bar") < 0).mean().clip(lower_bound=1e-10)
        ).alias("vol_ratio"),

        pl.len().alias("window_bars"),
    ])

    return features


def compare_events_vs_controls(
    events_features: pl.DataFrame,
    controls_features: pl.DataFrame,
) -> pl.DataFrame:
    """Side-by-side comparison of event vs control features."""
    if len(events_features) == 0 or len(controls_features) == 0:
        return pl.DataFrame()

    # aggregate each group
    def agg_group(df: pl.DataFrame, label: str) -> dict:
        return {
            "group": label,
            "count": len(df),
            "avg_body_before": float(df["avg_body_pct_before"].mean()),
            "avg_range_before": float(df["avg_range_pct_before"].mean()),
            "avg_body_during": float(df["avg_body_pct_during"].mean()),
            "avg_range_during": float(df["avg_range_pct_during"].mean()),
            "avg_vol_ratio": float(df["vol_ratio"].mean()),
            "avg_upper_wick": float(df["avg_upper_wick_during"].mean()),
            "avg_lower_wick": float(df["avg_lower_wick_during"].mean()),
        }

    event_stats = agg_group(events_features, "events")
    control_stats = agg_group(controls_features, "controls")

    comparison = pl.DataFrame([event_stats, control_stats])
    return comparison


def run_comparison(version: int | None = None) -> None:
    """Run full comparison pipeline."""
    events_df = load_events()
    windows_df = load_windows()

    if len(events_df) == 0:
        print("No events. Run discover_events.py first.")
        return

    if len(windows_df) == 0:
        print("No windows. Run extract_windows.py first.")
        return

    print(f"Events: {len(events_df)}")
    print(f"Windows: {len(windows_df)} rows")

    # compute event features
    events_features = compute_event_features(windows_df)
    events_features.write_parquet(report_path("event_features.parquet"))
    print(f"Event features → {len(events_features)} events")

    # generate controls
    controls_df = generate_control_windows(events_df, version)
    if len(controls_df) == 0:
        print("No controls generated.")
        return

    # compute control features
    controls_features = compute_event_features(controls_df)
    controls_features.write_parquet(report_path("control_features.parquet"))
    print(f"Control features → {len(controls_features)} controls")

    # compare
    comparison = compare_events_vs_controls(events_features, controls_features)
    if len(comparison) > 0:
        print("\n" + "=" * 60)
        print("EVENTS vs CONTROLS")
        print("=" * 60)
        print(comparison)
        comparison.write_parquet(report_path("comparison_events_vs_controls.parquet"))

    # direction comparison (events only)
    if "direction" in events_features.columns:
        dir_events = events_features.filter(pl.col("direction") != "control")
        if len(dir_events) > 0:
            dir_comp = dir_events.group_by("direction").agg([
                pl.len().alias("count"),
                pl.col("magnitude_pct").mean().alias("avg_magnitude"),
                pl.col("vol_ratio").mean().alias("avg_vol_ratio"),
                pl.col("avg_body_pct_before").mean().alias("avg_body_before"),
                pl.col("avg_range_pct_before").mean().alias("avg_range_before"),
            ]).sort("direction")
            print("\nDIRECTION COMPARISON:")
            print(dir_comp)
            dir_comp.write_parquet(report_path("compare_direction.parquet"))

    # symbol comparison
    sym_comp = events_features.group_by("symbol").agg([
        pl.len().alias("count"),
        pl.col("magnitude_pct").mean().alias("avg_magnitude"),
        pl.col("vol_ratio").mean().alias("avg_vol_ratio"),
        pl.col("avg_body_pct_before").mean().alias("avg_body_before"),
        pl.col("avg_range_pct_before").mean().alias("avg_range_before"),
    ]).sort("symbol")
    print("\nSYMBOL COMPARISON:")
    print(sym_comp)
    sym_comp.write_parquet(report_path("compare_symbol.parquet"))


if __name__ == "__main__":
    run_comparison()
