"""Experiment: does gating SHORTS on LTF-confirmed reversal rescue them?

GTC transcript: the short is taken AFTER the lower-TF reversal confirms ("увидели
подтверждение на младшем таймфрейме и полетели вниз"). The detector currently emits
shorts on the bare fade too (ltf_pending, score 0.7), LTF only a strength upgrade. This
counts ONLY shorts with setup.micro_confirmed (ltf_confirmed) and reports their avg R.
If that subset is profitable, gate live shorts on LTF-confirmed; else shorts have no edge.

Run:  uv run python -m research.backtest_short_ltfgate research/dataset_v10
"""
from __future__ import annotations

import collections
import glob
import os
import sys

from research.backtest_scanner import (
    _load, _closed_upto, _simulate, TF_MS, _horizon_days, _TP1_MOVE,
)
from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer

_HERE = os.path.dirname(os.path.abspath(__file__))


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
        confirmed = bool(getattr(setup, "micro_confirmed", False))
        horizon = int(_horizon_days(setup.direction, setup.meso_tf) * 86_400_000)
        tp1 = entry * (1 + _TP1_MOVE) if long else entry * (1 - _TP1_MOVE)
        deep = geo.get("primary_target") or setup.target
        deep_move = (float(deep) - entry) / entry if long else (entry - float(deep)) / entry
        if deep_move < _TP1_MOVE:
            continue
        outcome, r_mult = _simulate(fine, t, setup.direction, entry, geo["stop"],
                                    tp1, deep, horizon, avg_price=geo.get("averaging_price"))
        # bucket: short_confirmed / short_pending / long
        bucket = "long" if long else ("short_confirmed" if confirmed else "short_pending")
        completed.append((bucket, outcome, r_mult))
    return completed


def main(ds: str) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    per = collections.defaultdict(collections.Counter)
    r_by: dict[str, float] = collections.defaultdict(float)
    n_by: dict[str, int] = collections.defaultdict(int)
    for sym in syms:
        for bucket, outcome, r_mult in run_symbol(ds, sym):
            per[bucket][outcome] += 1
            r_by[bucket] += r_mult
            n_by[bucket] += 1
    print("===== SHORT LTF-GATE experiment =====")
    for b in ("long", "short_confirmed", "short_pending"):
        n = n_by[b]
        print(f"  {b}: {dict(per[b])}  R={r_by[b]:+.1f}  avgR={r_by[b]/max(n,1):+.2f}  (n={n})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "dataset_v10"))
