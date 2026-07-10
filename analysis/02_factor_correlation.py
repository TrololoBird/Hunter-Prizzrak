"""Factor correlation matrix from hunt_scan JSONL factor_panel (4 factors).
Reads via Python json.loads to avoid Polars NDJSON struct parse issues."""
from __future__ import annotations
import polars as pl
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

# ── 1. Load all hunt_scan daily files ───────────────────────────────────────
files = sorted(DATA.glob("hunt_scan-2026-*-*.jsonl"))
print(f"Scan files ({len(files)}): {[f.name for f in files]}")

rows = []
for f in files:
    with open(f) as fh:
        for line in fh:
            row = json.loads(line)
            fp = row.get("factor_panel")
            if fp and isinstance(fp, dict):
                fp_row = {"symbol": row.get("symbol"), "ts": row.get("ts")}
                for k in ("momentum_rsi15", "trend_adx1h", "deriv_oi_z", "deriv_funding"):
                    fp_row[k] = fp.get(k)
                rows.append(fp_row)
            if len(rows) % 5000 == 0:
                print(f"  loaded {len(rows)} rows...")

print(f"\nTotal rows with factor_panel: {len(rows)}")

# ── 2. Convert to Polars DataFrame ──────────────────────────────────────────
df = pl.DataFrame(rows)
print(f"DataFrame: {df.height} rows × {df.width} cols")
print(df.schema)

# Remove rows with any null factor value
df_num = df.drop_nulls(subset=["momentum_rsi15", "trend_adx1h", "deriv_oi_z", "deriv_funding"])
print(f"Rows after null drop: {df_num.height} ({df_num.height/len(rows)*100:.1f}%)")

# ── 3. Descriptive stats ────────────────────────────────────────────────────
factor_cols = ["momentum_rsi15", "trend_adx1h", "deriv_oi_z", "deriv_funding"]

print("\n═══════════ DESCRIPTIVE STATS ═══════════")
print(df_num.describe())

# ── 4. Pearson correlation ──────────────────────────────────────────────────
def print_corr_matrix(label: str, df: pl.DataFrame, cols: list[str]) -> None:
    print(f"\n═══════════ {label} ═══════════")
    corr_mat = df.select(cols).corr()
    for i, c in enumerate(cols):
        vals = {c2: f"{corr_mat[c2][i]:+.4f}" for c2 in cols}
        print(f"  {c:20s} {vals}")

print_corr_matrix("PEARSON CORRELATION", df_num, factor_cols)

# ── 5. Spearman correlation ─────────────────────────────────────────────────
spearman_df = df_num.select(
    [pl.col(c).rank().alias(c) for c in factor_cols]
)
print_corr_matrix("SPEARMAN CORRELATION", spearman_df, factor_cols)

# ── 6. Distribution per factor ──────────────────────────────────────────────
print("\n═══════════ FACTOR DISTRIBUTIONS ═══════════")
for col in factor_cols:
    s = df_num[col].drop_nulls()
    zeros = (s == 0).sum()
    pos = (s > 0).sum()
    neg = (s < 0).sum()
    t = s.len()
    print(f"  {col:20s} zero={zeros:>6} ({zeros/t*100:.1f}%)  "
          f"pos={pos:>6} ({pos/t*100:.1f}%)  neg={neg:>6} ({neg/t*100:.1f}%)")
    print(f"  {'':20s} mean={s.mean():+.4f}  median={s.median():+.4f}  "
          f"std={s.std():.4f}  min={s.min():+.4f}  max={s.max():+.4f}")

# ── 7. Daily coverage ───────────────────────────────────────────────────────
print("\n═══════════ DAILY COVERAGE ═══════════")
df_num = df_num.with_columns(pl.col("ts").str.slice(0, 10).alias("day"))
daily_counts = df_num.group_by("day").agg(pl.len().alias("n")).sort("day")
for row in daily_counts.iter_rows():
    print(f"  {row[0]:12s} {row[1]:>6}")

# ── 8. Factor co-occurrence analysis ───────────────────────────────────────
print("\n═══════════ FACTOR PAIRWISE NON-ZERO CO-OCCURRENCE ═══════════")
for i, c1 in enumerate(factor_cols):
    for c2 in factor_cols[i+1:]:
        both_ok = df_num.filter(
            (pl.col(c1) != 0) | (pl.col(c2) != 0)
        ).height
        both_zero = df_num.filter(
            (pl.col(c1) == 0) & (pl.col(c2) == 0)
        ).height
        print(f"  {c1:20s} × {c2:20s}: both_nonzero={both_ok:>6}  both_zero={both_zero:>6}")

# ── 9. Symbol-level stats ──────────────────────────────────────────────────
print("\n═══════════ TOP-15 SYMBOLS (by factor rows) ═══════════")
sym_counts = df_num.group_by("symbol").agg(pl.len().alias("n")).sort("n", descending=True)
print(f"  Total symbols with factor data: {sym_counts.height}")
for row in sym_counts.head(15).iter_rows():
    print(f"    {row[0]:20s} {row[1]:>6}")
