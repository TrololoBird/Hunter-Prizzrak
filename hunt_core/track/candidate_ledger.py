"""Candidate path-log — the B4 cornerstone.

One record per candidate (fired AND filtered), written in two phases:

- **A/B/C at decision time (t0)**, immutable — identity, decision-time context,
  and model outputs. Never recomputed later: recomputing a "decision-time"
  feature with newer code is a lookahead leak.
- **D at t0+H_max (backfill)** — the raw forward OHLCV path plus a denormalized
  derived cache (MFE/MAE/time-to/ret_at_offsets/first_passage_grid). The path is
  the source of truth; every derived stat is a *view* over it, never logged as
  a primitive fact, so any TP/SL/H scheme (including future trailing-stop
  schemes, which a static first-passage grid cannot reconstruct) can be scored
  retrospectively against the same candidates without re-fetching data or
  changing the schema.

Storage: JSONL, one row per candidate_id, two writes (A/B/C then a D patch
identified by the same candidate_id). Denormalized per-candidate for v1 per the
accepted tradeoff (tens of GB/month at scale; can split into a separate
(symbol, t0_bucket) path table later without touching A/B/C).

Note (audit G-78): the decision-write API (``new_candidate_id`` /
``record_candidate_decision``) is intentionally inert — no caller writes
decision rows yet — pending B4 backfill wiring into the detector's
per-candidate decision path.
"""
from __future__ import annotations

import gzip
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from hunt_core import serde
from hunt_core.paths import DATA

LOG = structlog.get_logger("hunt_core.track.candidate_ledger")

CANDIDATE_LEDGER_PATH = DATA / "candidate_observations.jsonl"

# Default forward-path horizon. Deliberately generous ("ночь" could be a fixed
# 8-12h or a vol-adaptive vertical barrier — undecided) so the vertical barrier
# can be cut offline from the stored path; the reverse (a too-short H_max) is
# unrecoverable without re-fetching. 24h at 1m resolution = 1440 bars/candidate.
DEFAULT_H_MAX_HOURS = 24


def new_candidate_id() -> str:
    return uuid.uuid4().hex


def record_candidate_decision(
    *,
    candidate_id: str,
    symbol: str,
    decision_ts_ms: int,
    entry_tf: str,
    direction: str,
    signal_price: float,
    assumed_fill_price: float | None,
    atr_entry_tf: float | None,
    realized_vol: float | None,
    bar_interval: str,
    regime_tag: str | None,
    universe_size: int | None,
    scan_cycle_id: str | None,
    raw_confluence_score: float | None,
    sub_scores: dict[str, float],
    gate_decision: str,
    gate_reason: list[str],
    rank_in_cycle: int | None,
    delivered: bool,
    code_version_hash: str | None = None,
    h_max_hours: int = DEFAULT_H_MAX_HOURS,
    path: Path | None = None,
) -> None:
    """Write the A/B/C immutable half of a CandidateObservation at t0.

    Called for EVERY candidate the detector considers — fired and filtered —
    not just delivered signals. This is what makes precision@k / PR-AUC and
    gate-blindness fixes possible: a filtered candidate whose forward path
    would have hit TP is a gate false-negative, and it is only visible if this
    function was called for it too.
    """
    row: dict[str, Any] = {
        "candidate_id": candidate_id,
        "symbol": str(symbol).upper(),
        "decision_ts": decision_ts_ms,
        "decision_ts_iso": datetime.fromtimestamp(decision_ts_ms / 1000, tz=UTC).isoformat(),
        "entry_tf": entry_tf,
        "scan_cycle_id": scan_cycle_id,
        "code_version_hash": code_version_hash,
        "signal_price": signal_price,
        "assumed_fill_price": assumed_fill_price,
        "atr_entry_tf": atr_entry_tf,
        "realized_vol": realized_vol,
        "bar_interval": bar_interval,
        "direction": str(direction).lower(),
        "regime_tag": regime_tag,
        "universe_size": universe_size,
        "raw_confluence_score": raw_confluence_score,
        "sub_scores": dict(sub_scores or {}),
        "gate_decision": gate_decision,
        "gate_reason": list(gate_reason or []),
        "rank_in_cycle": rank_in_cycle,
        "delivered": bool(delivered),
        "h_max_hours": h_max_hours,
        "backfilled": False,
        "record_kind": "decision",
        "ts": datetime.now(UTC).isoformat(),
    }
    p = path or CANDIDATE_LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(serde.dumps_str(row) + "\n")


def record_candidate_forward_path(
    *,
    candidate_id: str,
    symbol: str,
    decision_ts_ms: int,
    forward_ohlcv: list[list[float]],
    forward_dq: dict[str, Any],
    derived: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Write the D (forward-path realization) patch for a candidate.

    Appended as a SEPARATE row (record_kind="path"), joined to the decision
    row by candidate_id at read time — JSONL is append-only, so this is not an
    in-place mutation of the t0 record (which stays immutable on disk).
    ``derived`` is a denormalized cache reconstructed entirely from
    ``forward_ohlcv``; it must never be trusted as ground truth on its own —
    re-derive from the path if the derivation logic changes.
    """
    row: dict[str, Any] = {
        "candidate_id": candidate_id,
        "symbol": str(symbol).upper(),
        "decision_ts": decision_ts_ms,
        "forward_ohlcv": forward_ohlcv,
        "forward_dq": dict(forward_dq or {}),
        "derived": dict(derived or {}),
        "record_kind": "path",
        "ts": datetime.now(UTC).isoformat(),
    }
    p = path or CANDIDATE_LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(serde.dumps_str(row) + "\n")


_ROTATE_BYTES = 1_000_000_000  # 1 GB


def rotate_ledger_if_large(
    *,
    path: Path | None = None,
    max_bytes: int = _ROTATE_BYTES,
) -> Path | None:
    """Roll the ledger into a gzip archive once it exceeds ``max_bytes``.

    The denormalized-per-candidate schema is a deliberate tradeoff (see the
    module docstring: "tens of GB/month at scale"), so growth is by design — but
    a single unbounded file is not. Live it reached 7.7 GB, and every
    ``load_pending_backfill`` reads the whole thing.

    Nothing is deleted: the file is renamed and gzipped into
    ``data/archive/``, and a fresh empty ledger takes its place. Rows are
    self-contained JSONL (a decision row and its path patch are joined by
    ``candidate_id`` at read time), so an archived pair stays joinable —
    but a decision row and its later path patch CAN land either side of a
    rotation, so **offline readers must scan the archives together with the
    live file**, not the live file alone.

    Args:
        path: Ledger to rotate; defaults to ``CANDIDATE_LEDGER_PATH``.
        max_bytes: Size threshold above which the file is rolled.

    Returns:
        The archive path when a rotation happened, else ``None``.
    """
    p = path or CANDIDATE_LEDGER_PATH
    try:
        if not p.is_file() or p.stat().st_size <= max_bytes:
            return None
    except OSError:
        return None

    archive_dir = p.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = archive_dir / f"{p.stem}-{stamp}.jsonl.gz"

    # Rename first: the writer only ever appends, so moving the inode out of the
    # way is atomic on POSIX and a concurrent append cannot interleave with the
    # compression pass.
    staged = archive_dir / f"{p.stem}-{stamp}.jsonl.tmp"
    try:
        p.rename(staged)
    except OSError:
        LOG.warning("candidate_ledger_rotate_rename_failed | path=%s", p)
        return None
    try:
        with staged.open("rb") as src, gzip.open(archive, "wb") as dst:
            shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
        staged.unlink()
    except OSError:
        # Compression failed — keep the raw file rather than lose observations.
        LOG.exception("candidate_ledger_rotate_gzip_failed | staged=%s", staged)
        return None
    LOG.info("candidate_ledger_rotated | archive=%s", archive.name)
    return archive


def load_pending_backfill(
    *,
    now_ms: int,
    path: Path | None = None,
    max_rows: int = 500,
) -> list[dict[str, Any]]:
    """Decision rows whose H_max window has elapsed and have no path row yet.

    Reads the whole ledger (JSONL, append-only) — fine at current volume; if
    this becomes a bottleneck, move to an index file keyed by candidate_id.
    """
    p = path or CANDIDATE_LEDGER_PATH
    if not p.is_file():
        return []
    decisions: dict[str, dict[str, Any]] = {}
    has_path: set[str] = set()
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = serde.loads(line)
            except serde.JSONDecodeError:
                continue
            cid = row.get("candidate_id")
            if not cid:
                continue
            if row.get("record_kind") == "path":
                has_path.add(cid)
            elif row.get("record_kind") == "decision":
                decisions[cid] = row
    pending: list[dict[str, Any]] = []
    for cid, row in decisions.items():
        if cid in has_path:
            continue
        h_max_ms = int(row.get("h_max_hours") or DEFAULT_H_MAX_HOURS) * 3600 * 1000
        if now_ms - int(row["decision_ts"]) < h_max_ms:
            continue  # window not elapsed yet
        pending.append(row)
        if len(pending) >= max_rows:
            break
    return pending


__all__ = [
    "CANDIDATE_LEDGER_PATH",
    "rotate_ledger_if_large",
    "DEFAULT_H_MAX_HOURS",
    "new_candidate_id",
    "record_candidate_decision",
    "record_candidate_forward_path",
    "load_pending_backfill",
]
