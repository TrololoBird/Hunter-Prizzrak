"""Calibrate hunt_core.scanner.detect thresholds against real research events.

Runs the ACTUAL detector primitives (detect_impulse, detect_absorption,
detect_bokovik, candle_fade_ratio) against each event's own pre-event window
from windows.parquet — not an abstract proxy metric. This answers: "if we
require threshold X, how often would it actually fire on our 107 confirmed
manipulation events?" instead of guessing a number.

Output: reports/detector_calibration.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import report_path  # noqa: E402

from hunt_core.scanner.detect.events import (  # noqa: E402
    detect_impulse, detect_bokovik, candle_fade_ratio, two_bar_reversal, rejection_at_peak,
)


def _event_pre_df(windows: pl.DataFrame, event_id: int) -> pl.DataFrame:
    """Rebuild an ohlcv_to_df-shaped frame for one event's pre-window (bars < 0)."""
    rows = (
        windows.filter((pl.col("event_id") == event_id) & (pl.col("relative_bar") < 0))
        .sort("relative_bar")
    )
    return rows.select([
        pl.col("abs_ts").alias("ts"),
        pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"),
    ])


def _achieved_absorption_ratio(df: pl.DataFrame, impulse_idx: int) -> float | None:
    """Same math as detect_absorption, but returns the ratio achieved instead of a bool."""
    if impulse_idx < 1 or impulse_idx >= len(df):
        return None
    pre = float(df["close"][impulse_idx - 1])
    imp_open = float(df["open"][impulse_idx])
    imp_close = float(df["close"][impulse_idx])
    is_green = imp_close > imp_open
    extreme = float(df["high"][impulse_idx]) if is_green else float(df["low"][impulse_idx])
    imp_range = abs(extreme - pre)
    if imp_range <= 0:
        return None
    post = df.slice(impulse_idx)
    retrace_extreme = float(post["low"].min()) if is_green else float(post["high"].max())
    return abs(retrace_extreme - extreme) / imp_range


def calibrate_absorption(windows: pl.DataFrame, events: pl.DataFrame) -> dict:
    ratios: list[float] = []
    n_no_impulse = 0
    for row in events.iter_rows(named=True):
        pre_df = _event_pre_df(windows, row["event_id"])
        if len(pre_df) < 20:
            continue
        direction = "up" if row["direction"] == "long" else "down"
        imp_ok, imp_idx = detect_impulse(pre_df, lookback=30, direction=direction)
        if not imp_ok or imp_idx is None:
            n_no_impulse += 1
            continue
        ratio = _achieved_absorption_ratio(pre_df, imp_idx)
        if ratio is not None:
            ratios.append(min(ratio, 3.0))  # cap runaway ratios from tiny impulse_range
    return {"ratios": ratios, "n_events": len(events), "n_no_impulse": n_no_impulse}


def calibrate_bokovik(windows: pl.DataFrame, events: pl.DataFrame) -> dict:
    widths, touches_list, atr_ratios, n_found = [], [], [], 0
    for row in events.iter_rows(named=True):
        pre_df = _event_pre_df(windows, row["event_id"])
        if len(pre_df) < 65:
            continue
        b = detect_bokovik(pre_df, window=30, min_touches=0, max_width_pct=9999.0)
        if b is None:
            continue
        n_found += 1
        widths.append(b["width_pct"])
        touches_list.append(b["touches"])
        atr_ratios.append(b["atr_ratio"])
    return {"widths": widths, "touches": touches_list, "atr_ratios": atr_ratios,
            "n_events": len(events), "n_found": n_found}


def _event_full_df(windows: pl.DataFrame, event_id: int) -> pl.DataFrame:
    rows = windows.filter(pl.col("event_id") == event_id).sort("relative_bar")
    return rows.select([
        pl.col("abs_ts").alias("ts"),
        pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume"),
    ])


def calibrate_fade(windows: pl.DataFrame, events: pl.DataFrame) -> dict:
    """Only meaningful for short (Pattern B) events — fade happens at the top before the dump.

    Must call candle_fade_ratio with peak_high= (as patterns.py actually does) — the
    no-peak_high branch is a different code path with different semantics.
    """
    body_ratios, range_ratios = [], []
    two_bar_hits, reject_hits, any_hits = 0, 0, 0
    shorts = events.filter(pl.col("direction") == "short")
    n_analyzed = 0
    for row in shorts.iter_rows(named=True):
        full_df = _event_full_df(windows, row["event_id"])
        if len(full_df) < 30:
            continue
        n_analyzed += 1
        peak = row["peak_price"]
        br, rr = candle_fade_ratio(full_df, n=8, peak_high=peak)
        body_ratios.append(br)
        range_ratios.append(rr)
        tb = two_bar_reversal(full_df, peak)
        rj = rejection_at_peak(full_df, peak)
        fade_old = br <= 0.50 and rr <= 0.60
        two_bar_hits += tb
        reject_hits += rj
        any_hits += 1 if (tb or rj or fade_old) else 0
    return {
        "body_ratios": body_ratios, "range_ratios": range_ratios,
        "n_events": len(shorts), "n_analyzed": n_analyzed,
        "two_bar_hits": two_bar_hits, "reject_hits": reject_hits, "any_hits": any_hits,
    }


def _pct(vals: list[float], p: float) -> float:
    return float(np.percentile(vals, p)) if vals else float("nan")


def build_report() -> str:
    windows = pl.read_parquet(report_path("windows.parquet"))
    events = pl.read_parquet(report_path("events_merged.parquet"))

    lines = ["# Detector Calibration (ACTUAL detect_* functions vs. 107 real events)\n"]
    lines.append(f"Source: `hunt_core/scanner/detect/events.py` functions run directly against "
                 f"each event's own pre-event window from `windows.parquet`. n_events={len(events)}.\n")

    abso = calibrate_absorption(windows, events)
    lines.append("## detect_impulse → detect_absorption (Pattern A / B impulse step)\n")
    lines.append(f"- Events with a detectable impulse (body ≥ 1.5×ATR, current code threshold): "
                 f"{len(abso['ratios'])} / {abso['n_events']}")
    lines.append(f"- No impulse found at all: {abso['n_no_impulse']} / {abso['n_events']}")
    if abso["ratios"]:
        r = abso["ratios"]
        lines.append(f"- Achieved absorption ratio — median: {np.median(r):.2f}, "
                     f"P25: {_pct(r,25):.2f}, P75: {_pct(r,75):.2f}, max: {max(r):.2f}")
        for thr in (0.80, 0.50, 0.35, 0.20):
            hit = sum(1 for x in r if x >= thr)
            lines.append(f"  - threshold ≥{thr:.2f} → fires on {hit}/{len(r)} "
                         f"({hit/len(r)*100:.0f}%) of events with a detected impulse")
    lines.append("")

    bok = calibrate_bokovik(windows, events)
    lines.append("## detect_bokovik (post-impulse sideways range)\n")
    lines.append(f"- Sideways range found in pre-event window: {bok['n_found']} / {bok['n_events']} "
                 f"(width/touch filters disabled to see raw shape)")
    if bok["widths"]:
        w, t, a = bok["widths"], bok["touches"], bok["atr_ratios"]
        lines.append(f"- width_pct — median: {np.median(w):.1f}%, P75: {_pct(w,75):.1f}%, max: {max(w):.1f}%")
        lines.append(f"- touches — median: {np.median(t):.1f}, P25: {_pct(t,25):.1f}")
        lines.append(f"- atr_ratio (current/prior) — median: {np.median(a):.2f}, P75: {_pct(a,75):.2f}")
    lines.append("")

    fade = calibrate_fade(windows, events)
    lines.append("## Pattern B step 4: candle_fade_ratio / rejection_at_peak / two_bar_reversal\n")
    lines.append(f"- n short events analyzed: {fade['n_analyzed']} / {fade['n_events']}")
    if fade["body_ratios"]:
        br, rr = fade["body_ratios"], fade["range_ratios"]
        lines.append(f"- candle_fade body_ratio — median: {np.median(br):.2f}, P75: {_pct(br,75):.2f}")
        lines.append(f"- candle_fade range_ratio — median: {np.median(rr):.2f}, P75: {_pct(rr,75):.2f}")
        n = len(br)
        for b_thr, r_thr in ((0.50, 0.60), (0.70, 0.80), (0.90, 0.95)):
            hit = sum(1 for b, r in zip(br, rr) if b <= b_thr and r <= r_thr)
            lines.append(f"  - candle_fade alone, body≤{b_thr}/range≤{r_thr} → {hit}/{n} ({hit/n*100:.0f}%)")
        lines.append(f"- two_bar_reversal alone: {fade['two_bar_hits']}/{n} ({fade['two_bar_hits']/n*100:.0f}%)")
        lines.append(f"- rejection_at_peak alone: {fade['reject_hits']}/{n} ({fade['reject_hits']/n*100:.0f}%)")
        lines.append(f"- **combined (current code: fade OR reject OR two_bar)**: "
                     f"{fade['any_hits']}/{n} ({fade['any_hits']/n*100:.0f}%)")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    report = build_report()
    out = report_path("detector_calibration.md")
    out.write_text(report)
    print(report)
    print(f"\n→ {out}")
