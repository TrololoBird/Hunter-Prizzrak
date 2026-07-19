# ADR 0004 — Native module rewrite: typed MarketView replaces the row-dict

Architecture LAW (memory engine-native-modules-from-scratch): engine stays NATIVE on documented libs;
modules rebuilt FROM SCRATCH on the engine's native contract (NOT the deprecated engine_adapters/).
Headline: the untyped row-dict (host of the phantom-key/falsy-zero/name-lie defect family) is replaced
by a frozen strict Pydantic-v2 `MarketView` built from engine.snapshot() (presence ⟺ proven-fresh).
`hunt_core/view/models.py` (committed 97ae2fa) is the concrete core.

## EXECUTION PROGRAM (synthesized)

# ADR-0004 — Native module rewrite: execution program

**Status:** Accepted · **Supersedes execution detail in:** ADR-0003 (engine cutover) · **Date:** 2026-07-18
**Depends on:** engine facade (`engine/{api,state,multi,rest,orderflow,funding_stats,spot,freshness}.py`) landed and warm.

---

## 1. Architecture summary (one screen)

**Decision.** The untyped `dict[str, Any]` row (host of the phantom-key / falsy-zero / name-lie / orphan-field defect family) is replaced by a **frozen, strict, `extra="forbid"` Pydantic v2 `MarketView`** built from `engine.snapshot()`. Pydantic v2, **not msgspec**: at ~3 builds/s the Polars feature compute dominates wall-time by 3–4 orders of magnitude, so Rust-core validation is free and buys the validators/computed-fields that make the invariants enforceable.

**The load-bearing invariant.** `snapshot().optional(name)` returns a value **iff** its plane is present and fresh at `now_ms`, else `None` (`state.py:167`). `MarketView` carries that result, so **presence ⟺ engine-proven-fresh**. `None` means exactly «нет данных» — one meaning, no age to re-check, no `Fresh[T]` wrapper. I-6 becomes structural: nothing writes a substitute. I-5 is discharged upstream — engine frames are closed-only (`rest.seed_ohlcv` drops the forming bar; WS merges only newly-closed), so `[-1]` IS the newest closed bar and the whole `idx=-2 if closed` shim family deletes.

**Data flow.**
```
MultiEngine(PINNED∪cli).start() + SpotEngine.start()   ← constructed once in run_loop
        │
   MarketViewBuilder.build(sym)  ──▶  MarketView (frozen/strict)         [TRACKED/warm path]
        │                                   │
   engine.exchange + rest.*  ──▶ scanner lean input                       [DYNAMIC REST tail]
        │
   ┌────┴─────────────────────────────────────────────────────┐
   features/  MarketView→FeaturePanel   maps/ MarketView→MapBundle   (typed, fail-loud Optional)
   prizrak/   assemble_prizrak(view,panel)→PrizrakOutput               [Module 1, live-measured]
   scanner/   REST-tail→ManipulationSetup                              [Module 2, backtest-gated]
   track/     typed SignalOpenRequest/SignalState (shared seam)
   deliver/   PrizrakSetup|ManipulationSetup → str (two disjoint lanes)
```
Every field traces to one engine call; nothing fabricated; `None` propagates from `optional()`. The 851-line tiered `snapshot_symbol` (hot/carry/delta, `frame_cache`, `_fetch_rest_pack`, `_patch_market_live`, `merge_ws_kline_closed`) collapses to ~40 lines because the engine's push-state store *is* the hot path.

**Construction.** `pydantic-settings BaseSettings` with `extra="forbid"` over `(config.defaults.toml, config.toml)` — a stray/moved TOML key becomes a **boot error**, killing the "edit silently no-ops" trap. The two strategies remain disjoint because there is **no shared dict left to cross** (`tests/test_module_boundary.py` holds by construction).

---

## 2. Staged execution plan

Principle: **additive foundations first, seam-swap and deletion last.** Each stage ends green (`ruff` · `mypy` · `vulture` · `pytest`). The module boundary and I-5/I-6 hold at *every intermediate commit* — green is required mid-migration, not only at the end. This is multi-session (est. 8–12 focused sessions; S3, S7, S8, S11 are each a session-plus).

| # | Stage | Kind | Row-dict status |
|---|---|---|---|
| S0 | Extract keep-utilities + relocate classifiers | additive/move | untouched |
| S1 | Config → `pydantic-settings` | replace loader | untouched |
| S2 | View core (`models/price/build/runtime`) | additive | parallel, unused |
| S3 | `features/` → `FeaturePanel` | additive | parallel |
| S4 | `maps/` → typed `MapBundle` | additive | parallel |
| S5 | track seam typing (`SignalOpenRequest`) | additive | both callers build it |
| S6 | Engine construction **alongside** legacy plane | coexistence opens | legacy still live |
| S7 | **SCANNER** cutover → REST tail | cutover | **dies for Module 2** |
| S8 | **PRIZRAK** cutover → `assemble_prizrak` | cutover | **dies for Module 1** |
| S9 | `track/`+`deliver/` rest-wiring + `SignalState` | cutover | shared seam typed |
| S10 | Seam-swap: delete `snapshot_symbol` + legacy plane | **row-dict fully dead** | — |
| S11 | Deletion: transport + data + `engine_adapters/` | cleanup | — |

Coexistence window: **S6–S9**. Kill-the-row-dict milestone: **§4**.

---

## 3. Per-stage detail

### S0 — Extract pure keep-utilities + relocate classifiers
- **Files:** new `hunt_core/toolkit/book_math.py` (verbatim from `market/client.py:2192-2568` + `view_from_book`), new `hunt_core/toolkit/ohlcv.py` (from `factory.py`: `ccxt_ohlcv_to_frame`, `finalize_kline_frame`, `resample_ohlcv_from_1m`, `drop_unclosed_ohlcv_tail`, …). Relocate `underlying_type_of` / `is_linear_usdt_swap_market` / `try_binance_id_from_ccxt` out of `market/symbols.py` to a keep-module. Repoint consumers (`features/prepare.py`, `features/snapshot.py`, `maps/orderbook.py`, `deliver/manipulation_delivery.py`, `track/path_backfill.py`).
- **Pattern:** pure move; extract-then-repoint-then-(later)-delete. Never delete-then-scramble.
- **Verify:** `ruff`+`mypy` green; grep old hosts import cleanly; existing tests unchanged.
- **Risk:** `research/` offline scripts may import sync helpers (`create_sync_binance_future`) from `factory.py` — grep `research/` first; if used, move them to `toolkit/ohlcv.py`, don't strand. COIN-filter must stay fail-open (tokenized-equities memory).

### S1 — Config → `pydantic-settings`
- **Files:** new `hunt_core/domain/settings.py` (`HuntSettings(BaseSettings)`, `SettingsConfigDict(toml_file=(defaults, overlay), extra="forbid")`, sub-models `EngineSettings`/`FilterConfig`/… all `extra="forbid"`); flip `config.py:31` `_StrictModel` `extra="ignore"`→`forbid`; route through `TomlConfigSettingsSource`.
- **Pattern:** every documented key becomes a declared field or the process won't boot; the config-drift-auditor's job becomes a type error.
- **Verify:** boot the loader against real `config.defaults.toml`+`config.toml`; **audit for currently-ignored keys first** — any `[gate.*]`-style dead section now raises. `config-drift-auditor` subagent.
- **Risk:** a key that was silently fallback-wins (memory `config-file-map`) surfaces as a boot failure — that is the point, but stage it first so the failure is isolated.

### S2 — View core (additive, nothing consumes yet)
- **Files:** new `hunt_core/view/models.py` (`MarketView` + `Klines/Book/Derivs/Orderflow/Cross/Spot`), `view/price.py` (`resolve_price(snap)→PriceQuote|None`, replaces `market/live_price.py`'s fallback ladder — freshness is the plane stamp, no `max_age_s`), `view/build.py` (`MarketViewBuilder.build`), `view/runtime.py` (`build_market_runtime`, `MarketRuntime`).
- **Pattern:** `_REQUIRED_TRACKED` = the full plane resolve-set (**incl. `bbo` distinct from `book`**; §grounding). `build()` reads each via `optional()`; `None` propagates; `not_ready`/`plane_ages` carried for diagnostics.
- **Verify:** unit tests against a fake `MarketSnapshot` (present/absent/stale planes) asserting presence⟺fresh and that a genuine `0.0` survives (no `or 0 or None`). `mypy` proves `.get()` is impossible on frozen models.
- **Risk:** three schema gaps to resolve now, not paper over: (a) `Book.bid/ask`←`bbo`, `bids/asks`←`book`; (b) add `Derivs.quote_volume_24h_fut` from the futures `ticker` plane (kills the `vol_24h_m*1e6` phantom-key); (c) `Derivs.index` has no dedicated plane — derive from `rest.fetch_ohlcv_series(price="index")` latest or leave `None` (`_index_of` is a placeholder).

### S3 — `features/` → `FeaturePanel`
- **Files:** new `features/frame.py` (`frame_from_bars`), `features/models.py` (`FeaturePanel`/`Frames`/`TfSummary`/`Regime`/`VolumeProfile`/`FactorPanel`), `features/build.py` (`compute_features(view)`), `features/summary.py` (retyped `tf_summary`/`regime_of`/`volume_profile_of`), retyped `factors.py`. Keep `prepare_frame.py` body (pure Polars) unchanged. **`compute_features` lands alongside `prepare_symbol`** — old god-object path still live.
- **Pattern:** pure `MarketView → FeaturePanel`. `features/` sheds its entire market-enrichment half (that's now `view.derivs/orderflow/book/cross/spot`). ADX/DI/Supertrend/VP/CVD stay hand-rolled pure-Polars.
- **Verify:** `no-lookahead-reviewer` (I-5); Hypothesis property "appending a forming candle changes no reading"; numeric parity of `compute_features` vs `prepare_symbol` on a fixture for the migrated fields.
- **Risk (headline fail-loud fix):** ccxt's 6-col `Bar` drops `taker_buy_base_volume` — so `session_cvd`/`rolling_cvd_24h`/`delta_ratio` have **no input**; today's branch fabricates `pl.lit(0.0)` (`prepare_frame.py:255`). Under the engine that is now the *only* branch → it **must become fail-loud `None`**, not a fabricated "balanced flow". Live-only CVD from the `trades` plane via `orderflow.taker_flow` is the deliberate, surfaced capability change. **talipp streaming migration is explicitly deferred** to a separate, TA-Lib-golden-gated step — not in this program (new dep + third RSI/MACD impl; do not bundle into a data-source rewrite).

### S4 — `maps/` → typed `MapBundle`
- **Files:** new `maps/feed.py` (`build_map_bundle(view, …)` + `MapTrade`/`_to_map_trades`/`_ccxt_liq_to_event`/`_ingest_liq`); retype `maps/engine.py` (`MapBundle`/`MapFeatures` frozen `extra="forbid"`, delete `apply_map_bundle_to_row` + OI/liq caches); `maps/oi.py:48` add `sumOpenInterest` key; `maps/orderbook.py:11` import swap to `toolkit/book_math`.
- **Pattern:** **map builder bodies change zero lines below their signatures** — only inputs re-source to the view. The ~40 `map_*`/`liq_*` keys become declared `MapFeatures` fields; a formatter reading a non-existent one is a mypy error.
- **Verify:** `phantom-key-scan`; `MapBundle` parity on a fixture; liq notional correct cross-venue (`qty = contracts × contractSize`).
- **Risk:** liq window is now REST-polled (300s deque, in-memory, re-warms on restart) not WS-event-pushed — bounded eviction loss under a cascade; acceptable (liquidations are low-trust; heatmap degrades to synthetic). Cross-venue book/VP merge has **no engine source** → `cross_walls=None`, `cross_vp=None`, single-venue Binance; restoring it is a separate engine increment.

### S5 — track seam typing (additive)
- **Files:** `track/tracker.py` — introduce `SignalOpenRequest` (typed emission DTO: entry band, stop, tp ladder, rr, direction, phase, tier, message_id). **Both** the legacy dict path and future typed paths construct it at the call site; `register_signal_open` accepts `req`.
- **Pattern:** types the shared seam **without** sharing a detection model — `PrizrakSetup`/`ManipulationSetup` never reference each other, only this common emission contract (which is what the `setup` dict already is).
- **Verify:** `pytest tests/test_module_boundary.py`; legacy caller still green.
- **Risk:** low — additive; keep `register_signal_open` tolerant of the legacy dict during coexistence.

### S6 — Engine construction alongside legacy plane (coexistence opens)
- **Files:** `runtime/cycle/_cycle_loop.py` — add `rt = await build_market_runtime(settings, cli_symbols)` (keep the 3× startup-retry verbatim) **while the legacy `create_hunt_market_plane` still constructs and drives `snapshot_symbol`.**
- **Pattern:** for one-to-two sessions the engine (MultiEngine+SpotEngine) and the legacy plane run **concurrently**; migrated modules read `rt`, legacy modules read the plane. This is the explicit, bounded coexistence cost (doubled transport). **No `to_legacy_row()` bridge** — the seam is two timers, not a translated row.
- **Verify:** `--once` smoke boots both; engine warms the tracked set (`plane_ages` populated); legacy path unaffected.
- **Risk:** doubled REST/WS weight for the window — monitor for 418s; keep it short (proceed straight to S7/S8).

### S7 — SCANNER (MANIPULATIONS) cutover → REST tail  ⟵ **row-dict dies for Module 2**
- **Files:** new `scanner/universe.py` (`fetch_universe_rows`→`TickerRow`), `scanner/fetch.py` (`fetch_scanner_ohlcv`→`ScannerInput`, `funding_ctx`→`FundingCtx`, **scanner-local interval-aware TTL cache**); `scanner/prescan.py` scorers take `TickerRow`; `patterns.py` `ManipulationSetup`→frozen Pydantic (**detect logic untouched**); `manipulation_delivery.py` `client→exchange`, delete `_fetch_symbol_data`/`_funding_ctx`; `_cycle_loop.py:364` passes `rt.exchange`.
- **Pattern:** scanner is the **dynamic REST tail** — `rest.fetch_all_tickers`/`fetch_ohlcv_series`/`fetch_funding_history` against `engine.exchange`; never `snapshot()`/`MarketView`. Wide-stop/добор geometry stays scanner-local.
- **Verify:** **`/backtest-gate` BEFORE and AFTER — report the R delta; assert within noise** (longs are the edge +2.06R). `/phantom-key-scan` (confirms `TickerRow`/`FundingCtx` retired the dict), `/prohibited-api-scan`, `mypy`, `test_module_boundary.py`.
- **Risk (highest in program):** the deleted `fetch_ohlcv_list_cached` was **load-bearing against 418 bans**; `rest.fetch_ohlcv_series` is uncached. The scanner-local TTL memo must reproduce its cadence (1d ~hourly, 5m every cycle) or cycle latency balloons / detections shift. Also: engine's unconditional `bars[:-1]` vs the old conditional forming-bar drop — confirm backtest R is invariant to the one-bar boundary difference; if not, the idx fix belongs in the engine helper.

### S8 — PRIZRAK cutover → `assemble_prizrak`  ⟵ **row-dict dies for Module 1**
- **Files:** new `prizrak/assemble.py` (`assemble_prizrak(view, panel, maps)→PrizrakOutput`, ~150 lines; **orchestrator + all detectors untouched**), `prizrak/models.py` (`PrizrakOutput`/`PrizrakCandidate` frozen, validate the orchestrator's internal dicts at the seam). Rebind 18 consumers (`prizrak/{build,format_telegram,liq_reconcile}`, `deliver/{confluence_grid,templates}`, `runtime/{analyst_assembly,query_service,…}`, `signals/*`, `track/outcome_ledger`). Delete `adapter.py::row_ohlcv_by_tf` (dead) and `entry.py` seam.
- **Pattern:** data-source swap at the seam; geometry byte-identical by construction (signatures unchanged). `MarketView` carries **raw `list[Bar]`** for prizrak (`klines_raw(tf)`) — no Polars round-trip; the closed-only guarantee retires the `drop_unclosed_ohlcv_tail` crutch. Preserve the documented `htf_bias` dual-shape as two typed fields on two models. `confluence.py` stays **local and unchanged** (tier-sliced window — moving it to features changes the numbers = a strategy change, deferred).
- **Verify:** **prizrak has NO backtest — measure on LIVE data** (CLAUDE.md). Compare `PrizrakOutput.summary` vs current `row["prizrak_summary"]` across a session on the pinned universe before deleting the old seam. `/phantom-key-scan`; live-log shows `not_ready` planes, never a fabricated value. `no-lookahead-reviewer`.
- **Risk:** the ~28-key candidate dict is the real name-lie surface — `extra="forbid"` on `model_validate` surfaces every orphan key the orchestrator writes but no consumer reads.

### S9 — `track/`+`deliver/` rest-wiring + `SignalState`
- **Files:** `track/{tracker,_trailing,_evaluate_levels,path_backfill}.py`, `_cycle_reconcile.py`, `_cycle_tick.py` — `client.fetch_klines_between`→`rest.fetch_ohlcv_between`; delete `reconcile_active_from_ticker`+`ws_feed` (warm reconcile via `view.last_price`, orphans via REST); `SignalState` mutable Pydantic model; delete `market/live_price.py`+`apply_live_price_to_row` (→ `view/price.py`).
- **Pattern:** `track/` stays the shared post-emission spine, typed via `SignalOpenRequest`/`SignalState`, no strategy geometry imported. Two disjoint deliver lanes bind to their own setup models.
- **Verify:** signal lifecycle tests; confirm a warm-then-dropped symbol still reconciles via the orphan REST path (`ORPHAN_RECONCILE_MINUTES=2` unaffected).
- **Risk (documented strict-mode exception):** `SignalState` is JSON-persisted — `extra="ignore"` on the **load boundary** (forward-compat with old on-disk rows) with an explicit migration, `extra="forbid"` in-memory. Call it out, don't do it silently.

### S10 — Seam-swap: delete `snapshot_symbol` + legacy plane  ⟵ **row-dict fully dead; coexistence closes**
- **Files:** `_cycle_loop.py` — remove `create_hunt_market_plane` construction, `client/ws_feed/spot_companion` unpack, `ws_feed.set_symbols/start`, per-tick `set_symbols`, cross-exchange refresh, `TickBatchCache`, lake warmup, **all five HTF frame-cache persist sites** (the stale-HTF blackout is structurally gone — engine re-seeds every plane on `start()`); `_cycle_tick.py` main tick rebuilt around `rt.builder.build(sym)` only; `plane.aclose()`→`rt.aclose()`. Delete `runtime/tick_assembly.py::snapshot_symbol`.
- **Pattern:** single transport (engine) from here. Main tick drives only the warm/tracked set; dynamic funnel is scanner-owned REST.
- **Verify:** full `--once --no-telegram` smoke (**note the trap: this silences the whole manipulation lane — exercises PRIZRAK path only**; run a telegram-enabled live smoke for the scanner lane). `mypy`/`vulture`/`pytest` all green.
- **Risk (record in this ADR):** ban-telemetry / IP-ban-detection has no engine equivalent (ccxt's throttler is internal). Replaced by `plane_ages()`-based staleness diagnostics; blackout self-restart loses its `_is_ban` suppressor (forced `False` → never suppress restart on a phantom ban). A genuine, logged capability reduction, accepted under "warm set fixed + REST tail". The rotating-100 live WS universe becomes "warm set fixed + REST tail" — the one real behavioral change.

### S11 — Deletion
- **Files:** rm `market/{client,streams,spot,cross,factory,capacity,ccxt_rest,rate_limit,weight_registry,ccxt_guard,live_price}.py`; rm `data/{collect,frame_cache,completeness,lake_warmup}.py`; **`rm -rf runtime/engine_adapters/`** (zero in-tree importers — the deprecated adapters go last, with the transport they wrapped); rewrite `market/__init__.py` and `data/__init__.py` barrels. **Keep:** `market/{symbols,symbol_gate,tick_registry,network}.py`, `data/{lake,universe,jsonl_io,tick_jsonl,baseline_store,symbol_blacklist}.py`.
- **Pattern:** dangling-import gate before each delete — a delete is blocked while any transport symbol (`HuntCcxtClient`, `market.client`, `TickBatchCache`, `create_hunt_market_plane`, `engine_adapters`, …) resolves outside a file being deleted in the same commit.
- **Verify:** the grep gate (§S11 in runtime-delete blueprint) fully green for both lanes; `vulture --min-confidence 80` (orphan attributes); `scripts/check_prohibited_apis.py`; full `pytest`.
- **Risk:** a consumer missed in S6–S10 fails the gate — do not delete until green. Fail-open masks an empty universe, so treat a silently-empty tracked/watchlist set as a gate failure.

---

## 4. Kill-the-row-dict milestone & coexistence

The row-dict does not die in one commit — it dies **per module, at each cutover**, exploiting that the two strategies already share no dict:

- **S7 — Module 2 (scanner):** the ticker `dict[str,Any]` and delivery dicts are replaced by `TickerRow`/`ScannerInput`/`FundingCtx`/frozen `ManipulationSetup`. Scanner reads the REST tail, never `MarketView`.
- **S8 — Module 1 (prizrak):** the `prizrak_*` row keys and the ~28-key candidate dict are replaced by `PrizrakOutput`/`PrizrakCandidate`. Prizrak reads `MarketView`+`FeaturePanel`+`MapBundle`.
- **S10 — the last producer:** `snapshot_symbol` (the sole remaining row-dict factory) is deleted; the dict is now fully dead.

**Coexistence (S6–S9)** is at the orchestrator, never at the row: two independent timers (`manipulation_task` vs `deep_task`, already separate), the engine and the legacy plane running concurrently for one-to-two sessions, migrated module on `MarketView` / legacy module on `snapshot_symbol`. **No `MarketView.to_legacy_row()` bridge** — that would re-import the engine-in-old-clothes anti-pattern at the view layer. Because the two paths share no dict, `ruff`+`mypy`+`pytest` stay green at every intermediate commit; the module boundary test holds by construction throughout. Recommended cut order: **scanner first** (its old path — `HuntCcxtClient`+`fetch_ohlcv_list_cached` — is self-contained and cheapest to freeze), then prizrak.

---

## 5. Explicit callouts

1. **`/backtest-gate` is mandatory around S7** (scanner source change): run the touch-based outcome backtest **before and after**, report the R delta, assert within noise. The 6 `research/backtest_*.py` import `advance_manipulation_scales`, so a real number returns — an *identical* number means the emission path wasn't actually touched. Two regression traps to check: TTL-cache cadence vs `fetch_ohlcv_list_cached`, and the unconditional `bars[:-1]` boundary difference.
2. **Prizrak is measured on LIVE data, never backtest** (S8). No `research/backtest_*` imports `hunt_core/prizrak/`; a run would return an unchanged, meaningless number. Validate `PrizrakOutput.summary` against the live `row["prizrak_summary"]` across a session on the pinned universe before deleting the old seam.
3. **The deprecated `runtime/engine_adapters/` trio and the whole transport layer are deleted LAST (S11)**, behind a green dangling-import gate — never built upon, never revived, and removed only once both lanes resolve cleanly off the engine.

---

*Program spans ~11 stages / multi-session. Green (ruff · mypy · vulture · pytest) is a per-commit gate, not an end-state. I-5, I-6, and the prizrak↔scanner boundary hold throughout by construction — the seam-swap (S10) and deletion (S11) come only after both modules read the engine natively.*

---

# SPINE

I have the full grounding — the engine's native contract (`api.py`/`state.py`/`multi.py`/`rest.py`/`orderflow.py`/`spot.py`/`freshness.py`), the 850-line untyped `snapshot_symbol`, the current row-dict key surface, and the existing Pydantic config pattern. Here is the spine.

---

# HUNTER module-rewrite — the NATIVE data-flow SPINE

Grounding: engine facade `hunt_core/engine/api.py:169` (`Engine.snapshot`), `state.py:148` (`MarketSnapshot.require/optional/ready/not_ready`), `multi.py:114-206` (`MultiEngine.primary` + `cross_*`), pure helpers `orderflow.py:46/84`, `funding_stats.py`, `spot.py:148`, `freshness.py:13-21`, `rest.py`. The thing being replaced: `runtime/tick_assembly.py::snapshot_symbol` (`:256-1107`) — 851 lines that build one `dict[str, Any]` with ~200 keys, the direct host of the phantom-key / falsy-zero / name-lie defect family (see the four in-code post-mortems at `:170-206`, `:975-986`, `:1049-1056`).

The `runtime/engine_adapters/` trio and `docs/adr/0003-streams-wiring-spec.md` are the DEPRECATED path — I read the wiring spec **only** to harvest its datum→plane table (which plane backs `depth_imbalance`, `ws_cvd`, `liquidation_score_5m`, etc.). None of it is revived below.

---

## 1. Typed core vs row-dict — **firm recommendation: replace the row-dict with Pydantic v2 strict**

### 1.1 Decision

The untyped `dict[str, Any]` dies. The new modules consume a **frozen, strict, `extra='forbid'` Pydantic v2 `MarketView`** built from `engine.snapshot()`. **Not msgspec** — the tick cadence is ~100 symbols / 30 s ≈ 3 builds/s; Polars feature compute (`prepare_symbol`, `build_factor_panel`) dominates wall-time by 3-4 orders of magnitude, so Pydantic v2's Rust-core validation is free here. msgspec's only edge (µs decode) buys nothing and costs the validators/computed-fields that make the invariants enforceable. Reserve msgspec *only* if a future profiler flags validation as hot (it will not).

### 1.2 The elegant invariant mapping — **presence ⟺ proven-fresh**

The engine already discharges I-6 structurally: `snapshot().optional(name)` returns the value **iff** its plane is present and fresh, else `None` (`state.py:167-171`). So the `MarketView` carries the *result* of that check: **a field is non-`None` iff the engine proved it fresh at `now_ms`.** `None` in the view means exactly нет данных — no second meaning, no age to re-check downstream. This is why the view needs no `Fresh[T]` wrapper: freshness is already collapsed into presence.

### 1.3 Model hierarchy (`hunt_core/view/models.py`, new)

```python
class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True,
                              arbitrary_types_allowed=True)  # arbitrary: pl.DataFrame

class Klines(_View):                 # closed-only frames (I-5); None = plane not_ready
    m1:  pl.DataFrame | None = None
    m5:  pl.DataFrame | None = None
    m15: pl.DataFrame | None = None
    h1:  pl.DataFrame | None = None
    h4:  pl.DataFrame | None = None
    d1:  pl.DataFrame | None = None
    w1:  pl.DataFrame | None = None
    def require(self, tf: str) -> pl.DataFrame: ...   # raises NotReady, mirrors snapshot.require

class Book(_View):                   # from "book" read-through + toolkit/book_math (E5b)
    bids: tuple[tuple[float, float], ...] | None = None
    asks: tuple[tuple[float, float], ...] | None = None
    bid: float | None = None
    ask: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None

class Derivs(_View):                 # value-backed planes: mark/funding/oi/basis/taker/global_ls/top_ls
    mark: float | None = None
    index: float | None = None
    funding: float | None = None
    oi: float | None = None
    basis: float | None = None
    taker_5m: float | None = None
    global_ls_5m: float | None = None
    top_ls_acct_5m: float | None = None
    top_ls_pos_5m: float | None = None
    # derived (features/funding_stats over rest.fetch_funding_history), also fail-loud None:
    funding_zscore: float | None = None
    funding_trend: str | None = None

class Orderflow(_View):              # engine/orderflow.taker_flow + price_change_pct over "trades"
    cvd_1m: float | None = None      # taker_flow(...)["delta"] — USDT notional (unit change vs old ws_cvd)
    cvd_5m: float | None = None
    buy_ratio_30s: float | None = None
    buy_ratio_60s: float | None = None
    price_chg_1m: float | None = None
    price_chg_5m: float | None = None
    liq_long_5m: float | None = None # engine/liquidations.liquidation_notional over "liq" 300s window
    liq_short_5m: float | None = None
    liq_score_5m: float | None = None

class Cross(_View):                  # MultiEngine.cross_* — per-venue dict, each value fail-loud None
    funding: dict[str, float | None] = Field(default_factory=dict)
    open_interest: dict[str, float | None] = Field(default_factory=dict)
    long_short: dict[str, float | None] = Field(default_factory=dict)
    liq_notional: dict[str, dict[str, float] | None] = Field(default_factory=dict)

class Spot(_View):                   # SpotEngine.spot_enrichments(sym, futures_mid=)
    spread_bps: float | None = None
    quote_volume_24h: float | None = None
    lead_return_1m: float | None = None
    taker_delta_usd: float | None = None
    taker_buy_ratio: float | None = None

class MarketView(_View):
    symbol: str
    now_ms: int
    last_price: float                # the one always-present field (ticker plane require)
    price_source: str
    klines: Klines
    book: Book
    derivs: Derivs
    orderflow: Orderflow
    cross: Cross
    spot: Spot
    not_ready: tuple[str, ...] = ()  # carried from MarketSnapshot for diagnostics/gates
    plane_ages: Mapping[str, float] = Field(default_factory=dict)  # engine.plane_ages(sym), E7 diag
```

Design notes:
- **Raw planes only.** The `tf_snapshot` summaries (rsi14, candle patterns, pp_flags — `tick_assembly.py:598-681`), `structure`, `mtf`, `fib`, `factor_panel` are **derived** and do **not** belong in `MarketView`. They are outputs of the `features/` layer, produced *from* the view (§4). This severs the god-object: the view is the raw contract; each derived layer has its own typed output.
- **Klines stay Polars** inside a typed holder (`arbitrary_types_allowed`). Pydantic does not validate frame *contents* — the engine already guarantees closed-only (`freshness.closed_bars`, `rest.seed_ohlcv` drops forming bar) and non-empty (absent → `None`). The type wall is at the *field* level (`h4: pl.DataFrame | None`), which is what kills the phantom-key class.

### 1.4 How each signature defect becomes a static / construction error

| Defect (today, on the dict) | On `MarketView` |
|---|---|
| **Phantom key** — `market.get("quote_volume_24h")` read, never written (`tick_assembly.py:986` post-mortem) | `view.derivs.quote_volume_24h` → **mypy: "Derivs has no attribute quote_volume_24h"**. No `.get()` exists on a frozen model. |
| **Falsy-zero `or`-chain** — `float(market.get("oi_z") or 0) or None` turns a real `0.0` into `None` | `view.derivs.oi_z` is `float | None`; a genuine `0.0` is `0.0`. There is no dict-fallback to write, so the `or 0 or None` idiom never appears. |
| **Name-lie** — three different `htf_bias` vocabularies under one key (`:1049`) | The field name *is* the schema. A producer can't publish a fourth `htf_bias`; and prizrak/scanner each own their own typed setup model (§4), never a shared field. |
| **Orphan field** — written, never read | `extra="forbid"` rejects an unknown key at construction; a declared-but-unread field is caught by `vulture` on the attribute (already in pre-commit). |
| **Stale/fabricated** — `or 1.0` on zero confidence (I-6) | Impossible: presence ⟺ fresh (§1.2). Nothing writes a substitute; the builder either sets the real value or leaves `None`. |

### 1.5 Migration cost vs payoff — and coexistence

The row-dict has ~200 keys read across `features/`, `prizrak/`, `scanner/`, `deliver/`, `track/`. But this is a **from-scratch module rewrite**, not an in-place port — the modules are being rebuilt to the engine's native contract regardless. So the "migration cost" is not *translating* consumers; it is *writing the new consumers against `MarketView` instead of against a dict*. That cost is strictly lower (types guide the author; mypy finds every miss) and it is the whole point.

**Coexistence during the phased landing — at the orchestrator, never at the row.** The two strategies already share no dict (CLAUDE.md; `tests/test_module_boundary.py`). Exploit that: rewrite **one module at a time**; the not-yet-rewritten module keeps running on the **old** `snapshot_symbol` path unchanged, while the rewritten one runs the new `MarketView` path. The runtime carries two assembly functions side by side for one or two sessions. **No `MarketView.to_legacy_row()` bridge** — that would be the engine-wearing-old-clothes anti-pattern re-imported at the view layer. The seam is two independent timers (they already are: `manipulation_task` vs `deep_task`, `_cycle_loop.py:364/456`), not a shared translated row. Recommended order: PRIZRAK first (continuous, no backtest gate, measured live) or MANIPULATIONS first (backtest-gated, self-contained) — either works because they're disjoint; do the one whose old path is cheapest to freeze.

---

## 2. The native assembly — what replaces `snapshot_symbol`

New file `hunt_core/view/build.py`. A stateless builder holding the engine handles; one method per symbol.

```python
class MarketViewBuilder:
    def __init__(self, multi: MultiEngine, spot: SpotEngine) -> None:
        self._multi = multi
        self._eng = multi.primary          # single-venue planes (multi.py:114)
        self._spot = spot

    _REQUIRED_TRACKED = (                    # the plane list for a warm symbol
        "ticker", "book", "trades", "mark", "funding", "oi", "basis",
        "taker_5m", "global_ls_5m", "top_ls_acct_5m", "top_ls_pos_5m", "oi_hist_5m",
        "liq", "kline.1m", "kline.5m", "kline.15m", "kline.1h", "kline.4h", "kline.1d", "kline.1w",
    )

    def build(self, symbol: str) -> MarketView:
        snap = self._eng.snapshot(symbol, self._REQUIRED_TRACKED)   # api.py:169
        now = snap.now_ms
        last = _require_last_price(snap)     # ticker plane -> raises NotReady if truly absent
        trades = snap.optional("trades")     # list[ccxt trade] | None
        liq    = snap.optional("liq")        # list[ccxt liq]   | None (300s-windowed below)

        klines = Klines(
            m1=_frame(snap.optional("kline.1m")),  m5=_frame(snap.optional("kline.5m")),
            m15=_frame(snap.optional("kline.15m")), h1=_frame(snap.optional("kline.1h")),
            h4=_frame(snap.optional("kline.4h")),  d1=_frame(snap.optional("kline.1d")),
            w1=_frame(snap.optional("kline.1w")),
        )                                     # _frame: list[Bar] -> pl.DataFrame via toolkit/ohlcv
        book  = book_math.view_from_book(snap.optional("book"))          # toolkit/book_math (E5b)
        deriv = Derivs(
            mark=_f(snap.optional("mark")),  index=_index_of(snap),  funding=_f(snap.optional("funding")),
            oi=_f(snap.optional("oi")),      basis=_f(snap.optional("basis")),
            taker_5m=_f(snap.optional("taker_5m")),
            global_ls_5m=_f(snap.optional("global_ls_5m")),
            top_ls_acct_5m=_f(snap.optional("top_ls_acct_5m")),
            top_ls_pos_5m=_f(snap.optional("top_ls_pos_5m")),
            # funding_zscore/trend filled by features/funding_stats over rest.fetch_funding_history
        )
        of = Orderflow(
            cvd_1m=orderflow.taker_flow(trades, window_ms=60_000,  now_ms=now)["delta"],
            cvd_5m=orderflow.taker_flow(trades, window_ms=300_000, now_ms=now)["delta"],
            buy_ratio_30s=orderflow.taker_flow(trades, window_ms=30_000, now_ms=now)["buy_ratio"],
            buy_ratio_60s=orderflow.taker_flow(trades, window_ms=60_000, now_ms=now)["buy_ratio"],
            price_chg_1m=_pct(orderflow.price_change_pct(trades, window_ms=60_000,  now_ms=now)),
            price_chg_5m=_pct(orderflow.price_change_pct(trades, window_ms=300_000, now_ms=now)),
            **_liq_notional_5m(liq, now, self._eng.contract_size(symbol)),  # liquidations.liquidation_notional
        )
        cross = Cross(
            funding=self._multi.cross_funding(symbol),
            open_interest=self._multi.cross_open_interest(symbol),
            long_short=self._multi.cross_long_short(symbol),
            liq_notional=self._multi.cross_liquidation_notional(symbol),
        )
        spot = Spot(**self._spot.spot_enrichments(symbol, futures_mid=book.mid() or last))
        return MarketView(symbol=symbol, now_ms=now, last_price=last,
                          price_source="engine_snapshot", klines=klines, book=book,
                          derivs=deriv, orderflow=of, cross=cross, spot=spot,
                          not_ready=snap.not_ready, plane_ages=self._eng.plane_ages(symbol))
```

Every field traces to one engine call; nothing is fabricated; `None` propagates from `optional()`. There is **no** hot/carry/delta tier machinery, no `frame_cache`, no `_fetch_rest_pack`, no `_patch_market_live`, no `merge_ws_kline_closed` — the engine's push-state store *is* the hot path (WS merges into the seeded frame, `state.py:118`), so the 851-line tiered assembly collapses to the ~40 lines above. That deletion is the structural payoff.

### 2.1 Hybrid universe — tracked vs dynamic tail

- **Tracked** (warm WS set = PINNED ∪ cli, §3): `builder.build(sym)` reads `snapshot()` planes. This is the pinned/prizrak/analyst continuous path.
- **Dynamic scanner tail** (arbitrary perps, not warm): the manipulation scanner already fetches its own OHLCV via REST (CLAUDE.md). Its native form calls the **engine REST helpers directly** against `engine.exchange` (`api.py:202`, "on-demand REST of NON-tracked symbols"): `rest.fetch_ohlcv_series(engine.exchange, sym, tf, limit=)`, `rest.fetch_funding_history(...)`, `rest.fetch_all_tickers(engine.exchange)` for the universe funnel (E3). It does **not** go through `snapshot()` (no warm planes exist) and does **not** build a full `MarketView` — it builds its own lean scanner input (§4). This preserves the module boundary: the scanner never touches the pinned view path.

A tracked symbol whose planes are partially `not_ready` yields a `MarketView` with `None` fields + populated `not_ready` — the consumer gates on `view.not_ready` / specific `None`s, exactly as `snapshot().ready` intends, instead of a `kline_integrity_reject` early-return dict (`tick_assembly.py:413`).

---

## 3. Runtime construction / lifecycle

### 3.1 Engine construction in `_cycle_loop.run_loop`

Replace the plane-factory block (`_cycle_loop.py:313-353`) with direct engine construction. New file `hunt_core/view/runtime.py`:

```python
async def build_market_runtime(settings: HuntSettings, cli_symbols: Sequence[str]) -> MarketRuntime:
    warm = tuple(dict.fromkeys(s.upper() for s in (*PINNED_SYMBOLS, *cli_symbols)))  # PINNED ∪ cli
    multi = MultiEngine(warm, timeframes=settings.engine.timeframes)
    await multi.start()                          # api.py:98 seeds+WS+watchdog+positioning; multi cross loop
    spot = SpotEngine(_spot_symbols(warm))
    await spot.start()
    return MarketRuntime(multi=multi, spot=spot,
                         builder=MarketViewBuilder(multi, spot))

@dataclass(slots=True)
class MarketRuntime:
    multi: MultiEngine
    spot: SpotEngine
    builder: MarketViewBuilder
    async def aclose(self) -> None:
        await self.multi.close()                 # closes primary Engine + secondaries (multi.py:208)
        await self.spot.close()
        await asyncio.sleep(1.5)                  # drain aiohttp/ccxt.pro sessions
```

Call-site changes in `_cycle_loop.py`:
- `:317` `plane = await create_hunt_market_plane_from_settings(settings)` → `rt = await build_market_runtime(settings, cli_symbols)`. Keep the 3× retry wrapper (`:315-329`) verbatim.
- `:330-332` `client/ws_feed/spot_companion` unpacking → gone; downstream loops take `rt.builder` (and `rt.multi` for cross / `rt.multi.primary.exchange` for the scanner REST tail).
- `:341` `client.fetch_status()` health check → `await rt.multi.primary.exchange.fetch_status()` (public ccxt method, allowed) or drop; it was a startup log only.
- `:352-353` `ws_feed.set_symbols(...)` / `ws_feed.start()` → **deleted**. The engine's warm universe is fixed at construction (`Ingest.start` over a fixed list, `ingest.py:62`); there is no per-tick WS rotation. The dynamic tail is REST (§2.1). *(This is the one real behavioral change: the old rotating-100 live WS universe becomes "warm set fixed + REST tail". Acceptable — pinned/prizrak are warm; scanner is REST by design.)*
- `:798-810` per-tick `set_symbols(active, ...)` + `kline_ws_enabled` gating → **deleted**; the main tick just calls `rt.builder.build(sym)` for warm symbols.
- `:1153` `await plane.aclose()` → `await rt.aclose()`.

`MultiEngine.primary` (`multi.py:114`) already exists, so no engine change is needed for this.

### 3.2 Config via `pydantic-settings` — kills the "TOML edit silently no-ops" trap

Replace the hand-rolled TOML loader + fallback-wins `.get()` chains (the documented trap in CLAUDE.md / memory `config-file-map`) with `pydantic-settings.BaseSettings`, `extra="forbid"`:

```python
# hunt_core/domain/settings.py (new; supersedes the tomllib loader in domain/config.py)
class EngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timeframes: tuple[str, ...] = ("1m","5m","15m","1h","4h","1d","1w")
    secondaries: tuple[str, ...] = ("okx","bybit","bitget")

class HuntSettings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file=("config.defaults.toml", "config.toml"),   # defaults then overlay
        env_prefix="HUNT_", extra="forbid", frozen=True,      # <-- forbid = no silent no-op
    )
    engine: EngineSettings = EngineSettings()
    filters: FilterConfig = FilterConfig()
    # ... existing sub-models, all extra="forbid"
    @classmethod
    def settings_customise_sources(cls, *a, **k):  # add TomlConfigSettingsSource
        ...
```

`extra="forbid"` means a mistyped or moved TOML key (`[gate.foo]` a loader silently ignored) is now a **startup validation error**, not a no-op. Every documented key is a declared field or the process won't boot — the config-drift-auditor's whole job becomes a type error. (The current `_StrictModel` uses `extra="ignore"` at `config.py:31` — that is precisely the trap; flip it to `forbid` and route through pydantic-settings' TOML source instead of the bespoke `tomllib` reader.)

---

## 4. What each module cluster BECOMES (the contract each blueprint binds to)

- **features/** → pure `MarketView → FeaturePanel` (typed). Consumes `view.klines.*` frames + `view.derivs/orderflow`, runs the Polars indicator stack (`prepare_symbol`, `build_factor_panel`), and returns a **new frozen `FeaturePanel` model** (tf-summaries, regime, fib, mtf) — the derived layer the view deliberately excludes. `funding_stats` (E4) fills `Derivs.funding_zscore/trend` from `rest.fetch_funding_history`. No dict, no `.get()`.
- **maps/** → `MarketView + live buffers → MapBundle` (typed). `apply_map_bundle_to_row` becomes `build_map_bundle(view, book_math_walls, oi_bars) → MapBundle`; its inputs (`book`, `oi_bars`, liq buffers) come from `view.book` + `rest.fetch_futures_data_series` (OI-hist, E2) instead of the phantom-key-riddled `market.get("vol_24h_m")*1e6` chain (`tick_assembly.py:963-996`). Fail-loud fields become `Optional`.
- **prizrak/** (Module 1, levels/accumulation) → own native assembly `assemble_prizrak(view, feature_panel) → PrizrakSetup` (typed), continuous over the warm/pinned universe. Owns its `_htf_bias` vocabulary internally; reads `MarketView` + `FeaturePanel`, never the scanner's models. Measured on live data (no backtest).
- **scanner/** (Module 2, manipulations) → own native assembly over the **REST tail**: `rest.fetch_all_tickers` funnel (E3) → per-candidate `rest.fetch_ohlcv_series` + `rest.fetch_funding_history` → `advance_manipulation_scales → ManipulationSetup` (typed). Never touches `snapshot()` or `MarketView`; wide-stop geometry stays scanner-local. `/backtest-gate` still applies.
- **track/** → post-emission lifecycle over a typed `SignalState`; unchanged in role (SL/TP, trail, follow-up, ledger), but reads `view.last_price` / `rest.fetch_ohlcv_between` (E2) for path-backfill instead of `client.fetch_klines_between`. Serves both modules identically via the shared `register_signal_open` (`track/tracker.py:469`).
- **deliver/** → typed `PrizrakSetup | ManipulationSetup → str` formatters. Two separate formatters (already separate: `deliver/_sections.py` vs `deliver/manipulation_delivery.py`); each binds to its module's typed setup model, so a field a formatter reads is a field the setup declares — a name-lie in a Telegram card becomes a mypy error.

Shared plumbing stays exactly two seams (CLAUDE.md): `track/tracker.py::register_signal_open` and the `TelegramBroadcaster`. **No shared row, no shared view between the two modules** — prizrak reads `MarketView`; scanner reads its own REST-tail input; the boundary test holds by construction because there is no common dict to leak across.

---

### Spine summary (the one-paragraph contract)

`MultiEngine(PINNED∪cli).start()` + `SpotEngine.start()` are constructed once in `run_loop`; a stateless `MarketViewBuilder` turns `engine.snapshot(sym, required)` + the pure helpers into a **frozen strict `MarketView`** where *presence ⟺ engine-proven-fresh* and every absent datum is `None`; the tracked/pinned path builds `MarketView`s, the dynamic scanner tail hits `engine.exchange` REST helpers directly; each module cluster consumes typed models (`MarketView`/`FeaturePanel`/its own setup) and emits typed models, so the phantom-key / falsy-zero / name-lie / orphan-field family is a mypy or construction-time error rather than a silent runtime lie; `pydantic-settings` with `extra="forbid"` makes a stray TOML key a boot failure. The two strategies remain disjoint because there is no shared dict left to cross.

---

# PER-MODULE BLUEPRINTS

I now have complete grounding across both scanner data seams, the pure detect layer, the engine REST surface, and the live wiring. Here is the blueprint.

---

# NATIVE-REWRITE BLUEPRINT — MANIPULATIONS module (`hunt_core/scanner/` + `deliver/manipulation_delivery.py`)

Binds to the SPINE. The scanner is the **dynamic REST tail** (spine §2.1, §4): it never touches `snapshot()` / `MarketView`, never a warm plane — it hits `Engine.exchange` through the pure `rest.*` helpers. Two data seams change; the pure detect layer does not.

## 0. Ground truth — the two seams and the one purity fact

The scanner touches transport in exactly **two places**, both currently on the deprecated `HuntCcxtClient`:

| Seam | Today (delete) | Native replacement | Consumer |
|---|---|---|---|
| **A. Universe funnel** | `client.fetch_ticker_24h()` — `prescan.py:817`, `run_scan` (bulk `fetch_tickers`, weight 40) | `rest.fetch_all_tickers(exchange)` (`rest.py:139`) | `prescan_from_tickers` / `rank_hunt_candidates` → `data/paths.WATCHLIST` |
| **B. Per-candidate delivery** | `client.fetch_ohlcv_list_cached` + `client.fetch_funding_rate_history` — `manipulation_delivery.py:440,418` | `rest.fetch_ohlcv_series(exchange, sym, tf, limit=)` (`rest.py:63`) + `rest.fetch_funding_history(exchange, sym, limit=)` (`rest.py:101`) | `advance_manipulation_scales(ohlcv_by_tf, …)` → `_geometry` → Telegram |

**The load-bearing fact:** `scanner/detect/patterns.py::advance_manipulation_scales` (`:1063`) and everything under it (`_advance_pattern_a/b/c`, `events.py`, `scoring.py`) is **already pure** — it takes `ohlcv_by_tf: dict[str, list[list[float]]]` and returns `(states, ManipulationSetup | None)`. **No line of the detect layer changes.** Its geometry, gates, thresholds, wide-stop, добор ladder, TF ladders stay scanner-local (CLAUDE.md, `test_module_boundary.py`). The rewrite is confined to (1) the two fetch wrappers and (2) the untyped ticker `row: dict[str, Any]` that feeds the funnel scorers.

## 1. Seam A — universe funnel: `fetch_all_tickers` + a typed `TickerRow`

`rest.fetch_all_tickers(exchange)` returns raw ccxt unified tickers `{ccxt_symbol: ticker_dict}` — it does **not** do the USDT-M-swap filter, the binance-id mapping, or the `underlyingType` stamp the old `fetch_ticker_24h` (`client.py:594-621`) did inline. That normalization is **scanner-native universe logic**, so it moves into the scanner, not back onto a client.

New file `hunt_core/scanner/universe.py`:

```python
class TickerRow(BaseModel):
    """Normalized 24h ticker — the funnel's ONLY input row (replaces dict[str, Any])."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str                       # binance id, e.g. "BTCUSDT"
    last_price: float
    quote_volume: float
    price_change_percent: float | None = None
    trade_count: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    underlying_type: str = ""         # "COIN" | "EQUITY" | … ; "" = unknown (fail-open)

async def fetch_universe_rows(exchange: Any) -> list[TickerRow]:
    tickers = await rest.fetch_all_tickers(exchange)          # engine helper, ccxt-native
    markets = getattr(exchange, "markets", None) or {}
    rows: list[TickerRow] = []
    for ccxt_sym, t in tickers.items():
        mkt = markets.get(ccxt_sym)
        if not is_linear_usdt_swap_market(mkt):               # pure classifier (see §6)
            continue
        sym = try_binance_id_from_ccxt(ccxt_sym, exchange=exchange)
        last = _safe_float(t.get("last")); qv = _safe_float(t.get("quoteVolume"))
        if not sym or not last or last <= 0 or not qv or qv <= 0:
            continue
        rows.append(TickerRow(
            symbol=sym, last_price=last, quote_volume=qv,
            price_change_percent=_safe_float(t.get("percentage")),
            trade_count=_safe_float((t.get("info") or {}).get("count")),
            high_price=_safe_float(t.get("high")), low_price=_safe_float(t.get("low")),
            underlying_type=underlying_type_of(mkt),
        ))
    return rows
```

This is field-for-field the old `fetch_ticker_24h` row (verified against `client.py:605-621`), so the funnel's numbers are unchanged — but now typed. `run_scan` (`prescan.py:805`) loses its `HuntCcxtClient.from_settings` construction and `client.load_markets()`; it takes `exchange = rt.multi.primary.exchange` and calls `fetch_universe_rows(exchange)`.

## 2. Seam B — per-candidate delivery: `fetch_ohlcv_series` + `fetch_funding_history`

`_fetch_symbol_data` (`manipulation_delivery.py:428`) and `_funding_ctx` (`:410`) are the whole B seam. Native form, new file `hunt_core/scanner/fetch.py` (moves the fetch out of `deliver/`, which should only format):

```python
async def fetch_scanner_ohlcv(exchange, symbol, sem, *, now_ms) -> ScannerInput:
    async def _one(tf: str) -> tuple[str, list[Bar] | None]:
        bars = await ohlcv_cached(exchange, symbol, tf, limit=_LOOKBACK_BY_TF[tf])  # §3
        if not bars:
            return tf, None
        # I-5: engine's fetch_ohlcv_series ALREADY drops the forming bar (rest.py:79),
        # so the old manual bars[:-1] guard (manipulation_delivery.py:443) is DELETED.
        # Staleness is NOT proven by the REST tail (no plane bound) — keep the gate:
        if now_ms - int(bars[-1][0]) > _MAX_STALE_MS_BY_TF.get(tf, 3_600_000):
            return tf, None
        return tf, bars
    async with sem:
        pairs = await asyncio.gather(*[_one(tf) for tf in _TIMEFRAMES], return_exceptions=True)
        funding = await funding_ctx(exchange, symbol)                                 # below
    ohlcv_by_tf = {tf: b for r in pairs if not isinstance(r, BaseException)
                   for tf, b in [r] if isinstance(tf, str) and b}
    return ScannerInput(symbol=symbol, ohlcv_by_tf=ohlcv_by_tf, funding=funding)

async def funding_ctx(exchange, symbol) -> FundingCtx | None:
    hist = await funding_history_cached(exchange, symbol, limit=10)                   # §3
    rates = [_finite(r.get("fundingRate")) for r in hist]
    rates = [r for r in rates if r is not None]        # fail-loud: skip missing, no `or 0.0`
    return FundingCtx(rate=rates[-1], peak=max(rates)) if rates else None
```

Two invariants preserved from the engine surface:
- **I-5 (no lookahead)** is now enforced *by the engine helper* — `fetch_ohlcv_series` returns `bars[:-1]` (`rest.py:79`, closed-only). The old code's manual forming-bar drop (`:441-446`) becomes dead and is removed. Net: one class of off-by-one the scanner used to own itself moves under the engine's guarantee.
- **Staleness stays scanner-local.** The snapshot path proves freshness via plane bounds; the REST tail has no such proof, so `_MAX_STALE_MS_BY_TF` (`:92`) is a **keep** — a 4h frame whose newest closed bar is 10h old must still be dropped to `None` (this is exactly the `klines.4h.stale` failure family in memory). Do not delete it thinking the engine covers it — the engine only covers *tracked* planes.
- **Fail-loud funding.** The old `_funding_short_signal` reads `float(fctx.get("rate") or 0.0)` (`patterns.py:142`) — a falsy-zero chain. Because `funding_ctx` now only ever emits a `FundingCtx` when at least one finite rate exists, and `funding_stats._finite` is reused, a genuine `0.0` rate survives and a missing one yields `None` (no synthetic zero). Keep `_funding_short_signal`'s downstream math but let it read typed fields.

## 3. The non-obvious concern the spine forces you to solve: **caching**

`client.fetch_ohlcv_list_cached` carried an **interval-aware TTL cache** — the old comment (`manipulation_delivery.py:437-439`) is explicit: *"was the dominant REST sink → 418 ban… a 1d frame is refetched ~hourly, not every cycle."* `client.fetch_funding_rate_history` was *"Cached 900s"* (`:416`). The engine's `rest.fetch_ohlcv_series` / `fetch_funding_history` are **thin, uncached** ccxt calls (`rest.py:1-9`: three jobs, none is per-candidate delivery caching).

Do the math with the live wiring: `_manipulation_scan_loop` runs every **300 s** (`_cycle_loop.py:150`) over `watchlist ∪ cli` (tens of symbols) × **6 TFs** + 1 funding = ~7 REST calls/symbol/cycle. Un-cached, a 1d/1w frame is refetched every 5 min instead of ~hourly — the exact REST amplification the old cache existed to kill.

ccxt's built-in weighted throttler (`rest.py:6`) makes this **ban-safe** but not **cheap** — it serializes the excess weight into latency, stretching each cycle. So the blueprint adds a **scanner-local, interval-aware TTL cache** in `scanner/fetch.py` — it is scanner state, not engine state (the engine's cache *is* its WS plane store, which the tail bypasses by design):

```python
# (symbol, tf) -> (fetched_monotonic, bars).  TTL = min(interval, cap) so 1d refetches
# ~hourly, 5m every cycle. Rebuilds the old fetch_ohlcv_list_cached behavior, natively.
_OHLCV_TTL_S = {"1w": 3600, "1d": 3600, "4h": 1800, "1h": 900, "15m": 300, "5m": 60}
```

This is a real, named design decision, not a gloss: **the engine is feature-complete for the tracked path but deliberately does not cache the dynamic tail — the scanner must re-supply that cache itself, or regress cycle latency.** (Flag for the reviewer; it is the single most likely correctness/perf regression in this rewrite.)

## 4. Typed models — killing the scanner's phantom-key surface

The scanner already uses frozen Pydantic for its *value objects* (`PrescanHit`, `HuntCandidate`, `UniverseConfig` — `prescan.py:67,595,22`). The untyped surface that remains — and that the spine targets — is the ticker `row: dict[str, Any]` threaded through `apply_quality_gates` → `score_hunt_row` → `compute_expansion_readiness`. That dict is **the** phantom-key grave: the deleted P1.18/P1.19 scorers (`prescan.py:580-592`) read `qvol_baseline_60d` / `qvol_5m` / `qvol_1h` that **no producer ever wrote** — returned a constant since day one. `TickerRow` (§1) with `extra="forbid"` makes that impossible: a scorer reading `row.qvol_5m` is a mypy error, not a silent constant.

Migration of the funnel scorers is mechanical: change signatures from `row: dict[str, Any]` to `row: TickerRow`, replace every `row.get("last_price")` with `row.last_price`, delete the `_safe_float(row.get(a) or row.get(b))` dual-key fallbacks (the model already normalized `high_price`/`low_price`, so `_enrich_ticker_rows` at `prescan.py:793` and the `high_24h`/`highPrice` aliasing in `apply_quality_gates`/`_range_stats` all disappear). `_hunter_thresholds()` config reads stay as-is until the config phase (spine §3.2) lands.

Per-candidate B seam gets two more frozen models in `scanner/fetch.py`:
```python
class FundingCtx(_M):  rate: float; peak: float          # replaces {"rate":…, "peak":…} dict
class ScannerInput(_M): symbol: str; ohlcv_by_tf: dict[str, list[list[float]]]; funding: FundingCtx | None
```
`ohlcv_by_tf` stays a raw dict — it is the detect layer's pure input contract (`advance_manipulation_scales`), which is out of scope to retype and whose shape is pinned by the boundary test.

## 5. `ManipulationSetup` → frozen Pydantic

`ManipulationSetup` (`patterns.py:102`) is a `@dataclass`, violating the project rule ("Pydantic BaseModel for domain models — no dataclasses"). It is the setup object that crosses **scanner → `_geometry` → Telegram → `register_signal_open`** and is read via defensive `getattr(setup, "micro_confirmed", …)` (`manipulation_delivery.py:320,571,595,613`) — those `getattr` defaults are a name-lie hedge. It is never mutated in place (built once by `_build_setup`, `:426`). Convert to `class ManipulationSetup(BaseModel, frozen=True, extra="forbid")`; every `getattr(setup, x, default)` becomes `setup.x` (mypy-checked), and the delivery formatter reading a field the setup doesn't declare becomes a compile error (spine §1.4 name-lie row). This is the scanner's own typed setup model per spine §4 — **not** shared with prizrak, **not** `MarketView`.

## 6. Cross-cutting prerequisite: three pure classifiers must survive `market/` deletion

`fetch_universe_rows` needs `underlying_type_of`, `is_linear_usdt_swap_market`, `try_binance_id_from_ccxt` — all in `hunt_core/market/symbols.py`, which the cutover deletes. They are **pure market-metadata classifiers** over `exchange.markets` (no transport), so they must relocate to a keep-module before this lands. Recommend `hunt_core/engine/exchanges.py` (already the ccxt-market home) or a scanner-local copy if no other consumer survives. This is a hard dependency of Seam A; sequence it first. (The COIN-filter is not cosmetic — memory `binance-tokenized-equities-filter`: ~132 non-crypto perps must stay out of the pump/dump universe; unknown fails **open**.)

## 7. Runtime wiring — `client` → `exchange`

Both entry points lose the `HuntCcxtClient` and take the engine's raw ccxt.pro handle (spine §3.1: `rt.multi.primary.exchange`):
- `_manipulation_scan_loop(cli_symbols, client, …)` (`_cycle_loop.py:145`) → pass `exchange = rt.multi.primary.exchange`. Symbol resolution (`watchlist ∪ cli − pinned − blacklist`, `:177-181`) is unchanged — still the non-pinned universe (prizrak owns pinned).
- `deliver_manipulation_setups(symbols, client, broadcaster, …)` (`:469`) → `(symbols, exchange, broadcaster, …)`. Internally `_fetch_symbol_data(client, …)` → `fetch_scanner_ohlcv(exchange, …)`.
- `run_scan(client=…)` (`prescan.py:805`) → `run_scan(exchange=…)`, drop `HuntCcxtClient.from_settings` + `client.close()`.
- Delete the `from hunt_core.data.frame_cache import reset_frame_cache` and `TickBatchCache` couplings *for the scanner path* — the scanner never shared those; they belong to the tick/prizrak path.

## 8. Module-independence guardrails (must hold by construction)

- New files (`scanner/universe.py`, `scanner/fetch.py`) import **only** `hunt_core.engine.rest`, the relocated classifiers, and `pydantic` — **never** `hunt_core.prizrak.*`, **never** `MarketView`, **never** `market/` or `data/`.
- Scanner keeps its **own** funding read (`FundingCtx` = `{rate, peak}`), distinct from the engine's `funding_stats.{zscore,trend,recent_extreme}`. The engine module is market-independent and *could* serve both, but folding it in would import prizrak-shared semantics into the manipulation edge and change delivered signals. Keep separate to preserve exact behavior.
- `tests/test_module_boundary.py` must stay green: the scanner emits `ManipulationSetup`, shares only `register_signal_open` (`tracker.py:469`) and the `TelegramBroadcaster`. No shared dict, no `MarketView`, no `FeaturePanel` reaches the scanner.

## 9. `/backtest-gate` — mandatory, this is a scanner-source change

Seam B changes what feeds `advance_manipulation_scales` (caching TTLs, staleness, the removed forming-bar drop). Even though the intent is behavior-preserving, the skill's own rule applies: any change to what the SCANNER detects requires the touch-based outcome backtest **before and after**, reporting the R delta (memory `scanner-verify-match-author-not-profit`: longs are the edge at +2.06R; shorts don't win). Run `/backtest-gate` on the same dataset pre/post-rewrite and assert R is within noise. Two specific regression traps to check the backtest for:
1. The **caching TTL** must reproduce `fetch_ohlcv_list_cached`'s refetch cadence — a too-long 5m TTL would serve a stale forming region and shift detections.
2. The **engine's unconditional `bars[:-1]`** vs the old *conditional* forming-bar drop: the old code kept the last bar when it was already closed (`bars[-1][0] + interval_ms <= now_ms`); `fetch_ohlcv_series` always drops it. On a just-closed boundary this is a **one-bar-fresher-vs-one-bar-staler** difference (memory `closed-bar-convention-off-by-one`). Confirm the backtest R is invariant to it; if not, the engine helper — not the scanner — is where an idx fix belongs.

## 10. New file layout & migration order

```
hunt_core/scanner/universe.py   NEW  fetch_universe_rows + TickerRow (Seam A)
hunt_core/scanner/fetch.py      NEW  fetch_scanner_ohlcv + funding_ctx + TTL cache + FundingCtx/ScannerInput (Seam B)
hunt_core/scanner/prescan.py    EDIT scorers take TickerRow; drop _enrich/dual-key aliasing; run_scan(exchange=)
hunt_core/scanner/detect/patterns.py  EDIT ManipulationSetup → frozen Pydantic  (detect logic UNTOUCHED)
hunt_core/deliver/manipulation_delivery.py  EDIT delete _fetch_symbol_data/_funding_ctx; import from scanner/fetch; getattr→attr; signature client→exchange
hunt_core/engine/exchanges.py   EDIT (or new keep-module) relocate underlying_type_of / is_linear_usdt_swap_market / try_binance_id_from_ccxt
hunt_core/runtime/cycle/_cycle_loop.py  EDIT _manipulation_scan_loop passes rt.multi.primary.exchange
```

Order: **(6) relocate classifiers → (5) typed `ManipulationSetup` → (4/1) `TickerRow` + Seam A → (3/2) TTL cache + Seam B → (7) wiring → (9) `/backtest-gate` → phantom-key-scan.** Seam A and Seam B are independent (funnel vs delivery) and can land in separate commits. Because the scanner is disjoint from prizrak, it can be the first or second module cut over (spine §1.5) with prizrak still on the old `snapshot_symbol` path — freeze whichever old path is cheaper; the scanner's old path (`HuntCcxtClient` + `fetch_ohlcv_list_cached`) is self-contained and cheap to freeze, making the scanner a good **first** cutover.

## 11. Risks / decisions to surface to the human

1. **Scanner-local TTL cache (§3)** is the highest-risk item — it re-implements a deleted client behavior. If omitted, no ban (ccxt throttles) but cycle latency balloons; if TTLs are wrong, detections shift and `/backtest-gate` will show R drift.
2. **Classifier relocation (§6)** is a prerequisite shared with the consumer-migration task (#24); if `market/symbols.py` is deleted before these move, the universe empties silently (fail-open masks it).
3. **`run_scan` vs live loop** are two callers of Seam A; both must move to `exchange` together, or the watchlist producer and consumer diverge.
4. Pre-commit gates to run before merge: `/backtest-gate` (R delta), `/phantom-key-scan` (confirms `TickerRow`/`FundingCtx` retired the dict), `uv run mypy hunt_core`, `/prohibition` scan (all calls are `fetch_*` public — no private CCXT), `pytest tests/test_module_boundary.py`.

Key files: `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/scanner/prescan.py`, `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/scanner/detect/patterns.py`, `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/deliver/manipulation_delivery.py`, `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/engine/rest.py`, `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/engine/api.py`, `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/cycle/_cycle_loop.py`, `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/market/symbols.py`.

---

I now have the full grounding. Returning the blueprint as my response.

---

# Native-rewrite BLUEPRINT — `hunt_core/track/` + `hunt_core/deliver/`

Binds to the SPINE. Scope: the post-emission lifecycle (`track/`) and the two formatter lanes (`deliver/`). Grounded in the real engine contract (`api.py:169` `snapshot`, `state.py:161-171` `require/optional`, `multi.py:143-206` `cross_*`, `rest.py:82/101` `fetch_ohlcv_between`/`fetch_funding_history`, `spot.py:148/190` `spot_enrichments`/`weekly_ohlcv`, `orderflow.py:46`) and the real consumers I read end-to-end.

Two facts frame everything below:
- **`track/` is a shared seam** (`register_signal_open` is one of the two module-crossing points, CLAUDE.md). It must serve PRIZRAK and MANIPULATIONS identically without importing either's geometry.
- **`deliver/` is already two disjoint lanes** (`_sections.py`/`format_telegram.py` = PRIZRAK; `manipulation_delivery.py` = MANIPULATIONS). The rewrite keeps them disjoint — each binds to its own typed setup model, never a shared row.

---

## 1. Data-read inventory → native mapping (the core ask)

| # | Consumer (file:line) | Old data read | Native replacement | Crutch that collapses |
|---|---|---|---|---|
| 1 | `tracker.py:383,392` `reconcile_active_from_ticker` | `resolve_live_price(sym, ws_feed=ws_feed, fallback=px)` | **Delete the function + its `ws_feed` param.** Warm symbols get extremes from the main tick (`builder.build(sym).last_price`); non-warm active signals are covered by the REST orphan path (#4). | The "ticker safety net for rotated-out symbols" is moot — SPINE §3.1 fixes the warm universe (no per-tick rotation), so every warm symbol is built every tick. |
| 2 | `tracker.py:389-390` / `_cycle_tick.py:490-493` | `ticker_by_sym[sym]["last_price"]` → `price_map` for `auto_resolve_active_signals` | `price_map[sym] = MarketSnapshot.optional("ticker")["last"]` for warm; `rest.fetch_ohlcv_between(...)[-1][4]` for orphans. `auto_resolve` stays a **pure function over `price_map`** — no data access inside, no change to its logic. | — |
| 3 | `tracker.py:600` `_tick_feature_latch` | `feature_vector_from_row(row)` | `panel.feature_vector()` on the typed `FeaturePanel` produced by `features/`. | Reads derived features, not raw planes → belongs in `FeaturePanel`, never `MarketView`. |
| 4 | `_cycle_reconcile.py:48-57, 100-109` `_reconcile_inwatch_active` / `_reconcile_orphan_signals` | `safe_fetch(lambda: client.fetch_klines_between(sym,"5m",start_ms,end_ms))` → Polars `df["high"].max()` etc. | `rest.fetch_ohlcv_between(engine.exchange, sym, "5m", start_ms=, end_ms=)` → `hi=max(b[2])`, `lo=min(b[3])`, `last=bars[-1][4]`. | `safe_fetch` wrapper + `HuntCcxtClient` import drop — `fetch_ohlcv_between` is already fail-loud `[]` (`rest.py:95`) and **closed-only** (`rest.py:97-98`, I-5). |
| 5 | `path_backfill.py:144` `run_backfill_pass` | `client.fetch_ohlcv_list(sym,"1m",since=,limit=1500,qos_context=)` | `rest.fetch_ohlcv_between(engine.exchange, sym, "1m", start_ms=decision_ts, end_ms=decision_ts+h_max_ms)`. Param `client` → `engine`. | — |
| 6 | `path_backfill.py:158-162` | `drop_unclosed_ohlcv_tail(ohlcv,"1m",exchange=client.exchange)` + `[decision_ts ≤ b[0] ≤ …]` window filter | **Delete the tail-drop.** `fetch_ohlcv_between` returns only fully-closed bars in `[start,end]`; the whole "list path bypasses finalize, drop the forming candle" hazard (the module's documented known-bias risk) disappears. | The forming-bar/lookahead crutch is now the engine's guarantee, not a caller obligation. |
| 7 | `manipulation_delivery.py:440` `_fetch_symbol_data` | `client.fetch_ohlcv_list_cached(sym, tf, limit=_LOOKBACK_BY_TF[tf])` | `rest.fetch_ohlcv_series(engine.exchange, sym, tf, limit=)` — the scanner REST tail (non-tracked perps, never `snapshot()`). | — (see **GAP-A** on caching) |
| 8 | `manipulation_delivery.py:443-446, 100-110` | forming-candle drop via `_INTERVAL_MS` + `bars[:-1]` | **Delete.** `fetch_ohlcv_series` already drops the forming bar (`rest.py:79`). `_MAX_STALE_MS_BY_TF` staleness gate stays scanner-local (it's a detection-quality filter over REST bars, not a plane-freshness check). | Second copy of the forming-bar crutch collapses. |
| 9 | `manipulation_delivery.py:418` `_funding_ctx` | `client.fetch_funding_rate_history(sym, limit=10)` → `r.get("fundingRate")` | `rest.fetch_funding_history(engine.exchange, sym, limit=10)` — returns raw ccxt dicts, so `.get("fundingRate")` is unchanged. | — |
| 10 | `_sections.py` (many) `row["cross_microstructure"]`, `format_cross_exchange_section(cx)` (`:909-1034`) | funding_8h / oi_total / mark_price / long-short per venue from `client.fetch_cross_exchange_snapshot` | Typed `Cross` from `MultiEngine.cross_funding/cross_open_interest/cross_long_short/cross_liquidation_notional(sym)` (`multi.py:143-206`), each already `{venue: value|None}` fail-loud. | — |
| 11 | `_sections.py:405-660` liq/DOM/book-walls; `walls["depth_imbalance"]` (`:875`) | `row["book_walls"]`, `row["market"]["liq_heatmap_*"|"liq_cascade_risk"|"liq_venue_events"|"mark_price"]` | Typed `MapBundle` from `maps/` (SPINE §4), built from `view.book` + `toolkit/book_math` (E5b) + `MultiEngine.cross_liquidation_notional`. `mark_price` → `view.derivs.mark`. | The `market.get(...)` liq/DOM phantom-key surface becomes typed `Optional` fields. |
| 12 | `format_telegram.py:14` `analysis.row.get("price")` | row price | `report.price` on typed `AnalystReport` (from `view.last_price` / the `PrizrakSetup`). | — |
| 13 | `format_telegram.py:249-321` `_spot_context_text` | `market["spot_futures_spread_bps"/"spot_quote_volume_24h"/"spot_taker_delta_usd"/"spot_taker_buy_ratio"]` | `view.spot.spread_bps/quote_volume_24h/taker_delta_usd/taker_buy_ratio` — typed `Spot` from `SpotEngine.spot_enrichments(sym, futures_mid=)` (`spot.py:148`). Field names already match 1:1. | — |
| 14 | `format_telegram.py:267` `market["vol_24h_m"]` (futures 24h quote-vol, for spot/fut ratio) | phantom-key hotspot (`vol_24h_m`, `×1e6`) | `view.derivs.quote_volume_24h_fut` — **NEW typed field from the futures `ticker` plane** (`snapshot.optional("ticker")["quoteVolume"]`). | Kills the exact `market.get("vol_24h_m")*1e6` chain the SPINE names as a phantom-key defect. See **GAP-B**. |
| 15 | `format_telegram.py:295` `row["spot_weekly_ladder"]` | row dict | `PrizrakSetup.spot_weekly_ladder`, derived by prizrak from `SpotEngine.weekly_ohlcv(sym)` (`spot.py:190`, lazy/cached/closed-only). | — |
| 16 | `tracker.py`/`_trailing.py`/`_evaluate_levels.py` — the `active`/`setup`/`sig` dicts (~60 keys each) | untyped `dict[str,Any]` mutated across ticks + persisted to `signal_state.json` | Typed **`SignalState`** (mutable Pydantic v2 model) + typed **`SignalOpenRequest`** at the seam (§3). | The lifecycle's own phantom-key/name-lie surface (`delivery_tier="armed"` phantom-fill post-mortem lives here). |

Pure utilities kept **as-is** per the task: `market/tick_registry.py` (`quantize_conservative`, `quantize_price`), `market/symbols.py`. They survive the `market/` deletion unchanged — relocate the two files to `toolkit/` if `market/` is removed, no logic change; `tracker.py:27` and `_trailing.py:7` keep importing them.

---

## 2. What `market/live_price.py` becomes natively (explicit ask)

`live_price.py` is a **fallback-ladder oracle with a manual 5 s age-gate** (`resolve_live_price`, `:63-126`): fresh WS last → BBO mid → mark → book → stale fallback, each path gated by `_stamp_is_stale(ts, max_age_s)` (`:49`). Every piece of it is already discharged by the engine:

- **The age-gate is the plane's `PlaneStamp`.** `snapshot.optional(name)` returns the value **iff** the plane is present and within its own `bound_ms` (`state.py:167-171`), else `None`. So `max_age_s`, `_stamp_is_stale`, and the whole "EVERY live path is age-gated" comment (`:76-80`) are replaced by per-plane freshness. There is no `"stale_ticker"`/`"missing"` source string — **presence ⟺ fresh** (SPINE §1.2); an absent price is `None`, fail-loud.
- **The fallback ladder becomes a pure read over one snapshot.** New `hunt_core/view/price.py`:

  ```python
  @dataclass(frozen=True, slots=True)
  class PriceQuote:
      price: float
      source: str        # "ticker" | "bbo_mid" | "book_mid" | "mark"
      now_ms: int

  def resolve_price(snap: MarketSnapshot) -> PriceQuote | None:
      tk = snap.optional("ticker")
      if isinstance(tk, dict) and (last := _pos_f(tk.get("last"))):
          return PriceQuote(last, "ticker", snap.now_ms)
      if (mid := _mid(snap.optional("bbo"))):   return PriceQuote(mid, "bbo_mid", snap.now_ms)
      if (mid := _mid(snap.optional("book"))):  return PriceQuote(mid, "book_mid", snap.now_ms)
      if (mk := _pos_f(snap.optional("mark"))): return PriceQuote(mk, "mark", snap.now_ms)
      return None   # нет свежей цены — fail-loud, no fabricated fallback
  ```

  No `ws_feed`, no `book` dict, no `fallback` float, no `max_age_s`. It takes a freshness-proven snapshot and returns a typed quote or `None`.
- **`MarketView.last_price` is this oracle's result at build time.** `build.py::_require_last_price(snap)` = `resolve_price(snap)`, raising `NotReady` if `None`; `price_source` on the view carries `PriceQuote.source` (the SPINE's placeholder `"engine_snapshot"` is replaced by the real source). Warm-symbol reconcile (#1) reads `view.last_price` directly — the oracle call is subsumed.
- **`apply_live_price_to_row` (`:129-166`) is deleted outright.** It mutated `row["price"]`, `row["price_source"]`, `row["price_stale"]`, `row["price_stale_delta_pct"]` — there is no row, and those are the name-lie/orphan-field surface. The builder sets `last_price`/`price_source` once at construction.
- **`resolve_price_quote` / `PriceQuote(stale=…)` → the typed `resolve_price` above.** The Telegram freshness footer that consumed `source`/`stale` now reads `PriceQuote.source` + `snap.not_ready`/`engine.plane_ages(sym)` (E7 diagnostics) for age, instead of a self-computed staleness bool.

Net: `market/live_price.py` (175 lines, on the delete list) → ~30-line pure `view/price.py` with **zero** `os.getenv`, zero fallback substitution, zero row mutation.

---

## 3. Typed models introduced in `track/`

The lifecycle mutates two untyped dicts today; both become typed. This is where the tracker's own phantom-key defects live (the `armed`-tier phantom-fill and the `delivery_tier` name-lies in memory).

**`SignalOpenRequest`** — the typed emission DTO at the shared seam. Both producers construct it from their own disjoint setup model; it carries only the geometry `register_signal_open` needs (entry band, stop, tp1/2/3, rr, direction, phase, tier, message_id, `entry_lifecycle_*`). This types the seam **without** sharing a detection model — the boundary test (`tests/test_module_boundary.py`) still holds because `PrizrakSetup` and `ManipulationSetup` never reference each other, only this common emission contract (which is what the dict `setup` argument already is today). `register_signal_open(state, *, req: SignalOpenRequest, features_open: FeatureVector|None, book_walls: BookWalls|None, now, entry_message_id)`.

**`SignalState`** — a mutable Pydantic v2 model for the persisted per-signal dict (status, phase, entry_lo/hi, stop_loss, original_stop_loss, tp1/2/3, extreme_hi/lo, trailing_active, tp1_managed, partial_fixed_pct, delivered_levels_snapshot, …). `_trailing.py`, `_evaluate_levels.py`, `close_signal` read/mutate typed attributes instead of `.get()`/`or 0` chains. **Persistence tension (GAP-C):** the state is JSON-persisted and load-migrated by `_backfill_signal_geometry` (`tracker.py:301`). Use `extra="ignore"` **only** on the load boundary (forward-compat with old on-disk rows) with an explicit migration step; keep `extra="forbid"` on the in-memory model so a typo in new code is a construction error. This is the one place the "strict everywhere" rule bends, for a documented reason (disk compat), and it must be called out rather than silent.

`_trailing.py`/`_evaluate_levels.py` management functions change signature from `row: dict|None` to `panel: FeaturePanel|None`; `_closed_atr1h_pct` → `panel.tf("1h").atr_pct`, `_squeeze_on_1h` → `panel.tf("1h").squeeze_on` (both derived, fail-loud `None`).

---

## 4. Cycle wiring changes

- `_cycle_loop.py:186` `deliver_manipulation_setups(symbols, client, broadcaster, tracker_state=)` → `(symbols, rt.multi.primary, broadcaster, tracker_state=)` (scanner hits `engine.exchange` REST).
- `_cycle_loop.py:464-468` `path_backfill_loop(client, …)` → `path_backfill_loop(rt.multi.primary, …)`.
- `_cycle_reconcile.py:9,48,100` — drop `HuntCcxtClient`/`safe_fetch`/`data.collect`; pass `engine` and call `rest.fetch_ohlcv_between` directly (fail-loud, no wrapper).
- `_cycle_tick.py:441-455` — delete the `reconcile_active_from_ticker` block and the `ws_feed` argument; warm actives reconcile in the main per-symbol loop, non-warm via `_reconcile_orphan_signals` (still REST). `price_map` (`:490`) built from warm `snapshot("ticker")` + orphan REST.

---

## 5. Deletions the rewrite banks

- `market/live_price.py` (§2), `reconcile_active_from_ticker` + `ws_feed` plumbing (#1), `drop_unclosed_ohlcv_tail` calls (#6, #8), the two forming-bar drop blocks (#8), `safe_fetch`/`HuntCcxtClient` in reconcile (#4), `apply_live_price_to_row`.
- The `market.get("vol_24h_m")*1e6` / `spot_*` / `liq_*` / `cross_microstructure` `.get()` chains in the two formatters become typed reads on `Spot`/`Cross`/`MapBundle`/`Derivs`.

---

## 6. Gaps / risks to flag (honest)

- **GAP-A — scanner OHLCV cache.** `fetch_ohlcv_list_cached` had an interval-aware TTL cache that was **load-bearing against 418 bans** (`manipulation_delivery.py:439` names it "the dominant REST sink → 418 ban"). `rest.fetch_ohlcv_series` is **uncached**. The scanner re-runs every ~300 s over the whole non-tracked tail. Do **not** drop the cache silently: either keep a thin scanner-local per-`(sym,tf)` TTL memo over `fetch_ohlcv_series` (1d refetched ~hourly, only 5m fast — mirror the old TTL), or add a cached variant to `engine/rest.py`. Recommend scanner-local memo (keeps the engine's `rest` helpers pure/stateless). This must be decided before cutover, not after the first ban.
- **GAP-B — futures 24h quote-volume has no typed home yet.** `vol_24h_m` (used by the spot/fut ratio, #14, and likely other consumers) must land on `Derivs` (from the `ticker` plane's `quoteVolume`) or the scanner's `fetch_all_tickers` funnel. `MarketView` as specced in the SPINE carries spot volume (`Spot.quote_volume_24h`) but not the futures side — add `Derivs.quote_volume_24h_fut`.
- **GAP-C — `SignalState` persistence** (§3): the `extra="ignore"`-on-load / `extra="forbid"`-in-memory split, plus the `_backfill_signal_geometry` migration, is the one strict-mode exception; make it explicit.
- **Behavioral change (accepted):** deleting `reconcile_active_from_ticker` trades the old rotating-universe safety net for "warm set fixed + REST orphan tail" — consistent with SPINE §3.1. Verify on live data that a signal opened on a warm symbol which later drops out of the warm set still reconciles via the REST orphan path (it does today; confirm the orphan anchor timing `ORPHAN_RECONCILE_MINUTES=2` is unaffected).

## 7. Module-boundary preservation

`track/` stays the shared post-emission spine, typed via `SignalOpenRequest`/`SignalState` — no PRIZRAK or MANIPULATIONS geometry imported. `deliver/` keeps two disjoint formatters: `manipulation_delivery.py` consumes `ManipulationSetup` + the REST tail and owns its wide-stop/добор geometry; `_sections.py`/`format_telegram.py` consume `AnalystReport`/`PrizrakSetup` + `MapBundle`/`Spot`/`Cross`. No shared row, no shared detection model → `tests/test_module_boundary.py` holds by construction. `/backtest-gate` still applies to the manipulation lane only.

**Relevant files:** `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/track/tracker.py`, `_trailing.py`, `path_backfill.py`, `_evaluate_levels.py`; `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/deliver/_sections.py`, `manipulation_delivery.py`, `prizrak/format_telegram.py`; delete `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/market/live_price.py` → new `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/view/price.py`; cycle wiring `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/cycle/_cycle_reconcile.py`, `_cycle_tick.py`, `_cycle_loop.py`; engine contract consumed `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/engine/{rest,multi,spot,state}.py`.

---

I have complete grounding across all three maps, the engine's native read-through surface, and the current tick feed. Here is the blueprint.

---

# Native-rewrite BLUEPRINT — `hunt_core/maps/`

Binds to the SPINE §4 (`maps/` → `MarketView + live buffers → MapBundle`, typed, fail-loud Optional). Three maps, one feeder, exact tuple shapes below.

## 0. What the three maps consume today (grounded)

The maps are **already pure pass-in functions** — none of them import ccxt or `market/client` transport (except the book-math helpers, addressed in §5). The god-object coupling is entirely in the **feed**, not the maps. Current feed: `tick_assembly.py:963-997` → `build_map_bundle(...)`; probe feed: `symbol_probe.py:317`. Liq events are pushed by a WS callback `market/streams.py:995` → `store.record_liquidation`.

| Map | builder | raw inputs it needs | current source |
|---|---|---|---|
| Orderbook (`orderbook.py:615`) | `build_orderbook_map` | `bids/asks` `list[(px,qty)]`, `deep_bids/deep_asks`, `cross_walls` dict, **`trades`** (objects), `book_history` deque, `daily_volume`, `price_change_pct` | WS live_book, deep cross-microstructure REST, `ws_feed.trade_buffer`, `store.book_history`, `vol_24h_m×1e6` |
| Liquidation (`liquidation.py:755`) | `build_liquidation_map` | **`buffers: dict[venue, deque[LiqEvent]]`**, `oi_bars: list[dict]`, `bracket_tiers`, scalars (`global_ls_ratio, oi_usd, funding_rate, top_ls_ratio, basis_pct`) | `ws_feed.liquidation_buffers()` (WS-push deques), `client.fetch_oi_bars_for_maps`, `client.get_cached_leverage_tiers` |
| Volume-profile (`volume_profile.py:271`) | `build_volume_profile_map` | `frames: dict[tf, pl.DataFrame]`, `cross_vp` dict | `prepared.work_1h/4h/15m/1d/1w`, cross-microstructure |

## 1. The datum → engine-plane mapping (read-through table)

| Map input | Engine-native source | shape / note |
|---|---|---|
| `bids` / `asks` | `view.book.bids` / `view.book.asks` (← `snapshot.optional("book")`, `_book_snapshot` at `api.py:53`, top `ORDER_BOOK_LIMIT=1000`) | `tuple[(px,qty),…]` → `list`. |
| `deep_bids` / `deep_asks` | **same** `view.book.bids/asks` | `ORDER_BOOK_LIMIT=1000` is already deep — the separate cross-microstructure deep-book REST fetch (`tick_assembly.py:951-959`) is **deleted**. The read-through carries the full depth. |
| `trades` (footprint/iceberg/CVD) | `snapshot.optional("trades")` (← `exchange.trades[symbol]`, `api.py:73`, `TRADES_LIMIT=1000`) | ccxt trade **dicts** → normalized `MapTrade` tuple (§4.1). This is a **raw live buffer**, NOT the `view.orderflow` scalars — the footprint needs per-trade price/qty/side, which `Orderflow` collapses away. |
| `daily_volume` | `snapshot.optional("ticker")["quoteVolume"]` (ticker plane stores the full ccxt ticker dict, `ingest.py:230-236`) | float, raw quote-USDT. **This is the correct home for the `quote_volume_24h` phantom key** the post-mortem at `tick_assembly.py:981-986` describes — source it from the ticker read-through, drop the `vol_24h_m×1e6` indirection. |
| `price_change_pct` | `view.orderflow.price_chg_1m` (← `orderflow.price_change_pct`, `orderflow.py:84`) | float fraction; multiply ×100 if the map wants pct (it compares `abs(...)<=0.12`, i.e. pct — keep the ×100). |
| **liq `buffers`** | `multi.cross_liquidations(symbol)` (`multi.py:160`) → per-venue ccxt liq **dicts** → `LiqEvent` tuple (§4.2), accumulated into map-owned deques | Binance = primary WS `!forceOrder` read-through; OKX/Bybit currently `None` (unwired). |
| liq notional correctness | `multi.primary.contract_size(sym)` / `market_contract_size(secondary_ex, sym)` (`liquidations.py:29`) | folded into the `LiqEvent.qty` (§4.2) so `qty×price` is correct cross-venue. |
| `oi_bars` | `rest.poll_futures_data(ex,"fapiDataGetOpenInterestHist",{symbol,period:"1h",limit:48})` → `oi_bars_from_frames(rows, view.klines.h1)` (`maps/oi.py:34`) | needs `sumOpenInterest` key added to oi.py fallback (§6). |
| `frames` (VP) | `view.klines.{m15,h1,h4,d1,w1}` (`snapshot.optional("kline.<tf>")`, closed-only, `api.py:69`) | `pl.DataFrame` holders straight off the view. |
| `cross_walls` / `cross_vp` | **`None`** for now | cross-venue **book/VP merge is NOT in the engine contract** — `MultiEngine` exposes cross funding/oi/lsr/liq only (`multi.py:143-206`), no cross orderbook. Single-venue Binance book → `venues=["binance"], source="ws"`. Flagged as scope in §9. |
| `bracket_tiers` | **`None`** → `_DEFAULT_LEVERAGE_TIERS=(10,25,50,100)` | leverage brackets are effectively private on Binance (`fapiPrivateGetLeverageBracket`); the default ladder is already built and labelled synthetic. §9. |
| `extra`: `funding_rate` / `basis_pct` / `ws_cvd` / `oi_z` | `view.derivs.funding` / `view.derivs.basis` / `view.orderflow.cvd_5m` / FeaturePanel `oi_z` | `oi_z` is a **derived** feature (features layer), threaded in as a scalar arg — not a `MarketView` field. |

## 2. New feeder module `hunt_core/maps/feed.py`

Replaces the 35-line feed block at `tick_assembly.py:917-997`. Stateless except for the map-owned `MapTimeSeriesStore` (§7). One entry point, retyped to the view:

```python
def build_map_bundle(
    view: MarketView,
    *,
    trades: list[dict[str, Any]] | None,              # snap.optional("trades") — ccxt trade dicts
    cross_liq: dict[str, list[dict[str, Any]] | None], # multi.cross_liquidations(sym)
    contract_sizes: dict[str, float | None],           # {venue: contractSize} for liq notional
    oi_bars: list[dict[str, Any]] | None,              # oi_bars_from_frames(OI-hist rows, view.klines.h1)
    oi_z: float | None = None,                         # from FeaturePanel (derived)
    store: MapTimeSeriesStore,
    cfg: MapsConfig | None = None,
) -> MapBundle | None:
    price = view.last_price
    if not cfg.enabled or price <= 0:
        return None
    store.touch_symbol(view.symbol)

    # --- book: read-through carries full depth; no deep-book REST ---
    bids = [t for t in (view.book.bids or ())]        # list[(px,qty)]
    asks = [t for t in (view.book.asks or ())]
    if bids or asks:
        store.sample_book(view.symbol, _book_history_snap(bids, asks, cfg.book_deep_top_n))

    # --- trades: ccxt dicts -> MapTrade tuples (§4.1) ---
    map_trades = _to_map_trades(trades)               # list[MapTrade]

    # --- liq: ccxt dicts -> LiqEvent, accumulate into map-owned per-venue deques (§4.2) ---
    _ingest_liq(store, view.symbol, cross_liq, contract_sizes)
    buffers = store.liq_buffers(view.symbol)

    orderbook = build_orderbook_map(
        symbol=view.symbol, current_price=price,
        bids=bids or None, asks=asks or None,
        cross_walls=None, trades=map_trades, daily_volume=view.derivs.quote_volume_24h or 0.0,
        book_history=store.book_history(view.symbol),
        price_change_pct=(view.orderflow.price_chg_1m * 100.0) if view.orderflow.price_chg_1m is not None else None,
        deep_bids=bids or None, deep_asks=asks or None, cfg=cfg,
    )
    liq_map = build_liquidation_map(
        buffers, symbol=view.symbol, current_price=price, cfg=cfg,
        bracket_tiers=None, oi_bars=oi_bars,
        global_ls_ratio=view.derivs.global_ls_5m, oi_usd=view.derivs.oi,
        funding_rate=view.derivs.funding, top_ls_ratio=view.derivs.top_ls_acct_5m,
        basis_pct=view.derivs.basis,
    )   # + calibration_confidence overlay, verbatim from engine.py:320-336
    vp_map = build_volume_profile_map(
        symbol=view.symbol, current_price=price,
        frames=_view_frames(view.klines), cross_vp=None, cfg=cfg,
    )
    ...  # assemble typed MapBundle (§8)
```

**The three map builders are untouched below their signatures** — `build_orderbook_map`, `build_liquidation_map`, `build_volume_profile_map` keep their exact bodies. Only their *inputs* re-source. This is why the maps are the cheapest cluster to migrate: they were already written against pass-in structures.

## 3. `apply_map_bundle_to_row` → typed output

Dies. `apply_map_bundle_to_row` (`engine.py:468`) mutated a `dict[str,Any]` row (`market.update(features)`, `row["maps"]=...`, `row["book_walls"]=...`). In the native world the `MapBundle` **is** the typed product; `derive_map_features` (`engine.py:383`) becomes a method returning a frozen `MapFeatures` model (§8), and prizrak/scanner read `bundle.orderbook`/`.liquidation`/`.volume_profile` and `MapFeatures` fields by attribute — no `market.get("map_*")` string reads. The ~40 `map_*` / `liq_*` keys currently sprayed into `market` (`engine.py:395-464`) become declared fields; a consumer reading a non-existent one is a **mypy error**, not a phantom key.

## 4. The exact tuple shapes (core deliverable)

### 4.1 Trade — ccxt read-through → `MapTrade`

The map footprint/iceberg/CVD already read trades via `getattr(pt,"ts_ms"/"price"/"qty"/"is_buy")` (`orderbook.py:92-97, 334-337, 513-522`). A `NamedTuple` (not Pydantic — this is a hot inner buffer of ≤1000 trades/symbol/tick; SPINE §1.1 reserves lean structs for the hot path) satisfies that attribute access verbatim, so **those three functions need zero changes**:

```python
class MapTrade(NamedTuple):
    ts_ms: int      # int(ccxt_trade["timestamp"])
    price: float    # float(ccxt_trade["price"])
    qty: float      # float(ccxt_trade["amount"])   -- BASE units; footprint does qty*price for notional
    is_buy: bool    # ccxt_trade["side"] == "buy"

def _to_map_trades(trades: list[dict] | None) -> list[MapTrade]:
    out: list[MapTrade] = []
    for tr in trades or []:
        ts, px, amt, side = tr.get("timestamp"), tr.get("price"), tr.get("amount"), tr.get("side")
        if ts is None or px is None or amt is None or side not in ("buy", "sell"):
            continue                       # fail-loud skip (I-6) — never a fabricated 0/0.5
        out.append(MapTrade(int(ts), float(px), float(amt), side == "buy"))
    return out
```

Field-by-field: ccxt unified trade `{timestamp, price, amount, cost, side}` (per `orderflow.py:5-6`) → `ts_ms←timestamp`, `price←price`, `qty←amount`, `is_buy←(side=="buy")`. This **retires the old fragile positional form** (`pt[0]=ts, pt[1]=qty, pt[3]=side_str, pt[4]=price` at `orderbook.py:92-99`) — the ccxt→NamedTuple path is the single canonical shape.

### 4.2 Liquidation — ccxt read-through → `LiqEvent`

Preserve the map's canonical `LiqEvent = tuple[int, str, str, float, float]` (`liquidation.py:30`) = `(ts_ms, symbol_UPPER, side_UPPER, qty, price)`, with **one documented semantic refinement**: `qty` becomes base-units (`contracts × contractSize`) so the map's `qty×price` notional (`_bucket_events`, `liquidation.py:408`) is correct on non-`contractSize==1` venues (OKX). This folds the engine's `liquidation_notional` correctness (`liquidations.py:43-54`) into the tuple without touching `_bucket_events`.

```python
def _ccxt_liq_to_event(ev: dict, *, symbol: str, contract_size: float | None) -> LiqEvent | None:
    ts, side, contracts, price = ev.get("timestamp"), ev.get("side"), ev.get("contracts"), ev.get("price")
    if ts is None or side not in ("buy", "sell") or contracts is None or price is None:
        return None                        # fail-loud skip (matches liquidations._event_notional)
    cs = ev.get("contractSize")
    cs = float(cs) if cs not in (None, 0) else contract_size   # market-resolved fallback
    if cs is None or cs <= 0:
        return None                        # no contract size anywhere -> cannot size -> skip
    qty_base = float(contracts) * cs
    if qty_base <= 0 or float(price) <= 0:
        return None
    return (int(ts), symbol, side.upper(), qty_base, float(price))
```

Field-by-field from ccxt unified liquidation `{symbol, side:"buy"/"sell", contracts, contractSize, price, timestamp}` (per `liquidations.py:6-9`):
- `ts_ms ← int(timestamp)`
- `symbol ← view.symbol` **verbatim** (the feeder must stamp and the builder must filter on the *same* string — `build_liquidation_map` filters `ev[1]==symbol.upper()` at `liquidation.py:783,392`; pass `symbol=view.symbol` there so unified `"BTC/USDT:USDT"` matches on both sides).
- `side ← side.upper()` → `"BUY"`/`"SELL"`. Semantics stay: `BUY` force-order = short liquidated → map's `short` bucket (`liquidation.py:411`); this matches the engine (`buy` → short, `liquidations.py:78-80`).
- `qty ← contracts × contractSize` (base units).
- `price ← float(price)`.

**Accumulation (the one piece of genuinely new state).** The engine's `liq` read-through is the shallow flat `exchange.liquidations` ArrayCache (`api.py:76-84`), evicted across all symbols; it is NOT a 300s-per-symbol window. The map's realized heatmap needs a rolling 300s window (`cfg.window_seconds`). So the feeder appends each tick's read-through events into map-owned per-venue deques (`store.liq_buffer(sym, venue)`, `engine.py:139`), dedup by the LiqEvent tuple at the boundary ms:

```python
def _ingest_liq(store, symbol, cross_liq, contract_sizes):
    for venue, evs in cross_liq.items():
        if not evs:                        # None (stale/absent) or [] -> nothing to add
            continue
        buf = store.liq_buffer(symbol, venue)
        last = buf[-1][0] if buf else -1
        for ev in evs:
            le = _ccxt_liq_to_event(ev, symbol=symbol, contract_size=contract_sizes.get(venue))
            if le is None:
                continue
            if le[0] > last or le not in buf:   # newer, or a same-ms event not already held
                buf.append(le)
```

This mirrors the old `store.record_liquidation` deque (`engine.py:147-160`) but **sourced from read-through polling instead of a WS-push callback** — so no revival of the deprecated `market/streams.py::_record_liquidation` push path. It is **in-memory only, NOT disk-persisted** (memory `sessions-windows-levels-liquidations`: liquidations are a live low-trust snapshot, the engine needs no liq persistence). On restart the buffer re-warms from the live tape within one window. Fidelity note in §9.

## 5. Book-math relocation (E5b — the one import the maps still hold)

`orderbook.py:11-19` imports 7 helpers from `hunt_core.market.client` (which is being deleted): `WallCluster, aggregate_cross_exchange_walls, depth_imbalance_by_zone, depth_snapshot_from_book, detect_wall_clusters, normalize_depth_levels, wall_cluster_to_dict`. These live at `client.py:2269-2568` and are **already pure** — plain tuples/dicts in, no ccxt, no I/O (I read all 300 lines). Move them **verbatim** to a new keep-module `hunt_core/toolkit/book_math.py` (+ their privates `_book_depth_percentile`, `top_depth_walls`, `_TOP_BOOK_WALL_LEVELS`, and the `math` import for `depth_imbalance_by_zone`'s exp-decay). Change `orderbook.py:11` import to `from hunt_core.toolkit.book_math import (...)`. Zero logic change; this is the E5b "extract book-math to keep-module" task (#23). `merge_cross_books` (`orderbook.py:815`) and `merge_full_depth_bins` (`orderbook.py:537`) — used only for the cross-venue merge that has no engine source yet (§9) — stay in `orderbook.py` unused-but-typed until cross-book lands.

## 6. OI-bars native path

Today: `client.fetch_oi_bars_for_maps(sym, period="1h", limit=48)` (`tick_assembly.py:939`). Native:

```python
rows = await rest.poll_futures_data(ex, "fapiDataGetOpenInterestHist",
                                    {"symbol": binance_id, "period": "1h", "limit": 48})
oi_bars = oi_bars_from_frames(rows or [], view.klines.h1)   # as-of backward join, keeps I-5 (oi.py:94)
```

`oi_bars_from_frames` (`oi.py:34`) already does the correct backward as-of join (no lookahead). **One-line gap to close:** its OI-key fallback (`oi.py:48`) reads `openInterestAmount|openInterest|oi` but the raw `/futures/data/openInterestHist` row key is `sumOpenInterest`. Add `sumOpenInterest` to that fallback tuple. (Alternative — `rest.fetch_futures_data_series(...,"sumOpenInterest")` + `oi_bars_from_scalar_series` — drops timestamps and tail-zips, losing the gap-robust join; prefer the timestamped `poll_futures_data` path.)

## 7. What stays map-owned state vs. engine read-through

The engine is **stateless-per-snapshot**; three time-series the maps need have **no engine equivalent** and stay in `MapTimeSeriesStore` (`engine.py:57`), now fed from the view instead of WS callbacks:

- **`book_history`** (sticky walls `orderbook.py:124`, spoof `:425`, depth-heatmap-matrix `:251`): rolling book samples. Fed each tick by `store.sample_book(sym, snap_of(view.book))`. Engine gives *current* book only — history is a legitimate map-layer derived series.
- **liq accumulation deques** (`_liq_by_venue`): §4.2. In-memory window reconstruction; not persisted.
- **`_vp_snapshots`**: VP developing-profile history; unchanged, fed by `build_volume_profile_map`.

Deleted from the store: `_oi_bars_cache` / `cache_oi_bars` / `get_cached_oi_bars` (`engine.py:104-117`) and `_liq_estimate_cache` (`:119-131`) — those cached the old `client.fetch_oi_bars_for_maps` / cross-microstructure REST that no longer exist; OI-bars now come per-tick from `rest.poll_futures_data`. `flush_lake` / `enqueue_lake` (the maps JSONL sink) stay unchanged.

## 8. Typed `MapBundle` — killing the phantom-key family

`MapBundle` is already Pydantic (`engine.py:27`) but with `extra`-dict escape hatches and three `Any`-typed opaque maps. Tighten to the SPINE's fail-loud-Optional contract:

```python
class MapBundle(_View):                 # frozen, extra="forbid", arbitrary_types_allowed (nests dataclasses)
    symbol: str
    ts_ms: int
    orderbook: OrderbookMap | None = None       # None = builder returned None (absent), never {}
    liquidation: LiquidationMap | None = None
    volume_profile: VolumeProfileMap | None = None
    features: MapFeatures | None = None          # replaces the extra{} + derive_map_features dict
```

`MapFeatures` = a frozen model of the ~40 scalars `derive_map_features` computes (`engine.py:397-463`): `map_book_imbalance_1pct`, `map_sticky_wall_count`, `liq_forward_confidence`, `liq_magnet_pull_long_pct`, `map_vp_poc`, `map_accumulation_score`, … each `float|str|int|None`. Every fail-loud field is `Optional` and only set when its source is real — the `liq_forward_confidence` `_conf_raw is not None else 1.0` guard (`engine.py:421-424`) survives *as a validator*, not a `.get()`-with-default. The `extra: dict[str,Any]` (`engine.py:44`) and `to_dict()`/`market.update()` string-spray disappear: prizrak/scanner read `bundle.features.liq_magnet_pull_long_pct` by attribute; a name-lie is a mypy error.

The nested `OrderbookMap`/`LiquidationMap`/`VolumeProfileMap` are `@dataclass` today (`orderbook.py:31`, `liquidation.py:241`, `volume_profile.py:54`). Optional convert-to-Pydantic-`_View` is a nice-to-have (the project rule prefers BaseModel over dataclass); not required for the feed rewrite, so stage it separately to keep the map-body diff at zero.

## 9. Scope notes / open items (flag, don't silently paper over)

1. **Cross-venue book & VP merge has no engine source.** `MultiEngine` exposes cross funding/oi/lsr/liq only (`multi.py:143-206`) — no `cross_orderbook`/`cross_volume_profile`. So `cross_walls=None`, `cross_vp=None`; maps run single-venue Binance (`venues=["binance"]`). Restoring the old cross-exchange wall/VP merge (`orderbook.py:537,815`; `volume_profile.py:325`) requires a new `MultiEngine.cross_orderbook` accessor — a **separate engine increment**, out of scope for the maps rewrite. `merge_full_depth_bins`/`merge_cross_books` stay dormant.
2. **`bracket_tiers` → default ladder.** Leverage brackets are `fapiPrivate` on Binance (CCXT-public-only bans it); the maps already fall back to `_DEFAULT_LEVERAGE_TIERS=(10,25,50,100)` and label bands `leverage_tier_estimate`. Net effect vs. today: likely nil (private tiers were unavailable without keys anyway). If any public `market["limits"]["leverage"]` exists per venue, wire it later.
3. **Liq window fidelity.** Old path: WS-push at event time → zero eviction loss. New path: read-through polled per 30s tick → under an extreme cascade the flat `exchange.liquidations` cache (1000 events across *all* symbols) can evict events that arrived and were dropped within one tick gap. Acceptable because liquidations are explicitly low-trust and the realized heatmap degrades to forward-only-synthetic when `event_count==0` (`liquidation.py:740`). Wiring secondary-venue `watchLiquidations` (task #26) would restore event-time fidelity — a separate optional increment.
4. **`now_ms` coupling.** The map internals use `int(time.time()*1000)` for window cutoffs (`orderbook.py:85,332,509`; `liquidation.py:385`). Threading `view.now_ms` through would align windows to the snapshot clock (minor I-6 consistency win); low priority, not required for the mapping.

## 10. Module-boundary preservation

Maps are **shared plumbing** (both strategies read `bundle`/`MapFeatures`), never geometry — unchanged by this rewrite. The feeder produces one typed `MapBundle`; prizrak consumes it via its `assemble_prizrak(view, feature_panel)` path and scanner via its REST-tail path — neither gains a cross-module field. `tests/test_module_boundary.py` holds by construction (no shared dict introduced). The maps carry no entry/stop/target/emission vocabulary, so the confusion-class the boundary test guards doesn't touch them.

---

**Files to create/change:** new `hunt_core/maps/feed.py` (feeder + `MapTrade`/`_to_map_trades`/`_ccxt_liq_to_event`/`_ingest_liq`); new `hunt_core/toolkit/book_math.py` (verbatim move from `market/client.py:2269-2568`); edit `hunt_core/maps/orderbook.py:11` (import swap only); edit `hunt_core/maps/oi.py:48` (add `sumOpenInterest` key); retype `hunt_core/maps/engine.py` (`MapBundle`/`MapFeatures`, delete `apply_map_bundle_to_row` + OI/liq caches). **Map builder bodies (`build_orderbook_map`/`build_liquidation_map`/`build_volume_profile_map`) change zero lines below their signatures** — the entire rewrite is in the feed and the two shape converters above.

---

I have complete grounding. Here is the blueprint.

---

# HUNTER RUNTIME-WIRING + DELETION BLUEPRINT

Grounded in the real code. All anchors verified this session. Where the SPINE under-specifies against what `_cycle_loop.py` actually couples to, I correct it and say so.

## 0. Grounding corrections to the SPINE (load-bearing, read first)

1. **`required` is the resolve-set, not just a gate.** `Engine.snapshot(sym, required)` (`api.py:184-200`) only materializes planes named in `required` into `_planes`; everything else is invisible to `optional()`. So the builder MUST pass the **full plane list** as `required` and read each with `optional()`. `not_ready` then names the cold subset. Consumers gate on specific `None`s / `view.not_ready`, never on `snap.ready`. The SPINE's `_REQUIRED_TRACKED` is correct as "all planes to resolve" — keep `oi_hist_5m`/`basis` in it even though they're optional reads.
2. **`bbo` ≠ `book`.** Ingest stamps `bbo` (best bid/ask scalar, `ingest.py:215-220`) and `book` (levels, `ingest.py:163-166`) as *separate* planes. `MarketView.Book.bid/ask` come from `bbo`; `bids/asks/depth_imbalance/microprice_bias` from `book`. Add `"bbo"` to the required list; the builder reads both.
3. **HTF frame-cache persistence is DELETED, not rewired.** Five sites in `_cycle_loop` (`:370-372, :509-517, :988-995, :1050-1062, :1096-1106`) exist solely to paper over the post-restart HTF-staleness blackout (memory: `stale-htf-cache-trap`). The engine re-seeds *every* kline plane via REST on `start()` (`api.py:112-124`), so that failure mode is structurally gone. All five sites are removed, not ported.
4. **The real `client`/`ws_feed`/`spot_companion` coupling is ~20 sites, not the SPINE's 6.** Full enumeration in §2.
5. **The deprecated `runtime/engine_adapters/` trio has ZERO in-tree importers** (verified). It can be `rm -rf`'d outright in this cutover — it is not on anyone's path. This is the one place the SPINE's "read-only reference" is moot: nothing consumes it.

---

## 1. New view layer (three new files; per SPINE §1.3/§2/§3.1)

- `hunt_core/view/models.py` — the frozen strict Pydantic v2 `MarketView` + sub-models (SPINE §1.3), with the §0.2 fix: `Book` carries `bid/ask` from `bbo`.
- `hunt_core/view/build.py` — `MarketViewBuilder` (SPINE §2). `_REQUIRED_TRACKED` gains `"bbo"`.
- `hunt_core/view/runtime.py` — `build_market_runtime()` + `MarketRuntime` dataclass (SPINE §3.1), exact form below.

`MarketRuntime` is the single handle the loop threads everywhere `plane`/`client`/`ws_feed`/`spot_companion` went:

```python
@dataclass(slots=True)
class MarketRuntime:
    multi: MultiEngine
    spot: SpotEngine
    builder: MarketViewBuilder
    @property
    def exchange(self):            # engine.exchange — REST tail for scanner/regime/backfill
        return self.multi.primary.exchange
    def tracked(self) -> frozenset[str]:
        return self.multi.primary.tracked_symbols()
    async def aclose(self) -> None:
        await self.multi.close(); await self.spot.close(); await asyncio.sleep(1.5)
```

---

## 2. `_cycle_loop.py` — exact edits

### 2A. Imports (`:12-71`)
- **Remove** from the `hunt_core.market` import block (`:38-47`): `CrossExchangeConfig, HuntLoadPlanner, apply_cross_exchange_env, create_hunt_market_plane_from_settings, fetch_secondary_ticker_overlay, load_cross_exchange_config, refresh_cross_exchange_cache`. **Keep** `gate_symbol_list` (moves to `hunt_core.market.symbol_gate` import — see §6 keep-set).
- **Remove**: `from hunt_core.data.collect import TickBatchCache, safe_fetch` (`:13`) → the loop no longer builds a batch cache; `safe_fetch` for `fetch_ticker_24h`/regime moves under the scanner/regime REST path.
- **Remove**: `from hunt_core.data.frame_cache import reset_frame_cache` (used `:370`) and both lazy `get_frame_cache` imports (`:510, :1054, :1101, :989`).
- **Add**: `from hunt_core.view.runtime import build_market_runtime`.
- `detect_local_proxies` (`:71`, `market.network`) — **keep** (network is a keep-module).

### 2B. Construct + start (replace `:313-353`)
Replace the `create_hunt_market_plane_from_settings` retry block **and** the `client/ws_feed/spot_companion` unpack **and** the `fetch_status` health probe **and** `ws_feed.set_symbols/start`:

```python
    rt = None
    _rt_exc: Exception | None = None
    for _attempt in range(1, 4):                     # keep the 3× startup-retry verbatim
        try:
            rt = await build_market_runtime(settings, cli_symbols)
            break
        except Exception as exc:
            _rt_exc = exc
            LOG.warning("hunt_market_runtime_startup_retry | attempt=%d error=%s",
                        _attempt, type(exc).__name__)
            if _attempt < 3:
                await asyncio.sleep(20.0 * _attempt)
    if rt is None:
        assert _rt_exc is not None
        raise _rt_exc
```

- `build_market_runtime` internally does `MultiEngine(PINNED∪cli, timeframes=settings.engine.timeframes).start()` + `SpotEngine(...).start()` (SPINE §3.1). `MultiEngine.__init__` already accepts `timeframes`/`secondaries` (`multi.py:46-51`) — no engine change.
- **Delete** `:335-337** `set_live_spot_companion(spot_companion)` — the analyst/deep loop takes `rt.spot` directly now (see 2E); `tick_state.set_live_spot_companion` and its getter are deleted with `HuntCcxtSpotCompanion`.
- **Delete** the `fetch_status` probe (`:339-351`): startup-log-only; `MultiEngine.start()` already logs `multi_engine_started` (`multi.py:73`). If a health line is wanted, `await rt.exchange.fetch_status()` is a public ccxt method (allowed).
- **Delete** `ws_feed.set_symbols(list(cli_symbols)); await ws_feed.start()` (`:352-353`) — the warm universe is fixed at construction (`Ingest.start` over a fixed list, `ingest.py`), there is no per-tick WS rotation.

### 2C. Background bands — rewire the four child tasks (they receive `rt` handles; internals are task-24)
| Band | Site | Edit |
|---|---|---|
| `manipulation_task` (scanner) | `:364-367` | pass `rt.exchange` (the REST tail) instead of `client`. `_manipulation_scan_loop(cli_symbols, exchange, broadcaster, send_telegram)` — its body's `deliver_manipulation_setups(..., client, ...)` is rebuilt in task-24 to the `rest.*` funnel (SPINE §4 scanner). |
| `tg_task` | `:442-443` | `build_hunt_telegram_commands(settings, ..., client=rt.exchange)` — `/signal` handler migrated in task-24. |
| `deep_task` (PRIZRAK analyst) | `:452-460` | `analyst_pinned_loop(rt, broadcaster, send_telegram=...)` — drops the `client=..., ws_feed=...` pair; the loop calls `rt.builder.build(sym)` + `assemble_prizrak(view, panel)` (SPINE §4 prizrak). |
| `path_backfill_task` (track) | `:462-470` | `path_backfill_loop(rt.exchange, interval_s=900.0)` — its `client.fetch_klines_between` becomes `rest.fetch_ohlcv_between(rt.exchange, ...)` (SPINE §4 track). |

### 2D. Delete the frame-cache HTF machinery (five sites)
Remove entirely: `reset_frame_cache()` (`:370-372`), the `load_htf_frames` reload (`:509-517`), the blackout-persist branch (`:988-995` — keep the `os._exit(1)` self-restart, drop the `persist_htf_frames` call above it), the periodic-persist block (`:1050-1062`), and the shutdown-persist block (`:1096-1106`). Rationale in §0.3.

### 2E. Main-tick body — the large surgery (`:519-905`)
This is where the SPINE's "warm set fixed + REST tail" (§3.1) lands. The current body funnels a *dynamic* `active` universe (prescan-merged, demand-shaped, load-planned) through `snapshot_symbol`. In the end state the **main tick drives only the warm/tracked set through `rt.builder`**; the dynamic funnel is scanner-owned REST.

Removed from the tick body (their machinery is engine-native or scanner-REST, all deleted in §6):
- **Regime refresh** (`:524-536`) — `refresh_market_regime(client)` → `refresh_market_regime(rt.exchange)` (regime reads all-tickers via REST; keep the band, swap the handle). *Not* deleted — rewired.
- **Prescan / watchlist / debounce / merge / demand-shaping** (`:538-744, :768-796`) — this whole scanner funnel (`run_scan`, `prescan_from_tickers`, `PrescanDebounceQueue`, `resolve_watch_universe` merge, `HuntLoadPlanner.plan_tick`) moves into `manipulation_task` over `rest.fetch_all_tickers` (SPINE §2.1/§4 scanner). The main tick no longer computes `active`/`hunt_active` from it.
- **`_overlay_ws_tickers` + `ws_feed.set_symbols`** (`:797-813`) — deleted (no WS rotation; §2B).
- **Cross-exchange refresh** (`:815-838`) — deleted; `MultiEngine._cross_loop` (`multi.py:77-110`) already polls funding/OI/LSR/liq per secondary on its own cadence. `view.cross.*` reads it.
- **`batch_cache = TickBatchCache()`** (`:402`) and its `tick_ctx` entry (`:855`) — deleted; `engine.snapshot()` IS the warm batch.
- **`tick_ctx`** (`:840-863`) shrinks to what `run_tick` still needs (see §3): `rt.builder`, `settings`, `mode_map` (now over the tracked set), `broadcaster`, `send_telegram`, `pump_store`, `symbol_state`, `feature_lake`, plus `prev_oi/last_bias/last_lifecycle_phase`. Drop `client, ws_feed, spot_companion, batch_cache, tier, tier_by_symbol, snapshot_parallel, cross_ex_cache, ticker_by_sym, minimums, prescan_outlier_by_sym`.
- **Lake warmup** (`:746-765`) — `ensure_lake_warm(client, ...)` deleted; the engine's REST seed gives every tracked symbol deep history at `start()`. (`lake_warmup.py` → DELETE, §6.)

Kept, rewired to `rt`:
- **Ban telemetry** (`:1018-1030`, `:928-934`) — `client.rest_gate.guard.telemetry` has no engine equivalent (ccxt's throttler is internal). Replace the whole ban-telemetry + IP-ban-detection block with engine diagnostics: `rt.multi.primary.plane_ages(sym)` (`api.py:219`) for staleness, and drop the `remaining_pause_s`/`weight_budget` reads. The blackout self-restart (`:975-995`) stays but keys off `assess_universe_health(rows)` alone (rows now come from the tracked-set MarketViews), with `_is_ban` forced `False` (no ban-gate available → never suppress restart on a phantom ban). *This is a genuine capability reduction — note it in the ADR.*
- **Universe-health assessment** (`:906-997`), **pinned startup brief** (`:998-1017`, swap `client=rt.exchange`), **digest** (`:1031-1039`), **checkpoint** (`:1040-1049`), **tick rotation** (`:1064-1079`), **pump_store save** (`:1063`) — all kept; only handle swaps.

### 2F. Teardown (`:1096-1155`)
- Delete the shutdown HTF-persist (§2D).
- `:1153` `await plane.aclose()` → `await rt.aclose()`.
- Child-task cancellation (`:1126-1151`) unchanged.

---

## 3. `_cycle_tick.py` — exact edits

The tick body is rebuilt around `rt.builder.build(sym)` → `MarketView` → typed assemblies. This is the seam where the untyped `snapshot_symbol` dies.

- **Imports (`:26-51`)**: drop `HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams, attach_cross_fields, merge_ws_cross_into_snapshot` (`market`); drop `SnapshotTier, TickBatchCache, refresh_tick_batch_cache, safe_fetch, sort_symbols_for_tick` (`data.collect`); drop `snapshot_symbol` (`tick_assembly`). Add `from hunt_core.view.build import MarketViewBuilder` (typed param).
- **Signature (`:104-129`)**: replace `client, ws_feed, spot_companion, batch_cache, tier, tier_by_symbol, snapshot_parallel, cross_ex_cache, ticker_by_sym, minimums, prescan_outlier_by_sym, intra_bar` with a single `builder: MarketViewBuilder`. Keep `settings, mode_map, broadcaster, send_telegram, prev_oi, last_bias, last_lifecycle_phase, pump_store, symbol_state, feature_lake`.
- **Delete the batch-cache refresh** (`:152-165`), **the ticker-24h fetch** (`:171-180`), **the spot_companion refresh** (`:181-214`) — all subsumed: `engine.snapshot` is warm, `view.spot` is `SpotEngine`-pushed, `view.derivs`/`view.last_price` replace the ticker batch.
- **`_snapshot_one`** (`:226-278`): body becomes `view = builder.build(sym); setup = assemble_prizrak(view, build_feature_panel(view))` (SPINE §4). Keep the `asyncio.wait_for(SYMBOL_TICK_TIMEOUT_S)` guard and the timeout/error → error-row fallback (`:257-278`) — but the "row" is now a typed setup-or-`None`, not a dict. `NotReady` from a cold plane is caught here → the symbol contributes a `not_ready` diagnostic, mirroring the old `watch_symbol_data_reject` (`:316-322`).
- **The per-symbol processing loop** (`:310-432`): `latch_row_setups`, `evaluate_followups`, `_reconcile_inwatch_active`, `_deliver_followup`, `ensure_fusion_lifecycle_fields/resolve_row_mtf`, feature-lake enqueue — these read/mutate the row-dict. They are **task-24 consumer-migration** (typed `PrizrakSetup` + `SignalState`). For the *wiring* step: the loop iterates `builder.tracked` symbols, gets typed setups, and the track/deliver calls bind to the setup model. `oi_val = (row.get("market")...).get("oi")` (`:356`) → `view.derivs.oi`.
- **`prev_oi` feedback** (`:357-358`): `prev_oi[symbol] = view.derivs.oi` when non-`None`.
- **Cross fields** (`:359-366`): deleted — `view.cross` is already populated from `MultiEngine.cross_*`; no `attach_cross_fields`/`merge_ws_cross_into_snapshot`.
- **Ticker safety-net + orphan reconcile** (`:434-500`): `reconcile_active_from_ticker(..., ws_feed=...)` drops `ws_feed`; the "ticker" price source becomes `view.last_price` for tracked symbols and `rest.fetch_ohlcv_between` for orphans (see §4). `used_weight_1m()` log (`:307`) → drop (no weight budget).

---

## 4. `_cycle_reconcile.py` — exact edits

- **Imports (`:7-9`)**: drop `from hunt_core.data.collect import safe_fetch` and `from hunt_core.market import HuntCcxtClient`. Add `from hunt_core.engine import rest`.
- **Signature** of `_reconcile_inwatch_active` / `_reconcile_orphan_signals` (`:24, :78`): `client: HuntCcxtClient` → `exchange: Any` (the ccxt.pro client from `rt.exchange`).
- **Both `client.fetch_klines_between(o_sym, "5m", start_time_ms=…, end_time_ms=…)` calls** (`:49-56, :101-108`) → `await rest.fetch_ohlcv_between(exchange, o_sym, "5m", start_ms=…, end_ms=…)` (`rest.py:82-98`, returns closed-only bars-list). The `safe_fetch` wrapper (`:48, :100`) is dropped — `rest.fetch_ohlcv_between` is already fail-loud (`[]` on error). The `df.is_empty()`/`df["high"].max()` consumers (`:58-63, :110-115`) adapt to the bars-list (`max(b[2] for b in bars)` etc.) or wrap via the extracted `toolkit/ohlcv.py::ohlcv_to_frame`.

---

## 5. `_impl.py` — exact edits

- **Drop `_overlay_ws_tickers`** (`:37-62`) and its `HuntCcxtStreams` import (`:16`): no WS-over-REST ticker overlay exists once the engine owns the ticker plane. Its two call sites (`_cycle_loop:797`, `_cycle_tick:223`) are deleted (§2E/§3).
- **Keep** `_load_state`/`_save_state` (`:65-92`, cooldown/delivery-state persistence — `data.lake`/`delivery_state`, both keep-modules) and `_TICK_LOCK`, `HUNT_SNAPSHOT_PARALLEL`, `SYMBOL_TICK_TIMEOUT_S`.
- The `SNIPER_CONFIG` constants (`:27-31`) are prizrak/scanner tunables — untouched.

---

## 6. DELETION MANIFEST

### `hunt_core/market/` — DELETE (transport replaced by `engine/`)
| File | Why it dies | Replaced by |
|---|---|---|
| `client.py` (108 KB) | `HuntCcxtClient` REST governor + derivs cache | `Engine` + `engine/rest.py` (**extract book-math first — §7**) |
| `streams.py` (95 KB) | `HuntCcxtStreams` WS mux + WS-derived orderflow | `engine/ingest.py` + `engine/orderflow.py` |
| `spot.py` (15 KB) | `HuntCcxtSpotCompanion` | `engine/spot.py::SpotEngine` |
| `cross.py` (43 KB) | cross-venue REST cache + `attach_cross_fields` | `MultiEngine._cross_loop` + `cross_*` |
| `factory.py` (24 KB) | plane factory + exchange builders | `build_market_runtime` + `engine/exchanges.py` (**extract OHLCV transforms first — §7**) |
| `capacity.py` | `HuntLoadPlanner` demand-shaping | ccxt throttler + fixed warm set |
| `ccxt_rest.py` | `HuntCcxtRestGate` custom governor | ccxt built-in throttler |
| `rate_limit.py` | custom rate limiter | ccxt throttler |
| `weight_registry.py` | weight accounting | ccxt throttler |
| `ccxt_guard.py` | `ccxt_method_available` | engine reads `ex.has` directly (`multi.py:68`); only transport files (all dying) import it — verified |
| `live_price.py` | `apply_live_price_to_row`/`resolve_live_price` | `view.last_price` (ticker plane) |

### `hunt_core/market/` — KEEP (per prompt: tick_registry/symbols/symbol_gate/network)
`symbols.py`, `symbol_gate.py`, `tick_registry.py`, `network.py`. Verified transport-clean: they intra-import only `market.symbols` (a pure helper); no `client`/`streams`/`spot` coupling. `__init__.py` is **rewritten** to export only these four + the extracted utilities (§7).

### `hunt_core/data/` — DELETE
| File | Why | Replaced by |
|---|---|---|
| `collect.py` | `TickBatchCache`/`refresh_tick_batch_cache`/`safe_fetch`/`sort_symbols_for_tick` batch-REST | `engine.snapshot()` warm batch |
| `frame_cache.py` | hot frames + HTF persist | `SymbolState` frames (`state.py:113-128`) |
| `completeness.py` | OHLCV completeness over client | `engine/freshness.py`; consumers (`tick_assembly`, `features/snapshot`) migrate in task-24 |
| `lake_warmup.py` | `ensure_lake_warm(client, …)` backfill | engine REST seed at `start()` |

### `hunt_core/data/` — KEEP (per prompt: lake/universe/persistence)
`lake.py`, `universe.py`, `jsonl_io.py`, `tick_jsonl.py`, `baseline_store.py`, `symbol_blacklist.py`. Notes: `universe.py`'s only `HuntCcxtClient` reference is a **comment** (`:314`) — transport-clean. `tick_jsonl.py`/`baseline_store.py` are persistence I/O; their row-*shaping* helpers (`ensure_fusion_lifecycle_fields`, `resolve_row_mtf`, `batch_update_baselines`) are consumer-migration (task-24), not deletions.
**Edit `data/__init__.py`**: the `__getattr__` shim (`:36-56`) re-exports `snapshot_symbol` (from `tick_assembly`) and `PrescanEngine`/`prescan_from_tickers`/… (from `collect`). Both sources die → repoint the prescan names to `hunt_core.scanner.prescan` (where they actually live) and drop `snapshot_symbol` entirely.

### `hunt_core/runtime/engine_adapters/` — DELETE OUTRIGHT (the deprecated trio)
`rm -rf` the directory. **Zero in-tree importers** (verified). SPINE mandates it must not be built upon; nothing depends on it, so it leaves with the transport layer, not after it.

---

## 7. EXTRACT-FIRST keep-utilities (before any deletion; these are pure and still needed)

### 7A. Book-math → `hunt_core/toolkit/book_math.py` (NEW)
Move verbatim from `client.py:2192-2568` (all pure, no transport deps): `depth_imbalance_from_levels`, `depth_imbalance_from_book`, `microprice_bias_from_book`, `_clamp`, `WallCluster`, `_book_depth_percentile`, `detect_wall_clusters`, `depth_imbalance_by_zone`, `top_depth_walls`, `normalize_depth_levels`, `depth_snapshot_from_book`, `aggregate_cross_exchange_walls`, `wall_cluster_to_dict`. Add the SPINE-referenced `view_from_book(book_snapshot) -> Book` constructor here (E5b). Consumers to repoint: `features/prepare.py`, `features/snapshot.py`, `maps/__init__.py`, `maps/orderbook.py` (+ `market/__init__.py`'s `normalize_depth_levels` re-export).

### 7B. OHLCV transforms → `hunt_core/toolkit/ohlcv.py` (NEW)
Move from `factory.py`: `_KLINE_FRAME_SCHEMA`, `interval_to_seconds`, `_close_time_ms`, `ccxt_ohlcv_to_frame`, `_ohlcv_frame_has_incomplete_tail`, `_drop_incomplete_ohlcv_tail`, `drop_unclosed_ohlcv_tail`, `finalize_kline_frame`, `resample_ohlcv_from_1m`, `min_1m_bars_for_resample`, `_RESAMPLE_FROM_1M_INTERVALS`, `ms_to_utc`, `extend_parsed_ws_kline`. These convert the engine's bars-list (`list[Bar]`) into the Polars frame `features/` expects — the builder's `_frame()` helper (SPINE §2) lives here as `ohlcv_to_frame(bars, tf, exchange)`. Consumers to repoint: `features/snapshot.py`, `deliver/manipulation_delivery.py`, `analyst_assembly.py`, `track/path_backfill.py` (+ `market/__init__.py` re-exports).

**Extraction ordering rule**: do 7A+7B, repoint all consumers, run `ruff`+`mypy` green, *then* delete the host files (`client.py`/`factory.py`). Never delete-then-scramble.

**Gate item to verify before deleting `factory.py`**: `create_sync_binance_future`/`fetch_klines_sync` — in-tree only `client.py`/`spot.py` (both dying) import them, but `research/` offline scripts (calibration/reconcile/tg_backtest, named in `factory.py:332` docstring) may. Grep `research/` first; if used, these sync helpers move to `toolkit/ohlcv.py` or a small `research/_offline_ccxt.py`, not deleted.

---

## 8. DANGLING-IMPORT DELETION GATE

Run **before** committing each deletion; the delete is blocked while any pattern has a hit outside the file being deleted. Zsh-safe (quote the `-E` patterns):

```bash
cd /Users/tonyaleksandrov/Documents/HUNTER
# transport classes/modules — must be ZERO after consumer migration
grep -rnE "HuntCcxtClient|HuntCcxtStreams|HuntCcxtSpotCompanion|HuntCcxtRestGate|HuntLoadPlanner" hunt_core --include="*.py" | grep -v __pycache__
grep -rnE "market\.(client|streams|spot|cross|factory|capacity|ccxt_rest|rate_limit|weight_registry|ccxt_guard|live_price)\b" hunt_core --include="*.py" | grep -v __pycache__
# data-layer deletions
grep -rnE "data\.(collect|frame_cache|completeness|lake_warmup)\b|TickBatchCache|refresh_tick_batch_cache|reset_frame_cache|get_frame_cache|ensure_lake_warm" hunt_core --include="*.py" | grep -v __pycache__
# the plane factory + adapters
grep -rnE "create_hunt_market_plane|HuntMarketPlane|engine_adapters" hunt_core --include="*.py" | grep -v __pycache__
# moved utilities must NOT still resolve via the old host
grep -rnE "from hunt_core\.market\.factory import|from hunt_core\.market\.client import" hunt_core --include="*.py" | grep -v __pycache__
# stale re-exports in the two barrels
grep -nE "create_hunt_market_plane|HuntCcxtClient|ccxt_ohlcv_to_frame|normalize_depth_levels" hunt_core/market/__init__.py
grep -nE "snapshot_symbol|from hunt_core\.data import collect" hunt_core/data/__init__.py
```

A green gate = every match is inside a file that is itself being deleted in the same commit, or is a re-export line being removed in the same edit. Any other hit means a consumer wasn't migrated — **do not delete yet**.

Also run the repo's own guard: `uv run vulture hunt_core --min-confidence 80` catches orphaned attributes left behind (pre-commit already runs it), and `scripts/check_prohibited_apis.py` confirms no private-CCXT crept in during the rewrite.

---

## 9. FINAL VERIFICATION (in order; each must pass before the next)

```bash
cd /Users/tonyaleksandrov/Documents/HUNTER
uv run ruff check .                         # line-length/banned-api (TID251: no pandas/requests)
uv run mypy hunt_core                       # the phantom-key/name-lie class is now a TYPE error on MarketView
uv run vulture hunt_core --min-confidence 80  # orphan-field / dead-branch guard
uv run python scripts/check_prohibited_apis.py  # private-CCXT scan (also in pre-commit)
uv run pytest tests/test_module_boundary.py     # prizrak↔scanner boundary still holds (regex/AST, transport-free — §grounding)
uv run pytest                                    # full suite
# smoke — but see the trap: --no-telegram silences the WHOLE manipulation lane (CLAUDE.md)
uv run python -m hunt_core watch --once --no-telegram   # exercises construct/start/build/teardown; PRIZRAK path only
```

Module-specific gates per the two-strategy law:
- **PRIZRAK** (prizrak lane rewired first or second): **no backtest** — measure on live data. After the tick rewire, run the `--once` smoke and confirm `MarketView`s build for the pinned/tracked set and `deep_task` emits (`/deep-analysis` skill; live-log `watch_symbol_data_reject` should show `not_ready` planes, never a fabricated value).
- **MANIPULATIONS** (scanner lane): run `/backtest-gate` (touch-based outcome R-delta) **before/after** the scanner's REST-funnel rewrite — the 6 `research/backtest_*.py` import `advance_manipulation_scales`, so a real number comes back; report the R delta. A same-number result means the scanner emission path wasn't actually touched yet.
- **Phantom-key sweep**: `/phantom-key-scan` over the working tree after the view layer lands — the whole point is that the signature defect family is now a `mypy` error on the frozen `MarketView`, so this should come back clean where it historically flagged the row-dict.

**Coexistence checkpoint** (SPINE §1.5): the loop may carry the old `snapshot_symbol` path for the not-yet-migrated lane and the new `builder.build` path for the migrated one across one or two sessions — they share no dict (`test_module_boundary.py` holds by construction), so `ruff`+`mypy`+`pytest` must stay green *at every intermediate commit*, not only at the end. Delete transport only when the gate in §8 is fully green for both lanes.

---

### Files touched (absolute paths)
- Edit: `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/cycle/_cycle_loop.py`, `/…/_cycle_tick.py`, `/…/_cycle_reconcile.py`, `/…/_impl.py`
- New: `/…/hunt_core/view/models.py`, `/…/hunt_core/view/build.py`, `/…/hunt_core/view/runtime.py`, `/…/hunt_core/toolkit/book_math.py`, `/…/hunt_core/toolkit/ohlcv.py`
- Rewrite barrels: `/…/hunt_core/market/__init__.py`, `/…/hunt_core/data/__init__.py`
- Delete (market): `client.py, streams.py, spot.py, cross.py, factory.py, capacity.py, ccxt_rest.py, rate_limit.py, weight_registry.py, ccxt_guard.py, live_price.py`
- Delete (data): `collect.py, frame_cache.py, completeness.py, lake_warmup.py`
- Delete (dir): `/…/hunt_core/runtime/engine_adapters/`
- Keep (market): `symbols.py, symbol_gate.py, tick_registry.py, network.py`
- Keep (data): `lake.py, universe.py, jsonl_io.py, tick_jsonl.py, baseline_store.py, symbol_blacklist.py`

**One capability regression to record in the cutover ADR**: the ban-telemetry / IP-ban-detection block (`_cycle_loop.py:928-934, 1018-1030`) has no engine equivalent (ccxt's throttler is internal). It is replaced by `plane_ages()`-based staleness diagnostics, and blackout self-restart loses its `_is_ban` suppressor. Acceptable per the "warm set fixed + REST tail" model, but it must be an explicit, logged decision — not a silent drop.

---

I have the complete picture. Writing the blueprint now — returned as my final response.

---

# Native-rewrite BLUEPRINT — `hunt_core/prizrak/` (Module 1: levels / accumulation)

## 0. Headline finding — "geometry unchanged" is FREE here

The prizrak **geometry core is already row-dict-free.** Every decision entry point takes raw OHLCV + a price scalar, not the row:

- `build_prizrak_signals(ohlcv_by_tf: dict[str, list[list[float]]], *, price, cfg, marketcap_series, dominance_changes, liq_context, abstain_sink)` — `orchestrator.py:2154`
- `compute_prizrak_structure(ohlcv_by_tf, *, cfg)` — `orchestrator.py:2434`
- `compute_interest_zones(ohlcv_by_tf, *, price, cfg, tf)` — `orchestrator.py:216`

`grep -E 'row\.get|row\[|market\.get' orchestrator.py` returns **two hits, both in comments** (`:23`, `:71`). accumulation.py, confluence.py, pp.py, poc.py, stop_volume.py, structure.py, traps.py, figures.py, invalidation.py — all consume `list[list[float]]` / `list[dict]` bars and `cfg`. None touch a row.

**Consequence:** the entire row-dict coupling of Module 1 lives in exactly three boundary layers, and the orchestrator needs **zero** changes. The rewrite is a *source swap at the seam*, which is why the "data-source rewrite, not a strategy change" contract is satisfiable by construction — the geometry functions' signatures are untouched, so byte-identical output is the default, not a goal to defend.

---

## 1. The three coupling layers (the only things that change)

| Layer | File | What it does with the row | Fate |
|---|---|---|---|
| **Dead OHLCV adapter** | `adapter.py::row_ohlcv_by_tf` (`:29`) | reads `row["timeframes"][tf]["ohlcv"]` — **never populated by the live pipeline** (confirmed in-code: `entry.py:32-37`, always returns `{}` live) | **DELETE.** Replaced by raw kline bars from `MarketView`. |
| **The seam** | `entry.py::ensure_prizrak_verdict` (`:24`) | reads `row["price"]`, `row["symbol"]`, `row["market"].{liq_cascade_risk, liq_synthetic_only, map_book_imbalance_1pct}`; **writes** 6 `prizrak_*` keys | **REWRITE** → `assemble_prizrak(view, feature_panel, map_bundle) -> PrizrakOutput`. |
| **Deliver/render** | `build.py` (`AnalystReport`), `structural_forecast.py`, `forecast_panel.py` | read `prizrak_*` back + `row["market"]`/`row["maps"]`/`row["price"]` | **REBIND** to typed `PrizrakOutput` + `MapBundle` (this is the `deliver/` side per SPINE §4). |

Everything else in `prizrak/` (the ~2450-line orchestrator, all detectors, config, доп-factors) is **kept as-is**.

---

## 2. The native seam — what replaces `entry.py`

New file `hunt_core/prizrak/assemble.py` (module-1-owned; the native replacement for `ensure_prizrak_verdict`). It sources its inputs from typed models and calls the **unchanged** orchestrator functions:

```python
def assemble_prizrak(
    view: MarketView,
    panel: FeaturePanel,           # features/ output (§5) — the indicator series
    maps: MapBundle,               # maps/ output (§4) — the 3 liq keys, typed
    *,
    cfg: PrizrakConfig | None = None,
) -> PrizrakOutput:
    cfg = cfg or PrizrakConfig.load()
    price = view.last_price                       # was row["price"]  (always-present ticker plane)
    if price <= 0:
        return PrizrakOutput.empty(view.symbol)

    ohlcv_by_tf = _raw_klines_for_tiers(view, cfg) # was ohlcv_by_tf kwarg / dead row_ohlcv_by_tf

    marketcap_series = (
        read_cached_series(view.symbol) if cfg.marketcap_enabled else None
    )                                              # unchanged: external CoinGecko cache read
    dominance_changes = (
        read_cached_changes_24h() if cfg.dominance_enabled else None
    )                                              # unchanged: external cache read
    liq_context = maps.liq_reconcile_context() if cfg.liq_reconcile_enabled else None
    #   -> {"liq_cascade_risk":…, "liq_synthetic_only":…, "map_book_imbalance_1pct":…}
    #      the ONLY gating input that was sourced from row["market"]

    abstain: list[dict[str, Any]] = []
    candidates = build_prizrak_signals(               # ORCHESTRATOR UNCHANGED
        ohlcv_by_tf, price=price, cfg=cfg,
        marketcap_series=marketcap_series,
        dominance_changes=dominance_changes,
        liq_context=liq_context, abstain_sink=abstain,
    )
    structure = compute_prizrak_structure(ohlcv_by_tf, cfg=cfg)   # UNCHANGED
    zones     = compute_interest_zones(ohlcv_by_tf, price=price, cfg=cfg)  # UNCHANGED

    return PrizrakOutput(
        symbol=view.symbol,
        candidates=[PrizrakCandidate.model_validate(c) for c in candidates],
        summary=_best(candidates),
        structure=structure, interest_zones=zones, abstain=abstain,
        bias_liq_conflict=_wait_tick_bias_conflict(structure, liq_context, cfg),
    )
```

Every read that was `row.get(...)` now traces to a typed field; every write that was `row["prizrak_*"] = …` becomes a field of the returned `PrizrakOutput`. **No row mutation.** The two `LOG.info` conflict-visibility calls (`entry.py:99`, `:122`) move here verbatim.

---

## 3. Key → typed-field mapping (the "flag every row-dict key" deliverable)

### 3a. Reads the seam does today → typed source

| Row-dict key (today) | Native typed source | Notes |
|---|---|---|
| `row["price"]` | `view.last_price` | the one always-present field (ticker `require`) — presence⟺fresh |
| `row["symbol"]` | `view.symbol` | |
| `row["timeframes"][tf]["ohlcv"]` (dead) | `view` raw klines per TF (§3c) | replaces `row_ohlcv_by_tf` entirely |
| `row["market"]["liq_cascade_risk"]` | `maps.liq_cascade_risk: str \| None` | **gating** — feeds strength via `compute_liquidation_factor` (`liq_reconcile.py:62`) |
| `row["market"]["liq_synthetic_only"]` | `maps.liq_synthetic_only: bool` | **gating** (`liq_reconcile.py:63`) |
| `row["market"]["map_book_imbalance_1pct"]` | `maps.book_imbalance_1pct: float \| None` | **gating** (`liq_reconcile.py:64`) |

These three are the **only** row-dict keys that touch the prizrak decision (strength). They must become typed `MapBundle` fields (SPINE §4: maps/ → `build_map_bundle(view) -> MapBundle`). Everything else in the inventory below is **render-only**.

### 3b. Writes (row mutations → `PrizrakOutput` fields)

`entry.py` writes six keys — `prizrak_abstain`, `prizrak_structure`, `prizrak_interest_zones`, `prizrak_signals`, `prizrak_summary`, `prizrak_bias_liq_conflict` — all become fields of `PrizrakOutput`. The deliver layer reads them off the object, not the row.

### 3c. Kline representation — a REFINEMENT to the SPINE's `MarketView` (load-bearing)

The SPINE models `MarketView.klines.*` as `pl.DataFrame`. **Prizrak does not want Polars** — its geometry is validated against the raw ccxt shape `[ts, o, h, l, c, v]` (`adapter.py:1-6` docstring is explicit: "the raw-CCXT-row shape every prizrak detector is built and validated against"), and `build_prizrak_signals` is typed `dict[str, list[list[float]]]`.

The engine's native kline plane **already returns exactly that**: `snapshot.optional("kline.4h")` → `[list(b) for b in frame]` = `list[[open_ms,o,h,l,c,v]]` (`api.py:69-71`). Wrapping it in Polars for `MarketView`, then having prizrak call `.rows()` to get lists back, is a needless per-tick round-trip that re-introduces a conversion shim of exactly the kind this rewrite deletes.

**Recommendation:** `MarketView` should carry the engine's **native `list[Bar]`** as the canonical kline form and derive the Polars frame **for features on demand** (features wants Polars; prizrak wants raw). Concretely, add a raw accessor the prizrak assembly uses:

```python
def _raw_klines_for_tiers(view: MarketView, cfg: PrizrakConfig) -> dict[str, list[list[float]]]:
    need = {tf for tier in (cfg.intraday, cfg.meso, cfg.macro) for tf in tier.timeframes}
    return {tf: bars for tf in need if (bars := view.klines_raw(tf)) and len(bars) >= 15}
```

This keeps prizrak's input byte-identical to today's fetched bars and avoids Polars↔list churn. (All six prizrak tier TFs — 5m/15m/1h/4h/1d/1w — are engine `_DEFAULT_TFS` planes seeded to `OHLCV_LIMIT=1000` bars, ample for macro `lookback_bars=150` and the 120-bar interest-zone window. `config.py:39-43`.)

One no-lookahead note preserved for free: the engine's frames are **closed-only** (`rest.seed_ohlcv` drops the forming bar; WS merges only newly-*closed* bars, `state.py:118-128`). The current path had to bolt on `drop_unclosed_ohlcv_tail` after a list-fetch (`analyst_assembly.py:229-233`) precisely because the old list path bypassed that drop. **That crutch disappears** — the engine plane is already I-5-clean, so `view.klines_raw(tf)[-1]` IS the newest closed bar.

---

## 4. Row-dict key inventory — the render (deliver-side) surface

These are read **after** the decision, by the report/format layer. They are name-lies-in-waiting today (dict `.get` on `row["market"]`/`row["maps"]`). In the native rewrite they become typed `MapBundle` / `MarketView` / `FeaturePanel` fields; a formatter reading a field the producer never set becomes a **mypy error**.

**`market.*` map keys (→ typed `MapBundle` fields):**
- `structural_forecast.py`: `oi_regime` (`:18`), `oi_change_pct` (`:21`), `delta_ratio` (`:23`), `map_accum_bid_absorption` (`:37`), `map_ask_thinning` (`:39`), `map_cvd_divergence` (`:41`), `map_vp_va_contraction` (`:43`), `map_vp_accumulation` (`:46`)
- `toolkit/targets.py` (via `collect_*_targets`): `map_void_above` (`:71`), `map_void_below` (`:151`), `map_cvd_divergence` (`:163`)
- `deliver/zone_confluence.py`: `funding` (`:114`)

**`maps.*` blocks (→ typed sub-models on `MapBundle`):** `maps.liquidation` (magnets/clusters), `maps.volume_profile` (`poc`, `naked_poc`, `hvn_nodes`, `va_contraction`, `accumulation`), `maps.orderbook` (`sticky_walls`, `wall_clusters`, `absorption_zones`) — `zone_confluence.py:63-114`, `targets.py:25-123`. Plus `row["session"]` (`targets.py:96`).

None of these gate the signal; all feed the Telegram card. They migrate with `deliver/`, not with the decision seam — but they must be typed at the same time, because `build.py`'s `interest_zones_text`/`_confluence_line` (`:347-353`) pass `row["market"]`/`row["maps"]` straight into `score_zone_confluence`.

---

## 5. Confluence indicators → features (the "talipp" flag) — read carefully

`confluence.py` hand-rolls, in numpy, `_rsi` (Wilder), `_ema`, `_macd_hist`, `_bb_width_pctile`, and a 2-swing `_divergence` (`:22-83`), called once per candidate at `orchestrator.py:1156` inside `_apply_confluence`, producing a bounded `[0.7,1.3]` multiplier that **feeds `strength`** (`:1182`).

**Finding that reframes the task:** `features/` **already** computes all four in canonical **pure-Polars** — `prepare.py` header states "Pure-Polars formulas are canonical for Wilder RSI/ATR/ADX, MACD, BB (ddof=0, population std — canonical Bollinger/TA-Lib)", and `polars_ta_bridge.py` exposes **series** accessors `rsi_series` (`:343`), `ema_series` (`:337`), `macd_series` (`:376`). So the honest architectural home for these indicators is the **existing Polars stack**, and the FeaturePanel should expose the per-TF **indicator series** (prizrak's `_divergence` needs the series over a lookback, not just the last scalar that `FeatureFrame` carries today — `feature_engine.py:53-63`).

**Two hard caveats — do NOT silently "move to features" as part of this data-source rewrite:**

1. **talipp is not a dependency** (`grep talipp pyproject.toml` → empty) and the codebase canon is Polars, not talipp. Introducing talipp would be a **new dep + a THIRD RSI/EMA/MACD implementation**, whose Wilder-seeding and MACD warmup differ subtly from both the Polars canon and confluence.py's numpy. Recommendation: bind to the **Polars series accessors already present**, not talipp.

2. **The window changes the numbers → this is a strategy change, not a data-source change.** confluence today computes on the **tier-sliced** frame (`ohlcv_by_tf[tf][-tier.lookback_bars:]`, e.g. 60 bars for meso — `orchestrator.py:2314`). It deliberately returns `ema200=None` when `len < 200` (`confluence.py:100`) and seeds Wilder RSI from the slice start. A FeaturePanel computed over the **full 1000-bar** frame yields a *different* EMA200 (a real number instead of `None`), a differently-seeded RSI, and a different BB-width percentile — which shifts the confluence multiplier and therefore every `strength` score. That violates "geometry UNCHANGED."

**Therefore, for the data-source rewrite: keep `confluence.py` LOCAL and unchanged.** It is already pure, offline-runnable, and row-free — it just needs `ohlcv`, which now comes from `view.klines_raw(tf)` sliced to `tier.lookback_bars` exactly as today. Fold the indicators into the Polars feature stack as a **separate, later, calibration-gated step** (measured on live data per CLAUDE.md — prizrak has no backtest), where the numeric divergence is validated before it ships. Flag it in the blueprint as future work; do not bundle it into the "just changed the data source" claim.

*(доп-factors marketcap/dominance stay put: `read_cached_series` (`marketcap_source.py:185`) and `read_cached_changes_24h` (`dominance_source.py:159`) are **cross-asset CoinGecko cache reads warmed off-process** — they are not futures-engine planes and do NOT belong on `MarketView`. The assembly reads them directly, exactly as `entry.py:52/60` does now.)*

---

## 6. Typed models to add (`hunt_core/prizrak/models.py`, module-1-owned)

The container swap (row → `PrizrakOutput`) is the easy 20%. The **real** name-lie surface is the **candidate dict itself** — `_base_summary` returns a `dict[str, Any]` with ~28 keys (`orchestrator.py:827-869`), enriched in `_apply_confluence` with `strength`, `fragility`, `trade_quality`, `confluence_drivers`, `confluence_score`, `confluence_evidence`, `htf_bias` (bare string — the deliberate dual-shape, `orchestrator.py:1231-1238`), `liq_conflict`, `liq_reconcile`, `marketcap`, `dominance` (`:1183-1238`), then `pattern` (`figures.py::tag_squeeze_pattern`, `orchestrator.py:1239/1930`) and `invalidation` (`build_invalidation`, `:1384/1500/1617/1737/1883`). `build.py::_render_candidate` reads ~30 of these via `summary.get(...)` (`:465-588`) — that is the exact spot where a formatter can read a key the orchestrator never wrote and fail silent.

```python
class PrizrakCandidate(_View):          # frozen, extra="forbid", strict
    action: Literal["long", "short", "wait"]
    entry_lo: float; entry_hi: float
    stop: float | None; stop_anchor: str | None; stop_buffer_pct: float | None   # I-6: real None
    tp_ladder: tuple[float, ...] = (); tp1: float | None = None; ...
    rr_primary: float | None; rr_conservative: float | None
    strength: float; fragility: float; trade_quality: str
    setup_kind: str; tf_tier: str; tf: str
    htf_bias: str | None = None                 # the SUMMARY shape = bare string (name is the schema)
    liq_conflict: bool = False
    confluence_drivers: tuple[Driver, ...] = ()
    invalidation: tuple[InvalidationCond, ...] = ()
    pattern: str | None = None
    # zone/poc/marketcap/dominance/liq_reconcile as typed sub-models

class PrizrakOutput(_View):
    symbol: str
    candidates: tuple[PrizrakCandidate, ...] = ()
    summary: PrizrakCandidate | None = None     # was row["prizrak_summary"] (max by strength)
    structure: PrizrakStructure                  # was row["prizrak_structure"]
    interest_zones: InterestZones                # was row["prizrak_interest_zones"]
    abstain: tuple[AbstainReason, ...] = ()      # was row["prizrak_abstain"]
    bias_liq_conflict: BiasLiqConflict | None = None   # was row["prizrak_bias_liq_conflict"]
```

Pragmatic path: have the orchestrator keep emitting its dicts internally (its 2450 lines of dict-building are the "kept" geometry), and validate at the seam — `PrizrakCandidate.model_validate(c)`. `extra="forbid"` on validate immediately surfaces any orphan key the orchestrator writes but no consumer reads, and mypy on the consumer side catches any phantom key a formatter reads. **Preserve the documented dual-shape** (`htf_bias` = dict in `structure`, bare string on the candidate — `orchestrator.py:1231-1238`, memory `htf-dual-shape`): model them as two *different* typed fields on two *different* models, which finally makes the "don't fix one into the other" rule a type, not a comment.

---

## 7. Consumer migration surface (who reads the `prizrak_*` keys)

18 files read `prizrak_summary`/`prizrak_signals`/`prizrak_structure`/`prizrak_interest_zones`/`prizrak_abstain`/`prizrak_bias_liq_conflict` — the full deliver+report+lifecycle set that must rebind to `PrizrakOutput`:

`prizrak/{build,format_telegram,liq_reconcile}.py`, `prizrak/engines/{calibration,delivery_policy,signal_queue}.py`, `deliver/{confluence_grid,templates}.py`, `runtime/{analyst_assembly,query_service,symbol_probe,telegram_commands}.py`, `signals/{__init__,lifecycle}.py`, `track/outcome_ledger.py`.

Per SPINE coexistence, these migrate in the **single PRIZRAK module swap** — the old `snapshot_symbol`+`ensure_prizrak_verdict` path keeps the not-yet-migrated MANIPULATIONS band running unchanged (separate timer, `_cycle_loop.py:364` vs `:456`). Note `signals/lifecycle.py` is the SPINE-flagged scaffolding hardcoded to `module=1` — it reads `row["prizrak_summary"]`; on the swap it reads `PrizrakOutput.summary`, and its module-1 hardcoding becomes honest (it only ever served prizrak).

---

## 8. Independence from MANIPULATIONS (invariant, verified)

- prizrak imports **nothing** from `scanner/` or `deliver/manipulation_delivery.py`; the only shared imports are `data.universe.PINNED_SYMBOLS` (a constant list) and `runtime.tick_state.deep_query_store` (`signal_queue.py:160/254`) — plumbing, not geometry.
- In the native design prizrak consumes `MarketView` + `FeaturePanel` + `MapBundle` and emits `PrizrakOutput`; the scanner consumes its own REST-tail input and emits `ManipulationSetup`. **No shared typed model, no shared dict.** `tests/test_module_boundary.py` holds by construction — there is nothing common left to leak.
- Geometry that must stay module-1-local and must NOT be pulled toward the scanner: the **structure-with-buffer stop** (`_structural_stop` `orchestrator.py:663`, wick-anchor стр.18, buffer 1–3% стр.33), the 4+-touch accumulation gate (`accumulation.py:104`), the HTF-bias veto (`_htf_gate` `orchestrator.py:1108`), and the RR-1:3 conservative sizing (`_rr_conservative` `:153`). None of these share code with the scanner's wide-stop-plus-добор geometry — keep it that way.

---

## 9. What ships byte-identical (the guarantee)

Because the orchestrator, all detectors, confluence.py, config.py, and the доп-factors are **kept**, and the only change is *where their inputs come from* (typed models instead of a dict) and *where their outputs go* (a typed return instead of a row mutation), the emitted candidates are numerically identical to today's — **provided** (a) `view.klines_raw(tf)` returns the same bars the current `fetch_ohlcv_list_cached`+`drop_unclosed_ohlcv_tail` path returns (closed-only, same depth), (b) the three liq keys map through `MapBundle` unchanged, and (c) confluence indicators stay on their tier-slice window (§5). Validate on live data (no backtest gates prizrak — CLAUDE.md), comparing `PrizrakOutput.summary` against the current `row["prizrak_summary"]` for the pinned universe across a session before deleting the old seam.

**Net:** the prizrak native rewrite is a ~150-line seam file (`assemble.py`) + a models file + a deliver rebind. The 2450-line decision engine is untouched. The row-dict — and its phantom-key/name-lie/falsy-zero surface — is deleted at the seam and replaced by `extra="forbid"` strict models on both the input (`MarketView`/`MapBundle`) and the output (`PrizrakOutput`/`PrizrakCandidate`) sides.

---

I have complete grounding. Returning the blueprint.

---

# NATIVE-REWRITE BLUEPRINT — `hunt_core/features/` (indicator/feature computation)

Binds to the SPINE's `MarketView`. Verified against the real engine (`engine/state.py`, `rest.py`, `orderflow.py`, `funding_stats.py`, `spot.py`) and the real features layer (`features/prepare.py:632`, `prepare_frame.py:996`, `polars_ta_bridge.py`, `snapshot.py`, `factors.py:70`, `shared.py`). File:line anchors throughout.

---

## A. What `features/` is today, and the two shapes it must split into

`features/` has one entry (`prepare_symbol`, `prepare.py:632`) but produces **two categorically different outputs** that the god-object `PreparedSymbol` (`domain/schemas.py:86`, ~150 fields) fuses into one:

1. **Pure indicator frames** — `_prepare_frame` (`prepare_frame.py:996`) computes RSI/MACD/EMA/BB/ATR/ADX/Stoch/OBV/VWAP/Supertrend/Keltner/Donchian/VP/CVD over each kline frame. **This is a pure `pl.DataFrame → pl.DataFrame` transform already.** It takes no market/positioning data.
2. **Market/positioning enrichment** — `apply_rest_enrichments_local` (`snapshot.py:633`), `_overlay_ws_market` (`:764`), `market_snapshot` (`:802`), `stamp_derivative_zscores` (`:199`). These read `pack`/`ws_snap`/`client.get_cached_*` and write funding/OI/LS/basis/taker/book/liq onto `PreparedSymbol` and into the `market` row-dict.

**The rewrite's core move: (2) is not `features/` at all anymore — it is exactly what the SPINE's `MarketView` already carries** (`view.derivs`/`view.orderflow`/`view.book`/`view.cross`/`view.spot`). So `features/` sheds its entire market-enrichment half. What remains is (1) plus the *derived summaries over frames* (`tf_snapshot`, `regime_snapshot`, `distribution_stats`, `build_factor_panel`, VP levels) — which become the typed **`FeaturePanel`** the SPINE names.

```
OLD:  SymbolFrames(+book) ─prepare_symbol─▶ PreparedSymbol(god-object: frames + 150 market fields)
NEW:  MarketView ─compute_features─▶ FeaturePanel(frames + typed summaries)   [pure]
      MarketView.derivs/orderflow/book/cross/spot                              [already the enrichment half]
```

`features/` becomes **pure `MarketView → FeaturePanel`**. No `client`, no `pack`, no `ws_snap`, no `get_cached_*`, no `PreparedSymbol`.

---

## B. Typed target — `FeaturePanel` (new, `hunt_core/features/models.py`)

Same Pydantic-v2 discipline as `MarketView` (`frozen`, `extra="forbid"`, `strict`, `arbitrary_types_allowed` for `pl.DataFrame`). Raw frames live in a typed holder; every derived summary is a typed sub-model with **all-`Optional` fields** so a warm-up `None` is representable and a name-lie is a mypy error.

```python
class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True,
                              arbitrary_types_allowed=True)

class Frames(_Model):                 # prepared indicator frames, closed-only; None = plane not_ready
    m1: pl.DataFrame | None = None
    m5: pl.DataFrame | None = None
    m15: pl.DataFrame | None = None
    h1: pl.DataFrame | None = None
    h4: pl.DataFrame | None = None
    d1: pl.DataFrame | None = None
    w1: pl.DataFrame | None = None
    def require(self, tf: str) -> pl.DataFrame: ...   # raises NotReady, mirrors MarketView.klines.require

class TfSummary(_Model):              # was tf_snapshot(...) dict (snapshot.py:1197) — now typed, all-Optional
    close: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    adx14: float | None = None
    ema20: float | None = None; ema50: float | None = None; ema200: float | None = None
    macd_hist: float | None = None
    vol_ratio: float | None = None
    stoch_k: float | None = None
    supertrend_dir: int | None = None
    plus_di: float | None = None; minus_di: float | None = None
    bb_pct_b: float | None = None; bb_width_pctile: float | None = None
    squeeze_on: bool | None = None
    donchian_high20: float | None = None; donchian_low20: float | None = None
    session_cvd: float | None = None; rolling_cvd_24h: float | None = None   # None, NOT 0.0 (see §F)
    trend: str | None = None
    candle: CandleShape | None = None
    bearish_rsi_div: bool | None = None; bullish_rsi_div: bool | None = None
    bearish_macd_div: bool | None = None; bullish_macd_div: bool | None = None
    return_zscore: float | None = None; return_skew: float | None = None; return_kurt: float | None = None
    close_time_ms: int | None = None
    # ... one field per key tf_snapshot emits; nothing else can be read off it

class VolumeProfile(_Model):          # was poc_/vah_/val_ scalar fields on PreparedSymbol
    poc: float | None = None; vah: float | None = None; val: float | None = None
    poc_direction: str | None = None

class Regime(_Model):                 # was regime_snapshot + symbol_regime_features
    market_regime: str | None = None
    bias_4h: str | None = None; bias_1h: str | None = None
    structure_1h: str | None = None
    regime_4h_confirmed: str | None = None; regime_1h_confirmed: str | None = None
    return_entropy_50: float | None = None; volume_regime_break: bool | None = None

class FactorPanel(_Model):            # was build_factor_panel (factors.py:70) — but reads MarketView, not row
    momentum_rsi15: float | None = None
    trend_adx1h: float | None = None
    flow_taker: float | None = None
    deriv_oi_z: float | None = None
    deriv_funding: float | None = None
    flow_cmf15: float | None = None

class FeaturePanel(_Model):
    symbol: str
    now_ms: int
    frames: Frames
    tf: Mapping[str, TfSummary]           # {"15m": ..., "1h": ..., "4h": ...} — key set fixed at build
    vp: Mapping[str, VolumeProfile]       # {"1h": ..., "15m": ...}
    regime: Regime
    factors: FactorPanel
    not_ready: tuple[str, ...] = ()       # carried from MarketView for gating
```

**Boundary note (CLAUDE.md / `test_module_boundary.py`):** `FeaturePanel` is consumed by **PRIZRAK** (`assemble_prizrak(view, panel)`) and by **maps/**. The **scanner** (MANIPULATIONS) does **not** consume it — it builds its own lean input over the REST tail. `FeaturePanel` therefore never crosses the two-strategy seam; the boundary holds by construction because there is no shared dict.

### The frame seam — engine `Bar` (6-col) → Polars frame (**the single most important structural finding**)

The engine's OHLCV plane is `Bar = list[float]  # [open_ms, open, high, low, close, volume]` (`state.py:23`), because ccxt's `fetch_ohlcv` normalizes to 6 columns (`rest.py:44`). **`taker_buy_base_volume`, `quote_volume`, `num_trades` do NOT survive** — grep-confirmed absent from all of `engine/`. The old REST path (`data/collect.py`) fetched Binance klines *with* those extended columns; the engine does not. Consequence for `features/` (detailed in §F): every per-bar column derived from `taker_buy_base_volume` — `session_cvd`, `rolling_cvd_24h`, `delta_ratio` (`prepare_frame.py:244` `_bar_delta_expr`) — has no input, and the current code fabricates `pl.lit(0.0)` (`:255,:258,:279`). That is a fresh I-6 violation the rewrite must convert to fail-loud `None`.

New pure builder (`features/frame.py`):
```python
def frame_from_bars(bars: list[Bar] | None) -> pl.DataFrame | None:
    """Engine 6-col Bar list -> OHLCV DataFrame with a temporal `open_time`. None if plane absent.

    Fail-loud: None (not an empty frame) when the plane is not_ready — mirrors MarketSnapshot.optional.
    open_time is built pure-Polars from open_ms (pl.from_epoch(ms)) for VWAP-session + CVD-session logic.
    NO taker_buy_base_volume / quote_volume / num_trades column is fabricated — they are genuinely absent.
    """
```

---

## C. Input mapping — every current input → engine plane/helper

| Today (`features/`, `snapshot.py`, `tick_assembly.py`) | Native engine source (on `MarketView` unless noted) |
|---|---|
| `SymbolFrames.df_{5m,15m,1h,4h}` (REST/frame_cache) | `view.klines.{m5,m15,h1,h4,d1,w1}` — already closed-only, WS-merged (`state.merge_frame`) |
| `pack["oi"]` / `client.get_cached_open_interest` (`snapshot.py:644`) | `view.derivs.oi` (plane `oi`) |
| `pack["oi_series"]` / `get_cached_oi_series` (`:646`,`:217`) | `rest.fetch_futures_data_series(fapiDataGetOpenInterestHist, "sumOpenInterest")`; latest = plane `oi_hist_5m` |
| `pack["oi_chg_5m"]`, `oi_chg_1h`, `oi_slope_5m`, `oi_z` (`market_snapshot :855-859`) | derived **pure** over the OI-hist series (`_series_z`/`_series_chg_pct`/`_series_ols_slope`, `snapshot.py:162-198`) — move into `features/` |
| `pack["gls_series"]` / `get_cached_gls_series`; `gls_z` (`:860`,`:234`) | `rest.fetch_futures_data_series(fapiDataGetGlobalLongShortAccountRatio, "longShortRatio")` |
| `pack["ls_5m"]`,`global_ls_5m`,`top_ls_5m` / `get_cached_*_ls_ratio` (`:652-671`) | planes `global_ls_5m`, `top_ls_acct_5m`, `top_ls_pos_5m` → `view.derivs.*` |
| `pack["taker_5m/15m/1h"]` / `get_cached_taker_ratio` (`:678`) | plane `taker_5m` → `view.derivs.taker_5m` |
| `pack["funding"]` / `get_cached_funding_rate` (`:682`) | plane `funding` → `view.derivs.funding` |
| `get_cached_funding_trend` / `_zscore` / `_recent_extreme` (`:686-693`) | `funding_stats.{funding_trend,funding_zscore,funding_recent_extreme}` over `rest.fetch_funding_history` → `view.derivs.funding_trend/funding_zscore` |
| `pack["basis_5m"]` / `get_cached_basis_stats` incl. `premium_zscore_5m`/`premium_slope_5m` (`:694-707`,`:308`) | plane `basis` → `view.derivs.basis`; premium z/slope = **pure** `_series_z`/`_series_ols_slope` over `rest.fetch_futures_data_series(fapiDataGetBasis, "basis")` |
| `premium_row` mark/index/settle (`:709-729`) | plane `mark` → `view.derivs.mark`; index via `_index_of` / `rest.fetch_ohlcv_series(price="index")` (see gap flag) |
| `pack["book_depth"]` / `_book_from_pack` (`snapshot.py:407`) + `depth_imbalance_from_book`/`microprice_bias_from_book` (imported from `market/client.py:2222/2235` in `prepare.py:24`) | plane `book` (read-through ccxt orderbook) + **`toolkit/book_math`** (E5b, task #23) → `view.book.{depth_imbalance,microprice_bias,bid,ask,...}` |
| `pack["agg_trades"]` / `fetch_agg_trade_snapshot`; `agg_trade_delta_*` | plane `trades` + `orderflow.taker_flow(...)` → `view.orderflow.{cvd_1m,cvd_5m,buy_ratio_30s,buy_ratio_60s}` |
| `ws_snap["ws_cvd_5m"]`, `agg_trade_delta_30s`, `agg_trade_buy_ratio_{30,60}s` (`:768-799`) | `orderflow.taker_flow(view trades, window_ms=…)` — **same math, one source, no REST/WS reconciliation** |
| `ws_snap["ws_price_chg_1m"]` (`tick_assembly.py:962`) | `orderflow.price_change_pct(trades, window_ms=60_000)` → `view.orderflow.price_chg_1m` |
| `ws_snap["live_depth_imbalance"]`,`live_microprice_bias` (`:786-793`) | `book_math` over plane `book` → `view.book.*` (WS-vs-REST book merge is gone; one live book) |
| `ws_snap["mark_live"]`,`basis_bps_live`,`funding_live`,`live_index_price` (`:772-781`,`:252-270`) | planes `mark`/`basis`/`funding` → `view.derivs.*` |
| `spot_companion.enrichments_for` / `spot_extra` (`tick_assembly.py:722`) | `SpotEngine.spot_enrichments(sym, futures_mid=)` (`spot.py:148`) → `view.spot.*` |
| `ticker["quote_volume"]` → `vol_24h_m` (`market_snapshot :885`); `ticker["trade_count"]` | plane `ticker` — **gap flag (§I): not on `MarketView` today** |
| `attach_cross_market_fields` (cross-exchange) | `MultiEngine.cross_{funding,open_interest,long_short,liquidation_notional}` → `view.cross.*` |
| `store.get_cached_oi_bars` (maps consumer) | `rest.fetch_futures_data_series` OI-hist (belongs to maps/, not features/) |

**Every `X or client.get_cached_Y` fallback chain (`snapshot.py:644-671`) collapses to one engine source.** The falsy-zero fix the code already fought for (`snapshot.py:675-685`: "is-None fallthrough, NOT `or`, a funding rate of exactly 0.0 is REAL") becomes structural — there is no second cache to fall through to, so the `or`-chain idiom cannot be written.

---

## D. Pure Polars over kline frames vs. talipp streaming (library-adoption #8)

### D.1 Stays PURE Polars over closed kline frames (unchanged internals, retyped I/O)

`_prepare_frame` (`prepare_frame.py:996`) and everything it calls stays a `pl.DataFrame → pl.DataFrame` transform — it is already pure, already closed-only, already the batch/recompute path talipp #8 says to leave alone:

- **VWAP + bands + deviation** (`:1029-1136`) — session-aware, needs the temporal `open_time`.
- **Donchian, volume_mean/ratio20** (`:1093-1104`).
- **ADX/DI** — `adx_from_polars_ta` (`polars_ta_bridge.py:137`) is **already hand-rolled pure-Polars Wilder** (replaced the broken TDX backend, verified rtol<1e-9). **Keep as-is** — do not hand this to talipp; it is the reliability *standard*, not a candidate.
- **Supertrend** (`shared.py:88`), **`wilder_mean`** (`shared.py:40`), **HMA** (`prepare_columns.py:329`), **ichimoku** (`:293`).
- **`_add_advanced_indicators` groups** (`prepare_frame.py:663-901`): BB-width/keltner/squeeze, OBV-ema, stoch, oscillators, chandelier, zscore, pivots.
- **Volume Profile** (`_volume_profile` `:919`, `_volume_profile_with_direction`) — **explicitly stays hand-rolled** (#8: "leave volume-profile hand-rolled"; it is a domain histogram, not a streaming indicator).
- **CVD / taker-flow columns** (`add_session_cvd :273`, `add_rolling_cvd_24h :252`) — stay hand-rolled per #8, **but change their fail-loud behavior** (§F).
- **Candle patterns, distribution_stats** (`snapshot.py:365`), **OLS/tail metrics, microstructure** (book-derived, last-bar-only).

### D.2 Becomes talipp streaming (`add()`-on-closed-bar), gated

Per #8, migrate **only** streaming RSI/MACD/EMA/SMA/Bollinger — currently `plta.RSI/MACD/EMA/BBANDS/ATR` (`polars_ta_bridge.py:337-439`). talipp's model *is* the engine's model: stateful per-(symbol,tf), O(1) per closed bar, returns explicit `None` during warm-up.

New streaming holder (`features/streaming.py`), one per tracked (symbol, tf), fed by the engine's newly-closed WS bar:
```python
class StreamingIndicators:
    """Per-(symbol,tf) talipp state. add() ONLY on closed bars → structurally cannot see the
    forming candle (I-5). snapshot() returns None-bearing readings during warm-up (I-6)."""
    def add_closed_bar(self, bar: Bar) -> None: ...      # bar = engine 6-col closed Bar
    def snapshot(self) -> IndicatorReadings | None: ...  # None until warm; each field None during warmup
```

**Mandatory gating before this migration ships (#8 hardening order, non-negotiable):**
1. **TA-Lib 0.7.0 golden-reference fixture** (test-only extra, never in `engine/`) pinning RSI/MACD/EMA/BB on a fixed OHLCV fixture.
2. **Hypothesis metamorphic property**: `talipp.add()`-incremental == `_prepare_frame` full-recompute at the tail (rtol bound); and appending a *forming* candle must not change any reading.
3. Only then flip the 5 indicators. The pure-Polars `_prepare_frame` **stays** as the backtest/recompute path and the equivalence oracle — talipp is the live hot path, not a replacement of the batch path.

**Do NOT talipp-migrate:** ADX/DI (already pure Wilder), VP, CVD/taker-flow, Supertrend, or anything reading multiple columns / book data.

---

## E. New function signatures (against typed `MarketView`)

```python
# hunt_core/features/frame.py  (new, pure)
def frame_from_bars(bars: list[Bar] | None) -> pl.DataFrame | None: ...

# hunt_core/features/prepare_frame.py  (kept; input now the closed engine frame, groups unchanged)
def prepare_frame(df: pl.DataFrame, *, groups: frozenset[str] | None = None) -> pl.DataFrame: ...
#   (was _prepare_frame :996 — same body; the `warmup_ema` trim + all indicator batches unchanged)

# hunt_core/features/summary.py  (was snapshot.py tf_snapshot/regime/distribution — output now typed)
def tf_summary(df: pl.DataFrame | None) -> TfSummary | None: ...          # None when frame absent
def regime_of(frames: Frames) -> Regime: ...
def volume_profile_of(df: pl.DataFrame | None, *, lookback: int, buckets: int) -> VolumeProfile: ...

# hunt_core/features/factors.py  (retyped: reads MarketView + TfSummary, NOT a row-dict)
def build_factor_panel(view: MarketView, tf15: TfSummary | None, tf1h: TfSummary | None) -> FactorPanel: ...
#   momentum_rsi15 <- tf15.rsi14; trend_adx1h <- tf1h.adx14; flow_taker <- view.derivs.taker_5m;
#   deriv_oi_z <- (pure z over OI-hist series); deriv_funding <- view.derivs.funding; flow_cmf15 <- tf15.cmf20

# hunt_core/features/build.py  (new — the entry that replaces prepare_symbol :632)
def compute_features(view: MarketView, *, groups: frozenset[str] | None = None) -> FeaturePanel: ...
```

`compute_features` body (sketch): `frames = Frames(m15=prepare_frame(frame_from_bars(...view via builder...)))` — in practice the builder passes the raw engine bars; `compute_features` calls `frame_from_bars` + `prepare_frame` per tf, then `tf_summary`, `regime_of`, `volume_profile_of`, `build_factor_panel`. `groups` reuses `resolve_prepare_groups_for_symbol` (`prepare_columns.py:176`) verbatim — the pinned-vs-lean group logic is orthogonal to the engine and survives.

**Deleted:** `prepare_symbol` (god-object assembly), `apply_rest_enrichments_local`, `_overlay_ws_market`, `market_snapshot`, `stamp_derivative_zscores`, `_book_from_pack`, `merge_ws_kline_closed`, `SymbolFrames`, the `client`/`pack`/`ws_snap` parameters everywhere, and the ~130 market/positioning fields on `PreparedSymbol` (they are now `view.derivs/orderflow/book/cross/spot`).

---

## F. Fail-loud rules (I-6)

1. **Absent plane → `None`, never empty frame.** `frame_from_bars(None) → None`; `Frames.m4h is None` iff `view.klines.h4 is None` iff the engine proved `kline.4h` not-ready. Presence ⟺ proven-fresh, inherited from `MarketSnapshot.optional` (`state.py:167`).

2. **⚠ CVD / taker columns must go `None`, not `0.0` (new fix).** `_bar_delta_expr` (`prepare_frame.py:244`) needs `taker_buy_base_volume`, which the engine's 6-col Bar does not carry. Today the absent-input branch fabricates `pl.lit(0.0)` for `session_cvd`/`rolling_cvd_24h` (`:255,:258,:279`) — a 0.0 that reads as "perfectly balanced flow." **Under the engine that branch is now the ONLY branch**, so it must become fail-loud: emit a null column (or omit it) → `TfSummary.session_cvd = None`. If per-bar CVD history is wanted back, it must come from the `trades` plane via `orderflow.taker_flow` (live only, no deep history) — a deliberate capability change to surface, not paper over with 0.0.

3. **Warm-up readings are `None`.** talipp returns `None` before its period fills → `TfSummary.rsi14 = None`, never `50.0`. Note the current code's defaults (`_col(df, "rsi14", 50, ...)`, `prepare_frame.py`/`snapshot.py:1279`) inject a fabricated 50/0.5/1.0 — those `default=` fills are dropped; the typed field stays `None`.

4. **Derived stats gate on min-N.** funding z/trend already `None` below `min_records` (`funding_stats.py:65,89`); premium/OI z/slope keep `_series_z`/`_series_ols_slope`'s `min_n` guards (`snapshot.py:184`). σ≤1e-12 → explicit `0.0` reading (a real value), never NaN — keep the guard, do **not** adopt `scipy.stats.zscore` (#8 hazard).

5. **No `or`-fallback chains.** One engine source per datum; the falsy-zero class (`float(x) or None`, `X or get_cached_Y`) is unwritable because there is no second source and no `.get()` on a frozen model.

6. **`extra="forbid"` on every sub-model** → an orphan/mistyped field is a construction error; `vulture` (pre-commit) catches a declared-but-unread field.

---

## G. Closed-bar / no-lookahead (I-5) handling

**The engine discharges I-5 upstream, so most of the current closed-bar machinery deletes:**

- Engine frames are **closed-only by construction**: `seed_ohlcv` drops the forming bar (`rest.py:60`), `freshness.closed_bars` = `cache[:-1]` (`freshness.py:15`), `merge_frame` appends only newly-closed WS bars (`state.py:118`). So `frame_from_bars` receives closed-only bars and **`idx=-1` IS the newest closed bar** (matches memory `closed-bar-convention-off-by-one` and the long comment at `snapshot.py:1208-1225`).
- **DELETE the `closed` shim in `tf_snapshot`** (`snapshot.py:1200,1235,1373`) and `_bar_close_time_ms(closed=)` (`:1048`) and the `…_closed` twin blocks — there is no forming bar to shift past, so the `-2 if closed` off-by-one family cannot exist. `tf_summary` reads `idx=-1` only.
- **DELETE `merge_ws_kline_closed`** (`snapshot.py:973`, called 4× at `tick_assembly.py:667-670`) — the engine's push-state store already merges closed WS bars into the seeded frame; there is no separate WS-closed overlay to reconcile.
- **talipp `add()` on closed bars only** — structurally cannot observe the forming candle (#8), which is a stronger I-5 guarantee than the vectorized path.
- Keep the Hypothesis property (#3/#8): "appending a forming candle changes no reading" — the mechanized proof of I-5 for both the pure and streaming paths.

`no-lookahead-reviewer` should run on this rewrite (per CLAUDE.md subagents).

---

## H. Coexistence & module boundary

- **Boundary preserved (`test_module_boundary.py`):** `features/ → FeaturePanel` is read by **PRIZRAK** and **maps/**, never by the **scanner** (which builds its own lean REST-tail input over `engine.exchange`). No shared dict crosses the seam.
- **Phased landing:** rewrite `features/` behind `compute_features` while the not-yet-migrated consumer keeps the old `prepare_symbol` path for one or two sessions — the two run side by side at the orchestrator (`_cycle_loop.py`), never via a `FeaturePanel.to_legacy_row()` bridge. `resolve_prepare_groups_for_symbol` and the whole pinned/lean group system carry over unchanged.

---

## I. Gaps / flags to resolve during implementation

1. **Extended kline columns are gone (biggest).** `taker_buy_base_volume`/`quote_volume`/`num_trades` do not survive ccxt's 6-col OHLCV. Impacts: per-bar CVD/`delta_ratio` (→ `None`, §F.2), and `delta_ratio`-fed microstructure. Decide explicitly: live-only CVD from `trades` plane, or accept `None`. Do **not** restore via a bespoke Binance-raw-kline fetch — that re-imports the old transport.
2. **Futures 24h ticker `quote_volume`/`trade_count`** (`market_snapshot :885-886`, `vol_24h_m`) has no field on `MarketView` today — only the `ticker` plane exists. Add a `view.derivs.quote_volume_24h`/`view.ticker` field, or read the plane in the builder. (Scanner uses `rest.fetch_all_tickers` for the universe; the tracked view needs the per-symbol value.)
3. **`Derivs.index`** has no dedicated engine plane (tracked planes: `mark,funding,oi,basis,taker_5m,global_ls_5m,top_ls_*,oi_hist_5m,ticker,book,trades,liq,kline.*`). Derive from `rest.fetch_ohlcv_series(price="index")` latest close or leave `None`; the SPINE's `_index_of(snap)` is a placeholder.
4. **`toolkit/book_math` (E5b, task #23) is a hard prerequisite** — `prepare.py:24-31` still imports `depth_imbalance_from_book`/`microprice_bias_from_book`/`detect_wall_clusters`/`depth_imbalance_by_zone` from `market/client.py:2222-2350`. These must land in `toolkit/book_math` before `features/` can drop its `market/client` import.
5. **`scipy` optional dep** in `distribution_stats` (`snapshot.py:349`) — keep the pure-Python fallback (`:354-362`); do not make scipy load-bearing.

**One-line contract:** `features/` becomes a pure `MarketView → FeaturePanel` transform — `frame_from_bars` + the unchanged pure-Polars `prepare_frame` (with ADX/Supertrend/VP hand-rolled and RSI/MACD/EMA/SMA/BB migrating to gated talipp `add()`-on-closed-bar), feeding typed `TfSummary`/`Regime`/`VolumeProfile`/`FactorPanel` where every warm-up or absent datum is `None` (the fabricated-0.0 CVD becoming the headline fail-loud fix), all closed-bar handling collapsing because the engine already guarantees I-5.