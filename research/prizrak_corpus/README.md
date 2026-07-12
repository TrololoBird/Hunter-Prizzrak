# Prizrak corpus — source material for the **Prizrak** module

Primary-source live разборы of the **PrizrakTrade** level/structure strategy that
`hunt_core/prizrak/` implements (decision authority; deep-analysis engine).

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
