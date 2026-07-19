I now have a complete picture. Writing the exhaustive migration spec as my return value.

---

# ADR-0003 Cutover — Migration SPEC for the two remaining pieces

**Scope:** `EngineStreams` (drop-in for `HuntCcxtStreams`) + runtime wiring of `EngineMarketPlane`. Analysis only — no code edited. All paths absolute. Applied by hand next.

---

## PART 0 — Load-bearing findings that shape both pieces (read first)

These are architectural facts discovered during recon that the spec below depends on. Each is a real gap, not a detail.

**0.1 — There is NO `EngineClient` facade class yet.** Only two mixins exist:
- `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/engine_adapters/client_market.py:46` → `_EngineClientMarketMixin` (needs `self._engine`)
- `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/engine_adapters/client_derivs.py:88` → `_EngineClientDerivsMixin` (needs `self._engine` **and** `self._multi`)

`engine_adapters/__init__.py` exports only `EngineSpot`. So exposing `.client` (PIECE 2) requires **composing** these two mixins into one `EngineClient` class that owns `__init__(self, engine, multi)`. Specified in §2.6.

**0.2 — The Engine has NO dynamic symbol (re)subscription.** `Ingest.start(symbols, timeframes)` (`/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/engine/ingest.py:62`) spawns one task per `(symbol, stream)` over a **fixed** list captured at construction; `Ingest.reconnect()` (:81) respawns the **same** `self._symbols`. There is no `add_symbol`/`remove_symbol`. `HuntCcxtStreams.set_symbols()` (`streams.py:322`), by contrast, rotates a live WS universe up to `_MAX_SYMBOL_STREAMS=100` **every tick** (`_cycle_loop.py:798`). **These are incompatible.** The hybrid model the client adapters already assume (`client_market.py:11-16` docstring) is the resolution: the engine keeps a **fixed warm WS set**; the dynamic scanner tail is served **on-demand via REST through `EngineClient`**. Consequence: **`EngineStreams.set_symbols()` becomes a no-op** and the warm universe is frozen at plane-construction time. This is the single biggest behavioral change and the #1 risk — see §1.4 and §2.2.

**0.3 — `MultiEngine` already owns the primary `Engine` privately.** `multi.py:53` `self._primary = Engine(symbols, timeframes)`. Building a **second** standalone `Engine` would double every Binance WS subscription from one IP → guaranteed 1006/ban churn (the exact failure `streams.py:390-408` fights). Therefore build **one** `MultiEngine` and expose its primary; do **not** construct a separate `Engine`. Requires adding `MultiEngine.primary` (§2.3). The task phrasing "Engine + MultiEngine + SpotEngine" resolves to "**one MultiEngine (which contains the Engine) + one SpotEngine**".

**0.4 — `client.fetch_status()` is called at `_cycle_loop.py:341`** but ADR-0003 §37 lists `fetch_status` as **dead — drop**, and it is absent from both client mixins. The health check will `AttributeError`. Must be handled (§2.5).

**0.5 — Two shape bridges are still ⬜** (ADR-0003 row 19 "shape bridge ⬜"):
- `live_funding_cross` old shape = `{venue: {"fundingRate","markPrice","indexPrice","ts_ms"}}`, secondaries **only** (no `binance` key). `MultiEngine.cross_funding(symbol)` returns `{venue: rate|None}` **including** `binance`. Bridge in §1.3.
- `liquidation_buffers()` old shape = `{venue: deque[(ts_ms, sym, side, qty, price)]}`; engine liq read-through yields ccxt liq **dicts**. Bridge in §1.3, and it requires the shared `maps/` parsers.

---

## PIECE 1 — `EngineStreams`

**New file:** `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/engine_adapters/streams.py`
**Class:** `EngineStreams` — drop-in for `hunt_core.market.streams.HuntCcxtStreams`.
**Constructor:** `EngineStreams(engine: Engine, multi: MultiEngine)` — `engine` is `multi.primary` (single-venue planes: book/trades/liq/mark/funding/ticker/kline); `multi` is for `live_funding_cross`. Holds no state of its own beyond these two handles + a stable `now_ms()`.

### 1.1 — Consumed public surface (the ONLY methods any consumer calls)

Exhaustive, from `grep -rhoE "ws_feed\.[a-z_]+"` + `.streams.` + `live_price.py`. **Everything else on `HuntCcxtStreams` is internal and must NOT be reproduced.**

| Method | Call sites (file:line) | Engine-backed implementation | Fail-loud rule |
|---|---|---|---|
| `snapshot(symbol)` | `tick_assembly.py:682`; `live_price.py:105` | Build the **consumed-only** dict in §1.2 (NOT the full 40-field dict). | Untracked symbol → return dict with all live fields `None` + `ws_connected=False`. Never fabricate. |
| `set_symbols(symbols, *, priority=None)` | `_cycle_loop.py:352,798` | **No-op** (warm set fixed at construction, §0.2). Log once at debug. | n/a (no data). |
| `start()` | `_cycle_loop.py:353` | **No-op** — `MultiEngine.start()` is awaited in the plane factory (§2.2). Kept for drop-in. | n/a. |
| `stop()` | `factory.py:630` (via `plane.aclose`) | **No-op** — teardown is `EngineMarketPlane.aclose` → `multi.close()`. | n/a. |
| `kline_ws_enabled` (property) | `_cycle_loop.py:803,810`; `features/snapshot.py:988` (via `merge_ws_kline_closed`) | `return True` (engine always streams `1m/5m/15m/4h`). | n/a. |
| `live_ticker(symbol, *, max_age_s=None)` | `_impl.py:54`; `live_price.py:82` | `engine.snapshot(sym, ("ticker",)).optional("ticker")` → reshape to `{"last":…, "quoteVolume":…, "percentage":…, "high":…, "low":…, "ts_ms":…}`. `optional` already age-gates (`FRESH_TICKER_S`); apply extra `max_age_s` against plane stamp. | Absent/stale plane → `None`. `last<=0` handled by caller. |
| `live_book(symbol)` | `tick_assembly.py:968` | `engine.snapshot(sym,("book",)).optional("book")` → `{"bids","asks","timestamp"}`; reshape to old book dict (`bid,ask,bid_qty,ask_qty,bids,asks,depth_imbalance,ws_depth_imbalance,microprice_bias,ts_ms`) via book-math (§1.5). | Absent book → `None`. |
| `live_bbo(symbol, *, max_age_s=None)` | `live_price.py:88` | Top-of-book from the `book` plane: `{"bid","ask","spread_pct"}`. | Absent/stale → `None`; missing bid or ask → `None`. |
| `live_funding(symbol, *, max_age_s=None)` | `live_price.py:99` | From `mark`+`funding` planes → `{"markPrice","indexPrice","fundingRate","ts_ms"}`. | Absent/stale mark → `None`. |
| `live_funding_cross(symbol, *, max_age_s=900)` | `features/snapshot.py:439`; `_cycle_tick.py:364` | Bridge `multi.cross_funding(symbol)` → `{venue:{"fundingRate":rate}}`, **drop `binance`**, drop `None`. §1.3. | No fresh secondary → `{}`. |
| `trade_buffer(symbol)` | `tick_assembly.py:969` | Convert `engine.snapshot(sym,("trades",)).optional("trades")` (ccxt trade dicts) → `deque[_AggPoint]`. §1.5 (needs `maps/`). | Absent → empty `deque()` (matches old). |
| `liquidation_buffers()` | `tick_assembly.py:970` | `{"binance": deque[...], <secondary>: deque[...]}` built from primary `liq` read-through + `multi.cross_liquidations`. §1.3 (needs `maps/` parsers). | No events → `{"binance": deque()}`. |
| `closed_kline_overlay(symbol, *, interval="1m")` | `features/snapshot.py:988` | Newest **closed** bar of `engine` frame `kline.<interval>` → reproduce `_bar_overlay` dict (`streams.py:838-860`): `close, closed_bar, ws_open_ms, ws_interval, candle{...}`. | Frame absent/empty → `None`. Interval the engine doesn't seed → `None`. |

Note `set_symbols` also references `PINNED_SYMBOLS` and `gate_symbol_list` internally in the old code (`streams.py:322-342`); as a no-op the adapter needs neither.

### 1.2 — `snapshot()` — EXACTLY the consumed output fields

The old `snapshot()` (`streams.py:679-770`) emits ~40 keys and **builds a full liquidation heatmap** (`build_liquidation_map` + `heatmap_to_market_dict`, `streams.py:696-718`). **Recon result: NONE of the heatmap/`liq_map` fields are read from `ws_snap` by any consumer.** The liquidation *maps* machinery is reached through a **different** path — `build_map_bundle(..., liq_buffers=ws_feed.liquidation_buffers())` at `tick_assembly.py:970` — never through `snapshot()`. **Therefore `EngineStreams.snapshot()` must NOT call `build_liquidation_map`/`heatmap_to_market_dict` at all.** That is the single biggest simplification in this piece.

Consumed fields, proven by grepping every `ws_snap[...]`/`ws_snap.get(...)`/`snap.get(...)` reader (`_overlay_ws_market` `features/snapshot.py:764-799`; `_patch_market_live` `tick_assembly.py:170-206`; `build_map_bundle` call `tick_assembly.py:961-994`; `resolve_live_price` `live_price.py:105-112`; `stamp_market_freshness` `completeness.py:905-912`):

| Field | Reader(s) | Engine source | Fail-loud |
|---|---|---|---|
| `ws_connected` | `features/snapshot.py:787,791`; `tick_assembly.py:176,180` (gate for depth/microprice overlay) | `True` iff symbol tracked **and** its `book` plane is fresh (`plane_ages(sym).get("book") <= FRESH_DEPTH_S`). | Untracked/stale → `False` (overlay simply skipped). |
| `agg_trade_delta_30s` | `features/snapshot.py:768` | `taker_flow(trades, window_ms=30_000, now_ms).["buy_ratio"]` (old "delta" = buy **share**, `streams.py:741`). | `None` when `count==0`. |
| `agg_trade_source` | `features/snapshot.py:771` | Constant string, e.g. `"engine_taker_flow"`. | n/a. |
| `agg_trade_buy_ratio_60s` / `_30s` | `features/snapshot.py:794-795` | `taker_flow(trades, window_ms=60_000/30_000, now_ms)["buy_ratio"]`. | `None` when no trades. |
| `agg_trade_delta_60s` / `_30s` | `tick_assembly.py:187-188` (hot-carry copy) | Same as buy-ratio (legacy alias, `streams.py:741-742`). | `None`. |
| `funding_live` | `features/snapshot.py:772` | `funding` plane scalar (float). | `None` if plane absent/stale. **Keep a genuine `0.0`.** |
| `mark_live` | `features/snapshot.py:774`; `tick_assembly.py:194` | `markPrice` from `mark` plane. | `None` if mark plane absent/stale. |
| `basis_bps_live` | `features/snapshot.py:776`; `tick_assembly.py:193` | `(mark-index)/index*10_000` from `mark` plane. | `None` if `index<=0` (never fabricate). |
| `live_depth_imbalance` | `features/snapshot.py:786`; `tick_assembly.py:175` | `depth_imbalance_from_book(...)` over `book` plane L1 (§1.5). | `None` if book absent. |
| `live_microprice_bias` | `features/snapshot.py:790`; `tick_assembly.py:179` | `microprice_bias_from_book(...)` over `book` plane L1 (§1.5). | `None` if book absent. |
| `ws_cvd_1m` / `ws_cvd_5m` | `tick_assembly.py:189-190`; `tick_assembly.py:994` (`ws_cvd=ws_snap["ws_cvd_5m"]` into map bundle) | `taker_flow(trades, window_ms=60_000/300_000, now_ms)["delta"]`. **⚠ UNIT CHANGE:** engine delta is **USDT notional**; old `ws_cvd` was base/NQ **qty** (`streams.py:555-570`). Sign is preserved (what the map divergence uses); magnitude scale differs → recalibrate any magnitude threshold. | `None` when no trades. |
| `ws_price_chg_1m` / `ws_price_chg_5m` | `tick_assembly.py:191-192`; `tick_assembly.py:962,987` (into map bundle `price_change_pct`) | `price_change_pct(trades, window_ms=60_000/300_000, now_ms) * 100.0` (engine returns **fraction**; old was **percent**, `streams.py:588` → ×100). | `None` if `<2` priced trades. |
| `liquidation_score_5m` | `tick_assembly.py:184` (parsed via `parse_liquidation_score`) | `liquidation_notional(primary_liq_events_windowed_300s)` → `short/total`, rounded 4. | `None` if `total<=0`. |
| `liquidation_long_notional_5m` / `liquidation_short_notional_5m` | `tick_assembly.py:185-186` | Same call → `long` / `short`. | `None`/omit if no events. |
| `live_mark_price` | `resolve_live_price` `live_price.py:107`; `tick_assembly.py:195` | `markPrice` from `mark` plane (same as `mark_live`). | `None`. |
| `live_mark_ts_ms` | `resolve_live_price` `live_price.py:111` (age-gate) | `mark` plane stamp `event_ms` (or `received_ms`). | `None` → treated as stale by `_stamp_is_stale`. |
| `live_funding_rate` | `tick_assembly.py:196` | `funding` plane scalar (same as `funding_live`). | `None`. |
| `live_index_price` | `features/snapshot.py` (grep-confirmed reader) | `indexPrice` from `mark` plane. | `None` if `index<=0`. |
| `ws_last_msg_age_s` | `completeness.py:905` (`stamp_market_freshness`); `diagnostics/data_plane_audit.py` | Freshest live-plane age for the symbol, s: `min(plane_ages(sym) over {book,trades,mark,ticker})`. | `None` if untracked. |

**DEAD snapshot fields — DO NOT reproduce** (0 readers, grep-verified): `ws_routed_market`, `ws_base_url`, `ws_socket_open`, `cross_ws_connected`, `liq_events_5m`, `liq_events_1m`, `liquidation_score_1m`, `agg_trade_buy_ratio_60s/30s` **as snapshot keys** (they are read only via the `agg_trade_*` names above — the duplicate emission is redundant), `agg_rpi_skew_60s`, `kline_{1m}_last_close_ms`, `kline_ws_interval`, `live_bid`, `live_ask`, `ws_depth_imbalance`, `live_quote_volume`, `live_price_change_pct`, `live_last_price`, and the entire `**liq_fields` / `**mark_snapshot` spread. Dropping these is correct per I-6 (a field nobody reads is a name-lie waiting to happen).

### 1.3 — Fields that require the shared `maps/` machinery vs pure read-through

**Pure read-through (no `maps/`):** everything in §1.2 except the liquidation trio; plus `live_ticker`, `live_bbo`, `live_funding`, `closed_kline_overlay`, `live_funding_cross`. Orderflow uses `hunt_core.engine.orderflow.taker_flow`/`price_change_pct` (pure, already built).

**Require shared `maps/` parsers (`hunt_core/maps/liquidation.py`):**
- `liquidation_buffers()` and the `liquidation_*` snapshot trio must turn ccxt liquidation **dicts** into `(ts_ms, sym, side, qty, price)` tuples. Reuse `normalize_liq_side`, `liq_contract_units`, `liq_price`, `liq_contract_size` (`maps/liquidation.py`, already imported by `streams.py:940-945`). `sym` must be the **Binance id** (`try_binance_id_from_ccxt`). This is the same conversion `_record_liquidation` does (`streams.py:930-999`) — lift it into a pure helper both the buffer path and the snapshot trio call.
  - Primary buffer: filter `engine.snapshot(sym,("liq",)).optional("liq")` per symbol → tuples.
  - Secondary buffers: `multi.cross_liquidations(symbol)` per venue → tuples.
  - **⚠** The old `_force_order_buffer` is a **process-lifetime ring** (`maxlen=8000`) accumulating every symbol; the engine `liq` read-through is only what ccxt's `ArrayCache` currently retains for that symbol. `liquidation_buffers()` is called **per-symbol** at `tick_assembly.py:970` (not global), so build it per-symbol on demand — this matches the call pattern. The 300 s window for the snapshot trio is applied by filtering `ts_ms >= now-300_000` before `liquidation_notional`.

**`trade_buffer(symbol)`** returns `deque[_AggPoint]` consumed by `build_map_bundle`→maps footprint/CVD. This depends on `maps/` still expecting `_AggPoint(ts_ms, qty, qty_full, is_buy, price)`. Convert ccxt trades → `_AggPoint` in the adapter, **or** migrate `maps/` to consume ccxt trade dicts as part of §3.3 of the ADR (cleaner). Flag: coordinate with the `maps/` feeder migration.

**`live_funding_cross` bridge:** `multi.cross_funding(symbol)` → `{v: rate|None}`; produce `{v: {"fundingRate": rate} for v,rate in ... if rate is not None and v != "binance"}`. The consumer `merge_ws_cross_into_snapshot` (`cross.py:368-376`) reads only `fundingRate` and `markPrice`; the engine cross poll (`multi.py:96`) stores **only the funding scalar** for secondaries, so `markPrice` is legitimately omitted (fail-loud; the secondary mark overlay is simply absent — acceptable degradation, funding is the actual divergence signal).

### 1.4 — The `set_symbols` no-op consequence (must be understood before applying)

With `set_symbols` a no-op, only the **construction-time warm universe** has WS-backed `snapshot()`/`live_book`/`trade_buffer`/liq fields. For the dynamic per-tick `active` universe (`_cycle_loop.py:798`, up to 100 incl. prescan outliers), non-warm symbols get `ws_connected=False` and `None` live fields → their microstructure (depth imbalance, CVD, live liq, live funding overlay) **disappears**, and price falls back through `resolve_live_price` to the REST `book`/ticker path. Pinned/analyst/prizrak symbols are unaffected (they're in the warm set). The manipulation scanner already fetches its own OHLCV via REST (CLAUDE.md), so it is unaffected. **Recommendation:** pass warm universe = `PINNED_SYMBOLS ∪ cli_symbols` (both known at construction, §2.2). **Faithful alternative (larger):** add `Ingest.set_symbols()` (spawn tasks for added, cancel for removed) + `Engine.retrack()` and have `EngineStreams.set_symbols` drive it — this restores the rotating-100 behavior but is a real engine feature, out of scope for a drop-in. Decide explicitly before applying.

### 1.5 — Book-math dependency (E5b)

`live_book`, `live_bbo`, `live_depth_imbalance`, `live_microprice_bias` need `depth_imbalance_from_book`, `depth_imbalance_from_levels`, `microprice_bias_from_book` — currently in `hunt_core/market/client.py` (imported at `streams.py:1500-1504`), slated to move to `toolkit/book_math.py` (ADR §2, task #23 E5b). `client_market.py:36` already imports `depth_snapshot_from_book` from the old location as an interim. `EngineStreams` should import from the **same** interim location until E5b lands, then follow the move. Do E5b first or accept the interim import.

---

## PIECE 2 — Runtime wiring (`EngineMarketPlane`)

### 2.1 — New plane type

**New file** (or append to `engine_adapters/`): `EngineMarketPlane` mirroring `HuntMarketPlane` (`factory.py:621-646`) attribute-for-attribute so `_cycle_loop.py:330-332` is untouched:

```
@dataclass(slots=True)
class EngineMarketPlane:
    client: EngineClient        # .client  (composed mixins, §2.6)
    streams: EngineStreams      # .streams (PIECE 1)
    spot: EngineSpot            # .spot    (exists: engine_adapters/spot.py)
    _multi: MultiEngine
    _spot_engine: SpotEngine

    async def aclose(self):
        await self._multi.close()   # closes primary Engine + secondaries
        await self._spot_engine.close()
        await asyncio.sleep(1.5)    # let aiohttp/ccxt.pro sessions drain (as factory.py:642)
    async def close(self): await self.aclose()
```

`aclose` replaces the old three-way `streams.stop()/client.close()/spot.close()`; `EngineStreams.stop`/`EngineSpot.close` are wired through the engines it already holds, so closing `_multi` + `_spot_engine` once is sufficient (do **not** also call `streams.stop()` — no-op — nor `client.close()`).

### 2.2 — Factory function

**New:** `create_engine_market_plane_from_settings(settings, cli_symbols) -> EngineMarketPlane` (needs `cli_symbols` for the warm set — signature differs from the old one, so the call site edit in §2.4 passes it).

Construction order (all awaits explicit):
1. `warm = tuple(dict.fromkeys(s.upper() for s in (*PINNED_SYMBOLS, *cli_symbols)))` — unified/normalized to ccxt symbols as the engine expects (the engine tracks under ccxt unified symbols; use `to_ccxt_symbol` post-`load_markets`, or pass Binance ids consistently — match whatever `Engine._symbols` is keyed on; the mixins normalize via `to_ccxt_symbol`, so pass unified).
2. `multi = MultiEngine(warm)` → `await multi.start()` (this runs `Engine.start()`: `load_markets` → REST-seed all TFs → spawn WS ingest → watchdog → positioning poller; then secondary `load_markets` + cross loop).
3. `spot_engine = SpotEngine(spot_symbols)` → `await spot_engine.start()`. `spot_symbols` = the spot form of `warm` (Binance spot markets; reuse the old companion's symbol derivation, or `warm` filtered to listed spot markets).
4. `client = EngineClient(engine=multi.primary, multi=multi)` (§2.3, §2.6).
5. `streams = EngineStreams(engine=multi.primary, multi=multi)`.
6. `spot = EngineSpot(spot_engine)`.
7. `return EngineMarketPlane(client, streams, spot, _multi=multi, _spot_engine=spot_engine)`.

Wrap 2–3 in try/except that closes partially-started engines on failure (mirror `factory.py:663-667`). Keep the caller's 3× retry (`_cycle_loop.py:315-329`) as-is.

**Universe source** (grep-confirmed):
- `PINNED_SYMBOLS` — `/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/data/universe.py:50` (`= load_pinned_symbols()`).
- `cli_symbols` — the `run_loop(cli_symbols, ...)` arg (`_cycle_loop.py:212`).
- The per-tick `active` universe (`resolve_watch_universe`, `_cycle_loop.py:662`) is **intentionally not** passed to the engine — it drives the (now no-op) `set_symbols` and, under the hybrid model, the dynamic tail goes through `EngineClient` REST. (Revisit only if you adopt the §1.4 faithful-alternative.)

### 2.3 — Add `MultiEngine.primary`

`/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/engine/multi.py` — add:
```
@property
def primary(self) -> Engine:
    return self._primary
```
Needed by both `EngineClient` and `EngineStreams` (§0.3). Trivial, non-breaking.

### 2.4 — Exact `_cycle_loop.py` edits

`/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/runtime/cycle/_cycle_loop.py`:

| Line | Now | Change |
|---|---|---|
| `:42` | `from ... import create_hunt_market_plane_from_settings` | Import `create_engine_market_plane_from_settings` (new location, e.g. `hunt_core.runtime.engine_adapters`). |
| `:317` | `plane = await create_hunt_market_plane_from_settings(settings)` | `plane = await create_engine_market_plane_from_settings(settings, cli_symbols)`. |
| `:330-332` | `client=plane.client; ws_feed=plane.streams; spot_companion=plane.spot` | **Unchanged** (attribute names identical). |
| `:341` | `st = await client.fetch_status()` | See §2.5. |
| `:352` | `ws_feed.set_symbols(list(cli_symbols))` | Harmless no-op; leave or delete. |
| `:353` | `await ws_feed.start()` | Harmless no-op (engine already started in factory); leave or delete. |
| `:798-801` | `ws_feed.set_symbols(list(active), priority=list(cli_symbols))` | Harmless no-op; leave. `ws_feed.kline_ws_enabled` at `:803,810` returns `True`. |
| `:1153` | `await plane.aclose()` | **Unchanged** (`EngineMarketPlane.aclose` exists). |

No changes needed to `analyst_pinned_loop(..., ws_feed=ws_feed)` (`:457`) — it consumes the same `ws_feed` surface. `set_live_spot_companion(spot_companion)` (`:337`) works: `EngineSpot` exposes `enrichments_for`/`fetch_weekly_ohlcv` (`engine_adapters/spot.py:46,69`).

### 2.5 — `fetch_status` (the `_cycle_loop.py:341` health check)

Choose one:
- **(a) Add a thin `fetch_status` to `EngineClient`:** `async def fetch_status(self): return await self._engine.exchange.fetch_status()` (ccxt public method — allowed). Cleanest, keeps the health log.
- **(b) Guard the call site:** wrap `:340-351` in `if hasattr(client, "fetch_status")`. Drops the check under the engine.

Recommend (a); it's one method and preserves the startup status log. Note this contradicts ADR §37 "fetch_status dead — drop"; the drop was judged from *data*-plane consumers, but the *health*-check consumer at `:341` was missed. Flag in the ADR when applying.

### 2.6 — Compose `EngineClient`

**New:** in `engine_adapters/` (e.g. `client.py`):
```
class EngineClient(_EngineClientMarketMixin, _EngineClientDerivsMixin):
    def __init__(self, engine: Engine, multi: MultiEngine) -> None:
        self._engine = engine
        self._multi = multi
```
Both mixins declare `_engine` (and derivs `_multi`) as class-level type-only attrs and never assign them (`client_market.py:53-55`, `client_derivs.py:91-92`). Export from `engine_adapters/__init__.py`. **Caveat:** verify the union of the two mixins' methods covers **every** `client.<x>` call in `runtime/`, `features/`, `maps/`, `track/`, `scanner/`, `deliver/` before deletion (§2.7) — the derivs mixin already lists `fetch_open_interest…snapshot_rest_cache_ages` (`client_derivs.py:141-720`) and market mixin `fetch_klines…fetch_index_ohlcv`. Two known holes to confirm during the client-migration stage (task #24, separate from these two pieces): `fetch_status` (§2.5) and `set_streams_reconnect`/`update_basis_from_websocket` (below).

### 2.7 — Deletable files + dangling-import checks

**After** these two pieces + the consumer migration (ADR §3.2-3.9) land, delete (ADR §3.10):
`/Users/tonyaleksandrov/Documents/HUNTER/hunt_core/market/{client,streams,spot,cross,capacity,ccxt_guard,ccxt_rest,rate_limit,weight_registry,live_price}.py` + `hunt_core/data/{collect,frame_cache,completeness}.py` (+ `runtime/symbol_probe.py`).

**Keep** (persistence/universe/pure): `hunt_core/data/{lake,universe,tick_jsonl,jsonl_io,baseline_store,lake_warmup,symbol_blacklist}.py`; `market/{tick_registry,symbols,symbol_gate,network}.py`.

**Dangling imports that block deletion — must be cut first (grep-confirmed):**
- `market/factory.py` itself is on **neither** list but the two client mixins **import from it**: `ccxt_ohlcv_to_frame`, `finalize_kline_frame` (`client_market.py:37`, `client_derivs.py:38`), and `close_exchange_async`/`create_pro_secondary_swap` are used by the old `streams.py`. Per ADR §2 these OHLCV transforms must move to `toolkit/ohlcv.py` (task #23) — do that move, or keep `factory.py` alive as a keep-module. **`factory.py` cannot be deleted while the mixins import from it.**
- `market/live_price.py` (`resolve_live_price`/`apply_live_price_to_row`) is imported by `tick_assembly.py:130,1074-1077` and `track/tracker.py:392`. It imports `HuntCcxtStreams` **only for a type hint** (`live_price.py:9`). To delete `streams.py`, `live_price.py` must first drop that type import (change to `Any` or the new `EngineStreams`) — and `live_price.py` itself is on the delete list, so its logic (the price oracle) must move to a keep-module or into `EngineClient`/`EngineStreams` during the consumer migration.
- `market/streams.py` is imported for type hints in `features/snapshot.py:12`, `tick_assembly.py` (`ws_feed: HuntCcxtStreams`), `_cycle_tick.py:118`, `_impl.py:40`, `_cycle_reconcile`/others. All are `TYPE_CHECKING`/annotation-only → swap to `EngineStreams` or `Any` at migration; none block runtime, but `grep -rn "from hunt_core.market.streams"` must return **zero** before deleting the file.
- `client.set_streams_reconnect` (`factory.py:669`) and `client.update_basis_from_websocket` (`streams.py:1250,1702`) are `HuntCcxtClient`↔`HuntCcxtStreams` coupling with **no** engine analog (the engine self-manages reconnect via its `Watchdog` and basis via the `mark` plane). Both call sites vanish when `streams.py`/`factory._create_plane_once` are replaced — confirm no other caller with `grep -rn "set_streams_reconnect\|update_basis_from_websocket"`.
- `maps/liquidation.py:34` comment references `hunt_core.market.streams` but has no import — cosmetic, update the comment.

**Deletion gate (ADR §3.10):** `grep -rn "from hunt_core.market.streams\|import HuntCcxtStreams\|from hunt_core.market.spot\|HuntCcxtSpotCompanion\|create_hunt_market_plane\|from hunt_core.market.live_price\|from hunt_core.data.collect\|from hunt_core.data.frame_cache\|from hunt_core.data.completeness" hunt_core` must be empty (outside the files being deleted), then `uv run vulture`, `uv run ruff check .`, `uv run mypy hunt_core`, `uv run pytest`. The module-boundary test (`tests/test_module_boundary.py`) and I-5/I-6 stay green.

---

## Sequencing note

`EngineStreams` (PIECE 1) has a hard dependency on **E5b book-math extraction** (§1.5) and the **`maps/` liq/trade shape bridges** (§1.3). Land E5b (task #23) + expose `MultiEngine.primary` (§2.3) + compose `EngineClient` (§2.6) **before** wiring `EngineMarketPlane`, or the plane will construct but `snapshot()`/`live_book`/`liquidation_buffers` will be half-backed. The wiring edit (§2.4) is the **last** step — it flips the whole `watch` loop onto the engine in one commit, so it must not land until PIECE 1 + the consumer migration (§3.2-3.9) are green.