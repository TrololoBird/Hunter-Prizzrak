"""Experiment harness — one mechanism, many experiment types.

The reviews asked for a single A/B mechanism instead of ad-hoc scripts. Every
experiment reduces to: pick a hypothesis, measure a leakage-adjusted effect on
the **holdout** slice of the Outcome Store, update the hypothesis posterior, mark
null when the effect is within noise. Nothing is deleted here — deletion is a
Track-C decision driven by an archived (3×null) hypothesis.

Experiment types:
  feature   — feat_* column's net_auc (real − coin_flip control). Implemented.
  engine    — engine_a vs engine_b per-signal outcome (module column match).
              H006 is archived: the pre-Prizrak gating engine it compared
              against no longer exists.
  version   — real vs coin_flip mean of a metric (e.g. ret_pct_4h), optionally
              scoped to one ``module`` (e.g. H010 scopes to "deep_prizrak" so
              pre-cutover rows never leak into the post-cutover edge check).

Posterior update (transparent, bounded — not a full Bayes net, deliberately):
  evidence e = clip(net_effect * SCALE, -0.5, 0.5); posterior = clip(prior + e)
where SCALE maps a strong effect (~0.15) to a ~0.45 posterior swing.
"""
from __future__ import annotations

import argparse
from math import sqrt
from pathlib import Path

import numpy as np
import polars as pl

from hunt_core.research.hypotheses import (
    ensure_seeded,
    record_experiment,
    save_registry,
)
from hunt_core.research.outcome_store import OUTCOMES_PARQUET
from hunt_core.research.stats import auc

NULL_ABS_EFFECT = 0.03   # |net_auc| / |delta_wr| below this = null
POSTERIOR_SCALE = 3.0    # effect 0.15 → ~0.45 posterior swing
_WILSON_Z = 1.96         # 95% CI


class NotReady(RuntimeError):
    """Experiment type is defined but its inputs aren't in the Store yet."""


# Which hypotheses map to which feature column (feature experiments).
_FEATURE_MAP = {
    "H004": "feat_momentum_z",
    "H005": "feat_domXburst",
}

# Engine-vs-engine: (engine_a, engine_b) matching module column.
# H006 archived 2026-07 — comparison engine no longer exists, see module docstring.
_ENGINE_MAP: dict[str, tuple[str, str]] = {}

# Version-vs-version: compare real vs coin_flip mean on a metric column.
_VERSION_MAP: dict[str, str] = {
    "H007": "ret_pct_4h",
}

# Version-vs-version scoped to one module — (metric_col, module).
_VERSION_MODULE_MAP: dict[str, tuple[str, str]] = {
    "H010": ("ret_pct_4h", "deep_prizrak"),
}

# Rule-vs-no-rule: compare rows where a gate decision was applied vs not.
_RULE_MAP: dict[str, str] = {
    "H008": "gate_decision",
}

# Feature-vs-removed: ablation proxy — above-median vs below-median.
_FEATURE_REMOVED_MAP: dict[str, str] = {
    "H009": "feat_dom_imbalance",
}


def _holdout(df: pl.DataFrame) -> pl.DataFrame:
    return df.filter(pl.col("holdout_split") == "holdout")


def _arrays(df: pl.DataFrame, col: str):
    sub = df.filter(pl.col("win").is_not_null()).select([col, "win"]).drop_nulls()
    if sub.height == 0:
        return None, None
    return sub[col].to_numpy().astype(float), sub["win"].to_numpy().astype(float)


def _wilson_ci(wins: int, n: int) -> tuple[float, float, float]:
    """Wilson score interval — returns (center, lower, upper)."""
    if n == 0:
        return 0.5, 0.0, 1.0
    z2 = _WILSON_Z ** 2
    p = wins / n
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = _WILSON_Z * sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n) / denom
    return center, center - margin, center + margin


def run_feature_experiment(
    df: pl.DataFrame, feature_col: str, *, run_id: str
) -> dict:
    """net_auc = auc(real) − auc(coin_flip control) on holdout."""
    ho = _holdout(df)
    if "feat_domXburst" == feature_col and feature_col not in ho.columns:
        ho = ho.with_columns((pl.col("feat_dom_imbalance") * pl.col("feat_trade_burst")).alias("feat_domXburst"))
    real = ho.filter(pl.col("cohort") == "real")
    ctrl = ho.filter((pl.col("cohort") == "control") & (pl.col("control_kind") == "coin_flip"))
    fr, wr = _arrays(real, feature_col)
    fc, wc = _arrays(ctrl, feature_col)
    if fr is None:
        raise NotReady(f"no real rows with {feature_col}")
    auc_r = auc(fr, wr)
    auc_c = auc(fc, wc) if fc is not None else 0.5
    net = round((auc_r or 0.5) - (auc_c or 0.5), 4)
    is_null = abs(net) < NULL_ABS_EFFECT
    return {
        "feature": feature_col, "n_real": real.height,
        "auc_real": round(auc_r, 4) if auc_r is not None else None,
        "auc_ctrl": round(auc_c, 4) if auc_c is not None else None,
        "net_auc": net, "is_null": is_null, "run_id": run_id,
    }


def run_engine_experiment(
    df: pl.DataFrame, engine_a: str, engine_b: str, *, run_id: str
) -> dict:
    """Compare winrate of two engines on holdout real rows.

    Returns delta = WR_a − WR_b with Wilson CIs.
    Null when |delta| < 0.03 or CIs overlap.
    """
    ho = _holdout(df).filter(pl.col("cohort") == "real")
    a = ho.filter(pl.col("module").str.contains(engine_a, literal=True))
    b = ho.filter(pl.col("module").str.contains(engine_b, literal=True))
    for name, sub in ((engine_a, a), (engine_b, b)):
        if sub.height < 30:
            raise NotReady(f"{name}: only {sub.height} holdout rows (need >=30)")
    n_a, w_a = a.height, a.filter(pl.col("win") == 1).height
    n_b, w_b = b.height, b.filter(pl.col("win") == 1).height
    c_a, lo_a, hi_a = _wilson_ci(w_a, n_a)
    c_b, lo_b, hi_b = _wilson_ci(w_b, n_b)
    delta = round(c_a - c_b, 4)
    ci_overlap = lo_a <= hi_b and lo_b <= hi_a
    is_null = abs(delta) < NULL_ABS_EFFECT or ci_overlap
    return {
        "engine_a": engine_a, "engine_b": engine_b,
        "n_a": n_a, "w_a": w_a, "wr_a": round(c_a, 4),
        "lo_a": round(lo_a, 4), "hi_a": round(hi_a, 4),
        "n_b": n_b, "w_b": w_b, "wr_b": round(c_b, 4),
        "lo_b": round(lo_b, 4), "hi_b": round(hi_b, 4),
        "delta": delta, "ci_overlap": ci_overlap,
        "is_null": is_null, "run_id": run_id,
    }


def run_version_experiment(
    df: pl.DataFrame, metric_col: str, *, run_id: str, module: str | None = None
) -> dict:
    """Compare real vs coin_flip mean on a metric column.

    Null when |z| <= 1.96 (not significant at 95%). If ``module`` is given,
    scopes both cohorts to that module column value first (e.g. to keep a
    post-cutover engine's edge check from being diluted by older rows).
    """
    ho = _holdout(df)
    if module is not None:
        ho = ho.filter(pl.col("module") == module)
    real = ho.filter(pl.col("cohort") == "real").filter(pl.col(metric_col).is_not_null())
    ctrl = ho.filter((pl.col("cohort") == "control") & (pl.col("control_kind") == "coin_flip")).filter(pl.col(metric_col).is_not_null())
    if real.height < 10 or ctrl.height < 10:
        raise NotReady(f"real={real.height} ctrl={ctrl.height} rows for {metric_col} (need >=10 each)")
    mr = real[metric_col].to_numpy().astype(float)
    mc = ctrl[metric_col].to_numpy().astype(float)
    mean_r = float(np.mean(mr))
    mean_c = float(np.mean(mc))
    delta = round(mean_r - mean_c, 4)
    var_r = float(np.var(mr, ddof=1)) / len(mr)
    var_c = float(np.var(mc, ddof=1)) / len(mc)
    se = float(sqrt(var_r + var_c))
    z = (mean_r - mean_c) / se if se > 0 else 0.0
    is_null = abs(z) <= _WILSON_Z
    return {
        "metric": metric_col, "n_real": len(mr), "n_ctrl": len(mc),
        "mean_real": round(mean_r, 4), "mean_ctrl": round(mean_c, 4),
        "delta": delta, "z_stat": round(z, 4), "is_null": is_null,
        "run_id": run_id,
    }


def run_rule_experiment(
    df: pl.DataFrame, rule_col: str, *, run_id: str
) -> dict:
    """Compare winrate of rows where dt_{rule_col} is not null vs null.

    Null when |delta_wr| < 0.03 or CIs overlap. Requires >=30 rows per group.
    """
    ho = _holdout(df).filter(pl.col("cohort") == "real")
    col = f"dt_{rule_col}"
    if col not in ho.columns:
        raise NotReady(f"column {col} not in outcome store")
    a = ho.filter(pl.col(col).is_not_null())
    b = ho.filter(pl.col(col).is_null())
    for name, sub in (("rule_applied", a), ("no_rule", b)):
        if sub.height < 30:
            raise NotReady(f"{name}: only {sub.height} holdout rows (need >=30)")
    n_a, w_a = a.height, a.filter(pl.col("win") == 1).height
    n_b, w_b = b.height, b.filter(pl.col("win") == 1).height
    c_a, lo_a, hi_a = _wilson_ci(w_a, n_a)
    c_b, lo_b, hi_b = _wilson_ci(w_b, n_b)
    delta = round(c_a - c_b, 4)
    ci_overlap = lo_a <= hi_b and lo_b <= hi_a
    is_null = abs(delta) < NULL_ABS_EFFECT or ci_overlap
    return {
        "rule_col": rule_col, "n_applied": n_a, "w_applied": w_a,
        "wr_applied": round(c_a, 4), "lo_applied": round(lo_a, 4),
        "hi_applied": round(hi_a, 4), "n_none": n_b, "w_none": w_b,
        "wr_none": round(c_b, 4), "lo_none": round(lo_b, 4),
        "hi_none": round(hi_b, 4), "delta": delta, "ci_overlap": ci_overlap,
        "is_null": is_null, "run_id": run_id,
    }


def run_feature_removed_experiment(
    df: pl.DataFrame, feature_col: str, *, run_id: str
) -> dict:
    """Ablation proxy — compare winrate above median vs below/zero on holdout real.

    Null when |delta_wr| < 0.03 or CIs overlap. Requires >=10 rows per group.
    """
    ho = _holdout(df).filter(pl.col("cohort") == "real")
    if feature_col not in ho.columns:
        raise NotReady(f"column {feature_col} not in outcome store")
    sub = ho.filter(pl.col(feature_col).is_not_null()).select([feature_col, "win"]).drop_nulls()
    if sub.height < 20:
        raise NotReady(f"only {sub.height} rows with non-null {feature_col} (need >=20)")
    median = float(sub[feature_col].median())
    high = ho.filter(pl.col(feature_col) > median)
    low = ho.filter((pl.col(feature_col).is_null()) | (pl.col(feature_col) <= median))
    for name, grp in (("above_median", high), ("below_null", low)):
        if grp.height < 10:
            raise NotReady(f"{name}: only {grp.height} rows (need >=10)")
    n_h, w_h = high.height, high.filter(pl.col("win") == 1).height
    n_l, w_l = low.height, low.filter(pl.col("win") == 1).height
    c_h, lo_h, hi_h = _wilson_ci(w_h, n_h)
    c_l, lo_l, hi_l = _wilson_ci(w_l, n_l)
    delta = round(c_h - c_l, 4)
    ci_overlap = lo_h <= hi_l and lo_l <= hi_h
    is_null = abs(delta) < NULL_ABS_EFFECT or ci_overlap
    return {
        "feature": feature_col, "median": round(median, 4),
        "n_high": n_h, "w_high": w_h, "wr_high": round(c_h, 4),
        "lo_high": round(lo_h, 4), "hi_high": round(hi_h, 4),
        "n_low": n_l, "w_low": w_l, "wr_low": round(c_l, 4),
        "lo_low": round(lo_l, 4), "hi_low": round(hi_l, 4),
        "delta": delta, "ci_overlap": ci_overlap,
        "is_null": is_null, "run_id": run_id,
    }


def _posterior(prior: float, net_effect: float) -> float:
    e = float(np.clip(net_effect * POSTERIOR_SCALE, -0.5, 0.5))
    return float(np.clip(prior + e, 0.0, 1.0))


def _print_result(hid: str, label: str, delta: float, is_null: bool, n: int | None, reg) -> None:
    verdict = "NULL" if is_null else ("SUPPORTS" if delta > 0 else "AGAINST")
    n_str = f" n={n}" if n is not None else ""
    print(f"  {hid}  {label:22s} delta={delta:+.3f}{n_str}  "
          f"\u2192 {verdict}  post={reg[hid].posterior:.2f}  streak={reg[hid].null_streak}")


def run_all(*, store_path: Path | None = None, run_id: str | None = None) -> int:
    p = store_path or OUTCOMES_PARQUET
    if not p.is_file():
        print(f"no outcome store: {p} \u2014 run research.build first")
        return 1
    df = pl.read_parquet(p)
    reg = ensure_seeded()
    rid = run_id or "manual"

    print("\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550 EXPERIMENT RUN \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550")

    for hid, col in _FEATURE_MAP.items():
        if hid not in reg:
            continue
        try:
            res = run_feature_experiment(df, col, run_id=rid)
        except NotReady as exc:
            print(f"  {hid}  SKIP ({exc})")
            continue
        h = reg[hid]
        new_post = _posterior(h.prior, res["net_auc"])
        supports_null_hyp = hid == "H004"
        is_null = res["is_null"]
        record_experiment(
            reg, hid, run_id=rid, delta=res["net_auc"],
            posterior=(1.0 - new_post) if supports_null_hyp else new_post,
            is_null=is_null and not supports_null_hyp,
            note=f"feature={col} net_auc={res['net_auc']} auc_real={res['auc_real']}",
        )
        _print_result(hid, col, res["net_auc"], is_null, res["n_real"], reg)

    for hid, (eng_a, eng_b) in _ENGINE_MAP.items():
        if hid not in reg:
            continue
        try:
            res = run_engine_experiment(df, eng_a, eng_b, run_id=rid)
        except NotReady as exc:
            print(f"  {hid}  SKIP ({exc})")
            continue
        h = reg[hid]
        new_post = _posterior(h.prior, res["delta"])
        record_experiment(
            reg, hid, run_id=rid, delta=res["delta"],
            posterior=new_post,
            is_null=res["is_null"],
            note=f"engine {eng_a} vs {eng_b} delta_wr={res['delta']} ci_overlap={res['ci_overlap']}",
        )
        label = f"{eng_a} vs {eng_b}"
        total_n = res["n_a"] + res["n_b"]
        _print_result(hid, label, res["delta"], res["is_null"], total_n, reg)

    for hid, metric_col in _VERSION_MAP.items():
        if hid not in reg:
            continue
        try:
            res = run_version_experiment(df, metric_col, run_id=rid)
        except NotReady as exc:
            print(f"  {hid}  SKIP ({exc})")
            continue
        h = reg[hid]
        new_post = _posterior(h.prior, res["delta"])
        record_experiment(
            reg, hid, run_id=rid, delta=res["delta"],
            posterior=new_post,
            is_null=res["is_null"],
            note=f"version real vs coin_flip on {metric_col} delta={res['delta']} z={res['z_stat']}",
        )
        total_n = res["n_real"] + res["n_ctrl"]
        _print_result(hid, metric_col, res["delta"], res["is_null"], total_n, reg)

    for hid, (metric_col, module) in _VERSION_MODULE_MAP.items():
        if hid not in reg:
            continue
        try:
            res = run_version_experiment(df, metric_col, run_id=rid, module=module)
        except NotReady as exc:
            print(f"  {hid}  SKIP ({exc})")
            continue
        h = reg[hid]
        new_post = _posterior(h.prior, res["delta"])
        record_experiment(
            reg, hid, run_id=rid, delta=res["delta"],
            posterior=new_post,
            is_null=res["is_null"],
            note=f"version[{module}] real vs coin_flip on {metric_col} delta={res['delta']} z={res['z_stat']}",
        )
        total_n = res["n_real"] + res["n_ctrl"]
        _print_result(hid, f"{module}/{metric_col}", res["delta"], res["is_null"], total_n, reg)

    for hid, rule_col in _RULE_MAP.items():
        if hid not in reg:
            continue
        try:
            res = run_rule_experiment(df, rule_col, run_id=rid)
        except NotReady as exc:
            print(f"  {hid}  SKIP ({exc})")
            continue
        h = reg[hid]
        new_post = _posterior(h.prior, res["delta"])
        record_experiment(
            reg, hid, run_id=rid, delta=res["delta"],
            posterior=new_post,
            is_null=res["is_null"],
            note=f"rule dt_{rule_col} applied vs null delta_wr={res['delta']} ci_overlap={res['ci_overlap']}",
        )
        total_n = res["n_applied"] + res["n_none"]
        _print_result(hid, f"dt_{rule_col}", res["delta"], res["is_null"], total_n, reg)

    for hid, feat_col in _FEATURE_REMOVED_MAP.items():
        if hid not in reg:
            continue
        try:
            res = run_feature_removed_experiment(df, feat_col, run_id=rid)
        except NotReady as exc:
            print(f"  {hid}  SKIP ({exc})")
            continue
        h = reg[hid]
        new_post = _posterior(h.prior, res["delta"])
        record_experiment(
            reg, hid, run_id=rid, delta=res["delta"],
            posterior=new_post,
            is_null=res["is_null"],
            note=f"feature-vs-removed {feat_col} delta_wr={res['delta']} ci_overlap={res['ci_overlap']}",
        )
        total_n = res["n_high"] + res["n_low"]
        _print_result(hid, f"feat_removed:{feat_col}", res["delta"], res["is_null"], total_n, reg)

    save_registry(reg)
    print("\nregistry updated.")
    return 0


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Research experiment harness.")
    ap.add_argument("--run-id", default="manual")
    return run_all(run_id=ap.parse_args(argv).run_id)


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "run_feature_experiment", "run_engine_experiment", "run_version_experiment",
    "run_rule_experiment", "run_feature_removed_experiment",
    "run_all", "NotReady",
]
