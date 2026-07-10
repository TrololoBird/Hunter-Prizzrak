"""Edge report — real vs control, on the holdout slice only.

Answers one question: do real signals beat their matched controls on data the
loop never used to tune anything? Prints winrate ± 95% Wilson CI, mean ATR
return per horizon, MFE/MAE, the real−control delta, a leakage smoke-test, and a
statistical-power gate. It deliberately refuses to claim edge when n is too
small or when real ≈ control.

Run: .venv/bin/python analysis/02_edge_report.py [--all] [--min-n 100]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
OUTCOMES = DATA / "research" / "outcomes.parquet"

_HORIZONS = ["ret_pct_15m", "ret_pct_1h", "ret_pct_4h", "ret_pct_24h"]


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """(point, lo, hi) Wilson score interval for a proportion."""
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (p, centre - half, centre + half)


def _cohort_stats(df: pl.DataFrame) -> dict:
    labelled = df.filter(pl.col("win").is_not_null())
    n = labelled.height
    wins = int(labelled.select(pl.col("win").sum()).item() or 0) if n else 0
    p, lo, hi = wilson(wins, n)
    out = {"n": n, "wins": wins, "wr": p, "wr_lo": lo, "wr_hi": hi}
    for h in _HORIZONS:
        vals = df.select(pl.col(h)).drop_nulls()
        out[h] = vals.select(pl.col(h).mean()).item() if vals.height else None
    for m in ("mfe_pct", "mae_pct"):
        vals = df.select(pl.col(m)).drop_nulls()
        out[m] = vals.select(pl.col(m).mean()).item() if vals.height else None
    return out


def _fmt(x, nd=3):
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x)) else "  n/a"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="use all rows, not just holdout")
    ap.add_argument("--min-n", type=int, default=100, help="power threshold on real n")
    args = ap.parse_args(argv)

    if not OUTCOMES.is_file():
        print(f"no outcome store yet: {OUTCOMES}\nrun: python -m hunt_core.research.build --replay")
        return 1

    df = pl.read_parquet(OUTCOMES)
    scope = "ALL" if args.all else "HOLDOUT"
    if not args.all:
        df = df.filter(pl.col("holdout_split") == "holdout")

    print(f"═══════════ EDGE REPORT ({scope}) ═══════════")
    print(f"  store rows (scope): {df.height}   label_method: "
          f"{df['label_method'].value_counts().to_dict(as_series=False) if df.height else '{}'}")
    if df.height == 0:
        print("  no rows in scope — run the feeder first (or pass --all).")
        return 1

    real = df.filter(pl.col("cohort") == "real")
    real_stats = _cohort_stats(real)
    control_kinds = sorted(
        df.filter(pl.col("cohort") == "control")["control_kind"].drop_nulls().unique().to_list()
    )

    def line(name: str, s: dict) -> None:
        print(f"  {name:14s} n={s['n']:>6}  WR={_fmt(s['wr'])} "
              f"[{_fmt(s['wr_lo'])},{_fmt(s['wr_hi'])}]  "
              f"ret4h={_fmt(s.get('ret_pct_4h'))}%  "
              f"mfe={_fmt(s.get('mfe_pct'))} mae={_fmt(s.get('mae_pct'))}")

    print("\n── cohorts ──")
    line("real", real_stats)
    control_stats = {}
    for ck in control_kinds:
        cs = _cohort_stats(df.filter((pl.col("cohort") == "control") & (pl.col("control_kind") == ck)))
        control_stats[ck] = cs
        line(ck, cs)

    print("\n── real − control (winrate delta; CI-overlap flag) ──")
    suspect = False
    for ck, cs in control_stats.items():
        if not real_stats["n"] or not cs["n"]:
            continue
        delta = real_stats["wr"] - cs["wr"]
        overlap = not (real_stats["wr_lo"] > cs["wr_hi"] or cs["wr_lo"] > real_stats["wr_hi"])
        flag = "≈ (CI overlap)" if overlap else "distinct"
        if overlap:
            suspect = True
        print(f"  vs {ck:12s} ΔWR={_fmt(delta):>7}   {flag}")

    print("\n── verdict ──")
    if real_stats["n"] < args.min_n:
        print(f"  ⚠ INSUFFICIENT POWER — real n={real_stats['n']} < {args.min_n}; "
              f"cannot answer 'is there edge?' yet. Collect more outcomes.")
    elif suspect:
        print("  ⚠ LEAKAGE/REGIME SUSPECT — real winrate overlaps at least one "
              "control CI. Any positive edge here is NOT trustworthy; treat as a "
              "bug/regime artifact until real is CI-distinct from every control.")
    else:
        print("  ✓ real is CI-distinct from every control on holdout. Edge is "
              "plausible — proceed to forward confirmation before acting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
