"""Experiment: are the LONG patterns unfairly timed out by a too-short horizon?

Ground truth (research/manipulations_corpus/long_manip_3types): the long manipulations are
MEDIUM-TERM — "в среднесроке 250%", 100–400% moves that play out over weeks (BSB, ESPROC).
`backtest_scanner.FWD_DAYS` caps long horizons at 4–5 d — an ASSUMPTION, not ground truth.
The SHORT (pump-absorption) is genuinely fast (hours–2-3 d), so keep it short.

This re-runs the replay with a DIRECTION-AWARE horizon: shorts unchanged, longs extended
(HUNT_LONG_HORIZON_DAYS, default 21). Everything else identical. Compare per-direction R.

Run:  uv run python -m research.backtest_horizon_variant research/dataset_v10
"""
from __future__ import annotations

import collections
import glob
import os
import sys

from research.backtest_scanner import (
    _load, _closed_upto, _simulate, TF_MS, FWD_DAYS, _TP1_MOVE,
)
from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer

_HERE = os.path.dirname(os.path.abspath(__file__))
_LONG_HORIZON_DAYS = float(os.getenv("HUNT_LONG_HORIZON_DAYS", "21") or 21)


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
        # DIRECTION-AWARE horizon: longs are medium-term, shorts are fast.
        fwd_days = _LONG_HORIZON_DAYS if long else FWD_DAYS.get(setup.meso_tf, 3.0)
        horizon = int(fwd_days * 86_400_000)
        tp1 = entry * (1 + _TP1_MOVE) if long else entry * (1 - _TP1_MOVE)
        deep = geo.get("primary_target") or setup.target
        deep_move = (float(deep) - entry) / entry if long else (entry - float(deep)) / entry
        if deep_move < _TP1_MOVE:
            continue
        outcome, r_mult = _simulate(fine, t, setup.direction, entry, geo["stop"],
                                    tp1, deep, horizon, avg_price=geo.get("averaging_price"))
        completed.append((setup, entry, outcome, r_mult))
    return completed


def main(ds: str) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    grand, per_dir = [], collections.defaultdict(collections.Counter)
    r_by_dir: dict[str, float] = collections.defaultdict(float)
    for sym in syms:
        for setup, entry, outcome, r_mult in run_symbol(ds, sym):
            grand.append((setup.direction, setup.pattern_type, outcome, r_mult))
            per_dir[setup.direction][outcome] += 1
            r_by_dir[setup.direction] += r_mult
    outs = collections.Counter(g[2] for g in grand)
    r_total = sum(g[3] for g in grand)
    print(f"===== HORIZON VARIANT (long={_LONG_HORIZON_DAYS}d) SUMMARY: {len(grand)} setups | "
          f"{dict(outs)} =====")
    print(f"total R={r_total:+.1f}  avg R/trade={r_total/max(len(grand),1):+.2f}")
    for d in ("long", "short"):
        n = sum(per_dir[d].values())
        print(f"  {d}: {dict(per_dir[d])}  R={r_by_dir[d]:+.1f}  avgR={r_by_dir[d]/max(n,1):+.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "dataset_v10"))
