# ARCHITECTURE — target design & rationale

Status: **active north-star** (supersedes `docs/SPEC_v5.1.md`, which is deprecated —
it describes an abandoned quant pipeline: KER/EMA-slope/funding-percentile/OI-rank/
CoinMarketCap macro, and even contradicts `CLAUDE.md`'s "no CoinMarketCap"). Do not
align new code to SPEC_v5.1.

This document is the single place that states **what the system is**, **the two
strategies it runs and how they differ**, **the module boundaries**, **the operational
resilience contract**, and **how a change is proven good**. Read it before restructuring
anything.

---

## 1. What this is

A standalone crypto-futures **signal-analytics** product. Reads public Binance USDⓈ-M
market data via CCXT, engineers features with Polars, and delivers **manual** signals to
Telegram. No auto-trading, no private auth. Two independent strategies share only the
spine (`signals/`, `data/`, `market/`, `track/`) and never cross-import.

## 2. The two strategies — DO NOT CONFUSE THEM

The single most important design invariant, and the difference is **fundamental and
philosophical — not merely a stop parameter**. They come from **different source documents**
(the user's files are the source of truth, over code comments and over SPEC docs) and are
**two different games with different edges, frequencies, psychologies and asset universes**.
Imposing one's logic on the other (frequency, stop, entry, target) is a recurring, expensive
mistake — and is currently *why the scanner mis-fires* (§5).

### 2.1 The essential difference

- **PRIZRAK is a complete, systematic METHODOLOGY for reading any instrument via
  structure.** The PDF is a full curriculum (словарь → таймфрейм → накопление → уровни
  BUY/SELL ПОК → стоповый объём → ловушки → ПП → фигуры → индикаторы). The edge is
  **mechanical**: institutional накопления leave ПОК levels that price *respects*, so you
  trade level→level with confluence and defined risk. It is **reactive, continuous,
  high-frequency, RR-first** (золотой стандарт 1к3), works on BTC/majors/any liquid coin,
  every day, modest per-trade targets. "How to trade in general."

- **МАНИПУЛЯЦИИ is a rare, opportunistic PLAY for one narrow class of engineered events.**
  Not "how to trade" — how to catch a market-maker's pump/dump of **20–40%+ (крупные до
  60–180%)**. The trader is explicit: *«Никакая стратегия вам такую доходность никогда не
  покажет. Только манипуляции»* and *«штук пять-шесть в месяц»*. The edge is
  **behavioural/liquidity**: a крупный игрок *engineers* the move — выносит всех, снимает
  ликвидность, ликвидирует толпу, фиксирует позиции — in a *repeatable, anticipatable* way.
  You read **intent**, take the **opposite side of the liquidated crowd**, and therefore
  accept being briefly in the red (**пересиживание**) with a wide stop + **добор/усреднение**.
  **It is FAST, not среднесрок: «от пары часов до 2-3 дней».** The whole 20–40%+ move plays
  out inside a few days — you sit through the dip, average in, then it runs.
  **Trade mechanics (from the transcript):** first take-profit at a **+20% price move** →
  there you bank a partial and **move the stop to entry (breakeven), NOT to TP1** → the
  runner rides on to the deep 40%+ pool. "How to catch one specific, huge, engineered move."

### 2.2 Full contrast

| dimension | **PRIZRAK** (Deep, `prizrak/`) | **МАНИПУЛЯЦИИ** (Scanner, `scanner/`) |
|---|---|---|
| Source of truth | PDF «Мини Курс от PrizrakTrade» | the two transcript `.txt` files |
| Nature | complete systematic methodology | one narrow opportunistic play |
| Edge | mechanical — price respects ПОК levels | behavioural — MM engineers a repeatable манипуляция |
| Stance | **reactive** to a level/structure | **predictive** of where the MM drives price |
| Crowd relation | trades *with* structure | trades *against* the trapped/liquidated crowd |
| **Frequency** | **high / continuous**, any liquid coin | **rare: ~5–6 per MONTH**, hand-picked |
| Selectivity | every valid level is tradeable | only specific formations qualify (§2.3) |
| Asset universe | BTC/majors + any liquid perp | specific profiles: заскамленные one-candle dumps, coins в нисходящем канале обновляющие минимумы |
| **Stop** | **за структуру с запасом 1–3%** (стр.33) — behind накопление/тень-свечи/стоповый | **WIDE**, за экстремум манипуляции с запасом |
| Drawdown | exit to БУ if wrong (defined risk) | **пересиживание** — expect & hold brief red on purpose, average in |
| Money mgmt | 50% at TP → БУ → добор at levels | **добор/усреднение** in the dip → bank 50% at **+20% move** → stop to **entry (BE)** → runner to deep pool |
| **Target** | next structural level, **RR 1к3** | the **whole move: 20–40%+ (крупные до 60–180%)** |
| Horizon | intraday → swing | **FAST: «от пары часов до 2-3 дней»**, ride the whole pump/dump |
| Runs in | analyst path (`assemble_analyst_tick`): pinned + `/signal` | fast `watch` tick + `_manipulation_scan_loop`, non-pinned universe |

### 2.3 The only formations манипуляции trades (from the transcripts)

Longs (`2026-07-02*.txt`): **(1)** aggressive pump поглощён одной свечой → боковик → deep
long у низа / on слом нисходящей; **(2)** восходящий канал → манипуляция вниз → нисходящий
канал → боковик → **закреп выше предыдущего максимума** (needs бычьи объёмы, else «цена
может пойти дальше вниз»); **(3)** длинный нисходящий канал обновляющий минимумы →
накопление ликвидности → long. Short (`IMG_2700*.txt`, GTC): восходящий канал → wait for the
**финальный** свип максимумов **когда выше уже нет ликвидности** → частичный шорт → LTF
подтверждение разворота → ride the dump. Everything else is NOT a manipulation setup.

**Architectural consequence:** the scanner must be **rare and selective** (a few per month),
not fire like Prizrak on every zone. See §5.

**⚠ The word «манипуляция» is overloaded — do not conflate:** (1) the **STRATEGY** here =
an engineered pump/dump of **20+%, reaching 60–180%** (`scanner/`, the `.txt` transcripts);
(2) a generic word Prizrak sometimes drops in a level разбор for a **small local прокол/свип/
сквиз at a level** — ordinary level-trading vocabulary, NOT this strategy. A small squeeze at
a level is not the 60–180% play. Keep them separate in code, tests, and docs.

## 3. Module map (authoritative)

```
hunt_core/
  prizrak/    Deep engine (PRIZRAK strategy). Decision authority for pinned + /signal.
              build_prizrak_signals() → 0..N candidates → row["prizrak_signals"].
  scanner/    Manipulation detector (МАНИПУЛЯЦИИ). advance_manipulation_scales()
              (patterns A/A3/C long, B short) with per-symbol persisted state.
  toolkit/    shared primitives (manipulation fusion, order flow, robust stats)
  market/     CCXT client, rate limiting, WS/REST transport, proxy + PREFLIGHT
  signals/    shared spine: Signal, setup_id dedup, lifecycle states
  deliver/    Telegram formatting + delivery (per-strategy renderers)
  diagnostics/ data-plane audits + universe_health (operator signal)
  runtime/    cycle loop, analyst assembly, tick assembly, telegram commands
  data/ track/ domain/ features/ regime/ levels/ maps/ ...
docs/ARCHITECTURE.md   ← this file (SPEC_v5.1 deprecated)
research/backtest_scanner.py   faithful ladder-aware manipulation backtest
```

Invariant: **Deep and Scanner never import each other.** Shared logic goes through the
spine, not a cross-import.

## 4. Data-plane resilience contract (added after the 2026-07-11 incident)

Root cause of that incident: the SOCKS proxy died → every CCXT call hung → every symbol
failed the 4h-staleness gate → **no signal could form, silently** → the loop hung → the
faulthandler watchdog hard-killed the process hours later. Delivered zero signals, alerted
no one. The contract below prevents a silent repeat:

1. **Proxy preflight** (`market/network.py::proxy_reachable`) — a bounded TCP check at
   startup. A dead proxy is logged loudly (`hunt_proxy_unreachable`) instead of hanging.
2. **Universe health** (`diagnostics/universe_health.py::assess_universe_health`) — a
   PURE per-tick aggregate. When ≥50% of a ≥5-symbol universe fails data assembly it logs
   `hunt_universe_degraded`; at ≥90% for ≥3 consecutive ticks it fires a Telegram ops
   alert (data blackout). This is the missing operator signal — a mass blackout is now
   loud, not silent.
3. **Supervision** — unattended runs MUST use `scripts/watch.sh` with
   `HUNT_WATCH_SUPERVISE=1` so a watchdog hard-kill auto-restarts (crash-only, 15s). A
   bare `python -m hunt_core watch` has no restart and stays dead after a kill.
4. **Hang watchdog** — `faulthandler.dump_traceback_later(HUNT_WATCHDOG_S, exit=True)`
   stays: it dumps every thread's stack to `data/hunt_watchdog.log` then exits, so a hung
   loop becomes a restartable crash (not a frozen zombie). Default 300s.

Future work (specified, not yet built): proxy **failover** (rotate through
`effective_proxy_urls()` when the active one fails preflight mid-run, not only at start).

## 5. Validation gate — how a change is proven good

- **Prizrak**: verify via `assemble_analyst_tick` on live symbols + rendered message
  sanity vs the PDF methodology (structure/МТФ/maps, stop за структуру с запасом).
- **Manipulations**: `research/backtest_scanner.py` — a no-lookahead forward replay that
  drives the real detector and evaluates it with the **faithful FAST-play** risk model:
  wide stop, добор/усреднение in the dip, bank 50% at a **fixed +20% move**, stop to
  **entry (BE)**, runner to the deep 40%+ pool, **short horizon (2–5 days — the play is
  hours-to-days, NOT weeks)**. A detector change MUST be run through it before/after.
  `research/audit_horizon.py` cross-checks that the forward window isn't manufacturing
  timeouts. Current honest status (dataset_v9, 45 symbols, corrected mechanics): **−21R,
  win=0** — the +20% first-take IS reached (21 scratches) but no runner completes the deep
  pool, and timeouts (formations that don't move) + wide-stop losses dominate. Splitting
  the universe does not rescue it (low-cap coins −15R, stocks/majors −6R), so the remaining
  suspect is **detector fidelity**, not the risk model. NOTE: dataset_v9 is a poor
  manipulation universe — it is full of tokenized stocks (AMZN/GOOGL/COIN…) and mid-cap
  majors that do not fit the profile (low-cap, scammed, making new lows); a representative
  dataset is needed before trusting absolute R.
- **Always**: `ruff check .` + `mypy hunt_core` + `pytest` + a `watch --once --no-telegram`
  smoke run before considering a change done.

## 6. Known debt / next architectural moves

1. **Detector fidelity is the open problem (§2, §5).** With the risk model now corrected
   (fast horizon, +20% first-take, stop→entry, добор), the scanner is −21R / win=0 on
   dataset_v9: the +20% take is reached (21 scratches) but no runner completes the deep
   pool, and non-moving formations (timeouts) + wide-stop losses dominate. This is a
   DETECTION problem — the patterns are firing on setups that chop or get stopped, not the
   clean 20–40%+ engineered moves of §2.3 — not a risk-model or "over-firing frequency"
   problem. (An earlier version of this doc claimed "over-fires 12.4/symbol/month, ~60–100×"
   and prescribed a selectivity gate; that figure used a wrong ~8-day window — the real
   cadence is ~2.3/coin/month over the 41-day 1h span — and a magnitude-threshold gate was
   the wrong lever. Both were removed.) Next: audit A/A3/B/C against the transcript
   formations (entry timing, the §2.3 sequence) AND test on a REPRESENTATIVE universe —
   dataset_v9 is full of tokenized stocks/majors that don't fit the manipulation profile.
2. **`orchestrator.py` is ~1650 lines** — the candidate generators (`_zone_candidate`,
   `_forward_*`, `_pp_candidate`, `_trap_flip_candidate`, stop-volume) should split into
   `prizrak/candidates/` modules; keep `build_prizrak_signals` as the thin assembler.
3. **Data footprint** — `data/candidate_observations.jsonl` reached 2.2 GB; the rotation
   that exists for `analyst_ticks`/`data_plane_audit` should cover every high-volume
   JSONL, with a size budget.
4. **Config drift** — `config.defaults.toml` is the single threshold source; keep dead
   overrides out of `config.toml` (silently ignored). Remove SPEC_v5.1 once nothing links
   it.
