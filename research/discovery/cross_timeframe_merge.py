"""
Cross-timeframe event merge — deduplicate events found on multiple TFs.

The same pump might be detected on 5m, 15m, and 1h.
This module merges them into a single canonical event, keeping the
highest-resolution timeframe as primary and recording which TFs detected it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import report_path  # noqa: E402

# TF priority: smaller = higher resolution = primary
TF_PRIORITY = {
    "1m": 0, "3m": 1, "5m": 2, "15m": 3, "30m": 4,
    "1h": 5, "2h": 6, "4h": 7, "6h": 8, "8h": 9,
    "12h": 10, "1d": 11, "3d": 12, "1w": 13, "1M": 14,
}

# events are considered the same if they overlap in time by this fraction
OVERLAP_THRESHOLD = 0.3  # 30% overlap → merge


def _ts_range_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    """Return fraction of the shorter event that overlaps with the longer one."""
    overlap_start = max(a_start, b_start)
    overlap_end = min(a_end, b_end)
    if overlap_start >= overlap_end:
        return 0.0
    overlap = overlap_end - overlap_start
    a_len = a_end - a_start
    b_len = b_end - b_start
    shorter = min(a_len, b_len)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def merge_cross_tf(events_path: str | Path | None = None) -> pl.DataFrame:
    """
    Read events.parquet, merge events that represent the same market move
    across different timeframes.

    Returns a new DataFrame with canonical events.
    """
    if events_path is None:
        events_path = report_path("events.parquet")
    else:
        events_path = Path(events_path)

    if not events_path.exists():
        print(f"No events at {events_path}")
        return pl.DataFrame()

    events = pl.read_parquet(events_path)
    if len(events) == 0:
        return events

    print(f"Input: {len(events)} events across {events['timeframe'].n_unique()} timeframes")

    # group by symbol
    symbols = events["symbol"].unique().to_list()
    all_merged: list[dict] = []
    raw_to_merged: list[dict] = []  # raw_event_id → merged_event_id
    merged_id = 0

    for sym in symbols:
        sym_events = events.filter(pl.col("symbol") == sym)
        # sort by start time
        sym_events = sym_events.sort("event_start_ts")

        # convert to list of dicts for easier manipulation
        event_dicts = sym_events.to_dicts()

        # greedy merge: for each event, try to merge with an existing canonical event
        canonical: list[dict] = []

        for ev in event_dicts:
            merged = False
            for can in canonical:
                # Overlap is checked against the anchor range (the
                # founding/highest-resolution event's own start/end), never
                # against the merged/expanded display range. Comparing
                # against the ever-growing union causes a snowball where one
                # event drags in unrelated later events, which drag in more,
                # collapsing an entire symbol's history into one "event".
                overlap = _ts_range_overlap(
                    ev["event_start_ts"], ev["event_end_ts"],
                    can["_anchor_start"], can["_anchor_end"],
                )
                if overlap >= OVERLAP_THRESHOLD:
                    # merge into canonical
                    _merge_into(can, ev)
                    raw_to_merged.append({
                        "raw_event_id": ev.get("event_id"),
                        "merged_event_id": can["_merged_id"],
                    })
                    merged = True
                    break

            if not merged:
                # start new canonical event
                ev_dict = _to_canonical(ev)
                ev_dict["_merged_id"] = merged_id
                ev_dict["_anchor_start"] = ev["event_start_ts"]
                ev_dict["_anchor_end"] = ev["event_end_ts"]
                merged_id += 1
                canonical.append(ev_dict)
                raw_to_merged.append({
                    "raw_event_id": ev.get("event_id"),
                    "merged_event_id": ev_dict["_merged_id"],
                })

        all_merged.extend(canonical)

    if not all_merged:
        print("No events after cross-TF merge.")
        return pl.DataFrame()

    # remove internal bookkeeping fields before saving
    for ev in all_merged:
        ev.pop("_merged_id", None)
        ev.pop("_anchor_start", None)
        ev.pop("_anchor_end", None)

    result = pl.DataFrame(all_merged)
    out = report_path("events_merged.parquet")
    result.write_parquet(out)

    # save raw→merged mapping
    mapping_df = pl.DataFrame(raw_to_merged)
    mapping_out = report_path("event_merge_mapping.parquet")
    mapping_df.write_parquet(mapping_out)

    n_before = len(events)
    n_after = len(result)
    n_reduced = n_before - n_after
    print(f"\nCross-TF merge: {n_before} → {n_after} events (reduced {n_reduced})")
    print(f"Raw→merged mapping: {len(mapping_df)} rows → {mapping_out}")

    # show which TFs contributed
    if "detected_on_tfs" in result.columns:
        print("\nTF coverage of merged events:")
        for row in result.select("detected_on_tfs").head(20).iter_rows(named=True):
            print(f"  {row['detected_on_tfs']}")

    return result


def _to_canonical(ev: dict) -> dict:
    """Convert a raw event dict to a canonical merged event."""
    return {
        "event_id": ev.get("event_id"),
        "symbol": ev["symbol"],
        "primary_tf": ev["timeframe"],
        "event_start_ts": ev["event_start_ts"],
        "event_peak_ts": ev["event_peak_ts"],
        "event_end_ts": ev["event_end_ts"],
        "direction": ev["direction"],
        "magnitude_pct": ev["magnitude_pct"],
        "duration_bars": ev["duration_bars"],
        "duration_ms": ev["duration_ms"],
        "start_price": ev["start_price"],
        "peak_price": ev["peak_price"],
        "end_price": ev["end_price"],
        "max_drawdown_before_peak_pct": ev.get("max_drawdown_before_peak_pct", 0.0),
        "max_pullback_after_peak_pct": ev.get("max_pullback_after_peak_pct", 0.0),
        "trigger_reasons": ev.get("trigger_reasons", ""),
        "n_triggers": ev.get("n_triggers", 1),
        "detected_on_tfs": ev["timeframe"],
        "n_tfs": 1,
    }


def _merge_into(can: dict, ev: dict) -> None:
    """Merge event ev into canonical event can (in-place)."""
    # extend time range to cover both
    can["event_start_ts"] = min(can["event_start_ts"], ev["event_start_ts"])
    can["event_end_ts"] = max(can["event_end_ts"], ev["event_end_ts"])

    # keep the peak with larger magnitude
    if ev["magnitude_pct"] > can["magnitude_pct"]:
        can["event_peak_ts"] = ev["event_peak_ts"]
        can["magnitude_pct"] = ev["magnitude_pct"]
        can["peak_price"] = ev["peak_price"]
        can["start_price"] = ev["start_price"]
        can["end_price"] = ev["end_price"]

    # prefer higher-resolution TF as primary; also re-anchor the overlap
    # reference to the highest-resolution event's own (unexpanded) range,
    # since that's the most precise estimate of the event's true duration
    can_pri = TF_PRIORITY.get(can["primary_tf"], 99)
    ev_pri = TF_PRIORITY.get(ev["timeframe"], 99)
    if ev_pri < can_pri:
        can["primary_tf"] = ev["timeframe"]
        can["_anchor_start"] = ev["event_start_ts"]
        can["_anchor_end"] = ev["event_end_ts"]

    # update duration from the wider range
    can["duration_ms"] = can["event_end_ts"] - can["event_start_ts"]

    # combine trigger reasons
    can_reasons = set(can["trigger_reasons"].split(", ")) if can["trigger_reasons"] else set()
    ev_reasons = set(ev["trigger_reasons"].split(", ")) if ev.get("trigger_reasons") else set()
    can["trigger_reasons"] = ", ".join(sorted(can_reasons | ev_reasons))

    # track TFs
    existing_tfs = set(can["detected_on_tfs"].split(", "))
    existing_tfs.add(ev["timeframe"])
    can["detected_on_tfs"] = ", ".join(sorted(existing_tfs, key=lambda t: TF_PRIORITY.get(t, 99)))
    can["n_tfs"] = len(existing_tfs)

    # update drawdown/pullback to worst case
    can["max_drawdown_before_peak_pct"] = max(
        can.get("max_drawdown_before_peak_pct", 0),
        ev.get("max_drawdown_before_peak_pct", 0),
    )
    can["max_pullback_after_peak_pct"] = max(
        can.get("max_pullback_after_peak_pct", 0),
        ev.get("max_pullback_after_peak_pct", 0),
    )


if __name__ == "__main__":
    merge_cross_tf()
