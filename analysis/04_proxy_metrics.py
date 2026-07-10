"""Proxy metrics — the "not-worse" gate for Track B/C when edge n is too small.

At ~1-3 signals/day, proving a WR improvement needs ~1000 trades (1-3 years).
So most changes are gated on *not regressing* cheap, fast-moving proxies rather
than on a proven edge delta. This computes the proxies that are recoverable from
the research Outcome Store + candidate ledger, and freezes them as a baseline.

Metrics (formulas):
  data_completeness = mean(atr present AND not dq_gaps AND label != unknown)   [real cohort]
  gap_rate          = mean(dq_gaps)                                            [real cohort]
  atr_present_rate  = mean(atr is not null)   ← surfaces the known ATR bug
  labelled_rate     = mean(label != 'unknown')
  signal_count_CV   = std/mean of real signals per UTC day                     (dispersion)
  degradation_rate  = candidate rows dropped as unusable / total (from build skip stats)

latency_p95 is intentionally N/A here: replay data carries no per-tick fetch
latency. It is a *runtime* proxy — wire it from the live pipeline, not replay.

Run: .venv/bin/python analysis/04_proxy_metrics.py [--freeze]
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

DATA = Path(__file__).resolve().parents[1] / "data"
OUTCOMES = DATA / "research" / "outcomes.parquet"
BASELINE = DATA / "research" / "proxy_baseline.json"


def compute() -> dict:
    if not OUTCOMES.is_file():
        raise FileNotFoundError(f"no outcome store: {OUTCOMES} — run research.build first")
    df = pl.read_parquet(OUTCOMES)
    real = df.filter(pl.col("cohort") == "real")
    n = real.height
    if n == 0:
        return {"n_real": 0}

    atr_present = real.select((pl.col("atr").is_not_null()).mean()).item()
    gap_rate = real.select(pl.col("dq_gaps").cast(pl.Float64).mean()).item()
    labelled = real.select((pl.col("label") != "unknown").mean()).item()
    completeness = real.select(
        (
            pl.col("atr").is_not_null()
            & (~pl.col("dq_gaps"))
            & (pl.col("label") != "unknown")
        ).cast(pl.Float64).mean()
    ).item()

    # signals per UTC day → coefficient of variation (dispersion of cadence)
    per_day = (
        real.with_columns(
            (pl.col("t0_ms") // 86_400_000).alias("day")
        )
        .group_by("day")
        .agg(pl.len().alias("c"))
    )
    if per_day.height >= 2:
        mean_c = per_day.select(pl.col("c").mean()).item()
        std_c = per_day.select(pl.col("c").std()).item()
        cv = (std_c / mean_c) if mean_c else None
    else:
        mean_c = float(n) if per_day.height == 1 else None
        cv = None

    m = {
        "computed_at": datetime.now(UTC).isoformat(),
        "n_real": n,
        "data_completeness": round(completeness, 4),
        "atr_present_rate": round(atr_present, 4),
        "gap_rate": round(gap_rate, 4),
        "labelled_rate": round(labelled, 4),
        "signals_per_day_mean": round(mean_c, 2) if mean_c is not None else None,
        "signal_count_cv": round(cv, 4) if cv is not None else None,
        "latency_p95_ms": None,  # runtime-only; not in replay data
        "degradation_rate": None,
    }

    # Wire degradation_rate from build stats
    build_stats_path = OUTCOMES.parent / "build_stats.json"
    if build_stats_path.is_file():
        try:
            build = json.loads(build_stats_path.read_text())
            skipped = build.get("skipped", 0)
            candidates_with_path = build.get("candidates_with_path", 0)
            total = skipped + candidates_with_path
            m["degradation_rate"] = round(skipped / total, 4) if total else 0.0
        except Exception:
            pass

    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true", help="write current values as frozen baseline")
    args = ap.parse_args(argv)

    m = compute()
    print("═══════════ PROXY METRICS ═══════════")
    for k, v in m.items():
        print(f"  {k:22s} {v}")

    if BASELINE.is_file():
        base = json.loads(BASELINE.read_text())
        print("\n── vs frozen baseline ──")
        for k in ("data_completeness", "gap_rate", "labelled_rate", "signal_count_cv", "degradation_rate"):
            b, c = base.get(k), m.get(k)
            if isinstance(b, (int, float)) and isinstance(c, (int, float)):
                delta = c - b
                flag = "WORSE" if (k in ("gap_rate", "degradation_rate") and delta > 0.02) or (k in ("data_completeness", "labelled_rate") and delta < -0.02) else "ok"
                print(f"  {k:22s} base={b}  now={c}  Δ={delta:+.4f}  {flag}")

    if args.freeze:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(m, indent=2))
        print(f"\nfrozen → {BASELINE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
