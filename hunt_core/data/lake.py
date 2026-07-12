"""Batch tick JSONL + feature parquet lake + tracker flush off hot path (P9)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import polars as pl

from hunt_core.paths import LAKE_PARQUET, SIGNAL_STATE, TICK_JSONL


class LakeDataError(RuntimeError):
    """Feature lake read/write failure."""


_tick_lines: list[str] = []
_tracker_flush: tuple[dict[str, Any], Path] | None = None
_cooldown_flush: tuple[dict[str, str], Path] | None = None


def buffer_tick_rows(rows: list[dict[str, Any]]) -> None:
    from hunt_core.diagnostics.tick_diagnostics import append_tick_diagnostics
    from hunt_core.data.tick_jsonl import serialize_tick_row

    for row in rows:
        append_tick_diagnostics(row)
        _tick_lines.append(serialize_tick_row(row))


def flush_tick_buffer() -> int:
    if not _tick_lines:
        return 0
    from hunt_core.data.jsonl_io import append_jsonl_lines

    append_jsonl_lines(TICK_JSONL, list(_tick_lines))
    n = len(_tick_lines)
    _tick_lines.clear()
    return n


def buffer_tracker_state(state: dict[str, Any], path: Path | None = None) -> None:
    global _tracker_flush
    _tracker_flush = (state, path or SIGNAL_STATE)


def buffer_cooldown_state(state: dict[str, str], path: Path) -> None:
    global _cooldown_flush
    _cooldown_flush = (state, path)


def _merge_tracker_state(on_disk: dict[str, Any], buffered: dict[str, Any]) -> dict[str, Any]:
    """Merge a buffered tracker-state mutation into whatever is on disk right now.

    Three independent call sites (``_cycle_tick.py``'s per-symbol tick loop,
    ``_cycle_loop.py``'s intra-bar delivery loop, ``_cycle_reconcile.py``) each
    call ``load_tracker_state()`` for their OWN in-memory copy, mutate it, and
    later buffer+flush that copy — with no lock and no re-read before writing.
    A blind overwrite here meant whichever of the three flushed last won
    entirely, silently erasing every other loop's concurrent changes since its
    load. Confirmed in practice: 8 intra-bar signals registered in-memory
    (verified against the buffer right after registration), only 1 survived to
    disk — the per-tick loop's own stale copy, flushed afterward, overwrote the
    other 7. Per-key merge on the known mutable collections shrinks the lost-
    update window to "two writers touch the exact same key in the same beat"
    instead of "any two flushes anywhere stomp each other".
    """
    if not isinstance(on_disk, dict) or not on_disk:
        return buffered
    if not isinstance(buffered, dict):
        return on_disk
    merged: dict[str, Any] = dict(on_disk)
    for key in ("signals", "followup_sent"):
        merged[key] = {**(on_disk.get(key) or {}), **(buffered.get(key) or {})}
    disk_hist = on_disk.get("closed_history") or []
    buf_hist = buffered.get("closed_history") or []
    if buf_hist or disk_hist:
        seen: set[str] = set()
        combined: list[Any] = []
        for rec in list(disk_hist) + list(buf_hist):
            ident = json.dumps(rec, sort_keys=True, default=str) if isinstance(rec, dict) else str(rec)
            if ident in seen:
                continue
            seen.add(ident)
            combined.append(rec)
        merged["closed_history"] = combined
    for key, val in buffered.items():
        if key in ("signals", "followup_sent", "closed_history"):
            continue
        merged[key] = val
    return merged


def flush_tracker_state() -> bool:
    global _tracker_flush
    if _tracker_flush is None:
        return False
    state, path = _tracker_flush
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            on_disk = {}
        state = _merge_tracker_state(on_disk, state)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    _tracker_flush = None
    return True


def flush_cooldown_state() -> bool:
    global _cooldown_flush
    if _cooldown_flush is None:
        return False
    state, path = _cooldown_flush
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _cooldown_flush = None
    return True



_MAP_LAKE_PATH = LAKE_PARQUET.parent / "maps" / "snapshots.jsonl"


def flush_map_lake() -> int:
    """Flush MapTimeSeriesStore ring-buffer snapshots to JSONL lake."""
    try:
        from hunt_core.maps.engine import get_map_store
        return get_map_store().flush_lake(_MAP_LAKE_PATH)
    except Exception:
        return 0

def flush_lake() -> None:
    flush_map_lake()
    flush_tick_buffer()
    flush_tracker_state()
    flush_cooldown_state()


def _parquet_path(symbol: str, tf: str) -> Path:
    sym = str(symbol or "").strip().upper()
    return LAKE_PARQUET / sym / f"{tf}.parquet"


class FeatureLakeWriter:
    """Buffered parquet feature writer — flush on close."""

    def __init__(self) -> None:
        self._buf: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def enqueue(self, symbol: str, ts: str, tf: str, payload: dict[str, Any]) -> None:
        row = {"symbol": symbol, "ts": ts, "tf": tf, **payload}
        key = (str(symbol).upper(), str(tf))
        with self._lock:
            self._buf.setdefault(key, []).append(row)

    def close(self) -> None:
        with self._lock:
            pending = dict(self._buf)
            self._buf.clear()
        for (symbol, tf), rows in pending.items():
            if not rows:
                continue
            path = _parquet_path(symbol, tf)
            path.parent.mkdir(parents=True, exist_ok=True)
            new_df = pl.DataFrame(rows, infer_schema_length=len(rows) or None)
            if path.exists():
                try:
                    old = pl.read_parquet(path)
                    new_df = pl.concat([old, new_df], how="diagonal_relaxed")
                except DEFENSIVE_EXC:
                    pass
            new_df.write_parquet(path)


DEFENSIVE_EXC = (OSError, ValueError, pl.exceptions.PolarsError)


def read_features(
    symbol: str,
    start_ts: str,
    end_ts: str,
    *,
    tf: str = "15m",
) -> pl.DataFrame:
    path = _parquet_path(symbol, tf)
    if not path.exists():
        raise LakeDataError(f"no lake parquet for {symbol} {tf}")
    df = pl.read_parquet(path)
    if df.is_empty() or "ts" not in df.columns:
        return df
    return df.filter((pl.col("ts") >= start_ts) & (pl.col("ts") <= end_ts))


def append_feature_row(symbol: str, ts: str, tf: str, payload: dict[str, Any]) -> None:
    w = FeatureLakeWriter()
    w.enqueue(symbol, ts, tf, payload)
    w.close()


def get_feature_lake_writer() -> FeatureLakeWriter:
    return FeatureLakeWriter()


def serialize_tick_row(row: dict[str, Any]) -> str:
    from hunt_core.data.tick_jsonl import serialize_tick_row as _serialize

    return _serialize(row)


def append_tick_rows(rows: list[dict[str, Any]]) -> int:
    buffer_tick_rows(rows)
    return flush_tick_buffer()


def query_features(symbol: str, *, tf: str = "15m", limit: int = 500) -> pl.DataFrame:
    path = _parquet_path(symbol, tf)
    if not path.exists():
        return pl.DataFrame()
    df = pl.read_parquet(path)
    if limit > 0 and df.height > limit:
        return df.tail(limit)
    return df


def query_baseline_stats(symbol: str, *, tf: str = "15m") -> dict[str, Any]:
    df = query_features(symbol, tf=tf, limit=0)
    if df.is_empty():
        return {"symbol": symbol, "tf": tf, "rows": 0}
    return {"symbol": symbol, "tf": tf, "rows": df.height}


def import_ticks_to_lake(_path: Path) -> int:
    return 0


class LakeStore:
    """Compat placeholder for lake sqlite backend."""

    def __init__(self, _path: Path | None = None) -> None:
        pass


__all__ = [
    "FeatureLakeWriter",
    "LakeDataError",
    "LakeStore",
    "append_feature_row",
    "append_tick_rows",
    "buffer_cooldown_state",
    "buffer_tick_rows",
    "buffer_tracker_state",
    "flush_cooldown_state",
    "flush_lake",
    "flush_tick_buffer",
    "flush_tracker_state",
    "get_feature_lake_writer",
    "import_ticks_to_lake",
    "query_baseline_stats",
    "query_features",
    "read_features",
    "serialize_tick_row",
]
