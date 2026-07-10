"""Feature importance — causal-disciplined, cost-aware, leakage-checked.

Unit of research = the feature (not the module). Three disciplines the reviews
demanded, all enforceable off the extended Outcome Store:

1. TEMPORAL: only decision-time (t0) features are used — the `feat_*` columns are
   frozen at t0, the label is a forward outcome. So association here is
   predictive-by-construction (feature precedes move), not look-ahead.
2. COST: each feature carries an acquisition cost; ranking is by gain / cost, not
   gain alone (EMA is O(1); order-flow needs a persistent WS + ring buffer).
3. LEAKAGE / SPURIOUSNESS: the same feature→outcome association is measured on the
   coin_flip control cohort. A feature that "predicts" the real outcome AND the
   control's outcome equally is picking up regime/base-rate, not signal. Reported
   as `net_auc = auc_real - auc_control`.

AUC is the rank statistic P(feature higher on a win than on a loss), computed
without scipy/sklearn. |net_auc-0| is the leakage-adjusted gain.

Run: .venv/bin/python analysis/03_feature_importance.py [--all]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from hunt_core.research.stats import auc as _auc

DATA = Path(__file__).resolve().parents[1] / "data"
OUTCOMES = DATA / "research" / "outcomes.parquet"

# feature column → acquisition cost (relative units; higher = pricier to collect)
FEATURE_COST = {
    "feat_dom_imbalance": 3.0,   # needs live order book (WS depth + ring buffer)
    "feat_trade_burst": 2.0,     # needs live agg-trade stream
    "feat_momentum_z": 1.0,      # from klines, O(1)
    "dt_raw_confluence": 1.0,    # already computed composite
}


def _cohort_arrays(df: pl.DataFrame, col: str):
    sub = df.filter(pl.col("win").is_not_null()).select([col, "win"]).drop_nulls()
    if sub.height == 0:
        return None, None
    return sub[col].to_numpy().astype(float), sub["win"].to_numpy().astype(float)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="all rows, not just holdout")
    args = ap.parse_args(argv)
    if not OUTCOMES.is_file():
        print(f"no outcome store: {OUTCOMES}\nrun: python -m hunt_core.research.build --replay")
        return 1

    df = pl.read_parquet(OUTCOMES)
    if not args.all:
        df = df.filter(pl.col("holdout_split") == "holdout")
    real = df.filter(pl.col("cohort") == "real")
    ctrl = df.filter((pl.col("cohort") == "control") & (pl.col("control_kind") == "coin_flip"))

    scope = "ALL" if args.all else "HOLDOUT"
    print(f"═══════════ FEATURE IMPORTANCE ({scope}) ═══════════")
    print(f"  real n={real.height}  coin_flip control n={ctrl.height}")
    print("  (temporal: t0 features → forward label; net_auc = real − control)\n")

    # add interaction feature (H005): dom_imbalance × trade_burst
    inter = pl.col("feat_dom_imbalance") * pl.col("feat_trade_burst")
    real = real.with_columns(inter.alias("feat_domXburst"))
    ctrl = ctrl.with_columns(inter.alias("feat_domXburst"))
    cost = dict(FEATURE_COST)
    cost["feat_domXburst"] = 3.0  # requires both order book + trade stream

    rows = []
    for col in [*FEATURE_COST.keys(), "feat_domXburst"]:
        fr, wr = _cohort_arrays(real, col)
        fc, wc = _cohort_arrays(ctrl, col)
        auc_r = _auc(fr, wr) if fr is not None else None
        auc_c = _auc(fc, wc) if fc is not None else None
        if auc_r is None:
            continue
        net = (auc_r - auc_c) if auc_c is not None else None
        gain = abs(net) if net is not None else abs(auc_r - 0.5)
        c = cost.get(col, 1.0)
        rows.append((col, auc_r, auc_c, net, gain, gain / c, c))

    rows.sort(key=lambda r: r[5], reverse=True)
    print(f"  {'feature':22s} {'auc_real':>8} {'auc_ctrl':>8} {'net_auc':>8} {'gain/cost':>9} {'cost':>5}")
    for col, ar, ac, net, gain, gc, c in rows:
        ac_s = f"{ac:.3f}" if ac is not None else "  n/a"
        net_s = f"{net:+.3f}" if net is not None else "  n/a"
        print(f"  {col:22s} {ar:8.3f} {ac_s:>8} {net_s:>8} {gc:9.3f} {c:5.1f}")

    print("\n── reading ──")
    print("  auc≈0.5 → no predictive value. |net_auc| small → association is regime/")
    print("  base-rate, not signal (spurious). Rank by gain/cost, not auc alone.")
    print("  Feed these into the Hypothesis Registry via research.experiment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
