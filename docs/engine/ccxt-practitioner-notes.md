# ccxt practitioner notes — internals a data engine must get right

Companion to [data-catalog.md](data-catalog.md). The *internals/idioms* (not the data structures),
grounded in installed source `ccxt==4.5.59`. Cited line numbers are that version.

## 1. Number & precision (most consequential)
- `precisionMode` decides what `market['precision']['price']` MEANS: **`TICK_SIZE`** (all four venues) →
  it's the **tick grid itself** (`0.1`, `1e-7`), not a digit count. `DECIMAL_PLACES`→#decimals,
  `SIGNIFICANT_DIGITS`→#sig-figs. Branch with `is_tick_precision()`.
- Round computed prices with **`price_to_precision(sym, x)`** (ROUND) / `amount_to_precision`
  (**TRUNCATE** — never round size up) / `cost_to_precision`. Both raise `InvalidOrder` if the value
  collapses to `'0'`.
- ❌ `round(level, 6)` — a fixed decimal grid, NOT the tick: 10–100× too coarse on a `3.5e-5` coin
  (erases sub-0.15% buffers), false precision on BTC, plus banker's rounding on an already-lossy float.
- `ROUND_UP`/`ROUND_DOWN` are **rejected** by `decimal_to_precision` (asserts `∈ {TRUNCATE, ROUND}`) —
  ceil/floor-to-tick must be hand-rolled with `Decimal` (this is why `market/tick_registry.py` uses raw
  `Decimal` floor/ceil — the correct pattern).
- Serialise small numbers with `number_to_string(x)` — `str(1e-7)=='1e-07'` breaks any `Decimal(str(...))` path.

## 2. Pagination for deep history
- Binance clamps a single `fetch_ohlcv` to **1000** (`defaultLimit=500, maxLimit=1000`); raw API allows
  1500 — ccxt clamps silently. A `since→until` span wider than 1000 with `limit=None` returns only the
  **first 1000 from `since`** — silent truncation.
- Deep history: `params={'paginate': True}` → deterministic paginator (concurrent windows, dedup by ts,
  ascending), ceiling ≈ `paginationCalls(10) × maxEntriesPerRequest(1000)` = 10k bars; raise
  `paginationCalls` for more. Each window is throttled but sums IP weight.
- The engine's `rest.seed_ohlcv` now routes `limit>1000` through `paginate` (≤1000 stays one call).

## 3. Time
- `parse_timeframe(tf)` → **seconds**; **case-sensitive** (`M`=month, `m`=minute). The engine uses
  `exchange.parse_timeframe` (native), not a hardcoded map.
- `round_timeframe(tf, ts, ROUND_DOWN)` = the bar-boundary aligner (`ts − ts % (tf*1000)`); the freshness
  gate computes the same `(now // interval) * interval` for the forming-bar open (I-5, no lookahead).
- `adjustForTimeDifference` only affects **signed** requests (nonce/recvWindow) — a harmless no-op for a
  public-only engine; not required.

## 4. ccxt.pro order-book & cache
- Read-through of `exchange.ohlcvs` (`ArrayCacheByTimestamp`) / `exchange.trades` (`ArrayCache`) is correct:
  the caches **accumulate up to `*Limit` even under `newUpdates=True`**; `watch_*` returns only the delta.
- `watch_order_book` returns a **live-mutating `OrderBook`** — copy `bids`/`asks` before use if held across
  `await` (the engine's `_book_snapshot` copies synchronously → safe). Levels are stored as **floats** —
  round accumulated notionals at the end.
- Keep `checksum=True` (set) — the only integrity signal on a corrupt local book; on a gap ccxt raises
  `ChecksumError`, drops the book+subscription, re-seeds from a REST snapshot → catch-and-continue.
- `watch_*_for_symbols`: **≤200 symbols/call**, `streamHash` changes with the symbol set (sockets
  evicted/recreated).
- `newUpdates` default is **`True`** in 4.5.59 (the wiki "default false" note is stale).

## 5. Options / params
- `handle_option_and_params(params, method, name, default)` precedence: `params[name]` → `options[method]
  [name]` → `options[name]` → `default` (and strips it from params). `price` param picks the
  mark/index/premium kline endpoint; `defaultType`+`subType` pick fapi/dapi/spot.

## 6. Implicit API & `has`
- Every `api`-tree leaf → both camelCase & snake_case implicit methods carrying rate-limit `cost`, so ANY
  endpoint is callable: `ex.fapiDataGetTopLongShortPositionRatio({...})` (the engine uses these for the
  top-trader/basis signals ccxt doesn't unify).
- `has[x]`: `True` native · `False` none · `'emulated'` (truthy) built on another method — check `== True`
  only when you need native.

## 7. Market lifecycle
- `load_markets(reload=False)` memoised & concurrent-safe. `markets` (by symbol) vs `markets_by_id` (id →
  **list**). `market(sym)` resolves; `safe_market` never raises. `defaultType`/`defaultSubType` drive
  resolution — USDⓈ-M = `future`+`linear` (or the `binanceusdm` class).

## 8. Status / health
- ⚠️ Binance `fetch_status` calls a **signed** wallet endpoint (`sapiGetSystemStatus`) — a public-only
  client CANNOT use it. Liveness via `fetch_time` / OHLCV freshness (what the engine's watchdog does) is
  the right choice — do **not** add `fetch_status`.

## 9. Rate-limit cost
- `enableRateLimit=True` (set); Binance `calculate_rate_limiter_cost` scales by `byLimit` (klines
  1→10 by limit; depth 2→20) and charges heavier for unfiltered queries (`ticker/24hr` no-symbol = 40).
  `fetch_ohlcv(limit=1000)`=weight 5, `fetch_order_book(limit=1000)`=20. Leaky-bucket throttler.

---

## HUNTER engine audit (what's correct / watch-outs)
- ✅ Precision handled by `market/tick_registry.py` (TICK_SIZE, Decimal floor/ceil) — retires the `round(x,6)` trap.
- ✅ Read-through, `checksum` on, book-copy-before-compute, `parse_timeframe` native, no `fetch_status`.
- ✅ `newUpdates`-delta semantics accounted for (read the accumulating cache, not the watch return).
- 🔧 OHLCV seeding: now paginates when `limit>1000` (was single-call, silently capped).
- ⚠️ At **cutover**, any strategy that rounds an exchange-facing price must use `tick_registry` /
  `price_to_precision`, never float rounding; and sum book/depth notionals as floats then round.
