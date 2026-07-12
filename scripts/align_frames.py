#!/usr/bin/env python3
"""Align scene-change keyframes of a video with its transcript segments.

Produces, for a video + its ``<slug>.segments.jsonl`` (from ingest_manipulation_video.py):
- scene-change JPEGs with **real timestamps** in a temp dir (never committed), and
- ``<out>/frames.jsonl``: for each frame ``{i, t, hhmmss, file, said}`` where ``said`` is the
  narration spoken in a window around the frame's timestamp.

This is the substrate for a multimodal разбор: read the frames in order together with what
the narrator says at that moment, then synthesise a detailed timeline. It does NOT try to
auto-extract structured setups — the visual+audio pairing is left for the model to read.

Usage:
    uv run python scripts/align_frames.py VIDEO --segments research/<corpus>/<slug>.segments.jsonl
    # options: --threshold 0.3  --max-frames 120  --pre 4 --post 8  --out <dir>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _hhmmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"


def _probe_duration(ffmpeg: str, video: Path) -> float:
    out = subprocess.run([ffmpeg, "-i", str(video), "-hide_banner"],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def _extract_frames_with_ts(ffmpeg: str, video: Path, out_dir: Path,
                            interval: float) -> list[tuple[int, float]]:
    """Sample one frame every `interval` s (even coverage for screencasts where scene
    detection barely fires). The fps filter emits frames at 0, interval, 2·interval, … so
    frame i's timestamp is deterministic — no metadata parsing needed. Returns
    [(index, seconds)] in output (chronological) order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("scene_*.jpg"):
        old.unlink()
    subprocess.run(
        [ffmpeg, "-y", "-i", str(video),
         "-vf", f"fps=1/{interval},scale=1280:-1",
         "-vsync", "vfr", str(out_dir / "scene_%04d.jpg"),
         "-hide_banner", "-loglevel", "error"],
        check=True)
    frames = sorted(out_dir.glob("scene_*.jpg"))
    return [(p, i * interval) for i, p in enumerate(frames)]


def _load_segments(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _said_around(segments: list[dict], t: float, pre: float, post: float) -> str:
    lo, hi = t - pre, t + post
    hits = [s["text"] for s in segments if s["end"] >= lo and s["start"] <= hi]
    return " ".join(hits).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--segments", type=Path, required=True, help="path to <slug>.segments.jsonl")
    ap.add_argument("--out", type=Path, help="frames dir (default: temp/align_frames/<stem>)")
    ap.add_argument("--min-interval", type=float, default=6.0,
                    help="never sample finer than this many seconds")
    ap.add_argument("--max-frames", type=int, default=140)
    ap.add_argument("--pre", type=float, default=4.0, help="seconds of narration before the frame")
    ap.add_argument("--post", type=float, default=8.0, help="seconds of narration after the frame")
    args = ap.parse_args()

    video = args.video.expanduser()
    if not video.is_file():
        print(f"error: video not found: {video}")
        return 1
    if not args.segments.is_file():
        print(f"error: segments not found: {args.segments}")
        return 1

    ffmpeg = _ffmpeg_exe()
    out = args.out or (Path(tempfile.gettempdir()) / "align_frames" / video.stem)
    duration = _probe_duration(ffmpeg, video)
    # even coverage, bounded by --max-frames and never finer than --min-interval
    interval = max(args.min_interval, duration / args.max_frames) if duration else args.min_interval
    frames = _extract_frames_with_ts(ffmpeg, video, out, interval)

    segments = _load_segments(args.segments)
    rows = []
    for idx, (path, t) in enumerate(frames):
        rows.append({"i": idx, "t": round(t, 1), "hhmmss": _hhmmss(t),
                     "file": str(path),
                     "said": _said_around(segments, t, args.pre, args.post)})
    (out / "frames.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    print(f"frames dir: {out}")
    print(f"{len(rows)} aligned frames → {out / 'frames.jsonl'}")
    print("read them in order (image + `said`) to build the multimodal разбор.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
