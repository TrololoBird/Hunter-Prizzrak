"""Append-only JSONL helpers for data-layer writers (tick buffer, lake flush)."""
from __future__ import annotations

import shutil
import time
from pathlib import Path


_RETENTION_DAYS = 7  # Task 5: archive rotated files older than this


def _archive_if_expired(path: Path, *, max_age_days: int = _RETENTION_DAYS) -> None:
    """Move a rotated file to archive/ if its mtime exceeds max_age_days."""
    try:
        if not path.exists():
            return
        mtime = path.stat().st_mtime
        age_s = time.time() - mtime
        if age_s > max_age_days * 86400:
            archive_dir = path.parent / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / path.name
            shutil.move(str(path), str(dest))
    except OSError:
        return


def rotate_jsonl_if_needed(
    path: Path,
    *,
    max_bytes: int = 50_000_000,
    keep: int = 3,
) -> None:
    """Rotate oversized JSONL logs: ``path`` → ``path.1`` … ``path.{keep}``."""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    if keep < 1:
        return
    oldest = path.with_name(f"{path.name}.{keep}")
    try:
        if oldest.exists():
            oldest.unlink()
        for idx in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{idx}")
            dst = path.with_name(f"{path.name}.{idx + 1}")
            if src.exists():
                shutil.move(str(src), str(dst))
        rotated = path.with_name(f"{path.name}.1")
        shutil.move(str(path), str(rotated))
        path.touch()
    except OSError:
        return
    # Archive old rotated files
    for idx in range(1, keep + 1):
        p = path.with_name(f"{path.name}.{idx}")
        if p.exists():
            _archive_if_expired(p)


def append_jsonl_lines(path: Path, lines: list[str]) -> None:
    """Append raw JSONL lines with size-based rotation (TICK_JSONL pattern)."""
    if not lines:
        return
    rotate_jsonl_if_needed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if line.endswith("\n") else f"{line}\n")
