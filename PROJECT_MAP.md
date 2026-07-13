# HUNTER — Full Project Map

Purpose of this document: a complete structural map of `hunt_core` (~59k LOC, 18 sub-packages) to
ground the per-module review that follows. Built from a full file inventory + module docstrings + an
inter-package import graph. Confidence: **[confirmed]** = read from code/graph; **[inferred]** =
deduced, not yet verified per-module.

Companion: `MAPS_REVIEW.md` (the `maps/` package is already reviewed — the template for the rest).

---

## 1. What the system is

Crypto-futures **signal-analytics** (not a trading bot): reads Binance USDⓈ-M public data via CCXT
(REST + Pro WS), runs a Polars feature engine, and emits analysis to Telegram. Two independent
strategy modules share the infra:
- **Module 1 — Deep / Prizrak** (`prizrak/`): SMC-style structural methodology (accumulation, POC,
  ПП/trend-break, multi-scale structure).
- **Module 2 — Scanner / Hunter** (`scanner/`): universe-wide manipulation-pattern detection
  (Pattern A long / B short).

Entry point [confirmed]: `python -m hunt_core watch` → `__main__.py` → `_cli.py` →
`runtime/cycle/_impl.py` (`run_loop`/`run_tick`) → `runtime/cycle/_cycle_loop.py` (universe, prescan,
scheduling) → `_cycle_tick.py` (per-tick snapshot → analysis → delivery).

---

## 2. Layered architecture (by role, not size)

**Layer 0 — foundation / cross-cutting** (root + small shared): `paths.py` (canonical data paths,
fan-in 37), `errors.py`, `secrets.py`, `clock.py` (exchange-anchored wall clock), `bootstrap.py`
(sys.path + Polars stack check), `contract.py` (trade-plan contract, 1165 LOC — large for this layer),
`data_readiness.py`, `domain/` (config, knowledge ontology, schemas, snapshot types).

**Layer 1 — market data plane** (`market/`, fan-in 33): CCXT REST client (`client.py` 2295),
Pro WS streams (`streams.py` 1794), rate-limit/pacing (`rate_limit.py`, `ccxt_rest.py`, `ccxt_guard.py`,
`capacity.py`), cross-venue (`cross.py`), spot companion, symbol id mapping. **This is the crash
surface** (see `MAPS_REVIEW.md` §0 and the main remediation plan WS-1).

**Layer 2 — ingest & storage** (`data/`, fan-in 63, the most depended-upon package): REST/WS ingest
(`collect.py` 824), strict completeness gate (`completeness.py` 981), hot frame cache
(`frame_cache.py`), parquet/JSONL lake, universe resolution (`universe.py` 545), blacklist, warmup.

**Layer 3 — feature engine** (`features/`, 7967 LOC, analysis fan-in 33): per-frame indicator
pipeline (`prepare_frame.py` 1243, `prepare.py` 937), snapshot builders (`snapshot.py` 1334),
microstructure (`microstructure.py` 857), swing structure/pivots, volume profile, polars_ta bridge,
candle/chart patterns, factor registry.

**Layer 4 — analytics / maps / toolkit**: `maps/` (orderbook/liq/VP — reviewed), `levels/` (SL/TP
geometry, `levels.py` 1605), `regime/` (regime classifier + market survey), `toolkit/` (fusion score,
forecasts, trend/ADX canon, targets), `confluence/` (legacy MTF — flagged for retirement in main plan).

**Layer 5 — strategy modules**: `prizrak/` (Deep, `orchestrator.py` 1824 god-file), `scanner/`
(`patterns.py` 1035 state machine, `prescan.py` 975, `scoring.py` trivial-by-design).

**Layer 6 — lifecycle, tracking, delivery**: `signals/` (shared lifecycle spine), `track/` (active
tracker `tracker.py` 1141, SL/TP eval, follow-ups, ledgers, pump history), `deliver/` (Telegram
render — `telegram.py` 1017, `_sections.py` 879, `manipulation_delivery.py` 529).

**Layer 7 — orchestration & ops**: `runtime/` (7266 LOC, 23 files — the conductor; tick assembly
1148, cycle loop 1062, analyst assembly, query/stats/signals reports, telegram commands, telemetry,
**`heartbeat.py`** — already present), `diagnostics/` (data-plane audit, universe health),
`params/` (calibration store), `research/` (edge-measurement harness, control cohorts, outcome store).

---

## 3. Tick data-flow (the hot path) [inferred from imports + docstrings]

```
watch loop (_cycle_loop)                            background loops (armed alongside):
  └─ resolve universe (data/universe)                 • analyst_pinned_loop  (prizrak Deep)
  └─ prescan (scanner/prescan)                        • manipulation_scan_loop (scanner)
  └─ per symbol (_cycle_tick):                        • path_backfill_loop   (track/path_backfill)
       ingest (data/collect ← market/streams,ccxt)    • (proposed) dominance/marketcap refreshers
       completeness gate (data/completeness)
       frame cache (data/frame_cache)
       features (features/prepare_frame → snapshot)
       maps bundle (maps/engine)
       ┌─ Module 1: prizrak/entry → orchestrator → build
       └─ Module 2: scanner/detect/patterns
       lifecycle (signals/lifecycle) → track/tracker
       delivery gate (deliver/geometry, readiness) → deliver/telegram
       persist (data/lake, runtime/tick_io, track/events)
```

---

## 4. Inter-package dependency graph [confirmed]

Fan-out (package → its dependencies, count = distinct import sites):

```
runtime   -> data(44) deliver(24) prizrak(19) track(18) market(17) features(17) scanner(8)
             domain(8) params(5) diagnostics(4) toolkit(4) confluence(3) maps(3) regime(3) signals(3)
data      -> market(7) features(6) domain(4) ... runtime(1)     ← circular w/ runtime
track     -> params(6) scanner(2) data(2) ... runtime(1)
market    -> maps(10) data(3) ... runtime(2)                    ← circular w/ maps AND runtime
features  -> toolkit(4) market(4) data(3) levels(1)
deliver   -> track(4) scanner(3) prizrak(2) ...                 ← circular w/ prizrak
prizrak   -> deliver(4) data(3) ... runtime(1)                  ← circular w/ deliver AND runtime
scanner   -> track(2) data(2) features(2) ... deliver(1)
maps      -> market(2) toolkit(1) data(1) features(1)           ← circular w/ market
```

Fan-in (most depended-upon): `data`(63) · `paths`(37) · `market`(33) · `features`(33) · `deliver`(29)
· `track`(26) · `prizrak`(22) · `domain`(21) · `errors`(16) · `params`(16) · `maps`(16).

### Architecture smells surfaced by the graph [confirmed structurally / inferred as problems]
1. **Circular dependencies:** `runtime↔data`, `runtime↔market`, `runtime↔prizrak`, `market↔maps`,
   `prizrak↔deliver`. Each is a layering inversion. `market → maps(10)` is the most suspicious — the
   transport layer importing the analytics layer inverts the intended direction (maps should consume
   market, not vice-versa). To be traced during the `market/` review.
2. **`runtime` is a god-package** — imports 20 of 18 sibling packages (some via lazy import to dodge
   cycles, per `runtime/__init__.py` docstring "lazy exports to avoid import cycles" — an
   acknowledged smell).
3. **`data` as universal substrate (fan-in 63)** — appropriate for an ingest layer, but its own
   `completeness.py` (981) + `collect.py` (824) size suggests responsibilities worth splitting.
4. **`contract.py` (1165, root)** — its docstring says it "overlaps with engine imported from
   canonical source"; possible duplication to reconcile.

---

## 5. Known cross-cutting issues (from prior reviews / main plan)
- **Rule compliance:** 41 files on stdlib `logging`; 115 `@dataclass` occurrences (Pydantic rule).
- **God-files:** `client.py` 2295 · `prizrak/orchestrator.py` 1824 · `streams.py` 1794 ·
  `levels.py` 1605 · `snapshot.py` 1334 · `prepare_frame.py` 1243 · `contract.py` 1165 ·
  `tick_assembly.py` 1148 · `tracker.py` 1141 · `patterns.py` 1035.
- **Test coverage:** thin on `market/` (crash surface), `runtime/cycle`, `deliver/`.
- **`heartbeat.py` already exists** — WS-1.1 may be partially implemented; verify during runtime review.

---

## 6. Full file inventory
See the generated table in the review log; per-package file lists (LOC + one-line purpose) are the
basis for each module review. Largest files per package are called out in §2/§5 above.

---

## 7. Proposed review order (my recommendation)

Ordered by **risk × centrality × external-benchmarkability**, one review doc per package
(`REVIEW_<pkg>.md`), full external research each:

1. **market/** — ✅ complete (`REVIEW_market.md`). Crash surface + fan-in 33; validated the WS-1/ADR-0001
   diagnosis at code level (with corrections: spot budget cross-contamination F2, `await_pause` ban leak F3,
   fstream URL migration F8a).
2. **data/** — fan-in 63 (everything depends on it); completeness gate correctness is load-bearing.
3. **features/** — largest package, analysis fan-in 33; external TA/microstructure benchmarking.
4. **scanner/** — known negative expectancy (−21R); external manipulation-detection methodology.
5. **prizrak/** — the Deep engine; SMC methodology benchmarking; god-file split.
6. **levels/** — SL/TP geometry; external structural-level methods.
7. **track/** + **signals/** — lifecycle/tracker correctness (state machines, FSMs).
8. **runtime/** — orchestration, watchdog/heartbeat, the tick loop (ties back to WS-1).
9. **regime/**, **toolkit/**, **confluence/** — analytics support.
10. **deliver/**, **domain/**, **params/**, **research/**, **diagnostics/**, root modules — plumbing,
    contracts, ops.

`maps/` is complete (`MAPS_REVIEW.md`); `market/` is complete (`REVIEW_market.md`). Next: `data/`.
