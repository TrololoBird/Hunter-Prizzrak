---
name: backtest-gate
description: "MANIPULATIONS MODULE ONLY (hunt_core/scanner/ + deliver/manipulation_delivery.py). Run the touch-based outcome backtest before/after a change that alters signal EMISSION or position MANAGEMENT there, and report the R delta. DOES NOT APPLY to hunt_core/prizrak/ — no backtest imports it, so a run would return an identical number that means nothing; measure prizrak changes on live data instead. Use before merging a change to what the SCANNER detects or how the tracker manages a scanner signal (e.g. G-3, G-7, G-13). Pinning tests prove correctness, NOT 'better by R'."
---

## STOP — which module is your change in?

This skill covers **МАНИПУЛЯЦИИ only**. Check before running anything:

| Your change touches | This skill |
|---|---|
| `hunt_core/scanner/**`, `deliver/manipulation_delivery.py` | ✅ applies |
| `hunt_core/prizrak/**` | ❌ **does NOT apply — do not run it** |
| shared plumbing (`market/`, `data/`, `features/`) | ✅ only if a scanner path consumes it |

**Why prizrak is out of scope, mechanically:** every `research/backtest_*.py` imports
`advance_manipulation_scales` / `manipulation_delivery`; **none imports `hunt_core/prizrak/`**
(pinned by `tests/test_module_boundary.py`). So a prizrak change cannot move the number.
Running the gate anyway and reading "no change" as "no regression" is FALSE SAFETY — the
harness never executed the code you edited.

**What to do for a prizrak emission change instead:** run its OWN measurer,
`research/prizrak_replay.py` (added 2026-07-17) — a forward-replay of the production path
(`build_prizrak_signals`) over `dataset_v10` with no lookahead, touch-based outcome (limit
fill → stop/target first), reporting win rate and R-expectancy before/after. It is
deliberately NOT a `backtest_*.py` (that prefix is reserved for the manipulations harness and
must import the manipulations path); the boundary is now two-directional — `prizrak_replay.py`
must not import the scanner/delivery path, pinned by `tests/test_module_boundary.py`.
Reuse its `_load`/`_resolve`, hold an OOS slice (`--oos`), count independent episodes not just
n, and don't tune on the same slice you judge on (Bailey/López de Prado: in-sample max grows
with trials). Older precedent for a targeted one-off measurement: the стр.24 ТФ+1 ceiling
(229b1f7), 12 symbols × 40 setups. Either is a real measurement; the backtest gate would be null.


Some fixes change WHAT is emitted or HOW a position is managed, not just the Telegram text.
For those, a green pinning test is necessary but not sufficient — you must show the change
does not degrade realized expectancy. This skill runs that gate **for the scanner path**.

## When it applies

Every entry below is implicitly prefixed **«in `hunt_core/scanner/` / `manipulation_delivery`»**.
The same words describe real work in `hunt_core/prizrak/` too, and that is exactly the trap —
"target geometry" reads as universal, so an agent matches this skill to a prizrak change and
runs a harness that never imports it. Re-read the module table above before using this list.

- SCANNER detection changes (what qualifies as a setup / pattern): e.g. G-7 Pattern A.
- SCANNER entry/stop/target geometry, R:R gating: e.g. G-3 worst-entry basis.
  (`prizrak` target geometry — e.g. `_structural_targets` — is NOT this. See 229b1f7.)
- Tracker management of a SCANNER signal (breakeven, trailing, invalidation, TTL): G-70.
- SCANNER universe/emission filters.

Presentation-only changes (labels, stats display, dead-code deletion) do NOT need this —
in either module.

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
