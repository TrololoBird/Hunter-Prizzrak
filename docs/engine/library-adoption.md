# HUNTER Engine — Final Library Adoption Recommendation

Synthesis of all six category reports, ranked by reliability leverage against the two invariants (I‑6 fail‑loud, I‑5 no‑lookahead) and the push‑state model. Guiding filter: replace a hand‑rolled surface with a documented/vetted one, or close a gap that has actually caused production failures — otherwise don't add weight.

Every ADOPT/TRIAL pick below is confirmed working on Python 3.14 (pure‑Python or ships cp314 wheels); the only real 3.14 gate left is polars‑talib, flagged in TRIAL.

---

## 1. ADOPT NOW (ranked by reliability leverage)

**1. prometheus-client — closes the #1 recurring production gap (silent blackout).**
Your own MEMORY is dominated by *silent* failures structlog cannot alert on: `stale-htf-cache-trap` (universe goes dark ~40 min in, never recovers, `errors=0`), `pinned-4h-stale-blackout`, dead‑proxy blackout → watchdog kill. A `Gauge engine_seconds_since_last_msg{venue,stream}` set from the WS handler's last‑message timestamp is a *fail‑loud instrument by construction* — when data stops it climbs unbounded and becomes alertable in seconds instead of discovered hours later via `ps`. Add `Counter`s for `ws_reconnects_total{venue}` and `staleness_reject_total{stream}` (wire at the existing `watch_symbol_data_reject` / `klines.4h.stale` sites) and a `universe_healthy_symbols` gauge. Module: `hunt_core/engine/` + `runtime/cycle/`, exposed via the existing aiohttp app. **3.14:** pure‑Python, non‑issue. **Caveat:** emit‑only — you still need a scraper/cron‑curl threshold to alert; label by venue/stream, **never per‑symbol** (cardinality explosion).

**2. talipp — replaces the largest hand‑rolled numeric surface (streaming indicators).**
The only library whose computational model *is* the engine's model: stateful per‑symbol `add()`‑on‑close is O(1) per bar, versus vectorized libs that recompute the whole column every WS tick and force you to re‑derive "which row is the newest closed bar" — the exact `idx=-2` off‑by‑one that has shipped here repeatedly. Replaces the hand‑rolled RSI/MACD/EMA/SMA/Bollinger‑width math in `features/`+`engine/`. Strong on both invariants: returns explicit `None` during warm‑up (propagate as NotReady, never a fabricated number), and if you only ever `add()` closed bars it *structurally cannot* see the forming candle (I‑5). MIT, zero‑dependency, pure Python. **3.14:** pure‑Python, runs out of the box (no cp314 classifier yet — validate with one smoke run). **Caveat:** streaming half only — it does **not** cover volume‑profile (domain histogram, stays hand‑rolled) or the batch/backtest recompute path; and its migration must be gated behind a golden‑reference test (see hardening order).

**3. hypothesis — mechanizes I‑5/I‑6 from prose into executable proof.**
Today both invariants are enforced only by prose + review agents — which is precisely "the part that actually rots." Hypothesis is the single tool that converts them to adversarial proof, and Polars ships *first‑party* strategies (`polars.testing.parametric`) so the feature engine is fuzzable with no hand‑written generators. Concretely: generate frames with `null_probability>0` / all‑equal columns / single‑row → assert the output is explicit `None`/NotReady, never `0.0`/`1.0` (hunts the falsy‑zero / phantom‑key family mechanically); a metamorphic property that appending a *forming* candle must not change any detector output (would have caught every `idx=-2` regression); and a `RuleBasedStateMachine` over `{bar_close, ws_gap, reconnect, stale_tick}` interleavings — the exact class of the stale‑cache/TTL‑drift deadlocks in your memory. Module: `tests/`. **3.14:** wheels incl. free‑threaded 3.14t. **Caveat:** slower than example tests — cap `max_examples` in CI and commit the `.hypothesis` DB (or fix a seed) for reproducibility.

**4. pydantic-settings — kills the documented config silent‑no‑op trap.**
CLAUDE.md and `config-file-map` both name it: "some documented keys are fallback‑wins in the loader — editing the TOML silently no‑ops." That is a config‑drift member of the I‑6 family. Replace the hand‑rolled loader with a typed `BaseSettings` + `SettingsConfigDict(extra='forbid')` so a typo'd/unread key is a loud `ValidationError` at startup, not a silent no‑op, with explicit source precedence (`init > env > config.toml > config.defaults.toml`) via `settings_customise_sources` + `TomlConfigSettingsSource`. Reuses Pydantic v2 (already shipped) — near‑zero added weight. Module: `domain/` settings loader. **3.14:** supported. **Caveat:** real migration cost — pin the *current* precedence in a test first, then swap the loader under it, or you'll flip which file wins for some keys.

**5. aiolimiter — closes the one rate‑limit gap ccxt does NOT cover.**
ccxt owns throttling for *exchange* calls only. The engine hits **CoinGecko** directly (dominance/marketcap dop‑factors, `/global`, `/coins/markets`) and its free tier is aggressively 429‑happy — currently unthrottled or hand‑throttled. `AsyncLimiter(max_rate, time_period)` is a vetted leaky bucket. Module: the CoinGecko/non‑ccxt HTTP path. **3.14:** pure‑asyncio, runs fine (classifier cosmetic). **Caveat:** scope strictly to non‑ccxt endpoints — putting it in front of ccxt calls double‑throttles and fights ccxt's own limiter.

**6. orjson — free WS‑decode speedup, zero code change (the quick win).**
ccxt **already auto‑detects orjson** and uses it for WS/REST JSON decode precisely for big fast‑dispatch messages, so `uv add orjson` accelerates your hottest deserialization path (inside ccxt) with no code change; also usable as the structlog JSON renderer. Module: transport, transparently. **3.14:** ships cp314 wheels. **Caveat:** none material — it only decodes, touches no validation semantics, so no I‑6 interaction.

**7. pytest-asyncio — clean async test harness (plumbing that de‑risks everything above).**
The engine tests hand‑roll `asyncio.run`, which gives a fresh loop per test → shared ccxt/aiohttp sessions error "attached to a different loop," and no async fixtures (can't express a warm‑engine fixture). `asyncio_mode="auto"` + a loop‑scoped warmed‑engine fixture removes that bug class and makes the Hypothesis stateful tests and the fake‑WS feed first‑class. Module: `tests/`. **3.14:** supported. **Caveat:** ergonomics, not invariant enforcement; historically churny — pin `asyncio_mode` and the version in `pyproject`.

**8. time-machine — deterministic wall‑clock for freshness tests.**
For any freshness assertion on wall‑clock `datetime`/`time.time()` (cross‑DOM `_DOM_ACTIONABLE_MAX_AGE_S`, HTF‑cache TTL edges, funding‑window recency): pin "now," advance across the TTL bound, assert reject/accept flips exactly at the edge. Faster and more async‑safe than freezegun, ships a freezegun‑migration CLI. **3.14:** wheels incl. free‑threaded. **Caveat (load‑bearing for this async engine):** it deliberately does **not** mock `time.monotonic` (mocking it freezes the asyncio loop). So for staleness measured on `monotonic()` — the correct, NTP‑immune choice — **inject a clock** (`Callable[[], float]` defaulting to `time.monotonic`) and pass a fake in tests; use time‑machine only for genuine wall‑clock/`datetime` paths.

**Free hardening, no new dependency (do alongside):** set `ConfigDict(strict=True)` on the fail‑loud‑sensitive Pydantic v2 models (confidence, funding, prices) — v2 is lenient‑by‑default and will silently coerce `"0"`→`0`, an I‑6 violation. And keep the `funding_stats.py` guard pattern (`None` below `min_records`, explicit `σ ≤ 1e-12`) applied uniformly wherever a numpy/scipy reduction feeds a signal — that hand‑rolled code is the reliability *standard* here, not a candidate for replacement.

---

## 2. TRIAL (spike / gate first)

- **TA-Lib 0.7.0 (test‑only golden reference).** Not a runtime dep — the offline oracle that pins talipp's and residual hand‑rolled formulas (RSI/MACD/EMA/BB/ATR) against canonical output on a fixed fixture, turning "battle‑tested formula" into a verifiable assertion. **3.14:** cp314 wheels shipped 2026‑07‑04 (the historic install pain is resolved). Gate: dev/test extra only, never `hunt_core/engine/`.

- **pandera (Polars backend) — OHLCV shape/dtype contract.** Codifies the hand‑rolled frame validation into a fail‑loud `DataFrameModel` (`strict=True, coerce=False`: dtypes, monotonic timestamp, no OHLCV nulls, `high>=low`, `volume>=0`) at the `data/collect.py`→`features/prepare_symbol` boundary. **3.14:** in classifiers, rides your working Polars. Gate: run at ingest boundaries only (REST warmup, each newly‑closed bar) — **never full‑validate per tick**; `coerce=False` is mandatory or it silently casts and violates I‑6.

- **scipy.stats — robust funding z, behind existing guards.** Take `median_abs_deviation` for a modified z‑score (a single 0.3% funding spike inflates sample‑σ and crushes the plain z) plus vetted distribution CDFs. **3.14:** 1.16.1+ ships wheels. ⚠️ **I‑6 hazard:** never use `scipy.stats.zscore` — on flat input σ=0 it silently returns NaN (0/0), a *regression* over your explicit `≤1e-12 → 0.0` reading; and `median_abs_deviation` on constant input returns 0, so it too must stay behind the existing guard. Adopt the primitive, not the convenience wrapper.

- **uvloop — WS‑concurrency loop swap.** Real latency/CPU win for many long‑lived `watch_*` streams, near‑zero code change. **3.14:** requires uvloop **≥0.22** AND the new `loop_factory`/`uvloop.run()` pattern (policies removed in 3.16). Gate: benchmark first — the engine is usually network‑bound on exchange RTT, and it's a C‑ext that couples you to MagicStack's wheel cadence on every future CPython bump. No I‑5/I‑6 effect.

- **tenacity — declarative retry for REST one‑shots.** For funding‑history, universe‑batch, mark/index OHLCV, and CoinGecko fetches (`wait_exponential_jitter` + max‑attempt + retry‑on‑type). **3.14:** pure‑Python, supported. Gate: **do NOT wrap the ccxt.pro `watch_*` loops** — ccxt.pro already does WS reconnect + exponential backoff + keepalive internally; stacking tenacity there double‑backs off, and its "retry until one call succeeds" model doesn't fit a perpetual stream. Respects I‑6 (re‑raises, never fabricates).

- **msgspec — dict→typed‑struct ingress (narrow, profile‑first).** Its raw‑decode edge is largely pre‑captured once orjson is installed; the remaining fit is strict‑by‑default validation of ccxt's loose payload dicts at `engine/` ingress (more I‑6‑aligned than pydantic's lenient coercion). **3.14:** cp314/cp314t wheels present. Gate: adopt only if profiling shows that ingress is hot — otherwise it's a *second* schema dialect for no measured win.

- **duckdb — SQL/OLAP over the parquet lake, research‑scoped.** SQL with predicate pushdown / out‑of‑core joins over the whole lake for `research/backtest_*.py` and corpus analysis, zero‑copy from Polars. **3.14:** wheels since 1.5.4. Gate: strictly under `research/` — it's batch OLAP, the opposite of push‑state; never near `hunt_core/engine/`, and handle SQL nulls explicitly to stay I‑6‑honest.

- **polars-talib — batch TA inside LazyFrame (hard 3.14 gate).** Only for warmup‑backfill / backtest recompute. **Gate: a cp314 wheel is UNCONFIRMED** on PyPI and it links the TA‑Lib C lib (painful source build) — if no cp314 wheel, **AVOID**; the narrow batch‑TA need is likely covered by TA‑Lib‑in‑tests without a second native dep. Vectorized → no push‑state help, and I‑5 slicing stays your responsibility.

- **dirty-equals / pytest-benchmark — optional ergonomics.** `dirty-equals` makes freshness/shape snapshot asserts readable (`IsFloat` distinguishes real‑zero from missing — I‑6‑aligned) but enforces nothing plain asserts can't; single‑maintainer 0.x. `pytest-benchmark` guards only the "incremental stays flat as history grows" principle on the talipp hot path — noisy in CI and the 3.14 experimental JIT makes microbench numbers non‑deterministic unless you pin the interpreter. Both nice‑to‑have, neither load‑bearing.

---

## 3. AVOID / NOT NOW

- **anyio** — redundant: `asyncio.TaskGroup` + `asyncio.timeout()` on 3.14 already give structured concurrency/scoped timeouts; wholesale adoption is a rewrite for capability you have.
- **backoff** — repo archived read‑only (2025‑08); tenacity is the strict superset.
- **aiohttp[speedups]** — ccxt owns its session/WS transport; nothing hand‑rolled to replace, and aiodns/Brotli are C‑exts for a benefit that's noise next to RTT.
- **bottleneck** — Polars rolling (`rolling_mean/std`, `ewm_mean`) already covers it with *better* semantics: `null` for insufficient window, not silent `NaN`. Adding it means a Polars→numpy→back round‑trip for zero gain.
- **numba** — 3.14 support exists (0.63+, but arrived ~2 months late, confirming the lag pattern) yet there is **no numba‑shaped hot loop** in a per‑bar push engine; volume‑profile is already `np.histogram` at C speed. Adds an LLVM toolchain, an upgrade‑cadence anchor, and — ⚠️ **I‑6 hazard** — inside `@njit`, `0.0/0.0`→NaN / overflow→inf propagate with *no warning at all*.
- **river** — right philosophy (single‑sample `update()`) but ⚠️ **I‑6 hazard:** `stats.Var` with <2 samples returns a fabricated `0.0` and `EWVar` seeds from the first value — the exact "no fabricated dispersion during warm‑up" violation; stateful accumulators also desync across WS reconnects, unlike cheap stateless Polars‑window recompute over small bounded windows.
- **statsmodels** — depends on **pandas** (mechanically banned, ruff TID251); and there's no OLS/ARIMA need — "funding trend" is a 4‑step sign check.
- **polars_ta (wukan1986)** — hard‑requires **pandas + numba**: violates the no‑pandas invariant at the dependency level and drags a 3.14‑risky JIT.
- **patito** — redundant with pandera for frame validation, higher bus‑factor (single maintainer), thinner docs, unconfirmed 3.14.
- **`ta` / tulipy / mintalib / PyIndicators** — pandas‑based (banned) / unmaintained ~7yr / self‑described experimental / newer‑less‑battle‑tested, respectively. talipp + TA‑Lib cover the surface.
- **opentelemetry** — needs a collector/backend; distributed traces don't map onto one long‑lived streaming loop. prometheus‑client covers the freshness/reconnect need at a fraction of the weight. Revisit only if a trace backend already exists in ops.
- **dynaconf** — its layered‑merge + silent‑override philosophy *institutionalizes* the fallback‑wins ambiguity you're eliminating; wrong philosophy for a fail‑loud project, and redundant with Pydantic v2.
- **pyarrow (explicit)** — already transitive under Polars; native Polars parquet + duckdb cover Dataset/zero‑copy. No reliability gain.
- **rich** — a second renderer fights "structlog everywhere / no stdlib logging"; structlog's `ConsoleRenderer` already gives dev output. Dev‑only at best.
- **platformdirs** — ergonomics (per‑OS dirs), not a blackout/staleness win; adopt only if paths ever leave cwd.
- **freezegun** — superseded by time‑machine on speed, 3.14 posture, and async‑safety (it touches monotonic paths that deadlock the loop). Migrate any existing use via time‑machine's CLI.
- **schemathesis** — architectural mismatch: it fuzzes an HTTP/GraphQL API *you host*; this engine serves none. The only overlapping need (fuzz inbound WS‑payload deserialization) is served by Hypothesis directly.

---

## I‑6 hazard watchlist (libraries that risk silently fabricating a value)

Record these so no future adoption reintroduces a silent fill:
- `scipy.stats.zscore` — silent NaN on σ=0 (0/0); **worse than the current code.** Use `median_abs_deviation` behind the guard instead.
- `np.nanmean` / `np.nanstd` on empty/all‑NaN → NaN + a swallowable `RuntimeWarning`. The *guard* (length/finiteness check), not the call, is what satisfies I‑6.
- `river.stats.Var` — fabricated `0.0` variance for <2 samples.
- numba `@njit` blocks — NaN/inf propagate with no warning.
- Pydantic v2 default coercion — `"0"`→`0` silently; set `strict=True` on fail‑loud‑sensitive models.
- pandera with `coerce=True` — silent casts; force `coerce=False`.

---

## 4. Engine hardening order

Test/observability scaffolding first (so the risky hot‑path swap is provable and any regression is visible), then the gap‑closers, then the hand‑rolled replacement last.

1. **orjson** (`uv add orjson`) — zero‑code, zero‑risk; ccxt picks it up immediately. Free warm‑up.
2. **Free I‑6 hardening** — `strict=True` on fail‑loud Pydantic models; audit that the `funding_stats` guard pattern is applied uniformly at every numpy/scipy reduction feeding a signal. No new dep.
3. **pytest-asyncio** — lay the async harness (warm‑engine fixture, fake WS feed) before writing any new test.
4. **hypothesis + time‑machine (+ injected monotonic clock)** — encode I‑6 (fabricated‑value hunts), I‑5 (forming‑candle metamorphic + `idx=-1`==last‑closed), and freshness‑boundary properties, plus a `RuleBasedStateMachine` over reconnect/stale interleavings. This is the safety net for step 7.
5. **prometheus-client** — freshness Gauge + reconnect/staleness Counters. Makes the silent‑blackout family observable *before* you change hot‑path code; also validates the new instrumentation under the step‑4 stateful tests.
6. **aiolimiter** — throttle CoinGecko (non‑ccxt only). Independent, low‑risk gap‑closer.
7. **pydantic-settings** — pin current loader precedence in a test (using step‑3/4 harness), then swap to `extra='forbid'` typed settings so config drift becomes a startup crash.
8. **TA-Lib as test‑only oracle**, then **talipp** — add the golden‑reference fixture first, wire the Hypothesis "incremental == full‑recompute" equivalence property (step 4), and only then migrate streaming RSI/MACD/EMA/SMA/BB to talipp — `add()`‑on‑closed‑bar only, `None` propagated as NotReady. Leave volume‑profile, CVD/taker‑flow, and the ccxt‑native reconnect/rate‑limit crutches hand‑rolled.

Everything in TRIAL is measure‑first and slots in after this spine: pandera at ingest, scipy `median_abs_deviation` behind guards, duckdb in research, uvloop/tenacity/msgspec only if a profile or a 429/wheel check justifies them.