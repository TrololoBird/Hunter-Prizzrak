"""Rotate hunt tick JSONL — daily files, gzip archive, 14-day retention."""
from __future__ import annotations



import gzip
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hunt_core.paths import (
    DATA,
    HUNT_SCAN_JSONL,
    PREP_SHADOW_EVENTS,
    SETUP_CANDIDATES_EVENTS,
    SENT_MESSAGES,
    SIGNAL_EVENTS,
)

RETENTION_DAYS = 14
_TELEMETRY_JSONL = (
    SIGNAL_EVENTS,
    SENT_MESSAGES,
    PREP_SHADOW_EVENTS,
    SETUP_CANDIDATES_EVENTS,
)


def rotate_hunt_ticks(*, retention_days: int = RETENTION_DAYS, dry_run: bool = False) -> dict[str, int]:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    daily = DATA / f"hunt_scan-{today}.jsonl"
    stats = {"appended_lines": 0, "archived": 0, "pruned": 0}

    source = HUNT_SCAN_JSONL
    if not source.exists():
        return stats

    size = source.stat().st_size
    if size < 1024:
        return stats

    if daily.exists():
        if dry_run:
            with source.open(encoding="utf-8") as src:
                stats["appended_lines"] = sum(1 for _ in src)
        else:
            with source.open(encoding="utf-8") as src, daily.open("a", encoding="utf-8") as dst:
                for line in src:
                    dst.write(line)
                    stats["appended_lines"] += 1
    else:
        if not dry_run:
            shutil.move(str(source), str(daily))
        stats["archived"] = 1

    if not dry_run:
        HUNT_SCAN_JSONL.write_text("", encoding="utf-8")

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for pattern in ("hunt_scan-*.jsonl",):
        for path in sorted(DATA.glob(pattern)):
            stem = path.stem.replace("hunt_scan-", "")
            try:
                day = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < cutoff:
                gz = path.with_suffix(path.suffix + ".gz")
                if not dry_run:
                    with path.open("rb") as f_in, gzip.open(gz, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    path.unlink()
                stats["pruned"] += 1

    return stats


def rotate_telemetry_jsonl(
    paths: tuple[Path, ...] = _TELEMETRY_JSONL,
    *,
    max_bytes: int = 50_000_000,
    keep: int = 3,
) -> dict[str, int]:
    """Rotate oversized append-only telemetry JSONL (signal_events, prep_shadow, candidates)."""
    from hunt_core.track.events import rotate_jsonl_if_needed

    stats = {"checked": 0, "rotated": 0}
    for path in paths:
        stats["checked"] += 1
        try:
            size_before = path.stat().st_size if path.exists() else 0
        except OSError:
            continue
        rotate_jsonl_if_needed(path, max_bytes=max_bytes, keep=keep)
        try:
            size_after = path.stat().st_size if path.exists() else 0
        except OSError:
            size_after = 0
        if size_before >= max_bytes and size_after < size_before:
            stats["rotated"] += 1
    return stats
