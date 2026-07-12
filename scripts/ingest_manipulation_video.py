#!/usr/bin/env python3
"""Ingest a strategy video разбор (local file OR URL) into an in-repo corpus.

Pipeline (fully local transcription, no cloud STT): source → 16 kHz mono audio → mlx-whisper
(ru, biased by a domain glossary) → cleaned, ticker-corrected transcript + timestamped
segments in the target corpus under ``research/``. URLs are fetched with yt-dlp; manual
subtitles are used when present (skips whisper), otherwise the audio is transcribed.

Corpora keep the module boundary explicit (see memory ``two-strategies-source-of-truth``):
- ``--corpus manipulations`` → ``research/manipulations_corpus/`` (Scanner Manipulations,
  ``hunt_core/scanner``). Default.
- ``--corpus prizrak``       → ``research/prizrak_corpus/`` (Prizrak level trading,
  ``hunt_core/prizrak``).

Quality aids (per corpus, editable):
- ``<corpus>/_glossary.txt``    — extra terms fed to whisper as ``initial_prompt``.
- ``<corpus>/_corrections.txt`` — ``wrong => right`` lines applied to the transcript to fix
  systematically mis-heard tickers/jargon (ESPROC→ESPORTS, Зеребро→ZEREBRO, …).
- degenerate/silence-hallucination segments are dropped.
- ``manifest.jsonl`` + regenerated ``INDEX.md``; re-ingesting the same source (file sha256 or
  YouTube id) is a no-op.

Usage:
    uv run python scripts/ingest_manipulation_video.py ~/Downloads/record.mp4
    uv run python scripts/ingest_manipulation_video.py https://youtu.be/ID --corpus prizrak
    uv run python scripts/ingest_manipulation_video.py SRC --stdout          # classify, don't file
    uv run python scripts/ingest_manipulation_video.py SRC --frames --accurate

First run needs the local-only deps (Apple Silicon for mlx):
    uv pip install imageio-ffmpeg mlx-whisper yt-dlp
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPORA = {
    "manipulations": REPO_ROOT / "research" / "manipulations_corpus",
    "prizrak": REPO_ROOT / "research" / "prizrak_corpus",
}
MODULE_NOTE = {
    "manipulations": "This corpus feeds hunt_core/scanner (Scanner Manipulations), not Prizrak.",
    "prizrak": "This corpus feeds hunt_core/prizrak (Prizrak level trading), not the Scanner.",
}
MODELS = {"turbo": "mlx-community/whisper-large-v3-turbo",
          "accurate": "mlx-community/whisper-large-v3"}

DEFAULT_GLOSSARY = (
    "Крипто-разбор фьючерсов. Термины: манипуляция, памп, дамп, поглощение, импульс, "
    "импульсная свеча, боковик, консолидация, закреп, слом структуры, нисходящий канал, "
    "восходящий канал, ликвидность, ликвидация, стоп-лосс, тейк, добор, усреднение, "
    "пересиживание, лонг, шорт, хай, лой, уровень поддержки, снятие ликвидности. "
    "Тикеры: BTC, ETH, MANTA, VELVET, ZEREBRO, MAGIC, KAITO, DRIFT, PLAY, STRK, BIT, "
    "ESPORTS, BSB, HEA, CLO, EVAA, HMSTR, BLUAI, SKYAI, STG, POL, MATIC, ONDO."
)

_HALLUCINATION_RE = re.compile(
    r"^(продолжение следует|субтитр|редактор субтитров|спасибо за просмотр|"
    r"подписывайтесь|dimatorzok|amara\.org)", re.IGNORECASE)
_WORDISH_RE = re.compile(r"[a-zа-я0-9]", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError:
        _die("imageio-ffmpeg not installed. Run: uv pip install imageio-ffmpeg mlx-whisper yt-dlp")
    return imageio_ffmpeg.get_ffmpeg_exe()


# ── source resolution (local file or URL) ───────────────────────────────────
def _is_url(s: str) -> bool:
    return bool(_URL_RE.match(s))


def _ytdlp(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-m", "yt_dlp", *args],
                capture_output=capture, text=capture)


def _yt_meta(url: str) -> dict:
    try:
        out = _ytdlp(["--dump-single-json", "--no-download", url], capture=True).stdout
    except subprocess.CalledProcessError as e:
        _die(f"yt-dlp metadata failed: {e}")
    return json.loads(out)


def _yt_fetch(url: str, dest: Path, want_video: bool) -> Path:
    """Download bestaudio (or a ≤720p video when frames are needed) into dest dir."""
    ffmpeg = _ffmpeg_exe()
    fmt = "best[height<=720]/best" if want_video else "bestaudio/best"
    tmpl = str(dest / "src.%(ext)s")
    _ytdlp(["-f", fmt, "--ffmpeg-location", str(Path(ffmpeg).parent),
            "-o", tmpl, "--no-playlist", "--quiet", "--no-warnings", url])
    files = [p for p in dest.iterdir() if p.stem == "src"]
    if not files:
        _die("yt-dlp produced no output file")
    return files[0]


def _yt_manual_subs(url: str, lang: str, dest: Path) -> Path | None:
    """Download manual (human) subtitles only — auto-captions are too noisy to trust."""
    _ytdlp(["--skip-download", "--write-subs", "--sub-langs", lang, "--sub-format", "vtt/best",
            "-o", str(dest / "sub"), "--no-warnings", "--quiet", url])
    vtts = list(dest.glob("sub*.vtt"))
    return vtts[0] if vtts else None


def _parse_vtt(path: Path) -> list[dict]:
    def _ts(s: str) -> float:
        hh, mm, rest = s.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(rest.replace(",", "."))
    segs: list[dict] = []
    start = end = None
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\d+:\d+:[\d.,]+)\s*-->\s*(\d+:\d+:[\d.,]+)", line)
        if m:
            if buf and start is not None:
                segs.append({"start": start, "end": end, "text": " ".join(buf)})
            start, end, buf = _ts(m.group(1)), _ts(m.group(2)), []
        elif line.strip() and not line.startswith(("WEBVTT", "Kind:", "Language:")):
            txt = re.sub(r"<[^>]+>", "", line).strip()
            if txt and (not buf or buf[-1] != txt):
                buf.append(txt)
    if buf and start is not None:
        segs.append({"start": start, "end": end, "text": " ".join(buf)})
    return segs


# ── corpus helpers ───────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_duration(ffmpeg: str, src: Path) -> float:
    out = subprocess.run([ffmpeg, "-i", str(src), "-hide_banner"],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        return 0.0
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zа-я0-9]+", "_", text.lower()).strip("_")
    return (s[:60].rstrip("_")) or "video"


def _glossary(corpus_dir: Path) -> str:
    extra = corpus_dir / "_glossary.txt"
    if extra.is_file():
        return (DEFAULT_GLOSSARY + " " + extra.read_text(encoding="utf-8")).strip()
    return DEFAULT_GLOSSARY


def _corrections(corpus_dir: Path) -> list[tuple[re.Pattern[str], str]]:
    """Load `wrong => right` rules; applied case-insensitively on word boundaries."""
    path = corpus_dir / "_corrections.txt"
    rules: list[tuple[re.Pattern[str], str]] = []
    if not path.is_file():
        return rules
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=>" not in line:
            continue
        wrong, right = (p.strip() for p in line.split("=>", 1))
        if wrong:
            rules.append((re.compile(rf"(?<![\w]){re.escape(wrong)}(?![\w])", re.IGNORECASE),
                          right))
    return rules


def _apply_corrections(text: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pat, repl in rules:
        text = pat.sub(repl, text)
    return text


def _is_degenerate(text: str) -> bool:
    t = text.strip()
    if not t or _HALLUCINATION_RE.match(t):
        return True
    return len(_WORDISH_RE.findall(t)) / max(len(t), 1) < 0.45


def _extract_audio(ffmpeg: str, src: Path, wav: Path) -> None:
    _run([ffmpeg, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", str(wav), "-hide_banner", "-loglevel", "error"])


def _load_wav(wav: Path):
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _extract_frames(ffmpeg: str, src: Path, out_dir: Path, threshold: float) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-y", "-i", str(src),
          "-vf", f"select='gt(scene,{threshold})',scale=1280:-1",
          "-vsync", "vfr", "-frame_pts", "1",
          str(out_dir / "scene_%04d.jpg"), "-hide_banner", "-loglevel", "error"])
    return len(list(out_dir.glob("scene_*.jpg")))


def _load_manifest(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_index(corpus_dir: Path, rows: list[dict]) -> None:
    lines = ["# Corpus index", "",
             "_Auto-generated by scripts/ingest_manipulation_video.py — do not hand-edit._", "",
             "| Slug | Source | Duration | Ingested | Segments | Model |",
             "|------|--------|----------|----------|----------|-------|"]
    for r in sorted(rows, key=lambda x: x.get("ingested", "")):
        dur = f"{int(r['duration_s'] // 60)}:{int(r['duration_s'] % 60):02d}" if r.get("duration_s") else "?"
        lines.append(f"| `{r['slug']}` | {r['source']} | {dur} | {r['ingested']} | "
                     f"{r.get('n_segments', '?')} | {r.get('model', '?')} |")
    (corpus_dir / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── main ─────────────────────────────────────────────────────────────────────
def _transcribe(wav: Path, model: str, language: str, glossary: str) -> list[dict]:
    import mlx_whisper
    r = mlx_whisper.transcribe(
        _load_wav(wav), path_or_hf_repo=model, language=language,
        initial_prompt=glossary, hallucination_silence_threshold=2.0, verbose=False,
    )
    return r["segments"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="local video path OR a URL (YouTube, etc.)")
    ap.add_argument("--corpus", choices=sorted(CORPORA), default="manipulations",
                    help="target corpus / module (default: manipulations)")
    ap.add_argument("--name", help="output slug (default: from filename/title + date)")
    ap.add_argument("--accurate", action="store_true",
                    help="use large-v3 (more accurate ru) instead of turbo")
    ap.add_argument("--model", help="explicit mlx-whisper HF repo (overrides --accurate)")
    ap.add_argument("--language", default="ru")
    ap.add_argument("--subs", action="store_true",
                    help="for URLs: use manual subtitles if present (skip whisper)")
    ap.add_argument("--frames", action="store_true", help="also dump scene keyframes to a temp dir")
    ap.add_argument("--scene-threshold", type=float, default=0.3)
    ap.add_argument("--stdout", action="store_true",
                    help="print transcript to stdout and exit — do NOT write to the corpus")
    ap.add_argument("--force", action="store_true",
                    help="re-ingest even if already present: re-transcribe, overwrite the "
                         "transcript, replace the manifest row (use after pipeline upgrades)")
    args = ap.parse_args()

    corpus_dir = CORPORA[args.corpus]
    manifest_path = corpus_dir / "manifest.jsonl"
    model = args.model or (MODELS["accurate"] if args.accurate else MODELS["turbo"])
    ffmpeg = _ffmpeg_exe()
    is_url = _is_url(args.source)

    # Resolve identity for dedup + naming before doing expensive work.
    if is_url:
        meta = _yt_meta(args.source)
        ident, source_label = f"yt:{meta['id']}", args.source
        duration = float(meta.get("duration") or 0.0)
        default_name = _slugify(meta.get("title") or meta["id"])
    else:
        video = Path(args.source).expanduser()
        if not video.is_file():
            _die(f"source not found: {video}")
        ident, source_label = _sha256(video), video.name
        duration = _probe_duration(ffmpeg, video)
        default_name = f"{_slugify(video.stem)}_{_dt.date.today().isoformat()}"

    if not args.stdout and not args.force:
        corpus_dir.mkdir(parents=True, exist_ok=True)
        dup = next((r for r in _load_manifest(manifest_path)
                    if ident in (r.get("sha256"), r.get("source_id"))), None)
        if dup:
            print(f"already ingested as '{dup['slug']}' — pass --force to re-ingest.")
            return 0

    slug = args.name or default_name
    txt_path, seg_path = corpus_dir / f"{slug}.txt", corpus_dir / f"{slug}.segments.jsonl"
    if not args.stdout and txt_path.exists() and not args.force:
        _die(f"{txt_path} already exists — pass a different --name (or --force to overwrite).")

    rules = _corrections(corpus_dir)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        raw_segments: list[dict] | None = None
        if is_url and args.subs:
            print("[1/3] fetching manual subtitles …", flush=True)
            vtt = _yt_manual_subs(args.source, args.language, tmp)
            if vtt:
                raw_segments = _parse_vtt(vtt)
                print(f"      using manual subs ({len(raw_segments)} cues)")
            else:
                print("      no manual subs — falling back to whisper")

        if raw_segments is None:
            if is_url:
                print(f"[1/3] downloading audio ({duration/60:.1f} min) …", flush=True)
                media = _yt_fetch(args.source, tmp, want_video=args.frames)
            else:
                media = video
            wav = tmp / "audio.wav"
            print("      extracting 16 kHz mono …", flush=True)
            _extract_audio(ffmpeg, media, wav)
            print(f"[2/3] transcribing ({model}, lang={args.language}, glossary on) …", flush=True)
            raw_segments = _transcribe(wav, model, args.language, _glossary(corpus_dir))

        segments = [s for s in raw_segments if not _is_degenerate(s["text"])]
        for s in segments:
            s["text"] = _apply_corrections(s["text"].strip(), rules)
        dropped = len(raw_segments) - len(segments)
        text = " ".join(s["text"] for s in segments).strip()

        if args.stdout:
            print("\n" + text)
            print(f"\n--- {len(segments)} segments, {dropped} dropped, "
                  f"{len(rules)} correction rules (dry-run, not written) ---", file=sys.stderr)
            return 0

        # keyframes need the video; for URLs it was only downloaded when --frames was set
        if args.frames:
            src_for_frames = media if is_url else video
            frames_dir = Path(tempfile.gettempdir()) / "manip_frames" / slug
            print(f"[3/3] extracting keyframes → {frames_dir} …", flush=True)
            n = _extract_frames(ffmpeg, src_for_frames, frames_dir, args.scene_threshold)
            print(f"      {n} scene frames (NOT committed — read them with the Read tool)")

    txt_path.write_text(text + "\n", encoding="utf-8")
    with seg_path.open("w", encoding="utf-8") as f:
        for s in segments:
            f.write(json.dumps({"start": round(s["start"], 1), "end": round(s["end"], 1),
                                "text": s["text"]}, ensure_ascii=False) + "\n")
    print(f"      wrote {txt_path.relative_to(REPO_ROOT)} ({len(text)} chars)")
    print(f"      wrote {seg_path.relative_to(REPO_ROOT)} "
          f"({len(segments)} segments, {dropped} degenerate dropped)")

    row = {"slug": slug, "source": source_label,
           ("source_id" if is_url else "sha256"): ident,
           "duration_s": round(duration, 1), "n_segments": len(segments),
           "model": (model.split("/")[-1] if raw_segments and not (is_url and args.subs) else "subs"),
           "ingested": _dt.date.today().isoformat()}
    # idempotent: drop any prior row for this slug/source, then append (handles --force re-ingest)
    manifest = [r for r in _load_manifest(manifest_path)
                if r.get("slug") != slug and ident not in (r.get("sha256"), r.get("source_id"))]
    manifest.append(row)
    manifest_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in manifest) + "\n", encoding="utf-8")
    _write_index(corpus_dir, manifest)
    print(f"      updated {manifest_path.relative_to(REPO_ROOT)} + INDEX.md")
    print(f"done. {MODULE_NOTE[args.corpus]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
