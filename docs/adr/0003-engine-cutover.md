# ADR 0003 — Cutover: move the whole project onto the ccxt-native engine, delete the old data layer

Status: **in progress** (staged). Supersedes the transport half of `market/` + `data/{collect,frame_cache}`.
Decision (user, 2026-07-18): *«полностью перевести проект на новый движок, полностью удалить старый,
затем заняться модулем Призрак».* This ADR is the map; it MUST survive context compaction.

## 1. What replaces what

The old pull layer is ~12.4k LOC (`market/` 9.0k + `data/` 3.4k). Not all dies — the pure utilities
stay (extracted out of the transport). The **transport + caches** die and are replaced by
`hunt_core/engine/` (`Engine.snapshot(symbol, required) -> MarketSnapshot`, push-state, fail-loud).

| Old surface | Engine replacement | Status |
|---|---|---|
| `fetch_klines*` / `fetch_ohlcv_list*` / `get_cached_klines` | `snapshot().require("kline.<tf>")` (REST-seed + WS-merge frame) | ✅ exists |
| `fetch_order_book_depth_snapshot` / `_fetch_book_ticker_rest_detail` | `snapshot().require("book")` / `"bbo"` (read-through) | ✅ exists |
| `fetch_agg_trade_snapshot` | `"trades"` read-through + a pure agg helper | ⬜ helper |
| `fetch_open_interest` / `fetch_funding_rate` / `fetch_*_ls_ratio` / `fetch_taker_ratio` / `fetch_basis` | value-backed planes (`oi`,`funding`,`global_ls_5m`,`top_ls_*`,`taker_5m`,`basis`) | ✅ exists |
| `fetch_cross_exchange_snapshot` / secondary funding/tickers | `MultiEngine.cross_funding/oi/long_short/liquidations` | ✅ exists (shape bridge ⬜) |
| **`fetch_mark_ohlcv` / `fetch_index_ohlcv`** | `rest.fetch_ohlcv_series(price='mark'\|'index')` | ✅ **E1** |
| **`fetch_open_interest_series` / `fetch_global_ls_series`** | `rest.fetch_futures_data_series(method,params,key)` | ✅ **E2** |
| **`fetch_oi_bars_for_maps`** | OI-hist series (E2) + align-to-OHLCV moves to `maps/` at cutover | ◑ E2 (source ready) |
| **`fetch_klines_between`** (reconcile / backfill / completeness) | `rest.fetch_ohlcv_between(start_ms,end_ms)` (closed-only) | ✅ **E2** |
| `fetch_funding_rate_history` | `rest.fetch_funding_history` | ✅ **E2** |
| **`fetch_ticker_24h`** (scanner funnel, ALL perps) | `rest.fetch_all_tickers` (universe-wide REST batch) | ✅ **E3** |
| **`fetch_premium_index_all` / `fetch_funding_info_all` / `fetch_exchange_symbols`** | per-symbol mark/funding planes + `exchange.markets` meta — read at consumer-migration (tracked universe covers pinned) | ◑ E3 (per-symbol path) |
| **`get_cached_funding_rate_zscore` / `_trend` / `_recent_extreme` / `get_cached_basis_stats`** | funding-history buffer in engine → **stats computed in `features/`** (move out of transport) | ⬜ **E4** |
| **WS-derived** (`agg_trade_delta_30s/60s`, `live_microprice_bias`, `live_depth_imbalance`, `ws_price_chg_1m`, `closed_kline_overlay`, `live_book`, `trade_buffer`, `liquidation_buffers`) | pure helpers over engine `trades`/`book`/`liq` read-through | ⬜ **E5** |
| **SPOT** (`HuntCcxtSpotCompanion`: `refresh_symbols`, `enrichments_for`, `fetch_weekly_ohlcv`, taker-flow) | a **spot sibling engine** (ccxt spot client; own 6000/min budget) | ⬜ **E6** |
| `snapshot_rest_cache_ages` / `used_weight_1m` (diagnostics) | engine plane-age introspection + throttler weight | ⬜ **E7** (thin) |

**Dead — drop, do not port** (0 consumers, verified): `fetch_premium_index_ohlcv`,
`fetch_leverage_tiers`/`get_cached_leverage_tiers` (maps reads it but it's `None`-tolerant → drop),
`fetch_basis_from_ohlcv`, `fetch_status`.

## 2. Utilities that STAY (extract from the dying transport into a keep-module)

Pure, no-network, no-engine — currently trapped inside `market/client.py` / `factory.py` / etc. Extract
to survive the deletion:
- **book math** (`client.py` module-level): `normalize_depth_levels`, `depth_imbalance_from_book`,
  `depth_imbalance_from_levels`, `microprice_bias_from_book`, `detect_wall_clusters`,
  `depth_imbalance_by_zone`, `top_depth_walls`, `depth_snapshot_from_book`,
  `aggregate_cross_exchange_walls`, `wall_cluster_to_dict`, `WallCluster` → `toolkit/book_math.py`.
- **OHLCV transforms** (`factory.py`): `drop_unclosed_ohlcv_tail`, `resample_ohlcv_from_1m`,
  `min_1m_bars_for_resample`, `ccxt_ohlcv_to_frame`, `finalize_kline_frame` → `toolkit/ohlcv.py`.
- **already-clean modules to keep as-is**: `market/tick_registry.py`, `market/symbols.py`,
  `market/symbol_gate.py`, `market/network.py` (Telegram egress/proxy — unrelated to market data).
  Keep their paths or move under `toolkit/`; do NOT delete.

## 3. Consumer migration order (each stage: ruff+mypy+pytest+vulture, commit)

Data contract established by recon (see git note / memory `engine-ccxt-crossvenue-gotchas` sibling).
The `market`/`data` fetch+cache surface maps onto `snapshot()`; the row-dict shape is preserved at the
seam so downstream feature/format code is untouched at first.

1. **E1–E7 engine extensions** (additive, no consumer touched) — close every ⬜ gap above.
2. **`features/snapshot.py`** — `get_cached_*` readers → `snapshot()` planes; funding stats use E4.
3. **`maps/` feeder** — pass-in only; just swap what fills `oi_bars`/`book`/liq buffers.
4. **`runtime/tick_assembly.py`** (`snapshot_symbol`, the feature heart) — the big one; `_fetch_rest_pack`
   → engine planes, mark/index/oi-bars via E1/E2, WS via E5, spot via E6.
5. **`runtime/analyst_assembly.py`** — batch context (E3) + prizrak per-TF (`fetch_ohlcv_list_cached`→frames).
6. **`runtime/cycle/{_cycle_tick,_cycle_reconcile,_impl}.py`** — `refresh_tick_batch_cache`→engine batch,
   `fetch_klines_between`→E2, WS overlays→E5.
7. **`scanner/` manipulation path** (`deliver/manipulation_delivery.py`) — `fetch_ohlcv_list_cached` +
   `fetch_funding_rate_history` → engine. **Then `/backtest-gate` (scanner emission changed source).**
8. **`track/path_backfill.py`** — `fetch_ohlcv_list` (windowed 1m) → E2.
9. **`deliver/{_sections,telegram}.py`** — cross snapshot / rendering → engine.
10. **DELETE** `market/{client,streams,spot,cross,capacity,ccxt_guard,ccxt_rest,rate_limit,weight_registry,live_price}.py`
    + `data/{collect,frame_cache,completeness}.py` (+ `runtime/symbol_probe.py`). Keep `data/{lake,universe,
    tick_jsonl,jsonl_io,baseline_store,lake_warmup,symbol_blacklist}.py` (persistence/universe). Verify zero
    dangling imports (`grep`), vulture, full pytest.

## 4. Invariants preserved

No degradation / no stale / no fallback / no empty (I-6) — the engine already enforces this structurally
(`Plane.read` → `NotReady`). The cutover must NOT reintroduce a `or 0.0`/phantom-key path at the seam:
every bridged field is fail-loud `None` when its plane is `not_ready`. No-lookahead (I-5): frames stay
closed-only (`closed_bars`, `[:-1]`). Module boundary (prizrak↔manipulations) untouched — both read the
engine, neither shares a row.

## 5. Risk / honesty

The live bot is a throwaway test run (user), so there is no incremental live-migration guarantee to keep;
correctness is proven by unit tests + detached live smokes per stage (the full path needs Telegram, which
`--no-telegram` mutes — so manipulations are validated by `/backtest-gate`, prizrak by `/signal --live`).
This is multi-session work; each stage lands independently green. **Do not claim "done" until §3.10
verifies zero dangling imports and the old transport files are gone.**
