"""Offline forward-replay measurement for the PRIZRAK level/structure strategy.

This is NOT a `backtest_*.py` and deliberately not named one: `tests/test_module_boundary.py`
reserves that prefix for the manipulations harness (every `backtest_*.py` MUST import the
manipulations path and MUST NOT import prizrak). This file is the mirror — it drives the
PRIZRAK production path and must never reach into the scanner / manipulation_delivery. The
boundary is pinned in both directions by that test; keep it that way.

Why it exists: prizrak has no measurement at all. `data/hunt_outcome_ledger.jsonl` booked
100% win rates from phantom fills (pre-01a55c8), so every parameter in `hunt_core/prizrak/`
is currently unfalsifiable. This replays the REAL signal path bar-by-bar with no lookahead
and simulates whether the limit filled and then whether stop or target came first, to get a
truthful win rate and R-expectancy.

Faithful to the method, not the manipulations model:
  * PRIZRAK is LIMIT trading — «лимитки у боксов, вход на тесте» (стр.30). Entry is not
    immediate: price must return into the entry band within a wait window, else the setup
    expires unfilled (no trade, not a loss).
  * Stop is structural, RR≈1:3 (стр.9/33). Outcome is touch-based: after fill, whichever of
    stop / TP1 the forward path reaches first. TP2 recorded when present.
  * Signals come from `build_prizrak_signals` — the FULL production path, INCLUDING the
    gates the scanner harness could not model (`_apply_confluence`/`_htf_gate` veto, `min_rr`,
    confluence). So unlike `backtest_scanner.py`, the population here is the gated one.

Honesty caveats inherited from the scanner harness docstring, both apply:
  * NOT a calibration tool on its own — dataset_v10 (50 coins) is finite; hold an OOS slice
    when tuning, and count independent episodes, not just n (time-clustered crypto trades are
    correlated — an alt-dump hits every coin at once, deflating effective n).
  * A per-symbol, per-setup dedup mirrors the live `deep_cooldown_ok` cadence; without it the
    same setup recounts on every sampled tick and inflates n.

Usage:
    uv run python research/prizrak_replay.py                 # all coins, summary
    uv run python research/prizrak_replay.py --coins 12      # quick subset
    uv run python research/prizrak_replay.py --oos           # hold out a coin slice
"""
from __future__ import annotations

import argparse
import glob
import os
import statistics as st
from collections import defaultdict
from typing import Any

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import build_prizrak_signals

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DS = os.path.join(_HERE, "dataset_v10")

TF_MS = {
    "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
    "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
}
USE_TF = ["1w", "1d", "4h", "1h", "15m", "5m"]
_LOOKBACK = 250          # bars fed per TF per tick (>= the deepest tier lookback)
_WARMUP = 150            # skip the first N base-TF bars so every tier has history
_MAX_WAIT_BARS = 12      # setup-TF bars price has to return into the entry band, else expire
_HORIZON_BARS = 60       # setup-TF bars after fill for stop/target to resolve, else timeout


def _stems(ds: str) -> list[str]:
    return sorted({os.path.basename(f)[: -len("_4h.parquet")]
                   for f in glob.glob(os.path.join(ds, "*_4h.parquet"))})


def _load(ds: str, stem: str) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    for tf in USE_TF:
        f = os.path.join(ds, f"{stem}_{tf}.parquet")
        if not os.path.exists(f):
            continue
        df = pl.read_parquet(f).sort("timestamp")
        out[tf] = [
            [int(t), float(o), float(h), float(l), float(c), float(v)]
            for t, o, h, l, c, v in zip(
                df["timestamp"], df["open"], df["high"], df["low"], df["close"], df["volume"]
            )
        ]
    return out


def _closed_upto(rows: list[list[float]], tf: str, t: int) -> list[list[float]]:
    dur = TF_MS[tf]
    return [r for r in rows[-2 * _LOOKBACK - 5:] if r[0] + dur <= t][-_LOOKBACK:]


def _resolve(seg: list[list[float]], sig: dict[str, Any]) -> tuple[str, float] | None:
    """Touch-based outcome of ONE призрак limit setup over the forward path ``seg`` (bars of
    the setup's own TF, starting the bar after signal). Returns (outcome, R) or None if the
    limit never filled.

    Order of events on each bar: (1) fill — price enters [entry_lo, entry_hi]; (2) once
    filled, stop vs TP1 — whichever the bar's low/high reaches first (stop checked first on a
    bar that spans both, the conservative read). R is in units of entry→stop risk.
    """
    lo_b, hi_b = sig.get("entry_lo"), sig.get("entry_hi")
    stop, tp1 = sig.get("stop"), sig.get("tp1")
    direction = str(sig.get("action") or "").lower()
    if None in (lo_b, hi_b, stop, tp1) or direction not in ("long", "short"):
        return None
    entry = (float(lo_b) + float(hi_b)) / 2.0
    risk = abs(entry - float(stop))
    if risk <= 0:
        return None
    long = direction == "long"

    filled = False
    for i, (_ts, _o, hi, lo, _c, _v) in enumerate(seg):
        if not filled:
            if i >= _MAX_WAIT_BARS:
                return None  # limit expired unfilled — no trade
            if lo <= float(hi_b) and hi >= float(lo_b):  # bar overlaps the entry band
                filled = True
            else:
                continue
        # filled: resolve stop / target on this and every subsequent bar
        if i >= _MAX_WAIT_BARS + _HORIZON_BARS:
            return ("timeout", 0.0)
        hit_stop = (lo <= float(stop)) if long else (hi >= float(stop))
        hit_tp = (hi >= float(tp1)) if long else (lo <= float(tp1))
        if hit_stop:
            return ("loss", -1.0)
        if hit_tp:
            return ("win", abs(float(tp1) - entry) / risk)
    return ("timeout", 0.0) if filled else None


def replay(ds: str, stems: list[str], *, step: int = 8) -> dict[str, Any]:
    cfg = PrizrakConfig.load()
    results: list[tuple[str, float, str, str]] = []  # (outcome, R, tf, setup_kind)
    seen: set[tuple[str, str, str, int]] = set()     # dedup: (stem, tf, dir, round(entry))
    episodes: set[tuple[str, int]] = set()           # (stem, week) — independent-episode proxy

    for stem in stems:
        data = _load(ds, stem)
        base = data.get("4h")
        if not base or len(base) < _WARMUP + step:
            continue
        for i in range(_WARMUP, len(base), step):
            t = base[i][0] + TF_MS["4h"]
            sliced = {tf: _closed_upto(rows, tf, t) for tf, rows in data.items()}
            price = sliced["4h"][-1][4] if sliced.get("4h") else 0.0
            if price <= 0:
                continue
            for sig in build_prizrak_signals(sliced, price=price, cfg=cfg):
                tf = str(sig.get("tf") or "4h")
                direction = str(sig.get("action") or "").lower()
                entry_lo = sig.get("entry_lo")
                if tf not in data or entry_lo is None or direction not in ("long", "short"):
                    continue
                key = (stem, tf, direction, round(float(entry_lo), 8).__hash__())
                if key in seen:
                    continue
                seen.add(key)
                # forward path on the setup's OWN TF, starting after the signal bar
                fwd = [r for r in data[tf] if r[0] + TF_MS[tf] > t][: _MAX_WAIT_BARS + _HORIZON_BARS]
                out = _resolve(fwd, sig)
                if out is None:
                    continue
                outcome, r = out
                results.append((outcome, r, tf, str(sig.get("setup_kind") or "?"), direction))
                episodes.add((stem, t // TF_MS["1w"]))
    return {"results": results, "episodes": len(episodes)}


def _report(res: dict[str, Any]) -> None:
    rows = res["results"]
    if not rows:
        print("нет исходов")
        return
    filled = [r for r in rows if r[0] != "timeout"]
    wins = [r for r in rows if r[0] == "win"]
    losses = [r for r in rows if r[0] == "loss"]
    rs = [r[1] for r in rows if r[0] in ("win", "loss")]
    n = len(wins) + len(losses)
    wr = len(wins) / n * 100 if n else 0.0
    exp = st.mean(rs) if rs else 0.0
    print(f"сетапов (filled): {n}  |  timeout: {len(rows) - len(filled)}  |  "
          f"независимых эпизодов: {res['episodes']}")
    print(f"винрейт: {wr:.1f}%  |  R/сделку: {exp:+.3f}  |  курс закладывает WR 30-40% при RR 1к3")
    if rs:
        se = (st.pstdev(rs) / (len(rs) ** 0.5)) if len(rs) > 1 else float("nan")
        print(f"stderr(R): ±{se:.3f}  |  n<100 → выводы не делать, n<30 → шум")
    resolved = [r for r in rows if r[0] in ("win", "loss")]

    def _slice(dim_idx: int, order: list[str] | None = None) -> None:
        agg: dict[str, list[float]] = defaultdict(list)
        for row in resolved:
            agg[row[dim_idx]].append(row[1])
        keys = [k for k in (order or []) if k in agg] or sorted(agg, key=lambda k: -len(agg[k]))
        for k in keys:
            rr = agg[k]
            w = sum(1 for x in rr if x > 0)
            print(f"    {k:>18}: n={len(rr):<4} WR {w / len(rr) * 100:4.0f}%  R {st.mean(rr):+.2f}")

    print("\n  по ТФ:")
    _slice(2, USE_TF)
    print("\n  по направлению (лонг-эдж / шорт-слив — воспроизводит вывод сканера):")
    _slice(4, ["long", "short"])
    print("\n  по типу сетапа:")
    _slice(3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ds", default=_DEFAULT_DS)
    ap.add_argument("--coins", type=int, default=0, help="limit to first N coins (0 = all)")
    ap.add_argument("--step", type=int, default=8, help="sample every Nth 4h bar")
    ap.add_argument("--oos", action="store_true", help="hold out every 3rd coin as OOS, report both")
    args = ap.parse_args()

    stems = _stems(args.ds)
    if args.coins:
        stems = stems[: args.coins]
    if args.oos:
        insample = [s for k, s in enumerate(stems) if k % 3 != 0]
        oos = [s for k, s in enumerate(stems) if k % 3 == 0]
        print(f"=== IN-SAMPLE ({len(insample)} coins) ===")
        _report(replay(args.ds, insample, step=args.step))
        print(f"\n=== OUT-OF-SAMPLE ({len(oos)} coins) ===")
        _report(replay(args.ds, oos, step=args.step))
    else:
        print(f"=== dataset={os.path.basename(args.ds)}  coins={len(stems)}  step={args.step} ===")
        _report(replay(args.ds, stems, step=args.step))


if __name__ == "__main__":
    main()
