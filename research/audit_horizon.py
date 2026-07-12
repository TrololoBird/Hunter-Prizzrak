"""Audit whether the manipulation backtest's SHORT forward horizon (FWD_DAYS)
manufactures timeouts/losses — i.e. the strategy sits through drawdown for weeks
(пересиживание, среднесрок) but the sim gives 4h-meso setups only 10 days.

Detection is horizon-independent, so we run the detector ONCE per symbol, stash
each setup's sim inputs, and replay _simulate at several horizons. We also report
how much forward 1h data actually exists past each setup (the hard ceiling).

Run: uv run python -m research.audit_horizon research/dataset_v9
"""
from __future__ import annotations

import collections
import glob
import os
import sys

from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer
from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from research.backtest_scanner import TF_MS, _closed_upto, _load, _simulate

_HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86_400_000


def collect(ds: str, sym: str):
    data = _load(ds, sym)
    if "1h" not in data:
        return []
    fine = data["1h"]
    last_t = fine[-1][0]
    out, seen, states = [], set(), None
    for r in data["1h"]:
        t = r[0] + TF_MS["1h"]
        oc = {tf: _closed_upto(rows, tf, t) for tf, rows in data.items()}
        oc = {tf: v for tf, v in oc.items() if len(v) >= 20}
        if "4h" not in oc and "1d" not in oc:
            continue
        states, setup = advance_manipulation_scales(sym, oc, states, now_ms=t)
        if setup is None:
            continue
        meso_bars = oc.get(setup.meso_tf) or oc.get("1h")
        entry = float(setup.entry_ref or meso_bars[-1][4])
        geo = _geometry(setup, price=entry,
                        stop_buffer=_stop_buffer(meso_bars, pattern_a3=(setup.pattern_type == "A3")))
        if geo is None:
            continue
        key = (round(entry, 8), setup.direction, setup.meso_tf)
        if key in seen:
            continue
        seen.add(key)
        tp1 = geo.get("nearest_target") or geo.get("target") or setup.target
        deep = geo.get("primary_target") or geo.get("target") or setup.target
        fwd_days = (last_t - t) / DAY  # forward 1h data available past this setup
        out.append((setup.pattern_type, setup.direction, setup.meso_tf, fine, t,
                    entry, geo["stop"], tp1, deep, geo.get("averaging_price"), fwd_days))
    return out


def main(ds: str) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    rows = []
    for sym in syms:
        rows.extend(collect(ds, sym))
    print(f"dataset={os.path.basename(ds)}  setups={len(rows)}\n")

    # How much forward data do setups actually have?
    fwd = sorted(r[10] for r in rows)

    def q(p: float) -> float:
        return fwd[int(p * (len(fwd) - 1))]

    print(f"forward 1h-data past each setup (days): min={fwd[0]:.1f} "
          f"p25={q(.25):.1f} median={q(.5):.1f} p75={q(.75):.1f} max={fwd[-1]:.1f}")
    starved = sum(1 for f in fwd if f < 10)
    print(f"setups with <10 days of forward data (horizon-starved): {starved}/{len(rows)}\n")

    # Replay each setup at several horizons (days). Uses full available 1h span.
    print(f"{'horizon':>8} {'win':>4} {'scr':>4} {'loss':>5} {'to':>4} {'nodata':>6} {'totR':>7}")
    print("-" * 46)
    for hz_days in (None, 10, 20, 40, 90):
        oc = collections.Counter()
        totR = 0.0
        for (_p, direction, meso_tf, fine, t, entry, stop, tp1, deep, avg, _fd) in rows:
            if hz_days is None:
                from research.backtest_scanner import FWD_DAYS
                hz = int(FWD_DAYS.get(meso_tf, 5.0) * DAY)
            else:
                hz = int(hz_days * DAY)
            outcome, r = _simulate(fine, t, direction, entry, stop, tp1, deep, hz, avg_price=avg)
            oc[outcome] += 1
            totR += r
        label = "orig" if hz_days is None else f"{hz_days}d"
        print(f"{label:>8} {oc.get('win',0):4d} {oc.get('scratch',0):4d} {oc.get('loss',0):5d} "
              f"{oc.get('timeout',0):4d} {oc.get('nodata',0):6d} {totR:+7.1f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "dataset_v9"))
