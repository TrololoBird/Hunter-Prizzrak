# Sessions & historical windows for levels / zones / liquidations — decision record

Question (user, 2026-07-18): for **liquidations and levels**, study *sessions* and *historical
windows* for determining zones/levels. Answered from the **source-of-truth** (PDF course = PRIMARY;
video-razbor corpus = SECONDARY/SUSPECT) **cross-checked against verified ccxt/exchange data
availability**. Headline: the methodology is **structure-scoped, not session- or fixed-window-scoped**,
and the data constraints *align* with it — so the engine needs almost nothing new here.

## 1. What the methodology prescribes (source-of-truth)

### Levels / zones — historical window (PDF, well-covered)
- **A level has NO time expiry. It dies by being *worked* (отработка), not by aging.** «срока
  годности по времени у уровня нет» — a 2021 level worked in 2022/2023 (course_notes Стр. 22, 25).
  Staleness = **one worked touch on its TF**, never calendar age. (This is the code's real "already
  worked" rule, not a bar-count.)
- **The window IS the accumulation structure's own candle span** (first→last), on the level's own TF —
  **not** a fixed N-bar lookback. Fractal: a 1D/1W base can be year-long and nest many 1h–4h sub-bases
  (Стр. 20, 23, 24). Borders = first 2 touch points (Стр. 18).
- **Authoritative level tool = Fixed-Range Volume Profile anchored to the structure span** («натягивая
  профиль на структуру — захватить ВСЕ свечи структуры», on the level's TF; Стр. 26). VRVP
  (visible-range) is **discovery only, explicitly not for entries** (Стр. 63).
- **Full history matters**: ATH/ATL «за всю историю» are levels (Стр. 3); years-old levels stay valid.
- TF ladder 5m/15m/1h/4h/1D/1W (Стр. 17).

### Sessions / time — PDF is SILENT
- A full grep of all four course-note files for session/killzone/open/weekend/gap returned **zero**.
  Prizrak demands **nothing time-of-day**. CME appears once, **purely definitional** (Стр. 8) — no gap,
  no weekend, no CME-as-level. The word «гэп/gap» is absent from the entire prizrak corpus.
- **CME/weekend distrust is SECONDARY (video only, PDF-silent):** the author distrusts crypto structure
  that formed over a weekend (CME closed → «на SME графике … ничего нет … формировалась в выходной
  день» → won't pre-place limits; `prizrak_btc_heatmap.txt`). Operationalized as **weekend-structure
  distrust**, NOT a tradeable Sunday-gap level.
- **Session/candle-close clocks (Asian session, 6h/daily/weekly close at MSK) are the MANIPULATIONS
  module** (Vlad transcripts), **not prizrak** — must **not** be cross-wired into the prizrak path
  ([[why-agents-confuse-the-two-modules]]).

### Liquidations — PDF SILENT beyond the squeeze
- PDF mentions liquidations only inside the squeeze cascade (Стр. 8). **No liquidation heatmap /
  cluster as a level, no accumulation window.**
- The heatmap is SECONDARY (video): a **LIVE / current** liquidity snapshot, a **low-trust directional
  hint**, and the author explicitly refuses it for entries («я её лично никак использовать не буду»).
  **Never a level source.**

## 2. What the data actually allows (verified ccxt 4.5.59 `has`/source + Binance docs)

| Need | Availability | Verdict |
|---|---|---|
| OHLCV full history (structure span, ATH/ATL, years-old levels) | ✅ back to listing, paginated; **1W@1000 bars ≈ 19 yr**, 1D@1000 ≈ 2.7 yr | engine seed (1000/TF) is enough; read ATH/ATL off the **1W** frame |
| Daily/weekly/monthly opens (if ever needed) | ✅ free from UTC kline boundaries — no special source | derivable, no engine work |
| Trading-session killzones (manipulations only) | ✅ pure time-of-day math on timestamps | manipulations-module concern, not engine |
| CME gap / weekend flag | ❌ **not on ccxt** (crypto-only) — needs external CME source | defer; PDF-silent anyway |
| OI history | ~30 days, max 500 pts (`fetchOpenInterestHistory`) | fine (OI is a dop-factor, not a level) |
| Liquidation history | ❌ **none on any venue** — WS-only, no backfill (`fetchLiquidations` `has=False`/`NotSupported`) | matches methodology: heatmap is a **live** snapshot anyway |

## 3. Synthesis — methodology and data AGREE

- **Levels are structure-scoped and never expire by time** → the requirement is **level *persistence***
  (retain a level object until *worked*), a strategy-state concern, **not** a data-window concern.
  Full OHLCV history is available for the structure span + ATH/ATL. The engine already supplies this.
- **Sessions are not a prizrak concept** → the engine correctly stays session-agnostic for prizrak.
  The one data gap (CME not on ccxt) is moot because the PDF is silent; any CME/weekend feature is
  video-derived, suspect, and a **low-trust dop-factor** at most.
- **Liquidation history is unavailable — and unneeded.** Both the methodology (live, low-trust,
  never-a-level) and the data (WS-only) point the same way: treat liquidations as a **live**
  read-through. **Persisting a liquidation buffer for "historical zones" is NOT methodologically
  demanded** (it would serve a heatmap-as-level use the author explicitly rejects). The live
  session-length buffer is sufficient.

## 4. Concrete implications

**Engine / cutover (small):**
- No session machinery, no CME source, no liquidation persistence needed for faithfulness. Keep
  liquidations as a live WS read-through.
- When a strategy needs ATH/ATL «за всю историю», read the **1W** frame (1000 bars ≈ 19 yr covers full
  history; 1D@1000 may miss the true ATH for coins > ~2.7 yr old).
- Downgrade the "persist liq buffer for zones" idea: cross-venue WS liquidations remain a *nice-to-have
  low-trust dop-factor*, **persistence unnecessary**.

**PRIZRAK plan (deferred, post-cutover — `staged-jumping-wind.md`):**
- The real methodology gap is **fixed-lookback vs structure-span**: code uses fixed `lookback_bars`
  (5m/15m=80, 1h/4h=60, 1D/1W=150) + fixed VP lookbacks (15m=96, 1h=48), whereas the PDF wants the VP
  **anchored to the structure's candle span** on its TF (Стр. 26) and **no age cutoff** on validity.
  This is prizrak-detection work (relates to plan D2 «границы = первые 2 точки», D3 «сила = ТФ×объём»),
  not the data engine.
- CME/weekend distrust + liquidation heatmap = optional **secondary/suspect** dop-factors — re-verify
  against the PDF before letting either influence prizrak emission; keep session-clock timing inside
  the **manipulations** module.
