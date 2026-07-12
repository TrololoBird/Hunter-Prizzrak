# Manipulations corpus — source material for the **Scanner Manipulations** module

Primary-source разборы of the **манипуляции** trading strategy that `hunt_core/scanner`
implements (production detector: `hunt_core/scanner/detect/patterns.py::advance_manipulation_scales`).

> **Module boundary — do not confuse.** This corpus feeds the **Scanner Manipulations**
> module (`hunt_core/scanner/`), pumps/dumps of 20–400%+. It is **not** Prizrak
> (`hunt_core/prizrak/`, level/structure trading). See the memory
> `two-strategies-source-of-truth`.

## Contents

Each source has a `.txt` (clean transcript) and a `.segments.jsonl` (timestamped
`{start, end, text}`, seconds — use to locate a claim in the video). All auto-transcribed
locally with `scripts/ingest_manipulation_video.py` (mlx-whisper large-v3-turbo, ru).

| Slug | Source video | What it is |
|------|--------------|------------|
| `long_manip_3types_2026-07-02` | `2026-07-02 19.27.29.mp4` (9:48) | **Canonical teaching video — the 3 most reliable LONG manipulation formations.** |
| `short_manip_gtc_img2700` | `IMG_2700.MP4` (3:11) | **Canonical teaching video — the SHORT manipulation** (worked example: GTC, entry at the peak). |
| `vlad_short_record_2026-07-11` | `record.mp4` (42:40) | Live stream applying the strategy across BTC/ETH + alts; Академия/copy-trading promo. |

All by the same author: **«Влад SHORT» / channel «Owner of SHORT»**.

Keyframes (107 scene-change frames + contact sheets) were extracted during analysis but are
**intentionally not committed** (image bloat); regenerate from the source video with ffmpeg
`select='gt(scene,0.3)'` if needed.

## Formation catalogue (from the two canonical teaching videos)

Frequency: ~5–6 clean setups/month, each >50% чистого → hand-picked, RARE. "Достаточно
знать несколько формаций и постоянно искать их на графике."

**SHORT manipulation** (`short_manip_gtc_img2700`):
- Coin in a восходящий канал printing repeated impulses, each one **поглощён**.
- Wait for the **FINAL импульс that снимает ликвидность = updates the previous max(s)**.
  When the prev max is taken AND there is no liquidity left above → start scaling into short.
- Drop to a lower TF for the trigger: pump snapped up the liquidity → candles fade → an
  **impulse candle down closes red = the хай is formed** → short from the peak.
- Bank ~30% on the pump-short; runner to the support / impulse-low below (стопы лонгистов).
- Stop above the impulse-high.

**LONG manipulations — the 3 reliable types** (`long_manip_3types_2026-07-02`):
1. **Pump → single-candle absorption → боковик with follow-up pumps.** The big player who
   started dumping shows further pumps to (a) finish closing at the best price, (b) вынести
   shortistов. **Skip the FIRST pump; the NEXT is always more reliable** (after the lower
   liquidity is swept). Enter in the lower боковик only on **подтверждение слома нисходящей
   структуры**. Ex: ESPROC +160% (move >400%). Assets: coins "заскамились одной свечой вниз".
2. **Восходящий канал → манипуляция вниз → нисходящий канал → боковик → закреп ВЫШЕ пред.
   максимума.** Two outcomes: боковик breaks down (full absorption) OR — more often — **закреп
   above the previous max with bullish volumes** → снятие ликвидности. Enter on that закреп.
   Ex: ZEREBRO +20% (could be 50%), entry 0.032. "Каждый второй памп" → short the pump THEN
   long +50%, both directions.
3. **Long нисходящий канал updating lows (even without an initial pump) → боковик →
   накопление ликвидности → лонг.** Accumulate best entries in the боковик. Ex: BSB entry 0.2
   → +100% за вечер (+250% среднесрок), spot-friendly; HEA +100%; PLACE +62%.

Across all: the edge is engineered pumps/dumps, entries anchored to **impulse-set extremes**
(always hold liquidity), taken in EITHER direction, on liquid скам/low-cap coins — NOT majors.

## Strategy core distilled from the live stream (for the scanner)

**Manipulation SHORT setup (cleanest signal):**
1. Coin was MM-pumped "необоснованно", then drifts into a local боковик.
2. Previous max is updated **by an impulse candle → immediate поглощение** (absorption).
   Inside the consolidation, volume is being **redistributed** (the pumper fixes positions).
3. **Enter short on the absorption**; hold to full absorption of the pump / the impulse-low.
4. **Stop just above the impulse-high** (tight vs the move — e.g. MANTA ≈ 6% → RR ≈ 1:6).
5. **Take-profit at / just below the impulse-low** (impulse-set levels always hold liquidity /
   a resting seller → price returns there).

**Management:** partial fix at first support → stop to **breakeven** (not TP1) → runner to the
deep low; may **добор/усреднить + пересидеть** a brief red. Hedging (long+short on the same
coin) only when already in profit.

**Selectivity (critical):** skip pumps with **no clear formation** ("не ловить фому,
перезайдём позже") and dismiss small **20–35%** moves ("мелочи … любим движение пожирнее").
Reinforces the scanner over-firing finding — the edge is RARE and hand-picked.

**Long side (accumulation):** historical low set by an impulse = spot-долгосрок long, targets
in the hundreds of % (DRIFT, CLO, PLAY, STRK examples).

**Context/timing:** trade around big-candle closes (2h/4h/6h/12h; 6h at 21:00 MSK, weekly at
night/Asian session) and funding flips; the edge shifts to manipulations when majors chop.

**Assumptions to challenge (ground truth = these transcripts + real-data behavior, NOT the
existing code/docs/synthetic-tests):**
- The LONG types are explicitly **medium-term** ("в среднесроке 250%"). `research/backtest_scanner.py::FWD_DAYS`
  caps long horizons at 4–5 d — likely TOO SHORT, unfairly timing out A/A3/C. Test longer horizons.
- The two SHORT sources differ on the entry trigger: the GTC clip **waits for the LTF reversal
  confirmation** ("увидели подтверждение на младшем таймфрейме"); the live stream takes the short
  **at the peak on the fade** (LTF = strength upgrade only). The detector currently does the latter;
  whether gating on LTF-confirmed improves expectancy is an empirical question for the backtest.

_Commercial framing (paid «Академия», copy-trading, +300…+4271% P&L screenshots) is marketing —
treat the numbers as the author's claims, not verified results._
