"""Experiment: does a REACHABLE target fix Pattern C (long) expectancy, as it did B?

Pattern B (short) was fixed to target the impulse-set low (full absorption) instead
of the deepest distance-ranked pool (`_target_ladder`, score ∝ distance) — that
collapsed the ZEC-220 fantasy-RR / 57-timeout signature (backtest_target_variant.py).
Pattern C (long) still calls `_target_ladder` → `ladder[0]` (≥20%-floored, farther
-biased) → the same fantasy-RR pathology. This variant re-runs the SAME no-lookahead
replay overriding ONLY Pattern C's target with a reachable structural level, and
compares avg R / timeouts vs the baseline (setup.target).

Two reachable candidates tested:
  nearest = nearest structural swing-high above entry (first level the breakout hits)
  measmov = measured move: entry + (prior_high − post-peak low)  [manipulation amplitude]

Run:  uv run python -m research.backtest_c_target research/dataset_v11 [nearest|measmov|baseline]
"""
from __future__ import annotations

import collections
import glob
import os
import sys

from research.backtest_scanner import (
    TF_MS,
    _TP1_MOVE,
    _closed_upto,
    _horizon_days,
    _load,
    _simulate,
)
from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer
from hunt_core.scanner.detect.patterns import advance_manipulation_scales

_HERE = os.path.dirname(os.path.abspath(__file__))


def _nearest_swing_high_above(meso_bars, entry: float) -> float:
    """Nearest local-max high strictly above entry (first structural resistance)."""
    highs = [r[2] for r in meso_bars]  # OHLCV index 2 = high
    cands: list[float] = []
    for i in range(1, len(highs) - 1):
        h = highs[i]
        if h > highs[i - 1] and h >= highs[i + 1] and h > entry:
            cands.append(float(h))
    return min(cands) if cands else 0.0  # nearest above


def _measured_move(meso_bars, entry: float, lookback: int = 60) -> float:
    """entry + (prior_high − post-peak low): project the manipulation amplitude up."""
    seg = meso_bars[-lookback:] if len(meso_bars) > lookback else meso_bars
    hi = max((r[2] for r in seg), default=0.0)
    lo = min((r[3] for r in seg), default=0.0)
    amp = hi - lo
    return float(entry + amp) if amp > 0 else 0.0


def run_symbol(ds: str, sym: str, mode: str):
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
        horizon = int(_horizon_days(setup.direction, setup.meso_tf) * 86_400_000)
        long = setup.direction == "long"
        deep = geo.get("primary_target") or geo.get("target") or setup.target

        # ── VARIANT: reachable target for Pattern C longs only ──────────────────
        if setup.pattern_type == "C" and long and mode != "baseline":
            cand = (_nearest_swing_high_above(meso_bars, entry) if mode == "nearest"
                    else _measured_move(meso_bars, entry))
            if cand > entry:
                deep = cand
        # ────────────────────────────────────────────────────────────────────────

        deep_move = (float(deep) - entry) / entry if long else (entry - float(deep)) / entry
        if deep_move < _TP1_MOVE:
            continue
        tp1 = entry * (1 + _TP1_MOVE) if long else entry * (1 - _TP1_MOVE)
        strut = [tt for tt in (geo.get("ladder") or []) if tt and tt > 0
                 and (entry < tt < deep if long else deep < tt < entry)]
        tp_levels = sorted({tp1, *strut, deep}, reverse=not long)
        dobor_levels = [d for d in (geo.get("dobor_ladder")
                                    or [geo.get("averaging_price")]) if d and d > 0]
        outcome, r_mult, _mae, _legs = _simulate(fine, t, setup.direction, entry, geo["stop"],
                                                 tp_levels, dobor_levels, deep, horizon)
        completed.append((setup, entry, geo["stop"], deep, outcome, r_mult))
    return completed


def main(ds: str, mode: str) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    per_pattern: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    per_pattern_r: dict[str, list[float]] = collections.defaultdict(list)
    for sym in syms:
        for setup, _e, _s, _t, outcome, r_mult in run_symbol(ds, sym, mode):
            per_pattern[setup.pattern_type][outcome] += 1
            per_pattern_r[setup.pattern_type].append(r_mult)
    print(f"===== C-TARGET MODE={mode} · dataset={os.path.basename(ds)} =====")
    for p in sorted(per_pattern):
        c = per_pattern[p]
        rs = per_pattern_r[p]
        n = len(rs)
        avg_r = sum(rs) / n if n else 0.0
        print(f"  {p}: n={n} {dict(c)}  totalR={sum(rs):+.1f}  avgR={avg_r:+.3f}")


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "dataset_v11")
    mode = sys.argv[2] if len(sys.argv) > 2 else "baseline"
    main(ds, mode)
