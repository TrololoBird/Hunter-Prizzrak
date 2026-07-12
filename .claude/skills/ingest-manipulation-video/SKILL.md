---
name: ingest-manipulation-video
description: Transcribe a strategy video разбор (local file OR URL, e.g. a YouTube/«Owner of SHORT» stream) and file it into an in-repo corpus as source material. Defaults to the Scanner Manipulations corpus; can also route to Prizrak. Use whenever the user drops a new video разбор to be studied/archived.
disable-model-invocation: true
---

Ingest a video разбор into an in-repo corpus, fully locally (no cloud STT). Pipeline:
source (local file OR URL via yt-dlp) → 16 kHz mono audio → mlx-whisper (ru, biased by a
domain glossary) → cleaned, ticker-corrected transcript + timestamped segments, plus a
corpus manifest + INDEX.

**Module boundary is explicit via `--corpus`** (see memory `two-strategies-source-of-truth`).
Route by author/content: «Owner of SHORT»/Влад SHORT pump-dump plays → manipulations;
PrizrakTrade / level-structure разборы → prizrak.
- `--corpus manipulations` (default) → `research/manipulations_corpus/` → `hunt_core/scanner`
- `--corpus prizrak` → `research/prizrak_corpus/` → `hunt_core/prizrak`

Run:

```bash
uv run python scripts/ingest_manipulation_video.py "<PATH_or_URL>"
# options:
#   --corpus manipulations|prizrak   target corpus/module (default manipulations)
#   --name <slug>                    output name (default: from filename/title + date)
#   --accurate                       large-v3 (more accurate ru) instead of turbo
#   --subs                           for URLs: use manual subtitles if present (skip whisper)
#   --stdout                         print transcript only — classify BEFORE filing, no writes
#   --frames                         also dump scene keyframes to a temp dir (never committed)
```

Tips:
- **Unsure which module?** Run with `--stdout` first to read the transcript, then ingest into
  the right `--corpus`.
- **YouTube/URL** just works (yt-dlp); private Telegram streams must be downloaded to a file.
- Dedup is by file sha256 OR YouTube id — re-ingesting the same source is a no-op.
- Fix systematic ticker mis-hearings by adding `wrong => right` lines to
  `<corpus>/_corrections.txt`; extend the whisper bias via `<corpus>/_glossary.txt`.

First run only, install the local-only deps (Apple Silicon for mlx):

```bash
uv pip install imageio-ffmpeg mlx-whisper yt-dlp
```

What it produces per video, in the chosen corpus:
- `<slug>.txt` — cleaned transcript (degenerate/silence-hallucination segments dropped).
- `<slug>.segments.jsonl` — `{start, end, text}` timecodes; use to locate a claim in the video.
- appends to `manifest.jsonl` (slug, source, sha256, duration, n_segments, model, date) and
  regenerates `INDEX.md`. **Re-ingesting the same video (sha256 match) is a no-op.**

Tuning the glossary: recognition of tickers/jargon is biased by a built-in glossary; extend
it per corpus by dropping extra terms into `<corpus>/_glossary.txt`.

After it runs:
1. Read the new `<slug>.txt` (or `.segments.jsonl` for timecodes). If `--frames`, Read the
   keyframes from the printed temp dir to see the charts.
2. Produce the разбор: distil the formation(s) — for manipulations: impulse-обновление
   экстремума → поглощение → вход, stop за импульс-экстремум, тейк у противоположного
   импульс-уровня, БУ + добор + пересиживание, жёсткая селективность (пропуск бесформенных
   пампов и мелких 20–35% движений).
3. Do NOT commit the video or keyframes (image/binary bloat) — only transcript + segments +
   manifest/INDEX belong in git.

Keep the module label correct: never file «Owner of SHORT» streams under Prizrak.
