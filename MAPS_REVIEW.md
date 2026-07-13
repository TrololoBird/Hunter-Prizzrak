# HUNTER — Order Flow / Heatmap / Liquidation Map: Review & Modernization

Audience: Claude Code. Scope: `hunt_core/maps/orderbook.py`, `hunt_core/maps/liquidation.py`,
`hunt_core/maps/volume_profile.py`, `hunt_core/maps/config.py`, plus the WS data path in
`hunt_core/market/streams.py`. Method: full read of the current code + external survey (GitHub,
TradingView, CoinGlass/Binance docs, Reddit/blog methodology; ≥10 references per subsystem below).

Confidence tags used throughout: **[confirmed]** = read directly in the code; **[inferred]** =
deduced from code + data constraints, not observed live; **[external]** = from documentation/projects.

---

## 0. The data-fidelity ceiling (read this first — it caps all three maps)

Every recommendation below is bounded by what the input feed can actually carry. Three facts:

1. **Order book depth is shallow and time-sampled, not a continuous L2 stream.** [confirmed]
   `MapsConfig.book_top_n=5`, `book_deep_top_n=50`, `book_sample_interval_s=5.0`,
   `n_buckets=20`, `price_range_pct=5.0`. So the "heatmap" is the top ~50 levels, snapshotted
   ~every 5s, binned into 20 buckets over ±5% — i.e. **~0.5% per bucket** and **0.2 Hz** time
   resolution. Real depth heatmaps (Bookmap, Elenchev/order-book-heatmap) render the *full* book
   at per-tick cadence. HUNTER's is a coarse approximation by construction, not a bug — but it means
   iceberg/spoof/absorption detection is running on a sparsely sampled, truncated book.

2. **Binance's public liquidation stream is masked.** [external, load-bearing] `!forceOrder@arr`
   pushes **only the single largest liquidation per symbol per 1000 ms** (Binance docs). Since 2021
   the retail liquidation feed is deliberately throttled. So `source=realized` clusters
   *systematically undercount* real liquidations — you see the tip of each second's largest event,
   nothing else. This is exactly why the synthetic/forward path exists, and it's the right call.

3. **Trades are real.** [confirmed] `watch_trades_for_symbols` (CCXT aggTrades) feeds the footprint
   and CVD. This is genuine aggressive-flow data at good fidelity — the strongest input of the three.

Implication: the **liquidation forward-model and the trade-flow (CVD/footprint) are the honest edge**;
the **depth heatmap is the weakest link** and should be presented with the most humility.

---

## 1. Order flow (`maps/orderbook.py`)

### What it does today [confirmed]
A single `build_orderbook_map` produces: wall clusters, sticky walls, icebergs, absorption zones,
spoof flags, liquidity voids, zone imbalance, a trade **footprint** (buy/sell/delta per price bin),
`stacked_imbalance` (run of ≥3 same-sign delta bins), `cvd_divergence`, and a `depth_heatmap_matrix`
(last 12 book samples × top-6 buckets). Notable care already taken: the iceberg ratio is floored and
capped (`_RATIO_CAP=50`) to avoid near-empty-level blow-ups; spoof requires persistence across
`_SPOOF_MIN_PRIOR_SAMPLES=3` snapshots to filter MM refresh noise; cross-venue binning drops levels
on the wrong side of the reference price. This is more disciplined than most open-source order-flow
code I surveyed.

### External state-of-the-art (≥10)
tiagosiebler/orderflow (footprint-candle service, Binance/Bybit/OKX/Bitget/Gate) · AndreaFerrante/
Orderflow (Python tick-reshape + backtest + Postgres) · Tucsky/aggr (real-time multi-exchange trade
aggregator, the de-facto CVD reference) · 0xd3lbow/aggr.template (spot-vs-perp delta divergence
templates) · Azhagesan-dev/order-flow-chart (live footprint canvas) · GitHub topics `order-flow`,
`footprint-chart`, `volume-profile` (hubs, dozens of repos) · TradingView "Delta OrderFlow Sweep &
Absorption Toolkit" (MaxMaserati) · TradingView "OrderFlow Absorption Matrix" (ProjectSyndicate) ·
algostorm order-book heatmap methodology guide.

### Findings & recommendations
- **CVD is a 60s window delta, not a persistent cumulative line.** [confirmed]
  `_detect_cvd_divergence` sums signed notional over the last 60s and compares to `price_change_pct`.
  That is *delta*, not *cumulative* volume delta. Every serious reference (aggr, tiagosiebler,
  aggr.template) maintains a **running CVD series** and detects divergence as *price higher-high vs
  CVD lower-high* over a lookback. **Recommendation:** keep a persistent per-symbol CVD series
  (rolling, e.g. 15–60 min at bar granularity) and detect divergence structurally (swing-based),
  not by a single 60s-vs-price threshold. This is the single highest-value order-flow upgrade and
  directly serves the "surface our DOM/flow edge honestly" goal. *(Confidence the current form is
  weaker: high — it's a category difference, delta vs CVD.)*
- **Footprint has no stacked-imbalance diagonal.** [confirmed] `_stacked_imbalance` looks at a run of
  same-sign *bin* deltas vertically. True footprint "stacked imbalance" (trader-dale, MaxMaserati) is
  a **diagonal** bid×ask comparison (buys at ask price N vs sells at bid price N−1) exceeding a ratio
  (e.g. 3×) for ≥3 consecutive levels. Current version can't see that because the footprint bins
  store net delta, not the bid/ask matrix. **Recommendation:** if you want real footprint semantics,
  store buy@ask and sell@bid separately per bin and compute diagonal imbalance; otherwise rename the
  field to `delta_run` so it doesn't imply footprint stacked-imbalance it isn't computing.
- **Trade sign relies on `is_buy` / taker side.** [inferred] Fine with aggTrades' `m` flag, but
  confirm the stream sets the taker side correctly for the venue; a mislabel silently inverts CVD.
  Add one assertion/unit test on a known aggTrade sample.
- **Absorption/iceberg/spoof run on 5s-sampled top-50 book.** [confirmed/inferred] With 0.2 Hz
  sampling, a spoof that appears and pulls within <5s is invisible, and an iceberg that refreshes
  between samples is undercounted. **Recommendation:** either (a) accept this and label these signals
  as low-confidence hints (don't let them shift scores hard), or (b) drive them from the *event*
  stream (per-update book diffs) rather than the interval snapshots. Given the WS-1 weight budget,
  (a) is the pragmatic near-term choice; (b) is the modernization if/when you move to a diff-based
  book cache.
- **Good to keep:** the spoof persistence gate, the iceberg ratio cap, and cross-venue side-filter
  are correct and better than most surveyed repos — don't regress them in any refactor.

---

## 2. Depth heatmap (`_depth_heatmap_matrix` + `merge_full_depth_bins`)

### What it does today [confirmed]
`depth_heatmap_matrix` = for the last ≤12 book snapshots, top-6 buckets of resting notional with an
`intensity = depth / max(bucket)`. `merge_full_depth_bins` does a careful cross-venue price-bin merge
with epsilon-correct boundary assignment and stale-side filtering. Output is a sparse
price-bucket × sample grid.

### External state-of-the-art (≥10)
Elenchev/order-book-heatmap (Binance WS L2, brightness = resting size, trade-delta circles) ·
traderpedroso/heatmap (fork) · billpwchan/LiquidityMap (Python, multi-venue collectors + heatmap +
depth + spread + dashboard) · Tucsky/aggr (depth + trades) · Bookmap (commercial reference: full
book, per-tick, historical replay) · "real-time crypto order book heatmap" multi-exchange (Index α /
CVD / Delta) · GitHub topics `order-book`, `limit-order-book`, `orderbook-tick-data` · algostorm
heatmap trading guide (real-vs-fake liquidity reading).

### Findings & recommendations
- **Resolution is the core gap.** [confirmed] 20 buckets over ±5% = 0.5%/bucket. Bookmap-class tools
  are effectively tick-resolution. **Recommendation:** decouple heatmap resolution from the map's
  generic `n_buckets`; give the depth heatmap its own finer binning (e.g. 40–80 buckets over a
  *narrower* ±1–2% band near price, where the actionable liquidity is). Coarse ±5% wastes most
  buckets on empty far book. *(This mirrors the `vp_buckets 24→60` fix already made for volume
  profile — same reasoning, apply it to depth.)*
- **Only top-6 buckets per sample are retained.** [confirmed] `_depth_heatmap_matrix` keeps the 6
  densest buckets per sample, so a persistent medium wall that never ranks top-6 vanishes from the
  time series — you lose the "sticky band over time" signal that is the whole point of a heatmap.
  **Recommendation:** retain all non-empty buckets (or a fixed price grid) across samples so a level's
  *persistence* is visible, not just its per-sample rank.
- **No time-decay / persistence weighting.** [confirmed] A heatmap's value is showing which price
  levels *stay* thick. Currently each sample is independent. **Recommendation:** accumulate per-bucket
  presence over the retained window (e.g. EMA of notional per fixed price level) so "sticky liquidity"
  emerges as intensity — this is what Bookmap/Elenchev render and what traders actually read.
- **`intensity` is normalized per-sample, not globally.** [confirmed] `depth/max(acc.values())`
  re-scales every sample to its own max, so intensities aren't comparable across time. Normalize
  against a rolling global max for a stable color scale (Elenchev offers linear vs log — a log scale
  is worth copying; crypto depth is heavy-tailed).
- **Modernization path:** move from interval snapshots to a **maintained L2 book** (apply WS diffs to
  a local book, sample the *maintained* book on a fixed price grid). This gives real heatmap fidelity
  without more REST weight (diffs are already streamed). It's the biggest single upgrade but the most
  work; sequence it after WS-1 stability.

---

## 3. Liquidation map (`maps/liquidation.py`)

### What it does today [confirmed]
Three-layer model: (a) **realized** clusters from public CCXT liquidation events (masked feed);
(b) **synthetic** leverage-tier bands at `price × (1 ± 1/lev ± mmr)` from tiers `(5,10,20,50)` when no
realized events; (c) **entry-anchored forward zones** — for each ΔOI>0 bar, anchor at hlc3 and project
liq prices per leverage tier, weighted by `leverage_weights (0.35,0.30,0.20,0.10,0.05)` and split
long/short by `global_ls_ratio`. Adds `_consume_swept_levels` (decays heat once price passes a level),
`squeeze_fuel_scores` (funding + LS ratio + at-risk notional), OI-% at risk, and a calibration-overlap
confidence. Provenance discipline is strong: magnet-pull and fuel **exclude synthetic-only** data
(`realized_event_count > 0` gate) so estimates don't silently drive scoring, and everything is labeled
`liq_synthetic_only`. This is genuinely well-engineered and more honest than most retail tools.

### External state-of-the-art (≥10)
CoinGlass liquidation heatmap (product + API `liquidation-heatmap`, aggregated model3) · CoinGlass
methodology (OI + funding + volume + leverage-tier probability weights) · Hyblock Capital (commercial
liq heatmap) · StephanAkkerman/liquidations-chart (open-source Coinglass-style, Binance public data) ·
hgnx/binance-liquidation-tracker (forceOrder tracker, pandas) · xiaoshulittletree/binanceliquidation
listener (USD-M + COIN-M CSV logger) · binance/binance-futures-connector-python (forceOrder stream) ·
Tardis.dev (historical liquidation datasets) · DEXTools 2026 liquidation-map guide (methodology) ·
Binance `!forceOrder@arr` docs (the 1000ms single-largest throttle).

### Findings & recommendations
- **Leverage-tier set diverges from the industry model.** [external vs confirmed] CoinGlass/Hyblock
  weight **1×, 5×, 10×, 25×, 50×, 100×**; HUNTER uses `(5,10,20,50)`. Missing 25× and 100× matters:
  100× liquidations sit ~1% from entry (the densest, most-hit magnet) and 25× is a very common retail
  tier. **Recommendation:** align default tiers toward `(10,25,50,100)` or the full CoinGlass ladder,
  and *derive from `bracket_tiers`* whenever available (the code already parses `maintenance_margin_
  rate`/`max_leverage` — prefer real brackets, fall back to the industry ladder, not `(5,10,20,50)`).
  *(Confidence this improves magnet accuracy: medium-high — it's the documented industry approach and
  the near-price tiers are where price actually reacts.)*
- **Forward model is essentially the CoinGlass approach — good.** [inferred] Entry-anchored ΔOI +
  leverage tiers + funding + LS split is the right reconstruction and matches the external
  methodology. The differentiator to add is **cumulative, decaying persistence** of unswept zones
  across time (a level that has sat unhit accumulates "magnet" weight), rather than recomputing from
  the current OI-bar window each tick. `_consume_swept_levels` handles removal; add the accumulation
  side.
- **`_consume_swept_levels` uses a bucket-half heuristic.** [confirmed] It decays heat for buckets
  on the "passed" side via `b < n_buckets // 2`. This assumes symmetric bucketing around price and can
  mis-tag zones near the midpoint. **Recommendation:** decay by actual `center` vs `current_price`
  crossing with a small tolerance, independent of bucket index parity.
- **Realized-event undercount is unavoidable but should be surfaced.** [external] Given the 1000ms
  single-largest throttle, `realized_event_count` is a floor, not a count. The provenance labeling is
  right; consider also **aggregating realized liquidations across venues** (Bybit/OKX also stream
  liquidations via CCXT) — multi-venue realized data materially reduces the masking gap. The buffers
  dict already supports multiple venues (`build_liquidation_map(buffers=...)`); make sure more than
  Binance is actually wired in.
- **Squeeze-fuel is a clean, defensible composite.** [confirmed] Averaging normalized funding + LS +
  at-risk parts is reasonable; just document that it's an unweighted mean of available parts, so its
  value shifts meaning when inputs are missing (2-of-3 vs 3-of-3). Consider requiring ≥2 parts before
  emitting, to avoid a funding-only score reading as a full "fuel" signal.

---

## 4. Cross-cutting

- **Three separate bucketings, three separate `to_dict` shapes, all `@dataclass`.** [confirmed] The
  maps are `@dataclass` (violates the project's Pydantic rule — consistent with WS-3.2 of the main
  plan). When you convert, unify the price-binning into one shared helper (`span/price_min/bucket_size`
  is copy-pasted across orderbook/liquidation/volume_profile) — a single `PriceGrid` value object
  removes ~4 duplicated implementations and the epsilon-boundary logic that currently lives only in
  `merge_full_depth_bins`.
- **`maps/config.py` has a default drift bug.** [confirmed] `MapsConfig.vp_buckets` field default is
  `60` (with a comment explaining the 24→60 fix), but `from_defaults` reads
  `int(section.get("vp_buckets", 24))` — so when no TOML value is present it silently reverts to the
  coarse **24**. Fix the fallback to 60 to match the intended resolution.
- **stdlib logging in `liquidation.py`.** [confirmed] `LOG = logging.getLogger(...)` — same
  structlog-rule violation as WS-3.1; convert with the rest.
- **Testing.** [confirmed earlier] `maps/` is nearly untested. These are pure functions with clear
  inputs — ideal for unit tests. Prioritize: CVD/divergence, footprint binning boundaries, liquidation
  forward-zone projection math, and the `vp_buckets` config regression above.

---

## 5. Priority (what to change first)

1. **Persistent CVD series + structural divergence** (order flow) — highest edge-per-effort, real data.
2. **Fix `vp_buckets` config fallback (24→60)** — one-line correctness bug. [confirmed]
3. **Align liquidation leverage tiers to the industry ladder + prefer real brackets** — accuracy.
4. **Depth heatmap: own finer binning over a narrow band + per-bucket persistence/decay** — turns the
   weakest map into a real one.
5. **Multi-venue realized liquidations** — reduces the masking gap cheaply (buffers already support it).
6. **(Modernization, post-WS-1)** maintained diff-based L2 book → true heatmap fidelity.

All of the above are **analysis quality**, not runtime stability — they belong in Phase 2 of the main
plan and must be validated on a bot that survives (WS-1 first).

---

## References
Order flow: github.com/tiagosiebler/orderflow · github.com/AndreaFerrante/Orderflow ·
github.com/Tucsky/aggr · github.com/0xd3lbow/aggr.template · github.com/Azhagesan-dev/order-flow-chart
· github.com/topics/order-flow · github.com/topics/footprint-chart · github.com/topics/volume-profile
· TradingView MaxMaserati Delta OrderFlow Sweep & Absorption · TradingView ProjectSyndicate OrderFlow
Absorption Matrix.
Heatmap: github.com/Elenchev/order-book-heatmap · github.com/traderpedroso/heatmap ·
github.com/billpwchan/LiquidityMap · github.com/Tucsky/aggr · Bookmap (bookmap.com) ·
github.com/topics/order-book · github.com/topics/limit-order-book · github.com/topics/orderbook-tick-data
· algostorm.com/heatmap.
Liquidation: coinglass.com/pro/futures/LiquidationHeatMap · docs.coinglass.com/reference/liquidation-heatmap
· hyblockcapital.com · github.com/StephanAkkerman/liquidations-chart ·
github.com/hgnx/binance-liquidation-tracker · github.com/xiaoshulittletree/binanceliquidationlistener ·
github.com/binance/binance-futures-connector-python · docs.tardis.dev · DEXTools 2026 liq-map guide ·
Binance `!forceOrder@arr` stream docs.
