# ADR 0004 — Native module rewrite: typed MarketView replaces the row-dict

Status: DESIGN (spine complete; per-module blueprints pending — the design workflow's blueprint+
synthesis agents were cut off by a session usage limit, re-run wf_15b94cdd-525 to finish them).

Architecture LAW (memory engine-native-modules-from-scratch): engine stays NATIVE on documented libs;
modules are rebuilt FROM SCRATCH on the engine's native contract (NOT the deprecated engine_adapters/).
This ADR is the SPINE the per-module rewrites bind to. The headline decision: the untyped row-dict —
direct host of the phantom-key / falsy-zero / name-lie defect family — is replaced by a frozen, strict,
extra='forbid' Pydantic v2 `MarketView` built from `engine.snapshot()`, where **a field is non-None iff
the engine proved it fresh** (presence ⟺ proven-fresh, collapsing I-6 into the type).

---

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