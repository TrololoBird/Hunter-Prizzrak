"""Funnel analysis, gate loss, candidate pool — from outcome_ledger + signal_events."""
from __future__ import annotations
import polars as pl
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

# ── Load outcome ledger ──────────────────────────────────────────────────────
ledger = pl.read_ndjson(DATA / "hunt_outcome_ledger.jsonl")
print(f"outcome_ledger rows: {ledger.height}")
print(f"  event types: {ledger['event'].value_counts().to_dict(as_series=False)}")

# ── 1. Funnel: counts per stage ──────────────────────────────────────────────
funnel = {}
funnel["signal_events_total"] = (
    pl.scan_ndjson(DATA / "signal_events.jsonl")
    .select(pl.len())
    .collect()
    .item()
)

funnel["candidates"] = ledger.filter(pl.col("event") == "candidate").height
funnel["blocked"] = ledger.filter(pl.col("event") == "blocked").height
funnel["delivered"] = ledger.filter(pl.col("event") == "delivered").height
funnel["closed"] = ledger.filter(pl.col("event") == "close").height

# Count signal_events by type
ev = pl.read_ndjson(DATA / "signal_events.jsonl")
funnel["ev_blocked"] = ev.filter(pl.col("event") == "blocked").height
funnel["ev_funnel_lifecycle"] = ev.filter(pl.col("event") == "funnel_lifecycle").height
funnel["ev_funnel_prescan"] = ev.filter(pl.col("event") == "funnel_prescan").height

def pct(a: int, b: int) -> str:
    return f"{a/b*100:.1f}%" if b else "N/A"

print("\n═══════════ FUNNEL ═══════════")
print(f"  Total signal_events           {funnel['signal_events_total']:>8}")
print(f"    ├─ blocked                  {funnel['ev_blocked']:>8}")
print(f"    ├─ funnel_lifecycle         {funnel['ev_funnel_lifecycle']:>8}")
print(f"    ├─ funnel_prescan           {funnel['ev_funnel_prescan']:>8}")
print(f"    └─ other                    {funnel['signal_events_total'] - funnel['ev_blocked'] - funnel['ev_funnel_lifecycle'] - funnel['ev_funnel_prescan']:>8}")
print("")
print(f"  Ledger entries                {ledger.height:>8}")
print(f"    ├─ blocked                  {funnel['blocked']:>8}  (of ledger)")
print(f"    ├─ candidates               {funnel['candidates']:>8}  (of ledger)")
print(f"    ├─ delivered                {funnel['delivered']:>8}  ({pct(funnel['delivered'], funnel['candidates'])} of candidates)")
print(f"    └─ closed                   {funnel['closed']:>8}  ({pct(funnel['closed'], funnel['delivered'])} of delivered)")
print("")

# ── 2. Gate loss: blocked reasons breakdown ──────────────────────────────────
blocked = ledger.filter(pl.col("event") == "blocked")
print("═══════════ BLOCKER CODES (top-20) ═══════════")
blocker_exploded = blocked.explode("blockers").filter(pl.col("blockers").is_not_null())
bc = blocker_exploded["blockers"].value_counts().sort("count", descending=True).head(20)
for row in bc.iter_rows():
    print(f"  {row[0]:50s} {row[1]:>6}")

# Also count unique symbols
print(f"\n  Unique symbols blocked: {blocked['symbol'].n_unique()}")
print("  Top-10 blocked symbols:")
for row in blocked["symbol"].value_counts().sort("count", descending=True).head(10).iter_rows():
    print(f"    {row[0]:20s} {row[1]:>6}")

# ── 3. Candidate pool: what triggered candidates ────────────────────────────
candidates = ledger.filter(pl.col("event") == "candidate")
print("\n═══════════ CANDIDATE POOL ═══════════")
print(f"  Total candidates: {candidates.height}")
print(f"  Unique symbols:   {candidates['symbol'].n_unique()}")
print("  By direction:")
for row in candidates["direction"].value_counts().iter_rows():
    print(f"    {row[0]:>10s} {row[1]:>6}")

# Archetype distribution
if "archetype" in candidates.columns:
    arch = candidates.filter(pl.col("archetype").is_not_null())["archetype"].value_counts().sort("count", descending=True)
    print("  By archetype:")
    for row in arch.iter_rows():
        print(f"    {row[0]:35s} {row[1]:>6}")

# factors_top5 — extract domain distribution
cands_with_factors = candidates.filter(
    pl.col("factors_top5").is_not_null() & (pl.col("factors_top5").list.len() > 0)
)
if cands_with_factors.height > 0:
    print(f"  Candidates with factors_top5: {cands_with_factors.height}")
    # explode factor dicts
    domains = []
    for row in cands_with_factors["factors_top5"].to_list():
        for f in (row or []):
            if isinstance(f, dict):
                d = f.get("domain", "unknown")
                n = f.get("name", "unknown")
                domains.append(f"{d}/{n}")
    from collections import Counter
    dom_counts = Counter(domains)
    print("  Top-15 factor domain/name in candidates:")
    for name, cnt in dom_counts.most_common(15):
        print(f"    {name:45s} {cnt:>6}")

# ── 4. Delivered signals summary ────────────────────────────────────────────
delivered = ledger.filter(pl.col("event") == "delivered")
print("\n═══════════ DELIVERED SIGNALS ═══════════")
print(f"  Total delivered: {delivered.height}")
if delivered.height > 0:
    print("  By direction:")
    for row in delivered["direction"].value_counts().iter_rows():
        print(f"    {row[0]:>10s} {row[1]:>6}")
    print("  fusion_score stats:")
    fs = delivered["fusion_score"].drop_nulls()
    if fs.len() > 0:
        print(f"    mean={fs.mean():.2f}  median={fs.median():.2f}  min={fs.min():.2f}  max={fs.max():.2f}")
    print("  signal_type:")
    for row in delivered["signal_type"].value_counts().iter_rows():
        print(f"    {row[0]:>15s} {row[1]:>6}")

# ── 5. Signal history (closed signals) ──────────────────────────────────────
hist = pl.read_ndjson(DATA / "signal_history.jsonl")
print("\n═══════════ SIGNAL HISTORY (closed) ═══════════")
print(f"  Total: {hist.height}")
if hist.height > 0:
    print("  Close reasons:")
    for row in hist["close_reason"].value_counts().iter_rows():
        print(f"    {row[0]:30s} {row[1]:>6}")
    print("  PnL stats:")
    pnl = hist["pnl_pct"].drop_nulls()
    if pnl.len() > 0:
        print(f"    mean={pnl.mean():.3f}%  median={pnl.median():.3f}%  min={pnl.min():.3f}%  max={pnl.max():.3f}%")
    print("  Duration (min):")
    dur = hist["duration_min"].drop_nulls()
    if dur.len() > 0:
        print(f"    mean={dur.mean():.0f}  median={dur.median():.0f}  min={dur.min():.0f}  max={dur.max():.0f}")
