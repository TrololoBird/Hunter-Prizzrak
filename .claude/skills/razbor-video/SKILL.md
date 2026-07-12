---
name: razbor-video
description: Full multimodal разбор of a trading education/analysis video (local file or URL) for HUNTER — transcribe, align frames with audio, verify the narrator's claims against real market data (CCXT+Polars), and write a grounded <slug>.razbor.md into the right corpus. Use whenever the user drops a video разбор to study, or says «разбери это видео».
---

Turn a trading video разбор into a **verified case study** grounded in three layers — audio
(what he says), video (what the chart shows), and data (what actually happened). Everything is
local + public-data only; respects the module boundary (memory `two-strategies-source-of-truth`).

**Goal: extract ONLY what advances the project — not a retelling.** Every разбор must capture,
and only capture:
- **charts** — patterns/formations, levels, structures (BOS, ranges, channels, wedges);
- **indicators** — volume bars, RSI/MACD/MA, liquidity heatmap, funding, OI — whatever is
  actually on screen (read them off the frames; the audio often omits them);
- **timeframes** — the TF hierarchy and which decision happens on which TF;
- **logic/analytics** — the entry trigger, its precondition, and trade management;
- **data verification** — every level/target/claim checked against real OHLCV;
- **cause→effect** — mechanistic and quantified (not "it died" but *why* — e.g. MM
  distribution finished → no bid — and *how fast*: time-to-peak, drawdown speed, % from peak),
  so it becomes a concrete rule (entry/exit/filter) for the code.
Drop marketing, P&L bragging, and psychology unless they encode a testable rule.

Deps (first run, Apple Silicon for mlx):
```bash
uv pip install imageio-ffmpeg mlx-whisper yt-dlp
```

Given a `<src>` (local path or URL) and optionally a `--corpus`, do the steps in order.

### 0. Resolve the source — it is the video the user attached with the command
- The user attaches a **local file path or pastes an external URL with every invocation** —
  that is the source. Process THAT video. (Reminder: the pipeline reads from disk/URL, not from
  a chat attachment — if a file was dragged in, use its path.)
- Only if the arguments truly contain no path/URL: do NOT pick a video on your own (different
  sessions guess differently). List pending разборы and ask —
  `for f in research/*_corpus/*.segments.jsonl; do s=${f%.segments.jsonl}; [ -f "$s.razbor.md" ] || echo "$s"; done` — then stop and wait.
- Before a long pass on a large source (roughly >20 min or >100 MB), confirm with the user.

### ‼ "Already ingested" is NOT "already разобрано"
A cached transcript is not a finished разбор. The deliverable is the grounded
`<slug>.razbor.md` (audio ⊕ video ⊕ data), and the pipeline keeps improving (glossary,
corrections, frame alignment, data grounding). When the user hands you a video, **fully
(re-)разбор it** — do not skip because a transcript/manifest entry already exists. Re-ingest
with `--force` so the transcript reflects the current pipeline, then always run steps 3–5.

### 1. Classify the module (by CONTENT, not filename)
- Влад SHORT / «Owner of SHORT» / engineered pump-dump +100…+4000% / paid Академия → **manipulations** (`hunt_core/scanner`).
- PrizrakTrade / level-structure-zone level trading (ключевой уровень, накопление, ПОК, стоп за структуру) → **prizrak** (`hunt_core/prizrak`).
- Unsure? Peek first: `uv run python scripts/ingest_manipulation_video.py "<src>" --stdout` and read before filing.

### 2. Ingest (transcript + segments + manifest/INDEX) — always fresh
```bash
uv run python scripts/ingest_manipulation_video.py "<src>" --corpus <manipulations|prizrak> --force [--name <slug>]
```
`--force` re-transcribes and overwrites even if this source was ingested before (so the
transcript benefits from the latest glossary/corrections). Note the `<slug>` it writes.

### 3. Align frames with audio, then READ them
Needs a local video file (for a URL, download once: `yt-dlp -f "best[height<=720]" -o /tmp/<slug>.mp4 "<url>"`).
```bash
uv run python scripts/align_frames.py "<video>" --segments research/<corpus>_corpus/<slug>.segments.jsonl
```
Read **every** frame in chronological order together with each frame's `said` text — not a
sample. A shallow pass produces plausible-but-wrong conclusions: on the JCT case, reading 9 of
33 frames missed the exact levels and the real trade date and reported a clean win that the data
later contradicted. If the set is very large (>~80 frames), re-extract with a coarser
`--min-interval` so the full set is readable, then read all of it. Extract from the CHART what
the audio doesn't state: ticker, timeframe, drawn levels/zones/trendlines, exact prices
(crosshair OHLC), the **date of the setup candle**, P&L. **On any conflict the video ряд wins
over the audio** (e.g. whisper heard «GTC» but the chart shows JCTUSDT).

### 4. Verify the claims against real data (CCXT public + Polars)
For each instrument discussed:
```bash
uv run python scripts/ground_razbor.py <SYMBOL> --since <SETUP-CANDLE-DATE> --levels <levels-from-chart> --tf <tf>
```
`--since` must be the **actual setup-candle date read off the chart** (crosshair/candle), not an
approximate axis marker — the wrong window verifies the wrong move. Then check the FULL PATH,
not just the endpoint: did the target hit, but also **what was the max adverse excursion**
between entry and target (a second, bigger impulse can blow the stop even when the target is
eventually reached — the JCT short re-pumped +52.7% before working). Note discrepancies (history
overstated, a level never held, a non-monotonic path). Public `fetch_ohlcv` only — never a
private/trading method.

### 5. Write the разбор
Compose `research/<corpus>_corpus/<slug>.razbor.md` covering the extraction checklist above:
a timestamped timeline (what he says × what the chart/indicators show), the data-verified
levels/targets, and a **mechanistic, quantified cause→effect** for each case (why it worked or
died, how fast, how far). End with a section that maps each finding to **concrete code work**
for `hunt_core/scanner` or `hunt_core/prizrak` — the rule it supports, challenges, or the label
the backtest dataset should carry. Reference sample: `short_manip_gtc_img2700.razbor.md`.

### Rules
- **Never commit** the video or the keyframes (image/binary bloat) — only transcript, segments,
  manifest/INDEX, and the `.razbor.md`.
- Keep the module label correct; never file a Prizrak level разбор under manipulations or vice versa.
- Reference sample: `research/manipulations_corpus/short_manip_gtc_img2700.razbor.md`.
