"""
Summary — generate 5 research reports + reproducibility metadata.

Reports:
  1. Event inventory (counts by symbol, TF, direction)
  2. Merge impact (raw → merged reduction)
  3. Method co-occurrence (which methods fire together)
  4. Top similar event pairs (cross-symbol matches)
  5. Common pre-event patterns (what happens before events)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import get_active_version, report_path  # noqa: E402


def _ms_to_date(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _load(name: str) -> pl.DataFrame | None:
    p = report_path(name)
    if p.exists():
        return pl.read_parquet(p)
    return None


# ── Report 1: Event Inventory ───────────────────────────────
def report_inventory(events: pl.DataFrame) -> str:
    lines = ["# Report 1: Event Inventory\n"]

    lines.append(f"**Total events (before merge):** {len(events)}\n")

    # by symbol
    lines.append("## By Symbol\n")
    lines.append("| Symbol | Count | Long | Short |")
    lines.append("|--------|-------|------|-------|")
    for row in events.group_by("symbol").agg([
        pl.len().alias("count"),
        pl.col("direction").filter(pl.col("direction") == "long").len().alias("long"),
        pl.col("direction").filter(pl.col("direction") == "short").len().alias("short"),
    ]).sort("symbol").iter_rows(named=True):
        lines.append(f"| {row['symbol']} | {row['count']} | {row['long']} | {row['short']} |")

    # by timeframe
    lines.append("\n## By Timeframe\n")
    lines.append("| Timeframe | Count | Long | Short |")
    lines.append("|-----------|-------|------|-------|")
    for row in events.group_by("timeframe").agg([
        pl.len().alias("count"),
        pl.col("direction").filter(pl.col("direction") == "long").len().alias("long"),
        pl.col("direction").filter(pl.col("direction") == "short").len().alias("short"),
    ]).sort("timeframe").iter_rows(named=True):
        lines.append(f"| {row['timeframe']} | {row['count']} | {row['long']} | {row['short']} |")

    # magnitude stats
    lines.append("\n## Magnitude Distribution\n")
    stats = events.select([
        pl.col("magnitude_pct").mean().alias("mean"),
        pl.col("magnitude_pct").median().alias("median"),
        pl.col("magnitude_pct").min().alias("min"),
        pl.col("magnitude_pct").max().alias("max"),
        pl.col("magnitude_pct").std().alias("std"),
    ])
    if len(stats) > 0:
        row = stats.row(0)
        lines.append(f"- Mean: {row[0]:.2f}%")
        lines.append(f"- Median: {row[1]:.2f}%")
        lines.append(f"- Min: {row[2]:.2f}%")
        lines.append(f"- Max: {row[3]:.2f}%")
        lines.append(f"- Std: {row[4]:.2f}%")

    return "\n".join(lines)


# ── Report 2: Merge Impact ──────────────────────────────────
def report_merge_impact(events: pl.DataFrame, merged: pl.DataFrame, mapping: pl.DataFrame) -> str:
    lines = ["# Report 2: Merge Impact\n"]

    n_raw = len(events)
    n_merged = len(merged)
    reduction = n_raw - n_merged
    pct = (reduction / n_raw * 100) if n_raw > 0 else 0

    lines.append(f"- **Raw events:** {n_raw}")
    lines.append(f"- **After merge:** {n_merged}")
    lines.append(f"- **Reduced:** {reduction} ({pct:.0f}%)\n")

    # how many raw events map to each merged event
    if len(mapping) > 0:
        counts = mapping.group_by("merged_event_id").agg(pl.len().alias("n_raw"))
        lines.append("## Raw events per merged event\n")
        lines.append("| Merged ID | Raw events combined |")
        lines.append("|-----------|---------------------|")
        for row in counts.sort("n_raw", descending=True).head(20).iter_rows(named=True):
            lines.append(f"| {row['merged_event_id']} | {row['n_raw']} |")

    return "\n".join(lines)


# ── Report 3: Method Co-occurrence ──────────────────────────
def report_method_cooccurrence(events: pl.DataFrame) -> str:
    lines = ["# Report 3: Method Co-occurrence\n"]

    if "trigger_reasons" not in events.columns:
        lines.append("No trigger_reasons data.")
        return "\n".join(lines)

    # parse trigger_reasons and count pairs
    pair_counter: Counter[tuple[str, str]] = Counter()
    method_counter: Counter[str] = Counter()

    for reasons_str in events["trigger_reasons"].to_list():
        methods = sorted(set(r.strip() for r in reasons_str.split(", ") if r.strip()))
        for m in methods:
            method_counter[m] += 1
        for i in range(len(methods)):
            for j in range(i + 1, len(methods)):
                pair_counter[(methods[i], methods[j])] += 1

    # method frequency
    lines.append("## Individual Method Frequency\n")
    lines.append("| Method | Count | % of events |")
    lines.append("|--------|-------|-------------|")
    for method, count in method_counter.most_common():
        pct = count / len(events) * 100
        lines.append(f"| {method} | {count} | {pct:.1f}% |")

    # pair frequency
    lines.append("\n## Method Pairs (co-occurrence)\n")
    lines.append("| Method A | Method B | Count |")
    lines.append("|----------|----------|-------|")
    for (a, b), count in pair_counter.most_common(15):
        lines.append(f"| {a} | {b} | {count} |")

    # multi-trigger stats
    n_multi = len(events.filter(pl.col("n_triggers") > 1))
    lines.append(f"\n**Multi-trigger events:** {n_multi} / {len(events)} ({n_multi / len(events) * 100:.0f}%)")

    return "\n".join(lines)


# ── Report 4: Top Similar Event Pairs ───────────────────────
def report_similar_pairs(events: pl.DataFrame, windows: pl.DataFrame) -> str:
    lines = ["# Report 4: Top Similar Event Pairs\n"]

    if windows is None or len(windows) == 0:
        lines.append("No windows data.")
        return "\n".join(lines)

    # compute per-event feature vector (pre-event window only)
    pre_window = windows.filter(pl.col("relative_bar") < 0)
    if len(pre_window) == 0:
        lines.append("No pre-event window data.")
        return "\n".join(lines)

    features = pre_window.group_by("event_id").agg([
        pl.col("symbol").first(),
        pl.col("timeframe").first(),
        pl.col("magnitude_pct").first(),
        pl.col("abs_ts").first(),
        pl.col("body_pct").mean().alias("f_body_mean"),
        pl.col("body_pct").std().alias("f_body_std"),
        pl.col("range_pct").mean().alias("f_range_mean"),
        pl.col("range_pct").std().alias("f_range_std"),
        pl.col("volume").mean().alias("f_vol_mean"),
        pl.col("volume").std().alias("f_vol_std"),
    ])

    # drop events whose pre-window was too short to compute a std (e.g. only
    # 1 bar available near the start of history) — their NaN propagates
    # through the dot product and corrupts every pair it's compared against
    feat_cols = ["f_body_mean", "f_body_std", "f_range_mean", "f_range_std", "f_vol_mean", "f_vol_std"]
    n_before_dropna = len(features)
    features = features.filter(pl.all_horizontal([pl.col(c).is_not_nan() for c in feat_cols]))
    if len(features) < n_before_dropna:
        lines.append(f"*(dropped {n_before_dropna - len(features)} events with insufficient pre-window bars)*\n")

    if len(features) < 2:
        lines.append("Not enough events for comparison.")
        return "\n".join(lines)

    # compute pairwise cosine similarity (simplified)
    feat_matrix = features.select(feat_cols).to_numpy()

    # z-score each feature first — raw volume (1e6-1e11) otherwise swamps the
    # price-shape features (single-digit %), making cosine similarity just a
    # comparison of volume scale and producing near-1.0 scores for unrelated
    # events.
    col_mean = feat_matrix.mean(axis=0, keepdims=True)
    col_std = feat_matrix.std(axis=0, keepdims=True)
    col_std[col_std == 0] = 1
    feat_matrix = (feat_matrix - col_mean) / col_std

    # normalize
    norms = np.linalg.norm(feat_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    feat_norm = feat_matrix / norms

    # compute similarity matrix
    sim = feat_norm @ feat_norm.T

    # find top similar pairs (excluding self)
    n = len(features)
    pairs: list[tuple[float, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append((float(sim[i, j]), i, j))

    pairs.sort(key=lambda x: -x[0])

    lines.append("| Rank | Event A | Event B | Similarity |")
    lines.append("|------|---------|---------|------------|")
    for rank, (score, i, j) in enumerate(pairs[:15], 1):
        ev_a = features.row(i, named=True)
        ev_b = features.row(j, named=True)
        a_label = f"{ev_a['symbol']} {_ms_to_date(int(ev_a['abs_ts']))}"
        b_label = f"{ev_b['symbol']} {_ms_to_date(int(ev_b['abs_ts']))}"
        lines.append(f"| {rank} | {a_label} | {b_label} | {score:.3f} |")

    return "\n".join(lines)


# ── Report 5: Common Pre-Event Patterns ─────────────────────
def report_pre_event_patterns(events: pl.DataFrame, windows: pl.DataFrame) -> str:
    lines = ["# Report 5: Common Pre-Event Patterns\n"]

    if windows is None or len(windows) == 0:
        lines.append("No windows data.")
        return "\n".join(lines)

    pre = windows.filter(pl.col("relative_bar") < 0)
    if len(pre) == 0:
        lines.append("No pre-event data.")
        return "\n".join(lines)

    n_events = pre["event_id"].n_unique()
    lines.append(f"Analyzing pre-event windows of {n_events} events\n")

    # Pattern 1: range compression before event
    # Compare avg range in [-40, -20] vs [-20, 0]
    range_late = pre.filter(pl.col("relative_bar") >= -20)["range_pct"].mean()
    range_early = pre.filter((pl.col("relative_bar") >= -40) & (pl.col("relative_bar") < -20))["range_pct"].mean()
    if range_early and range_late:
        compression = (1 - range_late / range_early) * 100 if range_early > 0 else 0
        lines.append("## 1. Range Compression\n")
        lines.append(f"- Avg range [-40, -20]: {range_early:.4f}%")
        lines.append(f"- Avg range [-20, 0]: {range_late:.4f}%")
        lines.append(f"- Compression: {compression:.1f}%")
        pct_events = len(pre.filter((pl.col("relative_bar") >= -20)).group_by("event_id").agg(
            pl.col("range_pct").mean().alias("r")
        ).filter(pl.col("r") < range_early)) / n_events * 100 if range_early > 0 else 0
        lines.append(f"- Events with compression: ~{pct_events:.0f}%\n")

    # Pattern 2: volume buildup
    vol_early = pre.filter((pl.col("relative_bar") >= -40) & (pl.col("relative_bar") < -20))["volume"].mean()
    vol_late = pre.filter(pl.col("relative_bar") >= -20)["volume"].mean()
    if vol_early and vol_late and vol_early > 0:
        vol_change = (vol_late / vol_early - 1) * 100
        lines.append("## 2. Volume Buildup\n")
        lines.append(f"- Avg volume [-40, -20]: {vol_early:.0f}")
        lines.append(f"- Avg volume [-20, 0]: {vol_late:.0f}")
        lines.append(f"- Change: {vol_change:+.1f}%\n")

    # Pattern 3: directional bias
    # Count events where close > open in [-10, 0] (for longs)
    events_with_dir = events.filter(pl.col("direction") == "long")
    if len(events_with_dir) > 0:
        # for long events, check if pre-window has bullish bias
        long_events = events_with_dir["event_id"].to_list()
        pre_long = pre.filter(pl.col("event_id").is_in(long_events))
        bullish_bars = pre_long.filter(pl.col("relative_bar") >= -10).filter(pl.col("body_pct") > 0)
        total_bars_last10 = pre_long.filter(pl.col("relative_bar") >= -10)
        if len(total_bars_last10) > 0:
            bullish_pct = len(bullish_bars) / len(total_bars_last10) * 100
            lines.append("## 3. Directional Bias (Long Events)\n")
            lines.append(f"- Bullish bars in [-10, 0]: {bullish_pct:.0f}%")
            lines.append("- (Random would be ~50%)\n")

    # Pattern 4: body size distribution
    lines.append("## 4. Body Size Distribution\n")
    body_stats = pre.filter(pl.col("relative_bar") >= -20).select([
        pl.col("body_pct").abs().mean().alias("avg_abs_body"),
        pl.col("body_pct").abs().quantile(0.25).alias("q25"),
        pl.col("body_pct").abs().quantile(0.50).alias("q50"),
        pl.col("body_pct").abs().quantile(0.75).alias("q75"),
    ])
    if len(body_stats) > 0:
        row = body_stats.row(0)
        lines.append(f"- Mean |body|: {row[0]:.4f}%")
        lines.append(f"- P25: {row[1]:.4f}%")
        lines.append(f"- P50: {row[2]:.4f}%")
        lines.append(f"- P75: {row[3]:.4f}%\n")

    # Pattern 5: wick analysis
    lines.append("## 5. Wick Patterns\n")
    wick_stats = pre.filter(pl.col("relative_bar") >= -10).select([
        pl.col("upper_wick").mean().alias("avg_upper"),
        pl.col("lower_wick").mean().alias("avg_lower"),
        pl.col("range").mean().alias("avg_range"),
    ])
    if len(wick_stats) > 0:
        row = wick_stats.row(0)
        if row[2] > 0:
            upper_pct = row[0] / row[2] * 100
            lower_pct = row[1] / row[2] * 100
            lines.append(f"- Upper wick: {upper_pct:.0f}% of range")
            lines.append(f"- Lower wick: {lower_pct:.0f}% of range")
            lines.append("- (Balance = 50/50, upper-heavy = selling pressure, lower-heavy = buying pressure)\n")

    return "\n".join(lines)


# ── Report 6: Manipulation Signature (absorption / liquidity sweep) ─
def report_manipulation_signature(merged: pl.DataFrame) -> str:
    """
    Maps observed event geometry to the manipulation patterns described by
    traders: a real impulse that gets sharply absorbed/reversed afterward
    (pullback_after_peak), and a liquidity sweep that precedes the real move
    (drawdown_before_peak — price dips/spikes past a prior level before
    reversing into the recorded event).
    """
    lines = ["# Report 6: Manipulation Signature\n"]

    if len(merged) == 0:
        lines.append("No merged events.")
        return "\n".join(lines)

    n = len(merged)

    # "single-candle absorption": most of the move is given back right after the peak
    strong_absorption = merged.filter(pl.col("max_pullback_after_peak_pct") >= 50.0)
    lines.append("## Post-Peak Absorption (impulse given back)\n")
    lines.append(f"- Events with ≥50% of the move given back after peak: "
                 f"{len(strong_absorption)} / {n} ({len(strong_absorption) / n * 100:.0f}%)")
    lines.append(f"- Median pullback-after-peak: {merged['max_pullback_after_peak_pct'].median():.1f}%\n")

    # "liquidity sweep before the move": a meaningful drawdown precedes the peak
    swept = merged.filter(pl.col("max_drawdown_before_peak_pct") >= 20.0)
    lines.append("## Pre-Peak Sweep (drawdown before the real move)\n")
    lines.append(f"- Events with ≥20% drawdown before peak: "
                 f"{len(swept)} / {n} ({len(swept) / n * 100:.0f}%)")
    lines.append(f"- Median drawdown-before-peak: {merged['max_drawdown_before_peak_pct'].median():.1f}%\n")

    # multi-TF confirmation: how often the same event was seen on 3+ timeframes
    if "n_tfs" in merged.columns:
        multi_tf = merged.filter(pl.col("n_tfs") >= 3)
        lines.append("## Multi-Timeframe Confirmation\n")
        lines.append(f"- Events confirmed on ≥3 timeframes: "
                     f"{len(multi_tf)} / {n} ({len(multi_tf) / n * 100:.0f}%)\n")

    return "\n".join(lines)


# ── Experiment Metadata ─────────────────────────────────────
def save_experiment_meta(version: int | None = None) -> Path:
    """Save reproducibility metadata."""
    if version is None:
        version = get_active_version()

    meta = {
        "dataset_version": f"v{version}",
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # count outputs
    for name in [
        "events.parquet", "events_merged.parquet", "event_merge_mapping.parquet",
        "windows.parquet", "controls.parquet",
        "event_features.parquet", "control_features.parquet",
        "discovery_config.toml",
    ]:
        p = report_path(name)
        meta[f"has_{name}"] = p.exists()

    # counts
    events = _load("events.parquet")
    if events is not None:
        meta["n_events_raw"] = len(events)

    merged = _load("events_merged.parquet")
    if merged is not None:
        meta["n_events_merged"] = len(merged)

    windows = _load("windows.parquet")
    if windows is not None:
        meta["n_window_rows"] = len(windows)

    controls = _load("controls.parquet")
    if controls is not None:
        meta["n_control_rows"] = len(controls)

    out = report_path("experiment_meta.json")
    out.write_text(json.dumps(meta, indent=2, default=str))
    return out


# ── Main ────────────────────────────────────────────────────
def generate_summary(version: int | None = None) -> None:
    """Generate all 5 research reports + metadata."""
    events = _load("events.parquet")
    merged = _load("events_merged.parquet")
    mapping = _load("event_merge_mapping.parquet")
    windows = _load("windows.parquet")

    sections = []

    # Report 1
    if events is not None:
        sections.append(report_inventory(events))

    # Report 2
    if events is not None and merged is not None and mapping is not None:
        sections.append(report_merge_impact(events, merged, mapping))

    # Report 3
    if events is not None:
        sections.append(report_method_cooccurrence(events))

    # Report 4
    if merged is not None and windows is not None:
        sections.append(report_similar_pairs(merged, windows))

    # Report 5
    if merged is not None and windows is not None:
        sections.append(report_pre_event_patterns(merged, windows))

    # Report 6
    if merged is not None:
        sections.append(report_manipulation_signature(merged))

    # combine
    header = f"# Research Report\n\n*Generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}*\n\n---\n\n"
    full_report = header + "\n\n---\n\n".join(sections)

    out = report_path("research_report.md")
    out.write_text(full_report)
    print(f"Research report → {out}")

    # save metadata
    meta_path = save_experiment_meta(version)
    print(f"Experiment meta → {meta_path}")


if __name__ == "__main__":
    generate_summary()
