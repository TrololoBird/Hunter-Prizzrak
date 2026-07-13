# ADR 0001 — WeightGovernor: admission control over reactive backoff

**Status:** Implemented 2026-07-12 (core; live 2h validation gate pending next bot restart).
Implementation notes: (a) RESERVE re-scoped per REVIEW_market.md — fstream market streams are
weight-FREE; reserve covers only ccxt.pro REST depth-snapshot seeds + WS-API handshakes
(`HUNT_WEIGHT_RESERVE`, default 200; TARGET = floor(2400×`HUNT_WEIGHT_MARGIN` 0.75) − 200 = 1600).
(b) Registry: `hunt_core/market/weight_registry.py`, parameter-aware (klines/depth scale with
limit); `/futures/data/*` charged nominal weight 1 + own window; funding history gets its
500/5min side-pool bucket. (c) `force_floor` deleted; header is an advisory drift-check
(`hunt_weight_drift_high`) + the `header_stop` fuse. (d) Demand shaping in
`HuntLoadPlanner.plan_tick` drops lowest-priority non-pinned symbols to fit
`target_weight_per_tick` (acceptance test: `test_planner_never_exceeds_per_tick_budget`).
(e) QoS: background context families (`hot_enrich`, `path_backfill`) admit under a 0.8 ceiling
share. (f) Push-first FULLY delivered (second pass, same day): raw `/fapi/v1/klines` 12-element rows on
the REST path + `HuntProBinanceFutures.handle_ohlcv` extends parsed WS rows with the payload's
T/q/n/V/Q → frames carry REAL taker/quote fields (this also fixed a pre-existing silent defect:
ccxt truncation had zero-filled `taker_buy_base_volume` everywhere, degenerating `delta_ratio`
to 0 and bar delta to −volume). `resolve_kline_map` serves 1m cache-first behind four gates
(coverage / tail continuity / freshness / per-bar fidelity); duplicate-time merge resolves by
`num_trades` so a partial mid-candle WS row can never overwrite a closed bar (bar immutability —
no-lookahead review finding, fixed + pinned by tests).
**Scope:** `hunt_core/market/**` (rate_limit, ccxt_rest, capacity, client, streams), `hunt_core/data/universe.py`, the cycle loop's background loops.
**Supersedes the reactive framing of** WS-1.2–1.6 in the remediation plan.

## Context

The live bot died at ~5–6 min (= the 300s watchdog) because the REST weight pacer saturated its
self-imposed 1500/min ceiling and serialized every call with 12–21s `await` sleeps. WS-1.1 replaced
the watchdog with a progress heartbeat so a *slow* tick is no longer killed — but that only stops
the symptom. The weight model itself is **reactive** and structurally leaky:

1. **The budget is not the only path.** ccxt.pro snapshots, cached endpoints, and the spot companion
   consume IP weight **without going through `WeightBudgetManager.acquire`**. `force_floor` exists to
   retro-fit the local counter up to the server header (`x-mbx-used-weight-1m`) — a patch on leaky
   accounting, and asymmetric (it injects positive gaps, never trims, and mixes a rolling-60s local
   deque with Binance's fixed-clock-minute counter → the local estimate stays inflated after a reset).
2. **Demand is unbounded by budget.** The universe (`cap = MAX_DYNAMIC_SYMBOLS + pins + ignition`) is
   not tied to how much weight a tick can afford; the planner only rotates full/fast **within** an
   unbounded set, so a tick can *request* more than a minute holds → `acquire` starts sleeping.
3. **Decision is at the boundary, not provisioned ahead.** "Can this go?" is answered by watching the
   server header rise, not before the request is formed.

Binance enforces the limit **server-side, per IP**, WS-API weight is **shared** with REST against the
same **2400/min**, and 418 bans **scale 2 min → 3 days** (re-calling during a ban extends it). So the
target is: *a request must not leave the client until it is proven to fit our own accounting.*

## Decision

Move from "hope backoff catches up" to "over-running our own accounting is mathematically impossible
for everything that flows through the gate." Four pillars:

### 1. One WeightGovernor = the single admission choke point
Exactly one asyncio owner of the authoritative budget. Every weight-consuming call submits a request
with a **declared static weight** (endpoint weight is a spec fact, not something to measure). The
governor **admits or queues**; no call reaches the network without a granted token. For everything
through the gate, over-run is impossible *by construction*.

- **Static endpoint weight registry** (from the Binance USDⓈ-M spec), e.g. `fetchOHLCV` weight by
  `limit` bucket (1/2/5/10), `fetchOrderBook` by depth, `fetchTicker`=2, `fetchTickers`=40/80,
  `/futures/data/*` per its table. A single source-of-truth table, not per-call header reads.
- **Budget** = `TARGET = floor(2400 * MARGIN) − RESERVE`, `MARGIN ≈ 0.70–0.80` (target ~1700–1900).

### 2. Reserve for what the gate can't intercept
ccxt.pro / spot weight can't pass through `acquire` — but it is NOT "outside the budget" (WS-API
shares the 2400). Reserve a **static quota** for it; the governor hands out `TARGET − RESERVE`. The
untracked consumer is then structurally accounted, without pretending it's free.

### 3. Demand-shaping — fit demand to budget at PLAN time (WS-1.4/1.5 done right)
The universe planner first computes a tick's cost from the static weights, then **drops
lowest-priority symbols until the estimate fits the per-tick budget slice with margin**. The tick is
then *physically incapable* of requesting more than fits — `acquire` sleeps disappear not because we
removed them, but because we never reach them.

### 4. Push-first — take weight off REST entirely (largest structural lever)
Every datum sourced via WS (ccxt.pro `watchOHLCV`/book/trades) is a REST call **never made** = zero
weight. Stream the maximum; keep REST only for the un-streamable (funding history, `/futures/data/*`).
Cache with `TTL ≥ bar cadence` so nothing is re-fetched inside one candle. This removes the bulk of
weight from the budget before admission even runs.

### QoS classes
The watch tick outranks background loops. Under scarcity `path_backfill` / `analyst_pinned` **yield**
instead of competing as equals for one `_GLOBAL_WEIGHT_BUDGET`.

### Server header becomes advisory
`x-mbx-used-weight-1m` stops being the controller and becomes a **drift-check / safety** term:
reconcile local-vs-header at minute boundaries (fixes the `force_floor` asymmetry), but never rely on
it to avoid a ban.

## Honest limit — why not 100%
No client can fully guarantee it: the limit is enforced per-IP (shared with any other process / ccxt's
own internal retries / WS-API), and Binance's window is a fixed minute whose reset is not clock-synced
with ours (boundary overlap). Hence the mandatory conservative `MARGIN` + `RESERVE`, and the cheap
**`ip_ban` → skip-REST-phase circuit breaker stays as a backstop** — but as a fuse that in normal
operation never trips, not as the control system it is today.

## Consequences
- `force_floor` is **deleted as a class**, not fixed; the "1500→2000 ceiling" becomes unreachable in
  normal operation rather than a working mode; `acquire` pacing becomes a rare safety event.
- Sizeable market-layer refactor (governor, static registry, planner cost model, push-first migration,
  QoS) — larger than the plan's tuning items. **Sequence:** after WS-1.1 (done) makes the bot
  observable and after the cheap stabilizers (WS-1.2 ban-skip, WS-1.5 universe cap, WS-1.6 reconnect),
  so the redesign is validated on a surviving, logged bot — not blind.
- `enableRateLimit` in ccxt is only per-call ms spacing, not a global weight-aware budget — the
  governor must sit above it (already the case).

## Verification
Run under `scripts/monitor_live.py`: `bans_rl` (pacing events) should drop to near-zero, `used` stay
< `TARGET`, zero 418s, and a clean 2h run. A stability regression test asserts the planner never emits
a tick whose static-weight sum exceeds the per-tick slice.
