"""Experiment: does the SOURCE-FAITHFUL target rule fix Pattern B expectancy?

Transcript (research/manipulations_corpus/): the manipulation-short take-profit is the
IMPULSE-SET LOW — "тяните сделку на ПОЛНОЕ ПОГЛОЩЕНИЕ ПАМПА", TP just below the low the
impulse set ("там всегда сидит продавец"). The production detector instead aims at the
DEEPEST distance-ranked structural pool (`_target_ladder`, score ∝ distance) — that is the
ZEC-220 fantasy target and the 52% timeout / near-0 win signature.

This variant re-runs the SAME no-lookahead replay but overrides the deep (runner) target
for Pattern B shorts with the PUMP-BASE LOW (full pump absorption) computed from the meso
frame at signal time — everything else identical. Compare avg R vs the baseline.

Run:  uv run python -m research.backtest_target_variant research/dataset_v10
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


def _pump_base_low(meso_bars, lookback: int = 60) -> float:
    """Low of the pump structure = full-absorption target (transcript: impulse-set low)."""
    seg = meso_bars[-lookback:] if len(meso_bars) > lookback else meso_bars
    lows = [r[3] for r in seg]  # OHLCV: index 3 = low
    return float(min(lows)) if lows else 0.0


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
        horizon = int(FWD_DAYS.get(setup.meso_tf, 5.0) * 86_400_000)
        long = setup.direction == "long"
        tp1 = entry * (1 + _TP1_MOVE) if long else entry * (1 - _TP1_MOVE)

        # ── VARIANT: source-faithful deep target ──────────────────────────────
        if setup.direction == "short":
            base_low = _pump_base_low(meso_bars)
            # TP = just inside the impulse-set low range (transcript: "чуть ниже него")
            deep = base_low if base_low > 0 else (geo.get("primary_target") or setup.target)
        else:
            deep = geo.get("primary_target") or setup.target
        # ──────────────────────────────────────────────────────────────────────

        deep_move = (float(deep) - entry) / entry if long else (entry - float(deep)) / entry
        if deep_move < _TP1_MOVE:
            continue
        outcome, r_mult = _simulate(fine, t, setup.direction, entry, geo["stop"],
                                    tp1, deep, horizon, avg_price=geo.get("averaging_price"))
        completed.append((setup, entry, geo["stop"], deep, outcome, r_mult))
    return completed


def main(ds: str) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    grand, per_pattern = [], collections.defaultdict(collections.Counter)
    for sym in syms:
        for setup, entry, stop, target, outcome, r_mult in run_symbol(ds, sym):
            grand.append((setup.direction, setup.pattern_type, outcome, r_mult))
            per_pattern[setup.pattern_type][outcome] += 1
    outs = collections.Counter(g[2] for g in grand)
    wins, losses = outs.get("win", 0), outs.get("loss", 0)
    scratch, to = outs.get("scratch", 0), outs.get("timeout", 0)
    decided = wins + losses + scratch
    r_total = sum(g[3] for g in grand)
    print(f"===== VARIANT (impulse-low target) SUMMARY: {len(grand)} setups | "
          f"win={wins} scratch={scratch} loss={losses} timeout={to} =====")
    if decided:
        print(f"profitable (win+scratch)={100*(wins+scratch)/decided:.0f}%  "
              f"total R={r_total:+.1f}  avg R/trade={r_total/max(len(grand),1):+.2f}")
    print("by pattern:", {p: dict(c) for p, c in per_pattern.items()})


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "dataset_v10"))
