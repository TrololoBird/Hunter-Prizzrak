"""Walk-forward diagnosis of the Hunter detector against the 15 confirmed events.

Unlike the earlier ad-hoc backtest (which fed a single TF's window as the "1d"
macro — meaningless), this loads REAL multi-timeframe context from dataset_v8
(1d/4h/1h/15m/5m, whatever each covers) sliced to each forward step, exactly as
the live scanner would see it. For every event it walks the meso timeframe bar
by bar from the event start toward its peak, advancing the persistent state
machine, and reports:

  - first detection (direction vs expected), or
  - for a miss: the furthest stage/pattern the tracked state reached, so we can
    see WHICH stage gated it out (not just "missed").

Run: .venv/bin/python -m research.discovery.diagnose_misses
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from hunt_core.scanner.detect.patterns import advance_manipulation_scales  # noqa: E402

_TFS = ["1d", "4h", "1h", "15m", "5m"]
_DATASET = "research/dataset_v8"


def _load(sym: str, tf: str) -> pl.DataFrame | None:
    p = Path(f"{_DATASET}/{sym}_USDT_USDT_{tf}.parquet")
    if not p.exists():
        return None
    return pl.read_parquet(p).sort("timestamp")


def _rows_upto(df: pl.DataFrame | None, ts: float) -> list[list[float]]:
    if df is None:
        return []
    sub = df.filter(pl.col("timestamp") <= ts)
    return [[float(r["timestamp"]), float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["volume"])]
            for r in sub.iter_rows(named=True)]


def _meso_tf(frames: dict[str, pl.DataFrame | None]) -> str | None:
    for tf in ("4h", "1h"):
        if frames.get(tf) is not None and len(frames[tf]) > 60:
            return tf
    return None


def diagnose() -> None:
    events = pl.read_parquet("research/reports/events_merged.parquet").sort("event_start_ts")
    n_ok = n_wrong = n_miss = 0
    for row in events.iter_rows(named=True):
        sym = row["symbol"].split("/")[0]
        start_ts, peak_ts = row["event_start_ts"], row["event_peak_ts"]
        exp_dir = row["direction"]
        label = f"{row['symbol']:16s} {row['primary_tf']:4s} {exp_dir:5s} {row['magnitude_pct']:6.1f}%"

        frames = {tf: _load(sym, tf) for tf in _TFS}
        meso = _meso_tf(frames)
        if meso is None or frames.get("1d") is None:
            print(f"{label} -> NO_CONTEXT (meso={meso}, 1d={'yes' if frames.get('1d') is not None else 'no'})")
            n_miss += 1
            continue

        # forward steps = meso bars from event_start .. peak (inclusive), plus a few after
        meso_df = frames[meso]
        step_ts = meso_df.filter(
            (pl.col("timestamp") >= start_ts) & (pl.col("timestamp") <= peak_ts)
        )["timestamp"].to_list()
        if not step_ts:
            step_ts = [peak_ts]

        states: dict = {}
        emitted: list[tuple[str, str]] = []  # (pattern, direction) across the whole walk
        max_stage = (None, 0)
        # A manipulation (pump->dump) legitimately contains BOTH a long leg (the
        # pump) and a short leg (the dump); a live scanner emits each as its own
        # signal at its own time. So collect ALL directions caught across the
        # walk and count a HIT if the labeled leg's direction appears — not just
        # whichever completes first.
        for ts in step_ts:
            by_tf = {tf: _rows_upto(frames[tf], ts) for tf in _TFS if frames.get(tf) is not None}
            by_tf = {tf: bars for tf, bars in by_tf.items() if bars}
            states, setup = advance_manipulation_scales(sym, by_tf, states, now_ms=float(ts))
            for st_dict in states.values():
                st = (st_dict.get("pattern"), int(st_dict.get("stage", 0)))
                if st[1] > max_stage[1]:
                    max_stage = st
            if setup is not None:
                emitted.append((setup.pattern_type, setup.direction))

        dirs = {d for _, d in emitted}
        if exp_dir in dirs:
            n_ok += 1
            extra = f" (+also {sorted(dirs - {exp_dir})})" if dirs - {exp_dir} else ""
            print(f"{label} -> HIT {sorted(set(emitted))}{extra} [OK]")
        elif dirs:
            n_wrong += 1
            print(f"{label} -> ONLY-OPPOSITE {sorted(set(emitted))} [WRONG-DIR]")
        else:
            n_miss += 1
            print(f"{label} -> miss (furthest: pattern={max_stage[0]} stage={max_stage[1]})")

    print(f"\nOK={n_ok} WRONG(opposite-only)={n_wrong} MISS={n_miss} / {len(events)}")


if __name__ == "__main__":
    diagnose()
