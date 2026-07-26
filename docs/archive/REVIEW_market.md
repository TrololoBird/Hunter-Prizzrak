# REVIEW_market — `hunt_core/market/`: the CCXT data plane

> **АРХИВ** (2026-07-12). Ревью «всех 14 модулей `hunt_core/market/` (7 302 LOC)». Пакет расформирован 2026-07-19: транспорт → `hunt_core/engine/`, в `market/` остались только символы, гейт торгуемости, шаг цены и egress. Документ целиком про снесённый код.
>
> Перенесён из корня репозитория 2026-07-26: в корне он читался как действующая
> инструкция. История правок — в git.

Audience: project maintainer. Scope: all 14 modules of `hunt_core/market/` (7,302 LOC). Method: full read of the current code + external survey (≥10 references per subsystem). First doc in the `REVIEW_<pkg>.md` series (`PROJECT_MAP.md` §7); template = `MAPS_REVIEW.md`.

Confidence tags used throughout: **[confirmed]** = read directly in code · **[inferred]** = deduced from code + platform constraints · **[external]** = from docs/projects outside this repo.

Cross-refs: WS-1 (runtime stability), ADR-0001 (`docs/adr/0001-weight-governor-admission-control.md`), WS-3.1 (structlog), WS-3.2 (Pydantic-over-dataclass), `docs/ARCHITECTURE.md` §4 (data-plane resilience contract).

## 0. The IP-weight ceiling (what no client-side design can beat)

Analogous to `MAPS_REVIEW.md` §0: facts about the platform that cap what any redesign can promise.

1. **The limit is enforced server-side, per IP, on a fixed clock minute** [external]. Binance USDⓈ-M REST is weight-budgeted (2400/min REQUEST_WEIGHT); the server's minute window is not phase-synced with any local rolling window, so a local estimator is structurally ≥0 drift from the server counter. ADR-0001's `MARGIN + RESERVE` is the only honest posture.
2. **The IP is shared beyond this process** [confirmed/inferred]. `capacity.py:30-36` records that the deploy host egresses through a rotating private-NAT pool — *other tenants of the same NAT IP* consume the same budget, invisibly. An in-process governor can make *its own* overrun impossible, never the IP's.
3. **418 is a WAF verdict, not a weight verdict** [confirmed]. A real ban was observed at 900 req/5min on `/futures/data/*` — *under* the documented 1000 cap (`capacity.py:22-29`), and cold-start bursts on a fresh NAT IP tripped 418 within minutes (`capacity.py:32-36`). Request *rate shape* matters independently of weight: smoothing (already implemented, `rate_limit.py:37-49`) is load-bearing, not optional.
4. **Requests during a ban extend the ban** [external, load-bearing]. This makes "probe to see if the ban lifted" actively harmful — see finding §1-F3.

## 1. REST client & pacing stack (`client.py` · `ccxt_rest.py` · `rate_limit.py` · `ccxt_guard.py` · `capacity.py`)

### What it does today [confirmed]

Five modules form one pacing pipeline. `rate_limit.py` (192) holds the primitives: `WeightBudgetManager` (rolling-60s deque of (ts, weight)) and `SlidingWindowRateLimiter` (request-count window, optional `smooth_burst` spacing admissions at the sustained rate). Both `acquire` paths beat the WS-1.1 heartbeat before pacing sleeps (`rate_limit.py:111,190`) and give up with a catchable `TimeoutError` at 200s — deliberately inside the 300s watchdog (`rate_limit.py:73,161`). `ccxt_rest.py` (236) is the hub: `HuntCcxtRestGate` composes **process-global** budgets (`_GLOBAL_WEIGHT_BUDGET` at 1500/min pace, `_GLOBAL_REQUEST_BUDGET` 1000 req/min, `_GLOBAL_FAPI_BUDGET` 450 req/5min) plus one IP-wide `CcxtGuard` — correctly shared across every client instance because Binance limits are per-IP (`ccxt_rest.py:31-50`). `ccxt_guard.py` (301) classifies CCXT errors into `ip_ban | rate_limit | transport | other`, parses Retry-After / "banned until <epoch_ms>" / `banDuration` from exception text, and caps any parsed pause at 1h (`_MAX_SANE_PAUSE_S`, guarding against a past bug where an absolute epoch-ms was treated as relative seconds — `ccxt_guard.py:38-48`). `capacity.py` (211) holds the limit constants and `HuntLoadPlanner.plan_tick`, which rotates symbols between `full`/`fast` snapshot tiers against a static per-symbol weight estimate. `client.py` (2,294) is `HuntCcxtClient`: exchange lifecycle, ~90 fetch methods routed through the gate (`_rest_call`/`_direct_binance_fetch`/`_fapi_call`, `client.py:157-203`), ~24 hand-rolled TTL cache dicts (`client.py:104-131`), a secondary-exchange sub-client, clock sync against server time (`client.py:274-291`), and — after the class — an 11-function depth/orderbook analytics library (`client.py:1918-2276`).

Good to keep: the process-global budget singletons (the comment at `ccxt_rest.py:31-36` is exactly right); the `smooth_burst` cold-start smoothing; the 200s-inside-300s timeout invariant with heartbeat beats; the epoch-ms ban-parse fix with its 1h sanity clamp; the `watchOrderBookLimit=20` snapshot-weight cap (`factory.py:62-68`).

### External state-of-the-art (≥10)

Primary sources first; full URLs in References. **[external]** throughout.

1. **Binance futures General Info + exchangeInfo** — canonical semantics: per-IP (not per-key), `X-MBX-USED-WEIGHT-1M` on every response, 429 → continue → 418, bans 2min→3days; `rateLimits` confirms `REQUEST_WEIGHT/MINUTE/2400` for fapi. Some endpoint families have *side-pools* (funding-rate endpoints share 500/5min) — a correct registry is multi-bucket, like Binance's own `rateLimits` array.
2. **Binance futures WS-API General Info** — WS-API request/response weight IS shared with REST against the same 2400, and each WS handshake costs 5 weight. Crucially, **fstream market-data streams are governed by connection/message limits, not REQUEST_WEIGHT** — so the RESERVE (ADR pillar 2) is really for ccxt.pro's REST depth-snapshot seeds + handshakes, not for streaming itself.
3. **Binance spot LIMITS** — `Retry-After` on 429/418 is explicit for spot (6000/min budget — different counter than fapi; ammunition for F2). For futures it's probable but not clearly documented: verify empirically before the ban-guard depends on it.
4. **CCXT `async_support/base/throttler.py`** — token-bucket or rolling-60s-window modes, purely local, never reads headers, no priorities/reserve. `enableRateLimit` is per-call spacing (ADR-0001:85 is right). CCXT #13949 (maintainers: uniform rateLimit is wrong, per-endpoint weights needed) and **#27844** (fetchOHLCV default limit=1500 silently picks the max-weight variant — weight is a function of endpoint *and* parameters) mean registry entries must be parameter-aware functions, not constants.
5. **Hummingbot `AsyncThrottler`** — the closest existing analogue to the WeightGovernor: static declared `RATE_LIMITS` registry, `LinkedLimitWeightPair(pool, weight)`, rolling 60s window, single async-context-manager choke point, global `rate_limits_share_pct` headroom (their MARGIN). No header feedback at all.
6. **Freqtrade** — delegates to ccxt spacing + reactive `DDoSProtection` retries; its own issue tracker (#11247) documents bans on sustained bulk workloads. Evidence that spacing-only fails for exactly this system's workload shape.
7. **binance-connector-python (official SDK)** — does no client-side throttling; exposes the headers and leaves pacing to the app. There is no vendor-blessed throttler to reuse.
8. **cryptofeed / tardis-machine** — the data-collection peers are purely reactive (429 wait-and-retry, env-var pacing knobs). The governor design is ahead of, not behind, peer practice.
9. **Google SRE "Handling Overload"** — canonical client-side adaptive admission: excess load "fails locally without even reaching the network"; reactive backoff wastes the very budget it protects.
10. **Netflix concurrency-limits / AWS SDK adaptive retry** — production admission-control precedents; AWS documents the single-shared-limiter failure mode (one throttled bucket starves unrelated traffic) → per-bucket accounting inside the one governor, which is also what QoS classes need.
11. **GCRA (brandur.org) / aiolimiter / limits** — off-the-shelf primitives; GCRA is O(1) and weighted but window-faithful accounting (matching Binance's minute semantics) matters more near the boundary — hummingbot and ccxt's rolling mode both chose windows.
12. **Header-vs-local (dev.binance.vision + practitioner posts)** — the header is authoritative but *lagging* (excludes in-flight) and IP-global (includes foreign consumers behind the same NAT). Nobody mainstream reconciles both signals; the ADR's static-registry-leading + header-as-drift-check hybrid would exceed every surveyed implementation.

### Findings & recommendations

- **F1 — ADR-0001's diagnosis is confirmed at code level, with one correction** [confirmed]. Confirmed: weights are caller-declared with a default of `weight: int = 5` (`ccxt_rest.py:177`, `client.py:162`) — no static endpoint registry; `HuntLoadPlanner` shapes *tiers*, never *drops* demand (`capacity.py:143-158` always assigns every symbol a tier; `max_full` is even floored at `min_full_slots` **above** budget, `capacity.py:140`); planner and gate are not wired to each other (planner output is advisory; the gate discovers overload by sleeping). The correction: the ADR claims the spot companion consumes weight "without going through `acquire`" — **false**; `spot.py:70` routes through `acquire_binance_weight` on the shared gate. The spot problem is the opposite one (F2). A second refinement from the official docs [external]: fstream market-data streams consume **no** REQUEST_WEIGHT (connection/message limits only) — the weight ccxt.pro actually costs is its REST depth-snapshot seeds (already capped at depth-20, `factory.py:62-68`) and WS-API handshakes (5 each), so pillar 2's RESERVE should be sized to those, not to "streaming" — and pillar 4 (push-first) is even stronger than the ADR claims: streamed data is genuinely weight-free. **Recommendation:** implement ADR-0001 as written with pillar 2 re-scoped (RESERVE = ccxt.pro snapshot seeds + handshakes; spot moves to its own budget per F2), and fix F2 first — it corrupts the accounting the governor will rely on. *(Confidence: high.)*
- **F2 — Spot header cross-contaminates the futures weight budget** [confirmed]. Binance **spot** (`api.binance.com`) and **futures** (`fapi.binance.com`) have *separate* IP weight counters and limits, but `spot.py:78` calls `sync_weight_from_exchange(self._ex)` on the shared gate: the spot `x-mbx-used-weight-1m` header `force_floor`s the *futures* budget deque (`ccxt_rest.py:135-149` → `rate_limit.py:131`). Failure mode: spot's counter (limit 6000) reads e.g. 800 while futures local reads 300 → +500 phantom weight injected → futures pacing sleeps for capacity it actually has; and spot calls also double-charge the futures budget via `acquire_binance_weight`. **Recommendation:** give the spot companion its own `WeightBudgetManager` (spot limits) and never floor one venue's budget with another venue's header. One-line-class fix; do it before the governor. *(Confidence: high.)*
- **F3 — `await_pause` lets requests out during an active ban** [confirmed]. `HuntCcxtRestGate.await_pause` sleeps `min(remaining, cap_s=120)` **once** and returns (`ccxt_rest.py:121-133`); every `invoke*` then proceeds to the network. During a 30–60 min 418 pause (`_DEFAULT_IP_BAN_PAUSE_S=1800`, clamp 3600), a request escapes every ≤120s — and Binance *extends* bans for requests made during them (ADR-0001:26). The guard's own pause bookkeeping is undermined by its gate. **Recommendation:** loop until `remaining_pause_s()==0` (still beating the heartbeat, still raising the catchable timeout at 200s so the tick fails fast) — or better, surface `ip_ban` to the cycle loop as the ADR's skip-REST-phase circuit breaker so ticks degrade to WS-only instead of queueing. *(Confidence the leak is real: high; ban-extension impact: [external], verify against Binance docs.)*
- **F4 — `invoke_fapi` never charges the weight budget** [confirmed/inferred]. `acquire_fapi`/`invoke_fapi` acquire the request window + fapi window but not `weight_budget` (`ccxt_rest.py:104-107,151-169`). If `/futures/data/*` responses carry IP weight (they return the `x-mbx-used-weight-1m` header), the cost is only captured after-the-fact by `force_floor` — exactly the reactive pattern the ADR abolishes. **Recommendation:** the governor's static registry must be **multi-bucket** (Binance's own `rateLimits` array implies it: REQUEST_WEIGHT + the `/futures/data/*` window + side-pools like funding's shared 500/5min) with a `/futures/data/*` entry; until then, charge `invoke_fapi` a nominal weight. Registry entries must be parameter-aware functions, not constants — klines/depth weight scales with `limit` (ccxt #27844). *(Confidence: high on the multi-bucket need; medium on the exact fapi-data weight values.)*
- **F5 — `client.py` god-file: three separable libraries in one** [confirmed]. (a) exchange/session lifecycle + gate plumbing; (b) per-metric fetchers with ~24 copy-paste TTL cache dicts (`client.py:104-131`) — a generic `TTLCache[K,V]` helper would delete ~15 near-identical `get_cached_*` bodies; (c) the depth-analytics free functions (`depth_imbalance_*`, `detect_wall_clusters`, `WallCluster`, `client.py:1918-2276`) which are *analytics*, imported by `maps/`, `features/`, `data/` — the reverse edge of the `market↔maps` cycle. **Recommendation:** extract (c) to a leaf module (e.g. `hunt_core/maps/depth_analytics.py` or a new `hunt_core/microstructure/`), killing the cycle's reverse edge for free; then split (b) behind a cache helper. Route via WS-3 refactor phase, after the governor. *(Confidence: high.)*
- **F6 — planner smells** [confirmed, minor]. `_rot_rank` calls `ordered.index(sym)` inside a sort key — O(n²) on every tick (`capacity.py:122-125`); `EST_FAPI_*` telemetry knowingly drifts from `rest_pack_specs()` and is "silently misleading monitoring" per its own comment (`capacity.py:43-51`); `min_full_slots` floor can push the estimate past budget with no log. **Recommendation:** precompute an index map; derive per-tier fapi counts *from* `rest_pack_specs()` instead of parallel constants (single source of truth); log when the floor overrides the budget.
- **F7 — `is_ccxt_ip_ban`/`is_ccxt_rate_limited` allocate a fresh `CcxtGuard` (with policy + telemetry) per exception** [confirmed, minor] (`ccxt_guard.py:268-273`). Classification is stateless — make `classify` a module function and have the guard call it, not vice-versa.

## 2. WS streams (`streams.py` · `factory.py`)

### What it does today [confirmed]

`factory.py` (571) builds the CCXT config: `newUpdates` delta mode, keepalive 180s for Binance (raised from 30s after event-loop saturation caused self-closes every ~79s — `factory.py:84-92`), 20s for Bybit/OKX (`factory.py:186-190`), and caps the Pro order-book REST seed at depth 20 — the comment documents the incident math: 135 symbols × weight-20 snapshots on reconnect = 2700 weight in seconds → 418 (`factory.py:62-68`). It also assembles `HuntMarketPlane` (client+streams+spot) with a lazy import cycle to `client`/`streams`/`spot`. `streams.py` (1,793) is `HuntCcxtStreams`: ~15 watch-mux loops spawned from capability checks in `start()` (`streams.py:909-961`), symbol rotation capped at `_MAX_SYMBOL_STREAMS=100`, in-memory ring buffers (liquidations 8k, agg-trades 2k per symbol), kline-close detection with per-interval waiting/ready queues, and reconnect machinery: fatal-transport classification (`streams.py:311-324`), one-flight reconnect task with an 8s re-entry throttle (`streams.py:361-395`), a 45s post-reconnect quiet window suppressing 1006 cascades (`streams.py:390,406-410`), pre-emptive eviction of stale `/public/ws/…` clients after symbol rotation changed the stream URL (`streams.py:326-345`), and — the notable pattern — a dying watch loop parks itself with `await asyncio.sleep(300)` so the reconnect task can cancel it before it re-subscribes on the dying exchange (`streams.py:421-427`).

Good to keep: the quiet window + one-flight guard + parking trio (each closes a distinct duplicate-subscription race, and the comments document which); the stale-client eviction; the kline-backlog drain on reconnect; the `watchBidsAsks` removal note (`streams.py:941-942`, 4004 churn).

### External state-of-the-art (≥10)

**[external]** throughout; URLs in References.

1. **Binance futures WS market-stream rules (official)** — 1024 streams/connection (the old 200 figure is stale), 10 inbound msg/sec, 24h forced disconnect, server ping every 3min / pong within 10min. The spot numbers differ (5 msg/sec, 20s ping, 300 connection attempts/5min/IP — the safe planning figure for futures too since Binance says "IPs that are repeatedly disconnected may be banned").
2. **The fstream URL split (official change notice)** — market data moved to `wss://fstream.binance.com/public` (high-frequency) and `/market` (regular); legacy URLs were supported "until 2026-04-23" and afterwards only receive the public-category data. See F8a.
3. **`!forceOrder@arr` is a lossy snapshot, not a throttle** — "only the latest one liquidation order within 1000ms will be pushed"; intermediate orders are *dropped*, and there is no public REST substitute (`/fapi/v1/forceOrders` is auth-only). Liquidation volume built from this stream structurally undercounts during cascades — the honest ceiling for the maps/liq subsystem (consistent with `MAPS_REVIEW.md` §0).
4. **Local order book, futures rule** — each depth event's `pu` must equal the previous event's `u`, else re-snapshot from REST (futures rule differs from spot's `U == prev_u+1`). Whether ccxt.pro's binance implementation applies the futures rule for these symbols is a verify-item for the push-first migration.
5. **ccxt.pro architecture (manual + `ws/client.py` source)** — one `Client` per URL; per-`message_hash` futures; on error/close **every pending future on that client is rejected at once** — the structural origin of 1006 cascades; reconnect is *lazy* (the next `watch*` call re-subscribes), there is no supervisor.
6. **ccxt.pro known issues** — #22662 (`watch_order_book` silently hangs forever; no stall detection), #20667 (random symbol subsets stop updating with no error), #10955/#14086 (1006 storms + community restart patterns), #23972 (`Promise.race` future leak in long-running watch loops), #10786 (leaks from re-instantiating exchange objects as a reconnect workaround — directly relevant to `reset_pro_exchange`). #22662/#20667 are the external proof of F9.
7. **cryptofeed `connection_handler.py`** — the closest OSS analogue: per-connection `_watcher` closes the socket when `now - last_message > timeout` (default check 30s), retry loop with `delay *= 2` (uncapped, no jitter — its known weakness), fatal-vs-retryable exception classification.
8. **unicorn-binance-websocket-api** — managed reconnect with an explicit stream-signal state machine (CONNECT / FIRST_RECEIVED_DATA / DISCONNECT / STREAM_UNREPAIRABLE) — a clean vocabulary for the rebuild orchestration; STREAM_UNREPAIRABLE ≈ `_ws_transport_fatal`.
9. **binance-futures-connector (official)** — deliberately ships *no* automatic reconnection (issue #149): Binance considers reconnect policy the application's job.
10. **Tardis.dev capture methodology** — connection-count decided per exchange-limit case; subscription-response validation; stale-connection detection with automatic restarts; ~99.9% completeness as the achievable bar.
11. **AWS backoff-and-jitter (blog + Builders' Library)** — full jitter more than halves contention among simultaneous re-connectors; retry budgets and capped retries. The standard alternative to a fixed 45s quiet window + fixed 8s throttle.
12. **Notably absent externally**: nothing in the surveyed libraries parks a dying loop (`sleep(300)`) — everyone cancels-and-rebuilds. The parking trick is original; F10 makes it event-driven so its one failure mode disappears.

### Findings & recommendations

- **F8a — fstream URL migration: RESOLVED, no action needed (initial read was wrong)** [confirmed]. The legacy `wss://fstream.binance.com/ws`/`/stream` strings in `ccxt/pro/binance.py` are only the *base constants*: since ccxt **v4.5.44** (PR #28091, merged 2026-03-16, plus fixes #28377/#28596 — all ancestors of the installed 4.5.59) a runtime `getWsUrl()` rewrites them per stream category. Verified against the installed package: depth → `/public/ws`; markPrice, kline, **forceOrder** → `/market/ws`. Binance's 2026-04-23 cutoff did land as announced (legacy connections keep only `/public`-category data — depth/bookTicker/trades — while kline/markPrice/forceOrder go silent; see jesse-ai #562 for the day-after breakage), but this repo was never exposed: ccxt migrated a month before the cutoff and `hunt_core/` contains no raw fstream URLs and no `urls['api']['ws']` overrides. **Standing constraints:** never downgrade ccxt below 4.5.44 (the migration floor); any future `urls` override must end in exactly `/ws` or `getWsUrl` passes it through untouched — i.e. back onto legacy semantics.
- **F8 — the transport class computes analytics** [confirmed]. `snapshot()` builds the liquidation heatmap inline — `build_liquidation_map`/`heatmap_to_market_dict`/`load_maps_config` are *top-level* imports (`streams.py:20-21`) used at `streams.py:594-616`, and `_record_liquidation` pushes into the maps store (`streams.py:817-824`). This is the forward edge of the `market→maps` inversion (§5a) and makes the WS class untestable without the analytics stack. **Recommendation:** `snapshot()` returns raw buffers + prices (it already exposes `liquidation_buffers()`, `streams.py:570-572`); the maps engine — already a store the streams push into — subscribes/builds. Mechanical move; the callers of `snapshot()` get the heatmap from `maps/engine` instead.
- **F9 — no per-stream staleness watchdog** [confirmed/inferred]. Health is one global `_last_msg_ms` plus a ticker-staleness count (`streams.py:218-236`). A single healthy mux (e.g. tickers) keeps `last_msg_age_s` fresh while another (e.g. liquidations — low-rate by nature) is silently dead; nothing restarts an individual stalled loop short of a full transport error. External handlers (cryptofeed's `_watcher`, unicorn, tardis) run per-channel no-message watchdogs, and ccxt.pro provably needs one supplied by the application: #22662 (order book hangs forever, no error) and #20667 (random symbol subsets go silent) are exactly this failure, and ccxt's own manual says reconnect is lazy — nothing re-subscribes until the app notices. **Recommendation:** track `last_msg_ms` per mux label; expose in `ws_health_metrics`; a mux stale beyond N× its expected cadence triggers the existing reconnect path. This becomes mandatory, not optional, once push-first (ADR pillar 4) makes WS the primary data source. *(Confidence: high.)*
- **F10 — reconnect has no backoff, and the parking sleep is a magic 300s** [confirmed]. `_reconnect_binance_pro` retries are throttled only by the 8s re-entry guard; consecutive failed reconnects loop at that fixed rate with no exponential backoff/jitter (`streams.py:364-367`), and the 300s park (`streams.py:427`) silently assumes the reconnect task always wins within 5 min — if reconnect *fails* (`streams.py:379-381` returns without respawn), parked loops wake onto a dead `_pro_ex`. **Recommendation:** exponential backoff with **full jitter** (AWS analysis: jitter more than halves contention among simultaneous re-connectors; also respects the ~300 connection-attempts/5min/IP budget) on reconnect attempts; park on an `asyncio.Event` set by the reconnect task instead of a timed sleep. Also watch ccxt #10786: repeatedly re-instantiating exchange objects (what `reset_pro_exchange` does) has leaked sessions historically — worth a memory check in the WS-1 live monitor. *(Confidence: high.)*
- **F11 — `_ws_transport_fatal` classifies by string-matching `repr(exc)`** [confirmed] (`streams.py:311-324`; "1006"/"4004" substrings, class-name matching). Brittle against ccxt.pro message changes, and overlaps `ccxt_guard.classify` without sharing code. **Recommendation:** fold WS classification into `ccxt_guard` (one classifier, table-tested — §6d).
- **F12 — config-by-env-var scattered through the hot path** [confirmed, minor]. `HUNT_CROSS_WS`, `HUNT_KLINE_WS_5M/15M`, `HUNT_MAPS_LIQ_CROSS` read via `os.getenv` at start/loop time (`streams.py:47-52,945-951`), some on every property access (`cross_ws_connected`, `streams.py:256-269`, with a redundant local `import os`). Belongs in the settings object (see `config` skill). Also `import time` shadowed locally at `streams.py:220`.

## 3. Cross-venue & spot companion (`cross.py` · `spot.py` · `live_price.py`)

### What it does today [confirmed]

`cross.py` (769) aggregates secondary-venue (Bybit/OKX/Bitget) order books, funding, OI, taker flow into consensus fields: stale-snapshot exclusion before merging books (`cross.py:440-459`), full-depth bin merging via the maps package (lazy imports, `cross.py:469-485`), per-venue + OI-weighted consensus for taker flow (`cross.py:501-556`). `spot.py` (193) is `HuntCcxtSpotCompanion`: fetch spot ticker + last-two 1m closes per symbol, derive `spot_lead_return_1m` and `spot_futures_spread_bps`, cache with 120s max-age, bounded concurrency 6. `live_price.py` (153) is the price oracle: fresh WS last → BBO mid → mark → book → stale fallback, with explicit source labeling and staleness flags — small and clean.

### External state-of-the-art (≥10)

**[external]** throughout; URLs in References.

1. **aggr / aggr-server (Tucsky)** — one adapter module per venue extending a common base, all emitting one normalized trade shape (exchange, pair, ts, price, size, side, liquidation-flag); liquidations are tagged trades riding the same aggregation path. The reference architecture for the CCXT secondary-venue adapters.
2. **Tardis.dev normalization** — factory-function normalizers per channel with a replaceable `Mapper` interface; every message carries dual timestamps (exchange vs local arrival) with an explicit fallback rule; book amounts are absolute levels, not deltas. The dual-timestamp convention is what `cross.py`'s `fetched_at_ms` staleness check approximates.
3. **cryptofeed** — standardized channels (L2_BOOK, TRADES, LIQUIDATIONS, OPEN_INTEREST) + canonical BASE-QUOTE symbol scheme with per-exchange `_parse_symbol_data`; normalizes trades to taker side even where venues report maker.
4. **CoinGlass aggregated liquidation/OI docs + practitioner caveats** — "aggregated" = per-exchange reported figures summed over a fixed venue list, each venue already under-reporting (Binance `!forceOrder` publishes ≤1 order/sec/symbol); heatmap levels are modeled estimates from OI + assumed leverage distributions. The honest framing for what this repo's cross-venue liq merge can claim.
5. **Hephaistos (academic)** — unified order book across 22 CEXes; names the non-simultaneity problem this repo's `_CROSS_BOOK_STALE_MS` exclusion addresses.
6. **CoinAPI order-book replay guide** — cross-exchange comparison requires globally normalized timestamps and shared schema; the timestamp-skew/symbol-mapping pitfall citation.
7. **Price-discovery literature (arXiv 2506.08718; JIMF spot-vs-futures)** — perp venues dominate BTC price discovery in most regimes, but Granger tests show mid-tier venues lead Binance more often than naive size-hierarchy assumes — grounding for the spot-lead/basis inputs and a caution against Binance-always-leads assumptions.
8. **Binance alternate REST endpoints (official)** — `api1`–`api4.binance.com` + `api-gcp.binance.com` are documented alternates; endpoint-level failover is officially sanctioned (relevant to F16's reframed future-work item).
9. **Circuit breaker (Fowler) + pybreaker/aiobreaker** — the canonical pattern the guard/preflight approximates by hand; maintained asyncio implementations exist.
10. **GCP health-check concepts** — TCP-connect checks are cheap but false-positive-prone (port open ≠ application alive); match check protocol to the service. Relevant to the TCP-only `proxy_reachable` preflight against an HTTPS dependency.
11. **Binance ban-recovery practice (official LIMITS + Binance Academy)** — the sanctioned recovery is waiting out `Retry-After`, prefer WS over REST polling; no reputable source endorses IP rotation, which sits on the wrong side of Binance ToS — the no-rotation design choice is compliance-correct, not just simpler.
12. **aiohttp long-running client practice (official docs + issue #1914)** — one `ClientSession` for the app lifetime, explicit `ClientTimeout`, `ttl_dns_cache` (the indefinite-DNS-cache bug bit exactly this collector shape when endpoint IPs rotate).

### Findings & recommendations

- **F13 — `cross.py` reaches into `HuntCcxtClient` privates** [confirmed]. `client._bin_sym`, `client._secondary_ccxt_symbol`, `client._get_secondary` — each suppressed with `noqa: SLF001` (`cross.py:510,520,523`). The secondary-venue access layer is split across two files with a private-API seam. **Recommendation:** when F5 splits `client.py`, extract the secondary-exchange sub-client as its own class owned by neither — `cross.py` and `client.py` both consume it publicly.
- **F14 — spot budget mischarge** — see F2 (the fix lives in this subsystem).
- **F15 — `cross.py`'s maps imports (5 sites)** — the remaining forward edges of §5a; same cut as F8: cross produces per-exchange snapshots, maps merges bins.
- Good to keep: stale-venue exclusion before book merge (`cross.py:440-459`) — external aggregators that skip this blend stale depth; `live_price.py`'s source-labeled fallback chain.

## 4. Network, symbols & gates (`network.py` · `symbols.py` · `symbol_gate.py`)

### What it does today [confirmed]

`network.py` (321): proxy URL resolution/masking, bounded TCP preflight `proxy_reachable` (3s), aiohttp session construction (SOCKS via `aiohttp-socks`, `rdns=True`; `ThreadedResolver` otherwise), `BanDetectionPolicy` (418/403/429), `is_proxy_transport_error`, and `detect_local_proxies` — a hardcoded local port scan (WARP/Clash/sing-box/Tor) used **only** by the Telegram delivery path; the module docstring documents that the rotating Binance proxy pool was deliberately removed after the 2026-07-11 incident (`network.py:1-15`). Binance now connects direct (`factory.py:563-571` ignores its `settings` parameter and hardcodes `proxy_url=None, trust_env=False`). `symbols.py` (156): strict Binance-id ↔ CCXT-unified mapping through `exchange.market()` only, with a linear-USDT-swap resolver that falls back to a full `markets` scan. `symbol_gate.py` (36): thin façade over `filter_tradable_symbols`.

### External state-of-the-art (≥10)

Shares the reference pool of §3 (subsystem B of the cross/network survey).

### Findings & recommendations

- **F16 — the resilience contract is implemented except its named future-work item** [confirmed]. Preflight ✓, universe-health alerting ✓ (in `diagnostics/`), supervision ✓, progress watchdog ✓ (WS-1.1, verified: `runtime/heartbeat.py` + rearmer `runtime/cycle/_cycle_loop.py:448-467` + beats in the pacer). Missing: proxy/endpoint **failover** mid-run (`docs/ARCHITECTURE.md:136-137`). Since Binance egress is now direct, the modern form of that item is *REST endpoint failover* (Binance publishes alternate API clusters) rather than proxy rotation. **Recommendation:** keep it on the backlog in that reframed form; not urgent while direct works.
- **F17 — `create_hunt_market_plane_from_settings(settings)` ignores `settings`** [confirmed] (`factory.py:563-571`). Honest per its docstring, but a trap: callers believe config applies. Rename or actually consume the network section.
- **F18 — `filter_tradable_symbols` is O(symbols × markets) worst-case** [confirmed, minor]. Each unknown symbol triggers a full `exchange.markets` scan (`symbols.py:75-84`). Fine at ~150 symbols / ~3k markets; build a one-shot id-index if the universe grows.
- **F19 — `BanDetectionPolicy` counts 403 as a ban and any `TimeoutError` as ban-worthy** [confirmed] (`network.py:172,183-189`) — with `ban_on_timeout=True` a slow proxy reads as a ban. Currently mostly inert (classification feeds `transport`, not pauses), but the flag names promise more than the semantics deliver. Tighten when folding classifiers together (F11).

## 5. Cross-cutting

- **(a) The `market→maps` inversion — all 10 sites** [confirmed]: `streams.py:20` (`build_liquidation_map`, `heatmap_to_market_dict` — top-level), `streams.py:21` (`load_maps_config` — top-level), `streams.py:818` (`get_map_store`), `cross.py:469,470` (`load_maps_config`, `merge_full_depth_bins`), `cross.py:666,671,726` (liq helpers + config), `cross.py:748` (`get_map_store`), `client.py:890` (`oi_bars_from_frames/scalar_series`). Plus the *reverse* edge: `maps/orderbook.py` imports `client.WallCluster` et al. The cycle exists because depth/liq *analytics* code sits on both sides. **The cut** (mostly mechanical): (1) move `client.py`'s depth-analytics free functions out (F5c) — removes the reverse edge; (2) `snapshot()`/cross return raw structures, maps builds (F8/F15) — removes the forward edges. After this, `market` imports `maps` zero times and the layering in `PROJECT_MAP.md` §2 holds.
- **(b) stdlib logging — 9 of 9 logging modules violate the structlog rule** [confirmed] (`client.py:37`, `streams.py:36`, `ccxt_rest.py:29`, `rate_limit.py:19`, `ccxt_guard.py` (via callers), `network.py:39`, `factory.py:26`, `spot.py:20`, `cross.py:18`, `symbols.py:11`). Zero structlog in the package; log calls are `%`-style `key=value` strings, so the migration is mechanical (`logger.info("event", key=value)`). Route via WS-3.1; convert whole-package in one PR to keep grep-ability.
- **(c) 16 `@dataclass` sites vs the Pydantic rule** [confirmed]. The frozen/slots value objects (`SpotMetrics`, `TickLoadPlan`, `WallCluster`, `PriceQuote`) are defensible; the *mutable state holders* (`HuntCcxtRestGate` `ccxt_rest.py:77`, `CcxtGuard`/`CcxtBanTelemetry` `ccxt_guard.py:51,78`, three in `streams.py`) are plain classes wearing a dataclass costume — being dataclasses buys nothing and invites accidental construction. WS-3.2: convert value objects to Pydantic (or grant a documented exemption for hot-path frozen slots), make state holders plain classes.
- **(d) Test coverage ≈ 1%** [confirmed]. Only `tests/test_proxy_preflight.py` (3 cases on `proxy_reachable`). Zero tests on the entire WeightGovernor blast radius. Minimal harness that pays for itself immediately, all pure-logic, no network: (1) **guard table-test** — a matrix of real CCXT exception texts (418 w/ "banned until <epoch_ms>", 429 w/ Retry-After, 1006, timeouts) → expected `BanKind` + pause; locks in the epoch-ms fix forever; (2) **budget timing tests** — `WeightBudgetManager`/`SlidingWindowRateLimiter` under a fake clock: admission math, smoothing spacing, the 200s TimeoutError, `force_floor` gap injection (and the F2 fix); (3) **planner regression test** (ADR-0001:90-91) — `plan_tick` never emits `estimated_binance_weight > target_weight_per_tick` (this test FAILS today via the `min_full_slots` floor — write it as the executable spec for pillar 3); (4) **`_ws_transport_fatal`/classifier table** once folded (F11).

## 6. Priority (what to change first)

Ordered; 0–3 are pre-governor stabilizers, 4–6 land with/after ADR-0001. All analysis here defers to WS-1 sequencing — nothing below touches strategy code.

> **Status (2026-07-12, same day):** items 1–3 are DONE. F2 fixed (`create_spot_rest_gate()` — own spot weight/request budgets + spot-sized `header_stop`, shared IP-wide ban guard; `ccxt_rest.py`, `spot.py`, `rate_limit.py`). F3 fully fixed: the ip-ban half landed as WS-1.2 (`RestBanSkip`, commit 73bb59f); the 429 half (pause > `cap_s` escaped after one chunk) now loops until the pause clears, bounded by the same 200s TimeoutError deadline as the budget acquires. F6 partially done (O(n²) rank index fixed; floor-over-budget now logged as `plan_full_floor_over_budget`). F7 done (`classify_ccxt_error` module function). §5d tests (1)–(3) landed as `tests/test_market_rate_gate.py` — 28 cases incl. the strict-xfail planner budget invariant, which is ADR-0001's acceptance spec. `ccxt_rest.py`, `capacity.py`, `rate_limit.py` and `spot.py` also converted to structlog — the whole pacing stack is now off stdlib logging (WS-3.1 head start; the edit guard blocks stdlib `logging` in new writes). F8a investigation: see finding.

0. **F8a fstream URL migration check** — ✅ resolved, no action: ccxt ≥4.5.44 already routes to `/public`+`/market` at runtime (verified on the installed 4.5.59). Constraint recorded: ccxt floor = 4.5.44.
1. **F2 spot/futures budget cross-contamination** — corrupts the accounting everything else trusts; ~20-line fix + budget test.
2. **F3 `await_pause` ban leak** — the one place the current design actively worsens a ban; small fix, pairs with the ADR's circuit-breaker.
3. **§5d tests (1)–(3)** — the guard/budget/planner tables are the regression net the governor refactor needs *before* it starts; (3) doubles as ADR-0001's acceptance test.
4. **ADR-0001 implementation** — with the F1 correction (RESERVE = ccxt.pro only) and F4 (fapi weight in the registry).
5. **§5a maps-inversion cut (F5c + F8 + F15)** — mechanical moves, kills the worst cycle in the import graph; schedule as the first WS-3 refactor since the governor touches the same files.
6. **F9/F10 per-stream staleness + reconnect backoff** — WS robustness; after the governor removes REST pressure, WS becomes the primary data path (push-first) and earns the hardening.

## References

REST pacing / admission control
· https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
· https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information
· https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-api-general-info
· https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits
· https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
· https://github.com/ccxt/ccxt/blob/master/python/ccxt/async_support/base/throttler.py
· https://github.com/ccxt/ccxt/issues/13949 · https://github.com/ccxt/ccxt/issues/27844
· https://hummingbot.org/connectors/connectors/api_throttler/
· https://github.com/hummingbot/hummingbot/blob/master/hummingbot/core/api_throttler/data_types.py
· https://www.freqtrade.io/en/stable/exchanges/ · https://github.com/freqtrade/freqtrade/issues/1764
· https://github.com/binance/binance-connector-python · https://github.com/sammchardy/python-binance/issues/398
· https://github.com/bmoscon/cryptofeed · https://docs.tardis.dev/tardis-machine/quickstart
· https://sre.google/sre-book/handling-overload/ · https://github.com/Netflix/concurrency-limits
· https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html
· https://brandur.org/rate-limiting · https://aiolimiter.readthedocs.io/ · https://limits.readthedocs.io/
· https://dev.binance.vision/t/what-does-x-mbx-used-weight-mean-in-response-header-of-restful-api/14337

WS streams
· https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams
· https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice
· https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams
· https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly
· https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Mark-Price-Stream
· https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md
· https://docs.ccxt.com/docs/pro-manual
· https://github.com/ccxt/ccxt/blob/master/python/ccxt/async_support/base/ws/client.py
· https://github.com/ccxt/ccxt/blob/master/python/ccxt/pro/binance.py
· https://github.com/ccxt/ccxt/issues/22662 · https://github.com/ccxt/ccxt/issues/20667
· https://github.com/ccxt/ccxt/issues/10955 · https://github.com/ccxt/ccxt/issues/14086
· https://github.com/ccxt/ccxt/issues/23972 · https://github.com/ccxt/ccxt/issues/10786
· https://github.com/bmoscon/cryptofeed/blob/master/cryptofeed/connection_handler.py
· https://github.com/oliver-zehentleitner/unicorn-binance-websocket-api
· https://github.com/binance/binance-futures-connector-python
· https://docs.tardis.dev/faq/general
· https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
· https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

Cross-venue / spot / network
· https://github.com/Tucsky/aggr · https://github.com/Tucsky/aggr-server
· https://docs.tardis.dev/node-client/normalization
· https://github.com/bmoscon/cryptofeed/blob/master/docs/exchange.md
· https://docs.coinglass.com/reference/aggregated-liquidation-history
· https://www.researchgate.net/publication/369588402 (Hephaistos unified order book)
· https://www.coinapi.io/blog/crypto-order-book-replay
· https://arxiv.org/abs/2506.08718 · https://www.sciencedirect.com/science/article/abs/pii/S0261560625001500
· https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information
· https://martinfowler.com/bliki/CircuitBreaker.html · https://github.com/danielfm/pybreaker · https://github.com/arlyon/aiobreaker
· https://docs.cloud.google.com/load-balancing/docs/health-check-concepts
· https://academy.binance.com/en/articles/how-to-avoid-getting-banned-by-rate-limits
· https://docs.aiohttp.org/en/stable/client_reference.html · https://github.com/aio-libs/aiohttp/issues/1914
