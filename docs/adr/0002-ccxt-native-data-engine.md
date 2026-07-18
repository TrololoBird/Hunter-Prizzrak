# ADR 0002 — Full migration to a ccxt.pro-native data engine

**Status:** Proposed (2026-07-18) — **greenfield engine design**. The current live bot is a
disposable test run, not a system to migrate incrementally; this ADR designs a **new core from
scratch** and hard-swaps it in. **Supersedes** ADR-0001's *custom* rate/weight machinery. Grounded
in: the ccxt/ccxt.pro manual + source (throttler, `ArrayCacheByTimestamp`, `binance.py::handle_order_book`)
read directly, and a survey of **22 real ccxt/ccxt.pro projects** (freqtrade, hummingbot, OctoBot,
cryptofeed, tardis, passivbot, ccxt's own examples, …).

**Scope:** `hunt_core/market/**`, `hunt_core/data/{collect,frame_cache}.py`, the cycle loop's
data plane. **Does not touch** strategy logic (`prizrak/`, `scanner/`), delivery, or `track/`.

---

## 1. Context — what we are removing and why

The data plane accreted **~4400 LOC** of custom machinery that *wraps and fights* ccxt:

| Custom machinery | LOC | What it re-implements |
|---|---|---|
| `rate_limit.py` + `capacity.py` + `weight_registry.py` + `ccxt_guard.py` + `ccxt_rest.py` | ~1269 | a weight governor / sliding-window limiter / ban circuit-breaker over ccxt |
| `frame_cache.py` + `collect.py` fallback chain | ~1089 | an OHLCV cache + the `derive-1m → cache → htf-serve → REST → WS` fallback ladder |
| `streams.py` per-interval WS state machine | ~2049 | hand-rolled subscribe/close/grace/reconnect per (interval) |

This machinery is the source of the recurring **defect class** we have fought all session:
the `klines.4h.stale` "frozen HTF frame, errors=0" blackout, the cache-TTL-vs-bar-cadence drift,
the falsy-zero fallbacks. Every one is a bug in *our* re-implementation of something ccxt.pro
already does natively and correctly.

**Goal:** go **fully ccxt.pro-native**, delete the crutches, under the hard invariant
**no degradation / no stale data / no fallbacks / no empty values**.

---

## 2. The central finding (honest, and it reshapes the requirement)

**"No fallback" taken literally (WS is the sole source, zero REST) is in direct conflict with
"no stale / no empty" — and every mature ccxt.pro engine resolves it the same way.**

- **freqtrade:** REST is the *source of truth*; WS is an accelerator that must **re-earn trust every
  refresh cycle** through an explicit freshness gate.
- **ccxt.pro order books & cryptofeed:** on a sequence-number gap they **discard and re-seed from a
  REST snapshot** — never interpolate past a hole.
- **hummingbot / OctoBot:** REST warm-up seed + a staleness watchdog that force-re-snapshots.

No surveyed project makes WS stand alone, because three things fundamentally need a REST cross-check:
**(a)** history/backfill depth, **(b)** order-book / OHLCV re-seed after a gap, **(c)** proving
freshness when a socket goes *silent* (ccxt exposes **no** last-update timestamp; a quiet stream is
indistinguishable from a frozen one without a wall-clock check — this is exactly our blackout).

**Reconciliation — the requirement becomes "no *silent* fallback":** we keep a thin, principled,
ccxt-native REST path, but every use of it is **explicit, fail-loud, and logged** (I-6 aligned).
The win is not *eliminating REST*; it is **collapsing a stale-prone multi-step fallback chain into
one deterministic source per datum + one ccxt-native REST re-seed, all fail-loud.**

> If the requirement is read as *literally forbidding any REST call*, then no engine surveyed — and
> none that can exist — guarantees no-stale/no-empty. The two goals contradict. We choose no-stale
> and no-empty, and make every REST touch loud.

---

## 3. Decision — delete 100% of the named custom machinery; each replaced by a ccxt-native equivalent

| Remove (custom) | Replace with (ccxt-native) |
|---|---|
| Sliding-window limiter + weight governor + `weight_registry` | `enableRateLimit: True` + ccxt's token-bucket throttler. Binance's `describe()` already carries **weighted per-endpoint `byLimit` costs** (`depth` 2→20 by limit, `klines` 1→10 by limit) — `calculate_rate_limiter_cost` charges heavy calls proportionally. freqtrade/OctoBot rely on this and add only **batching**. |
| Raw `/fapi/v1/klines` 12-element parsing | `exchange.fetch_ohlcv` (REST) + `exchange.watch_ohlcv` (WS); or `exchange.build_ohlcvc()` from `watch_trades` for deterministic closes. |
| `frame_cache` + TTL + `derive-1m→cache→htf→REST→WS` chain | `exchange.ohlcvs[sym][tf]` (`ArrayCacheByTimestamp`, `OHLCVLimit` deque), read **deepcopy-under-lock**; **one deterministic source per datum + explicit REST re-seed**, never a silent ladder. Evict-don't-stitch on a time gap. |
| `streams.py` per-interval WS state machine | Canonical `while True: try: await watch_*: except NetworkError: continue`; **one asyncio task per (symbol, tf)**; `un_watch` on teardown; ccxt.pro owns subscribe / reconnect / exponential backoff. |
| `ccxt_guard` ban circuit-breaker as the *control system* | Keep only as a **backstop fuse** (ADR-0001 already reframed it thus); the throttler makes it a rare event. |

### Native switches / idioms we adopt
- **`newUpdates: True`** — `watch_*` returns *only what changed since the last call*, so an unchanged
  read can never masquerade as a fresh one. (The single most important anti-stale switch.)
- **Drop the forming candle.** `ArrayCacheByTimestamp` **mutates `[-1]` in place** until the bar
  closes → over WS, `[-2]` is the newest *closed* bar. This IS our closed-only convention (I-5);
  read `cache[:-1]`.
- **Order-book freshness is genuinely native.** `binance.py::handle_order_book` seeds from a REST
  snapshot, validates the `U/u/pu` nonce continuity, and on a gap `raise ChecksumError` →
  `del self.orderbooks[symbol]` → next `watch_order_book` auto-re-seeds. We just **catch-and-continue**.
- **Freqtrade two-axis freshness gate** (the reusable primitive for our invariant):

  ```python
  # trust WS only if BOTH hold, else fall through to REST (logged at INFO — explicit, not silent)
  reached_last_closed = len(candles) > 1 and candles[-1][0] >= prev_closed_open_ts   # CONTENT
  ticked_recently     = last_ws_refresh_ts >= (candle_open_ts - half_candle)         # WALL-CLOCK
  ```
- **Staleness watchdog** (OctoBot): stamp `_last_msg_ts` on every frame; if a socket goes silent
  past a threshold, force-close (optionally recreate the ccxt client) to trigger a fresh subscribe.
  **This structurally kills the "frozen frame, errors=0" blackout class** — ccxt won't tell you the
  stream froze, so we time it ourselves.

---

## 4. How each part of the invariant is met

- **No empty/None** — `watch_*` **blocks until the first frame arrives**; a warm stream never yields
  empty. Enforce: warm-before-read (first `await` completes before any snapshot read) + REST
  `fetch_ohlcv` to seed history depth + `has['watchOHLCV']` capability-gate with a **loud UNSUPPORTED
  sentinel**, never a silent `{}`.
- **No stale** — order books: native nonce re-seed (§3). OHLCV: two-axis freshness gate + drop-forming
  + per-connection staleness watchdog. ~40 lines of *our* code **around** ccxt, not a custom engine.
- **No silent fallback** — the REST path exists for exactly three jobs (history seed; re-seed after a
  detected gap/watchdog trip; the freshness cross-check), and each logs explicitly.

---

## 5. The one genuine risk to size before committing

ccxt's throttler is weight-aware but **single-window**. Binance's `/futures/data/*` (OI-hist,
long/short, taker, basis — our positioning доп-факторы) has a **separate 1000-req/5-min count**
window that ccxt's one weight-window does not model, and the ADR-0001 bans were on exactly that pool.
**Mitigation:** with full push-first, `/futures/data/*` is the *only* recurring REST left and its
pinned-7 load is ~6.5% of the limit (measured). Options, in order of preference:
1. Validate empirically that ccxt's throttler + our request cadence stays under 1000/5min (it should).
2. If not, keep **one** thin ccxt-compatible limiter for the `/futures/data/*` class only (a fraction
   of `rate_limit.py`) — not the whole governor.

The **shared rotating NAT egress-IP** residual is unchanged and **orthogonal to library choice**
(ADR-0001 §"Honest limit"): no client can measure other tenants' traffic. Dedicated IP is the only
100% lever. This ADR does not claim to fix it.

---

## 6. The new engine — architecture

### 6.1 The core inversion: push-state, not pull-snapshot
The old engine is a **pull/snapshot** model (each 30s tick *fetches* the current state of every
symbol) bolted onto a **push/stream** reality. Every crutch — frame cache, fallback ladder,
per-interval state machine, weight governor, demand-shaping planner — exists only to bridge
pull-over-push. The new engine is **push-native**: long-lived `watch_*` tasks keep an always-warm
in-memory `MarketState`; strategies **read a freshness-proven view** at their tick and never trigger
a fetch. This deletes the entire tick-time weight-budget / demand-shaping problem: **there is no
per-tick REST burst because the tick does not fetch.**

```
 ccxt.pro watch_* (push) ──stamp received_ms──▶ MarketState.planes ──freshness gate──▶ snapshot(sym)──▶ strategy
        ▲                                             │                                      │
 health watchdog: silent stream → force-reconnect ────┘                        NotReady(reasons) → strategy abstains LOUD
 REST (ccxt-native, throttled): seed on start · reseed on gap/watchdog · poll /futures/data ──────────┘
```

### 6.2 Modules (`hunt_core/engine/` — replaces `market/` + `data/{collect,frame_cache}`)
- **`exchanges.py`** — one ccxt.pro instance per venue (Binance + OKX/Bybit/Bitget), configured
  `{'newUpdates': True, 'enableRateLimit': True, 'options': {'defaultType':'future','OHLCVLimit':1000,'tradesLimit':1000}}`.
  One instance per venue is mandatory (shared throttler).
- **`ingest.py`** — the watch supervisor. One asyncio task per `(symbol, stream)`; the canonical
  `while True: try: v = await watch_*(...) ; state.put(...) ; except NetworkError: backoff()`. Stamps
  `received_ms` on every frame; `un_watch` on teardown; `add_done_callback` purges a dead stream's
  state so it leaves zero residue. **Critical:** ccxt.pro does **not** back off internally — a bare
  `except: continue` becomes a hot reconnect loop that trips Binance's *300 new-connections/5min* ban.
  The loop MUST apply **jittered exponential backoff** (python-binance formula: `random()*min(60, 2**n−1)+1`,
  reset on success) — §11.C.
- **`state.py`** — the `MarketState` store (below). Immutable-swap or deepcopy-under-lock reads.
- **`freshness.py`** — the freshness verdict: two-axis gate (content: cache reached the last *closed*
  bar; wall-clock: ticked within ½-candle) + drop-forming (`[-2]`). Returns `Fresh|Stale|Absent` — never fabricates.
- **`rest.py`** — the thin ccxt-native REST path, used for exactly three jobs, each fail-loud + logged:
  `seed(symbol)` (warm-up history via `fetch_ohlcv`), `reseed(symbol, plane)` (on a detected gap /
  watchdog trip), `poll_positioning()` (the un-streamable `/futures/data/*` on its own cadence). Uses
  ccxt's native weighted throttler; no custom governor.
- **`health.py`** — **two-layer** staleness defense + a scheduled rotate:
  (1) ccxt.pro's free **ping-pong drop** (`keepAlive=5000ms × maxPingPongMisses=2 ≈ 10s`) catches a
  dead socket; (2) an **application no-message watchdog** — if `now − last_msg_ts` on a *connection*
  exceeds the bound (§11.B; live `ws_last_msg_age` is 0.3–0.5s, so **60s** is a 120–200× safe margin),
  force-reconnect / recreate the client. Plus a **scheduled 24h WS rotate** (Binance force-disconnects
  a connection at 24h; freqtrade pre-empts it once/day). **This kills the "frozen frame, errors=0"
  blackout class** — ccxt won't report a silent stream, so we time it. NB: for **event-driven** planes
  (trades, liquidations) silence ≠ staleness — a quiet symbol legitimately has no trades; the
  *transport* watchdog (per-connection, not per-plane) handles the dead-stream case, so a quiet tape is
  never mislabelled stale (§11.H).
- **`api.py`** — the *only* thing strategies call: `snapshot(symbol) -> MarketState | NotReady`.
  Replaces `tick_assembly`/`analyst_assembly`'s fetch-and-assemble. Strategies never touch ccxt.

### 6.3 The heart — `MarketState` / `Plane` make the invariant *structural*
Every datum is a `Plane` that knows its source and age; a read either returns fresh data or raises
`NotReady` with a reason. There is **no path** to a fabricated `0.0`, a phantom key, or a silent
fallback — I-6 becomes a type, not a review rule.

```python
class Source(Enum): WS; REST_SEED; REST_RESEED
class NotReady(Exception): ...            # carries the reason; strategy abstains loudly

@dataclass(frozen=True)
class Plane(Generic[T]):
    value: T | None
    source: Source
    received_ms: int          # wall-clock we got it
    event_ms: int             # exchange event time (bar close / book update)
    def read(self, now_ms: int, bound_ms: int) -> T:
        if self.value is None:                    raise NotReady(f"{type(self).__name__}: absent")
        if now_ms - self.received_ms > bound_ms:  raise NotReady(f"stale {now_ms-self.received_ms}ms>{bound_ms}")
        return self.value                          # only ever returns proven-fresh data

@dataclass(frozen=True)
class MarketState:
    symbol: str
    ohlcv: dict[str, Plane[list[Bar]]]   # per TF, CLOSED bars only (cache[:-1])
    book: Plane[OrderBook]; trades: Plane[deque]; mark: Plane[Mark]; funding: Plane[Funding]
    positioning: dict[str, Plane]        # OI/LS/taker/basis (REST poll)
    liquidations: Plane[deque]
```
`snapshot(symbol)` assembles this and, if any *required* plane is `Absent`/`Stale`, returns
`NotReady([...])` naming which — the strategy then abstains with a real reason (feeds the existing
`prizrak_abstain` / scanner-gate rendering), instead of consuming a stitched-together stale row.

### 6.4 How the three invariants become guarantees, not hopes
- **No empty** — `watch_*` blocks until the first frame; the engine does not expose a symbol until
  every required plane is warm (WS first-frame or REST seed). Capability-gate with a loud
  `UNSUPPORTED` sentinel, never `{}`.
- **No stale** — order books: native nonce re-seed (catch `ChecksumError`, continue). OHLCV: two-axis
  gate + drop-forming + watchdog force-reconnect. A plane past its bound → `NotReady`, loud.
- **No *silent* fallback** — REST touches only for seed / gap-reseed / positioning, each logged at
  INFO with the reason. The stale-prone `derive→cache→htf→REST→WS` ladder is gone.

## 7. Capacity — computed (not asserted)
For a 48-symbol scanner universe + 7 pinned, per-symbol streams `{kline 1m/5m/15m/4h, depth, aggTrade}`
plus 3 universe-wide `!arr` streams (mark, liq, ticker — one sub each, not ×48):

| Resource | Value | Headroom |
|---|---|---|
| WS subscriptions | **291** | Binance limit **1024/connection → 1 connection** |
| Subscribe ramp | 291 / (10 msg/s) ≈ **29 s** worst case; ~instant if combined-URL batched | under the 10 msg/s cap |
| asyncio watch tasks | ~291 | asyncio handles 10k+ |
| Cache memory | ohlcv 9 MB + trades 2 MB + books 0.2 MB ≈ **11 MB** | current RSS 300–700 MB |
| REST — warm-up seed | 240 `fetch_ohlcv` × w2 = 480 weight, **once**, throttled ~18 s | one-time |
| REST — `/futures/data` steady | pinned-7 **65/5min (6.5%)** → full-48 448/5min (45%) | of the separate 1000/5min window |
| REST — general weight 2400/min | steady-state **≈0** (everything else is WS) | <5% used |

The whole engine runs on **one WS connection + a REST trickle**. Binance's limits are not a design
constraint here; the old engine's weight-budget machinery was solving a problem the push model doesn't
have.

## 8. Build & cutover (greenfield — the live bot is disposable)
Because the running bot is a throwaway test run, there is **no incremental live-migration**:
1. Build `hunt_core/engine/` clean against the contracts above.
2. **Validate deterministically**: record a window of real WS+REST frames as fixtures; replay through
   the engine; assert freshness verdicts, drop-forming, gap-reseed, `NotReady` reasons. Add
   ccxt-safety + no-lookahead reviewers on `engine/**`.
3. **Point the strategies at `engine.snapshot()`**; delete `market/{rate_limit,capacity,weight_registry,
   ccxt_guard,ccxt_rest,streams}` and `data/{collect,frame_cache}` (~4000 LOC).
4. One clean test run: **zero `klines.*.stale`, zero 418, no frozen-frame window**, `used_weight` < target.

## 9. Consequences
- **~4000 LOC deleted**; data plane = "ccxt.pro + `MarketState` + ~40 lines of freshness discipline".
- The **blackout/stale defect class is designed out** (watchdog + native re-seed + drop-forming + typed
  fail-loud reads), not patched again.
- Tick-time weight budgeting, demand-shaping, and the ban circuit-breaker-as-controller **disappear**
  (the tick no longer fetches).
- We accept ccxt's single-window throttler (mitigation §5) and gain its community-maintained Binance
  quirk/reconnect handling.
- Secondary venues stay on ccxt — their abstraction is why ccxt is the right base.
- **Does not fix the shared-NAT-IP ban residual** (infra, §5) — orthogonal to the engine.

## 10. Verification
Deterministic fixture-replay parity + `vulture`/`mypy`/`ruff`/full suite + ccxt-safety & no-lookahead
reviewers on `engine/**`. End state on one test run: **zero `klines.*.stale`, zero 418**,
`used_weight` < target, no frozen-frame window in `data_plane_audit`.

---

## 11. Grounded parameters — no invented magic numbers

Every value is traced to one of three sources: **LIVE** (measured on the running bot, PID 23260,
`data_plane_audit`), **DOC** (Binance USDⓈ-M developer docs — the authoritative cadence/limit), or
**PROJECT** (a real repo, named). Values with **no defensible source are flagged `⚠ MEASURE`** — they
must be calibrated fail-loud against our own live logs, never copied as a constant.

### A. WS cache sizes (ccxt.pro options)
| Param | Value | Source |
|---|---|---|
| `OHLCVLimit`, `tradesLimit`, `ordersLimit`, `watchOrderBookLimit` | **1000** (ccxt.pro Binance default) | PROJECT `ccxt/pro/binance.py` describe().options — eviction bound, not fetch size; no project lowers it |
| `watchOrderBookRate` | **100 ms** | PROJECT same |

### B. Staleness → force-reconnect (the load-bearing timeout)
| Layer | Value | Source |
|---|---|---|
| ping-pong drop (transport) | `keepAlive 5000ms × maxPingPongMisses 2 ≈ **10s**` (free in ccxt.pro) | PROJECT `ccxt async ws/client.py` |
| no-message watchdog (app) | **60 s** (python-binance `NO_MESSAGE_RECONNECT_TIMEOUT`); cryptofeed uses 120s, OctoBot 240s | PROJECT + **LIVE** (`ws_last_msg_age` median **0.3s**, max 0.5s → 60s = 120–200× margin; 240s too slow for no-stale) |
| watchdog check interval | **30 s** | PROJECT cryptofeed `timeout_interval` |
| periodic full order-book re-snapshot | **3600 s** hard backstop | PROJECT hummingbot `FULL_ORDER_BOOK_RESET_DELTA_SECONDS` |
| scheduled WS rotate | **24 h** (Binance force-disconnects at 24h) | DOC futures WS + PROJECT freqtrade (rotates once/day) |

### C. Reconnect backoff (ccxt.pro does NOT do this for you)
| Param | Value | Source |
|---|---|---|
| formula | `delay = random()*min(60, 2**attempt − 1) + 1` (jittered expo, reset on success) | PROJECT python-binance `reconnecting_websocket.py` — most ban-safe; cryptofeed's uncapped `delay*=2` overshoots |
| cap | **60 s** | PROJECT python-binance |

### D. Order book depth
| Choice | Value | Source |
|---|---|---|
| **Preferred: partial book** `@depth20@100ms` | self-contained snapshot each update, **no local-book maintenance, no resync bug surface** | DOC futures WS + PROJECT cryptofeed `valid_depths` — best fit for "no stale/no empty" |
| Alt: diff depth + REST snapshot | snapshot `limit=1000`, nonce-validated | PROJECT ccxt.pro/hummingbot (auto-handled natively) |
| Native update speed | **250 ms** default (100/500 ms available) | DOC futures WS |

### E. REST poll cadences (the only recurring REST)
| Data | Cadence | Source |
|---|---|---|
| OI current (`/fapi/v1/openInterest`, w1) | **60 s** | PROJECT cryptofeed `delay=60` |
| **funding / mark price** | **take from WS `@markPrice` (`r`,`T` fields) — do NOT REST-poll** | DOC Mark-Price-Stream — removes a REST plane vs current design |
| Tier-B stats: OI-hist, top/global L/S, taker, basis (`/futures/data/*`) | **300 s (5-min), boundary-aligned** — Binance computes these every 5 min; faster = duplicate + budget waste | DOC futures-data (native granularity **5 min**) |

### F. Binance native cadences (freshness ground-truth) — all DOC
| Stream | Update | | Stream | Update |
|---|---|---|---|---|
| `@aggTrade` | 100 ms | | `@kline_*` | 250 ms |
| `@markPrice` | 3000 ms (`@1s`→1000) | | `@depth` | 250 ms (100/500 avail) |
| `@bookTicker` | real-time | | `@forceOrder` | ≤1/symbol/1000ms |
| funding settle | every 8 h | | | |

### G. Connection limits / sharding — all DOC (futures)
| Limit | Value |
|---|---|
| streams / connection | **1024** (we need 291 → 1 connection; shard at ~200/conn if universe grows) |
| SUBSCRIBE msgs / sec | **5** (warm-up ramp must respect this) |
| new connections / IP | **300 / 5 min** (⇒ backoff §C is mandatory) |
| connection lifetime | **24 h** (⇒ scheduled rotate §B) |
| ping / pong | server ping 3 min / pong within 10 min |

### H. Per-plane freshness bound (`Plane.read(bound_ms)`)
| Plane | bound | Source |
|---|---|---|
| price / bbo | **5 s** | LIVE (age 0.4s) + DOC (bookTicker real-time) |
| depth | **5 s** + 3600s resnapshot backstop | LIVE (age 0.4s, ttl_hint 5s) + PROJECT hummingbot |
| markPrice | **10–15 s** (3–5× the 3s cadence) | DOC (3s) × project multiple |
| kline `<tf>` (closed) | **`interval + 20 s`** (e.g. 15m→920s, 4h→14420s) | PROJECT freqtrade (only published closed-bar freshness formula; +20s = observed post-close emission lag) |
| OI-hist / L/S / taker | **native cadence + margin** (5min → ~360 s) | DOC (5-min granularity) |
| funding | **1× interval + margin** (8h) | DOC |
| **trades / liquidations** | **⚠ MEASURE** — event-driven; *silence ≠ stale* (quiet symbol = legitimately no trade). Use the transport watchdog (§B) for dead-stream; do NOT put a tight per-plane timeout here. | LIVE per-symbol calibration; no project gives a multiple |
| **per-symbol WS-vs-exchange emission lag** | **⚠ MEASURE** — freqtrade's `+20s` is only a starting point | LIVE |

**The two `⚠ MEASURE` rows are the honest boundary:** every other number here is cited; those two are
event-driven/exchange-specific and must be set fail-loud from our own live distribution, not invented
(this is exactly the "don't invent a 2.5% threshold" defect class — so they are parameters, calibrated,
logged, and revisited, never hardcoded constants presented as fact).
