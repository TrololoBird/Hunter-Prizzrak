"""Offline forward-replay backtest for the manipulation scanner.

Drives the production detector (``advance_manipulation_scales``) bar-by-bar over the
parquet dataset with **no lookahead** — at each step T, every timeframe contributes only
bars whose CLOSE time (ts + tf_duration) is <= T, and the persisted ``states`` dict is
carried across calls exactly like the live scan cadence. When a setup completes we build
the **production** entry/stop/target geometry (``manipulation_delivery._geometry`` +
``_stop_buffer``) and simulate which comes first on the forward path — stop or target —
to get a truthful win rate and R-expectancy.

Purpose: quantify whether the scanner actually has positive expectancy before trusting
its signals (the project's open "не доказал эффективность" question). Not a calibration
tool — the sample here (dataset_v8, 6 coins) is far too small to tune thresholds against;
expand the dataset first (see research/fetch/) to avoid overfitting.

Run:  uv run python -m research.backtest_scanner       (or: python research/backtest_scanner.py)
"""
from __future__ import annotations

import collections
import glob
import os

import polars as pl

from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DS = os.path.join(_HERE, "dataset_v8")

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000,
}
USE_TF = ["1w", "1d", "4h", "1h", "15m", "5m"]  # the ladders the detector actually reads
# Forward window within which the trade must resolve (else timeout). DIRECTION-AWARE:
# the SHORT (pump-absorption) is FAST — «от пары часов до 2-3 дней». But the LONG
# accumulation types are MEDIUM-TERM — «в среднесроке 250%», 100-400% over WEEKS
# (research/manipulations_corpus/long_manip_3types: BSB/ESPROC). Timing longs out at 4-5 d
# made them net-negative; at a medium-term horizon they are +0.54R/trade (dataset_v10).
# So keep shorts fast, give longs a medium-term leash.
FWD_DAYS = {"1w": 5.0, "1d": 5.0, "4h": 4.0, "1h": 3.0, "15m": 2.0, "5m": 1.5}  # SHORT
_LONG_HORIZON_DAYS = float(os.getenv("HUNT_LONG_HORIZON_DAYS", "21") or 21)  # LONG: medium-term


def _horizon_days(direction: str, meso_tf: str) -> float:
    return _LONG_HORIZON_DAYS if direction == "long" else FWD_DAYS.get(meso_tf, 3.0)
# First take-profit = a fixed +20% price move (transcript: «первый тп на 20% движения
# цены»), after which the stop moves to entry (BE) and the runner rides the deep 40%+ pool.
_TP1_MOVE = 0.20


def _load(ds: str, sym: str) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    for tf in USE_TF:
        f = os.path.join(ds, f"{sym}_{tf}.parquet")
        if not os.path.exists(f):
            continue
        df = pl.read_parquet(f)
        ts_col = "timestamp" if "timestamp" in df.columns else "time"  # v8 vs live-fetch schema
        df = df.sort(ts_col)
        ts = df[ts_col]
        if ts.dtype == pl.Datetime:  # live-fetch frames carry a datetime, v8 carries epoch-ms
            ts = ts.dt.epoch(time_unit="ms")
        out[tf] = [
            [int(t), float(o), float(h), float(l), float(c), float(v)]
            for t, o, h, l, c, v in zip(
                ts, df["open"], df["high"], df["low"], df["close"], df["volume"]
            )
        ]
    return out


def _closed_upto(rows: list[list[float]], tf: str, t: int) -> list[list[float]]:
    dur = TF_MS[tf]
    return [r for r in rows if r[0] + dur <= t]


# ── Managed-campaign parameters (source-grounded, env-tunable — NOT tuned on this data) ──
# The corpus is explicit that a trade is a managed campaign, not a static hold: «набирать
# ЧАСТИЧНО позиции», «доборы страховочные», «усреднив позиции» (выйти из минуса), «тейки
# частями по лестнице», «перезайдём позже». The old single-довор/single-TP model collapsed
# all of that. These knobs restore it; defaults mirror what the authors describe, not a fit.
_DOBOR_UNIT = float(os.getenv("HUNT_DOBOR_UNIT", "1.0"))   # size added per довор (base = 1.0)
_MAX_UNITS = float(os.getenv("HUNT_MAX_UNITS", "3.0"))     # cap: base + up to 2 доборов
_TP_FRAC = float(os.getenv("HUNT_TP_FRAC", "0.5"))         # fraction of open banked at each TP rung
_DOBOR_FR = (0.33, 0.66)                                   # довор rungs, fraction of entry→stop
_REENTRY = os.getenv("HUNT_REENTRY", "1") not in ("0", "", "false")       # перезаход (та же сторона)
_ALLOW_FLIP = os.getenv("HUNT_ALLOW_FLIP", "0") not in ("0", "", "false")  # short↔long (гипотеза)


def _run_leg(seg, direction, entry, stop, dobor_levels, tp_levels, deep):
    """Trade ONE managed leg over ``seg`` → (outcome, R, mae_R, exit_idx).

    Faithful to the method's management: WIDE structural stop on the whole position; a
    LADDER of доборы (усреднение) as price runs against, each recomputing the average entry;
    PARTIAL fixes banking ``_TP_FRAC`` of the open size at each successive structural TP;
    after the first fix the stop trails to break-even (the average); the remainder rides to
    the deep pool. PnL is in units of the initial per-unit risk (entry→stop, base size).

    Outcomes: 'loss' (wide stop before any TP), 'win' (deep pool reached), 'scratch' (some
    profit banked then BE/timeout), 'timeout' (nothing resolved). ``mae_R`` is the deepest
    adverse excursion vs the running average — the path-risk the endpoint hides (JCT +52.7%,
    the LONG round-trips −65…−96%; see the manipulations_corpus razbor files)."""
    risk1 = abs(entry - stop)
    if risk1 <= 0 or not seg:
        return ("nodata", 0.0, 0.0, 0)
    long = direction == "long"
    adv_hit = (lambda lv, lo, hi: lo <= lv) if long else (lambda lv, lo, hi: hi >= lv)
    fav_hit = (lambda lv, lo, hi: hi >= lv) if long else (lambda lv, lo, hi: lo <= lv)
    profit = (lambda px, avg: px - avg) if long else (lambda px, avg: avg - px)  # per-unit, signed

    pend_dobor = sorted(dobor_levels, reverse=long)   # nearest-to-entry first (below for long)
    pend_tp = sorted(tp_levels, reverse=not long)     # nearest-to-entry first (above for long)
    open_units, avg = 1.0, entry
    realized, mae_r = 0.0, 0.0
    first_tp, be_stop = False, stop
    for i, (_ts, _o, hi, lo, _c, _v) in enumerate(seg):
        # 1. доборы (усреднение) — only while still building the position (before first fix)
        if not first_tp:
            while pend_dobor and adv_hit(pend_dobor[0], lo, hi) and open_units + _DOBOR_UNIT <= _MAX_UNITS + 1e-9:
                d = pend_dobor.pop(0)
                avg = (avg * open_units + d * _DOBOR_UNIT) / (open_units + _DOBOR_UNIT)
                open_units += _DOBOR_UNIT
        # 2. path-risk vs the running average
        mae_r = max(mae_r, ((avg - lo) if long else (hi - avg)) / risk1)
        # 3. wide/BE stop on the whole position
        cur_stop = be_stop if first_tp else stop
        if (lo <= cur_stop) if long else (hi >= cur_stop):
            realized += open_units * profit(cur_stop, avg) / risk1  # signed (loss if adverse)
            return (("scratch" if first_tp else "loss"), realized, mae_r, i)
        # 4. partial fixes up the structural ladder
        while pend_tp and fav_hit(pend_tp[0], lo, hi):
            tp = pend_tp.pop(0)
            fix = open_units * _TP_FRAC
            realized += fix * profit(tp, avg) / risk1
            open_units -= fix
            if not first_tp:
                first_tp, be_stop = True, avg  # stop → break-even after the first take
        # 5. deep pool → close the runner
        if first_tp and fav_hit(deep, lo, hi):
            realized += open_units * profit(deep, avg) / risk1
            return ("win", realized, mae_r, i)
    return (("scratch" if first_tp else "timeout"), realized, mae_r, len(seg) - 1)


def _simulate(fine, t0, direction, entry, stop, tp_levels, dobor_levels, deep, horizon_ms):
    """Managed campaign = a sequence of legs (перезаход/переворот), summing R.

    Runs the primary leg; then, if enabled, models what the corpus describes ON TOP of a
    single position: **перезаход** — after a profitable exit that leaves horizon, re-enter
    the SAME side once when price returns to the entry zone («перезайдём позже»); and, only
    under HUNT_ALLOW_FLIP (the user's hypothesis, NOT stated by the authors), a **переворот**
    — after the move exhausts at the deep pool, trade the round-trip back.
    Returns (outcome, total_R, worst_mae_R, n_legs)."""
    seg = [r for r in fine if t0 < r[0] <= t0 + horizon_ms]
    if not seg or entry <= 0 or stop <= 0 or not (tp_levels and deep):
        return ("nodata", 0.0, 0.0, 0)
    outcome, total_r, mae_r, exit_idx = _run_leg(seg, direction, entry, stop,
                                                 dobor_levels, tp_levels, deep)
    if outcome == "nodata":
        return outcome, total_r, mae_r, 0
    legs, long = 1, direction == "long"

    # Перезаход в ту же сторону: only after a profitable exit (a stop-out means the structure
    # broke — do not re-offer). Re-enter once when price revisits the entry band.
    if _REENTRY and outcome in ("win", "scratch") and exit_idx < len(seg) - 2:
        tail = seg[exit_idx + 1:]
        band = abs(entry - stop) * 0.15  # "the entry zone", ~15% of risk wide
        for j, (_ts, _o, hi, lo, _c, _v) in enumerate(tail):
            if lo - band <= entry <= hi + band:
                o2, r2, m2, _ = _run_leg(tail[j:], direction, entry, stop,
                                         dobor_levels, tp_levels, deep)
                if o2 != "nodata":
                    total_r, mae_r, legs = total_r + r2, max(mae_r, m2), legs + 1
                break

    # Переворот short↔long (гипотеза, off by default): after the move exhausts at the deep
    # pool, trade the round-trip back toward the origin with a symmetric wide stop.
    if _ALLOW_FLIP and outcome == "win" and exit_idx < len(seg) - 2:
        flip_dir = "short" if long else "long"
        flip_entry, flip_deep = deep, entry  # round-trip target = the campaign's origin
        flip_stop = deep * (1.10 if flip_dir == "short" else 0.90)  # 10% beyond the extreme
        flip_tps = [flip_entry + (flip_deep - flip_entry) * f for f in (0.5, 1.0)]
        flip_dobors = [flip_entry + (flip_stop - flip_entry) * f for f in _DOBOR_FR]
        o3, r3, m3, _ = _run_leg(seg[exit_idx + 1:], flip_dir, flip_entry, flip_stop,
                                 flip_dobors, flip_tps, flip_deep)
        if o3 != "nodata":
            total_r, mae_r, legs = total_r + r3, max(mae_r, m3), legs + 1

    return (outcome, total_r, mae_r, legs)


def run_symbol(ds: str, sym: str):
    data = _load(ds, sym)
    if "1h" not in data:
        return []
    fine = data["1h"]  # full-history coverage for hit-detection (finer TFs span less time)
    completed, seen = [], set()
    states = None
    for r in data["1h"]:
        t = r[0] + TF_MS["1h"]  # 1h close time = scan cadence
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
        deep_move = (float(deep) - entry) / entry if long else (entry - float(deep)) / entry
        # A manipulation is a 20-40+% move. If the structural pool can't even reach the
        # +20% first take, this isn't the trade — don't score it.
        if deep_move < _TP1_MOVE:
            continue
        # Serie частичных фиксов: the fixed +20% first take (transcript «первый тп на 20%»)
        # PLUS the structural pool ladder, each rung banking _TP_FRAC. Доборы: the production
        # довор ladder (geo["dobor_ladder"]), fallback to the single averaging_price.
        tp1 = entry * (1 + _TP1_MOVE) if long else entry * (1 - _TP1_MOVE)
        strut = [t for t in (geo.get("ladder") or []) if t and t > 0
                 and (entry < t < deep if long else deep < t < entry)]
        tp_levels = sorted({tp1, *strut, deep}, reverse=not long)
        dobor_levels = [d for d in (geo.get("dobor_ladder")
                                    or [geo.get("averaging_price")]) if d and d > 0]
        outcome, r_mult, mae_r, legs = _simulate(fine, t, setup.direction, entry, geo["stop"],
                                                 tp_levels, dobor_levels, deep, horizon)
        completed.append((setup, entry, geo["stop"], geo.get("target") or setup.target,
                          outcome, r_mult, mae_r, legs))
    return completed


def main(ds: str = _DEFAULT_DS) -> None:
    syms = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{ds}/*.parquet")})
    grand, per_pattern = [], collections.defaultdict(collections.Counter)
    for sym in syms:
        res = run_symbol(ds, sym)
        print(f"\n===== {sym}: {len(res)} completed setups =====")
        for setup, entry, stop, target, outcome, r_mult, mae_r, legs in res:
            risk_pct = abs(entry - stop) / entry * 100
            tgt_pct = abs(target - entry) / entry * 100 if target else 0.0
            legs_tag = f" x{legs}" if legs > 1 else ""  # extra legs = перезаход/переворот
            print(f"  {setup.direction:5s} {setup.pattern_type:2s} meso={setup.meso_tf:3s} "
                  f"entry={entry:.6g} stop={stop:.6g}({risk_pct:.1f}%) tgt={target:.6g}({tgt_pct:.0f}%) "
                  f"-> {outcome:7s}{legs_tag} R={r_mult:+.1f} MAE={mae_r:.1f}R ev={','.join(setup.evidence[:2])}")
            grand.append((setup.direction, setup.pattern_type, outcome, r_mult, mae_r, legs))
            per_pattern[setup.pattern_type][outcome] += 1
    outs = collections.Counter(g[2] for g in grand)
    wins, losses = outs.get("win", 0), outs.get("loss", 0)
    scratch, to = outs.get("scratch", 0), outs.get("timeout", 0)
    decided = wins + losses + scratch  # scratch = TP1 banked, runner at BE (small +)
    r_total = sum(g[3] for g in grand)
    print(f"\n===== SUMMARY: {len(grand)} setups | win={wins} scratch={scratch} "
          f"loss={losses} timeout={to} =====")
    if decided:
        print(f"profitable (win+scratch)={100 * (wins + scratch) / decided:.0f}%  "
              f"total R={r_total:+.1f}  avg R/trade={r_total / max(len(grand), 1):+.2f}")
    # MAE audit: wins/scratches that ran ≥1R against the entry before resolving nearly
    # stopped out first — the endpoint hid a near-death path (JCT +52.7%, the LONG round-
    # trips). A high share here means a tighter stop would flip these to losses: the edge is
    # fragile and reported win-rate is optimistic. avg/max MAE quantify the hidden risk.
    good = [g for g in grand if g[2] in ("win", "scratch")]
    if good:
        mae_vals = [g[4] for g in good]
        near_death = sum(1 for m in mae_vals if m >= 1.0)
        print(f"MAE audit: avg={sum(mae_vals) / len(mae_vals):.2f}R  max={max(mae_vals):.1f}R  "
              f"wins/scratches that ran ≥1R against entry first={near_death}/{len(good)} "
              f"({100 * near_death / len(good):.0f}%)")
    extra_legs = sum(g[5] - 1 for g in grand)
    mode = ("flip+reentry" if _ALLOW_FLIP and _REENTRY else "flip" if _ALLOW_FLIP
            else "reentry" if _REENTRY else "single-leg")
    print(f"campaign: mode={mode}  доборы≤{int(_MAX_UNITS - 1)}  extra legs (перезаход/переворот)={extra_legs}")
    print("by pattern:", {p: dict(c) for p, c in per_pattern.items()})
    print("by direction:", dict(collections.Counter(g[0] for g in grand)))


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_DS)
