# Prizrak corpus — source material for the **Prizrak** module

Live разборы of the **PrizrakTrade** level/structure strategy that
`hunt_core/prizrak/` implements (deep-analysis engine).

> **Truth hierarchy (2026-07-17, user directive — memory `pdf-course-is-primary-truth`):**
> the PDF mini-course («Мини Курс по трейдингу от PrizrakTrade», 69 стр.) is THE primary
> truth for Prizrak. The разборы here are **secondary**: transcription/understanding was
> done by an agent and may contain errors — every conclusion derived only from a разбор
> is suspect until re-verified against the video. Full page-by-page multimodal notes of
> the PDF (text + every chart) live in `course_notes/`; the operational digest is
> `docs/PRIZRAK_METHODOLOGY.md`.

> **Module boundary — do not confuse.** This corpus feeds **Prizrak**
> (`hunt_core/prizrak/`) — level trading: ключевой уровень структуры, накопление/ПОК, ПП
> (истинный/ранний), ловушки (прокол vs пробой), стоповый объём, МТФ, **стоп ЗА СТРУКТУРУ
> с запасом 1–3%**. It is **not** the Scanner Manipulations corpus
> (`research/manipulations_corpus/`, engineered pumps/dumps 20–400%). See the memory
> `two-strategies-source-of-truth` and `prizrak-live-razbor-corpus`.

Each source has a `.txt` (clean transcript) + `.segments.jsonl` (timestamped), plus a
`manifest.jsonl` / `INDEX.md`. All auto-transcribed locally with
`scripts/ingest_manipulation_video.py --corpus prizrak` (mlx-whisper, ru, biased by
`_glossary.txt`). Videos/keyframes are intentionally not committed (bloat).

Classification is by **transcript content**, not filename — a разбор that turns out to be a
manipulation play belongs in the manipulations corpus instead.
