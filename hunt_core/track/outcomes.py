"""Shared tracker close_reason classification for stats and gates."""
from __future__ import annotations



from typing import Any

from hunt_core import serde

WIN_REASONS = frozenset({"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2", "trailing_stop_profit"})
LOSS_REASONS = frozenset(
    {
        "stop_hit",
        "bounce_invalidate",
        "trend_exhaustion",
        "reclaim_invalidation",
        "support_lost",
        "bias_flip",
        "lifecycle_stale",
        "opposite_signal",
    }
)
LEGACY_UNKNOWN = "legacy_unknown"
# Noise floor: |pnl_pct| at or below this is too small to call win/loss on PnL
# alone, so classification falls back to the reason label.
_PROFIT_STRUCTURAL_EXIT_MIN_PCT = 0.15


def is_polluted(row: dict[str, Any]) -> bool:
    """Canonical 'not a genuine live signal' test, shared by every reporter.

    A row is polluted (excluded from live win-rate) when it lacks the fields a
    real tracker open always records: an open timestamp, a detector score, and a
    fuel reading. Legacy/partial archive rows miss these and must never inflate
    or deflate live WR. Keep this the single definition — tracker and
    stats_report both import it so their n/WR reconcile.
    """
    return (
        not row.get("opened_at")
        or row.get("score") is None
        or row.get("fuel") is None
    )


def genuine_closed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed rows that are both genuine (not polluted) and carry a close_reason."""
    return [r for r in rows if not is_polluted(r) and r.get("close_reason")]


def entry_lifecycle_phase(sig: dict[str, Any]) -> str:
    """Immutable entry phase; fall back to lifecycle_phase for legacy rows."""
    return str(
        sig.get("entry_lifecycle_phase")
        or sig.get("lifecycle_phase")
        or sig.get("phase")
        or "?"
    )


def outcome_kind(reason: str, *, pnl_pct: float | None = None) -> str:
    """Classify a closed trade as win/loss/flat/unknown.

    Real PnL is authoritative whenever it clears the noise floor
    (``_PROFIT_STRUCTURAL_EXIT_MIN_PCT``), regardless of ``reason`` — the label
    only decides when PnL is unavailable. This used to special-case a hand-picked
    subset of loss reasons (``_STRUCTURAL_EXIT_REASONS``) as "can actually be a
    win if PnL says so", while every OTHER loss reason — including "stop_hit",
    the single most common close reason in the tracker — was hardcoded as a
    loss no matter what the real PnL showed. Confirmed against live tracker
    data: 16 of 41 closed trades were labeled "loss" despite positive PnL, all
    "stop_hit" closes where the stop had been trailed to breakeven-plus first
    (``_maybe_move_stop_to_breakeven`` in tracker.py moves ``stop_loss`` into
    profit territory on sufficient MFE, but the close-reason generator still
    just says generic "stop_hit" whether that stop is the original protective
    level or an already-profitable trailed one). The reported win rate was
    understating real performance by roughly half. A stop-loss's entire purpose
    is capping downside — if it closed in genuine profit, that is a win by any
    honest accounting, not a special case for a curated reason list.
    """
    if pnl_pct is not None:
        p = float(pnl_pct)
        if p > _PROFIT_STRUCTURAL_EXIT_MIN_PCT:
            return "win"
        if p < -_PROFIT_STRUCTURAL_EXIT_MIN_PCT:
            return "loss"
        # Inside the noise band (|pnl| <= floor) — fall through to the reason
        # label, since a near-zero PnL doesn't clearly say win or loss on its own.
    if reason in WIN_REASONS:
        return "win"
    if reason in LOSS_REASONS:
        return "loss"
    if reason == LEGACY_UNKNOWN and pnl_pct is not None:
        return "win" if float(pnl_pct) > 0 else "loss" if float(pnl_pct) < 0 else "flat"
    return "unknown"


def outcome_archive_key(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """Stable id for one tracker open → close leg (dedupe concurrent watch writers)."""
    opened = record.get("opened_at")
    if not opened:
        return None
    return (
        str(record.get("symbol") or "").upper(),
        str(record.get("direction") or "").lower(),
        str(opened),
    )


def _outcome_already_archived(path: Any, key: tuple[str, str, str]) -> bool:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-800:]):
        if not line.strip():
            continue
        try:
            rec = serde.loads(line)
        except serde.JSONDecodeError:
            continue
        if outcome_archive_key(rec) == key:
            return True
    return False


def append_outcome_record(path: Any, record: dict[str, Any]) -> None:
    """Single-writer outcome log append (§8E / P10)."""
    from pathlib import Path

    key = outcome_archive_key(record)
    if key is not None and _outcome_already_archived(path, key):
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(serde.dumps_str(record) + "\n")


def kpi_bucket(record: dict[str, Any]) -> str:
    """direction×phase key for stats rollup."""
    direction = str(record.get("direction") or "?")
    phase = entry_lifecycle_phase(record)
    return f"{direction}:{phase}"


__all__ = [
    "LOSS_REASONS",
    "WIN_REASONS",
    "append_outcome_record",
    "entry_lifecycle_phase",
    "genuine_closed",
    "is_polluted",
    "kpi_bucket",
    "outcome_archive_key",
    "outcome_kind",
]
