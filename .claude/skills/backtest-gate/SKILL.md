---
name: backtest-gate
description: Run the touch-based outcome backtest before/after a change that alters signal EMISSION or position MANAGEMENT (not just presentation), and report the R delta. Use before merging any change to what the scanner detects or how the tracker manages a signal (e.g. G-3, G-7, G-13, or reviving a phantom-key feature). Pinning tests prove correctness, NOT "better by R" — this skill is the emission gate.
---

Some fixes change WHAT is emitted or HOW a position is managed, not just the Telegram text.
For those, a green pinning test is necessary but not sufficient — you must show the change
does not degrade realized expectancy. This skill runs that gate.

## When it applies

- Scanner detection changes (what qualifies as a setup / pattern): e.g. G-7 Pattern A.
- Entry/stop/target geometry, R:R gating: e.g. G-3 worst-entry basis.
- Tracker management (breakeven, trailing, invalidation, TTL): e.g. G-70, reviving G-68/G-69.
- Universe/emission filters.
Presentation-only changes (labels, stats display, dead-code deletion) do NOT need this.

## Run (chunked — the full run OOMs, exit 137)

The backtest is `research/backtest_scanner.py::main(ds=...)` over a parquet dataset.
**The canonical dataset is whatever `research/dataset_active_version.txt` says** (currently
11 → `research/dataset_v11`, 120 coins × 6 TF) — do NOT trust hardcoded `_DEFAULT_DS`
(dataset_v8) or stale doc mentions of v10. The first unchunked v11 run was killed by the
OS (exit 137, memory). Run it memory-bounded — one PROCESS per symbol (a per-symbol loop
in one process still accumulates polars memory), emitting per-trade JSONL, then aggregate:

```bash
# baseline (stash or checkout the pre-change code), then after (the change): same dataset.
uv run python research/backtest_scanner.py   # inspect main(); pass a smaller ds / loop coins if it OOMs
```

If it still OOMs: split the dataset by symbol and run one symbol at a time, summing trades
and R, instead of the whole-dataset pass. Keep the exact same dataset + horizon for both
before and after so the delta is attributable to the change.

## Report

- Trades count, win-rate, mean R/trade — BEFORE vs AFTER, per side (longs are the edge,
  +2.06R historically; shorts don't win — see memory scanner-verify-match-author-not-profit).
- Verdict: does the change hold or improve mean-R without collapsing trade count? If it
  starves the funnel, the gate is a §5 knob (min_rr etc.), not the fix.

Scope caveat: this backtest replays ONLY the scanner pattern detector
(`advance_manipulation_scales` + production `_geometry`). It cannot measure fusion
(display/journal-only layer — no emission gate reads it), levels-veto, price_sanity, or
anything needing orderbook/taker/VP-map history (absent from OHLCV datasets). Small
datasets are far too small to TUNE against (overfit risk); use the
gate to catch REGRESSIONS, and expand the dataset (research/fetch/) before trusting absolute
R. See memory scanner-negative-expectancy-backtest.
