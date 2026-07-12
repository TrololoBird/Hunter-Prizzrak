"""Experiment: is the SHORT −0.68R real, or an artifact of unfaithful short mechanics?

The baseline sim banks TP1 at a fixed +20% and stops at geo["stop"] (≈3% above pump-high).
The transcripts say the short's FIRST take is ~30-35% ("взяли 30% чистого", MANTA "+35%") and
the stop sits "за импульс-хай С ЗАПАСОМ" (MANTA ≈6%, not 3%). This variant re-runs shorts with
those transcript-faithful mechanics (TP1=30%, stop buffer 6% above pump-high) and compares the
short avg R. Longs are left on the validated 21d medium-term horizon.

Run:  uv run python -m research.backtest_short_mechanics research/dataset_v10
"""
from __future__ import annotations

import collections
import glob
import os
import sys

from research.backtest_scanner import (
    _load, _closed_upto, _simulate, TF_MS, _horizon_days,
)
from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHORT_TP1 = float(os.getenv("HUNT_SHORT_TP1", "0.30") or 0.30)      # transcript ~30%
_SHORT_STOP_BUF = float(os.getenv("HUNT_SHORT_STOP_BUF", "0.06") or 0.06)  # ~6% above pump-high


def run_symbol(ds: str, sym: str):
    data = _load(ds, sym)
    if "1h" not in data:
        return []
    fine = data["1h"]
    completed, seen = [], set()
    states = None
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
        long = setup.direction == "long"
        horizon = int(_horizon_days(setup.direction, setup.meso_tf) * 86_400_000)
        deep = geo.get("primary_target") or setup.target
        if long:
            tp1 = entry * 1.20
            stop = geo["stop"]
        else:
            # transcript-faithful short mechanics
            tp1 = entry * (1 - _SHORT_TP1)
            stop = float(setup.sweep_extreme) * (1 + _SHORT_STOP_BUF)
        deep_move = (float(deep) - entry) / entry if long else (entry - float(deep)) / entry
        if deep_move < 0.20:
            continue
        outcome, r_mult = _simulate(fine, t, setup.direction, entry, stop,
                                    tp1, deep, horizon, avg_price=geo.get("averaging_price"))
        completed.append((setup.direction, outcome, r_mult))
    return completed


def main(ds: str) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    per_dir = collections.defaultdict(collections.Counter)
    r_by_dir: dict[str, float] = collections.defaultdict(float)
    n_by_dir: dict[str, int] = collections.defaultdict(int)
    for sym in syms:
        for direction, outcome, r_mult in run_symbol(ds, sym):
            per_dir[direction][outcome] += 1
            r_by_dir[direction] += r_mult
            n_by_dir[direction] += 1
    print(f"===== SHORT MECHANICS (TP1={_SHORT_TP1:.0%}, stopBuf={_SHORT_STOP_BUF:.0%}) =====")
    for d in ("long", "short"):
        n = n_by_dir[d]
        print(f"  {d}: {dict(per_dir[d])}  R={r_by_dir[d]:+.1f}  avgR={r_by_dir[d]/max(n,1):+.2f}  (n={n})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "dataset_v10"))
