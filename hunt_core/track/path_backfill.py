"""Backfill the D (forward-path) half of candidate_ledger entries.

Fetches raw 1m OHLCV for [decision_ts, decision_ts+H_max] via the project's
CCXT client and derives MFE/MAE/ret_at_offsets/first_passage_grid from it. The
derived block is a denormalized cache, not a primitive — any TP/SL/H scheme
(including future path-dependent ones like trailing stops) is scored offline
against ``forward_ohlcv``, never against the cache alone.

Known bias (documented, not hidden): when a single 1m bar's range crosses both
a favorable and adverse threshold, true intra-bar order is unrecoverable from
OHLC alone. This module resolves ties conservatively as loss-first (adverse
before favorable) — first_passage_grid/mfe/mae inherit this bias.
"""
from __future__ import annotations

import logging
from typing import Any

from hunt_core.track.candidate_ledger import (
    load_pending_backfill,
    record_candidate_forward_path,
)

_LOG = logging.getLogger(__name__)

_RET_OFFSETS_MIN = (15, 60, 240, 480, 1440)  # 15m/1h/4h/8h/24h
_FIRST_PASSAGE_ATR_GRID = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)


def _atr_move(direction: str, entry: float, price: float, atr: float) -> float:
    """Signed move in ATR units, positive = favorable to `direction`."""
    if atr <= 0 or entry <= 0:
        return 0.0
    raw = (price - entry) / atr
    return raw if direction == "long" else -raw


def compute_derived_from_path(
    *,
    direction: str,
    entry: float,
    atr: float,
    decision_ts_ms: int,
    forward_ohlcv: list[list[float]],
) -> dict[str, Any]:
    """Reconstruct MFE/MAE/ret_at_offsets/first_passage_grid from a raw path.

    ``forward_ohlcv`` rows are ``[ts_ms, open, high, low, close, volume]``,
    finer than entry_tf (1m), sorted ascending, covering [t0, t0+H_max].
    """
    if entry <= 0 or atr <= 0 or not forward_ohlcv:
        return {
            "mfe_atr": None, "time_to_mfe_min": None,
            "mae_atr": None, "time_to_mae_min": None,
            "ret_at_offsets": {}, "first_passage_grid": {},
        }

    best_favorable = 0.0
    best_favorable_ts: int | None = None
    worst_adverse = 0.0
    worst_adverse_ts: int | None = None
    # First-touch tracking: for each ATR threshold, the first bar whose
    # high/low crosses it (adverse resolved first within a bar — loss-first
    # tie-break, see module docstring).
    first_touch: dict[float, int | None] = {g: None for g in _FIRST_PASSAGE_ATR_GRID}
    ret_at_offsets: dict[str, float] = {}
    offsets_remaining = {m: (m, f"+{m}m" if m < 60 else f"+{m // 60}h") for m in _RET_OFFSETS_MIN}

    for bar in forward_ohlcv:
        ts_ms, _o, h, l, c, _v = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
        elapsed_min = (ts_ms - decision_ts_ms) / 60_000.0

        fav_extreme = h if direction == "long" else l
        adv_extreme = l if direction == "long" else h
        fav_move = _atr_move(direction, entry, fav_extreme, atr)
        adv_move = _atr_move(direction, entry, adv_extreme, atr)

        # Adverse first within the bar (conservative tie-break).
        if adv_move < worst_adverse:
            worst_adverse = adv_move
            worst_adverse_ts = ts_ms
        for g in _FIRST_PASSAGE_ATR_GRID:
            if first_touch[g] is None and adv_move <= -g:
                first_touch[g] = ts_ms  # touched the adverse side of this |g| first

        if fav_move > best_favorable:
            best_favorable = fav_move
            best_favorable_ts = ts_ms
        for g in _FIRST_PASSAGE_ATR_GRID:
            if first_touch[g] is None and fav_move >= g:
                first_touch[g] = ts_ms

        for m, (m_val, label) in list(offsets_remaining.items()):
            if elapsed_min >= m_val:
                close_move = _atr_move(direction, entry, c, atr)
                ret_at_offsets[label] = round(close_move, 4)
                ret_at_offsets[f"{label}_pct"] = round(
                    (c - entry) / entry * 100.0 * (1 if direction == "long" else -1), 4
                )
                del offsets_remaining[m]

    time_to_mfe = (
        round((best_favorable_ts - decision_ts_ms) / 60_000.0, 1)
        if best_favorable_ts is not None else None
    )
    time_to_mae = (
        round((worst_adverse_ts - decision_ts_ms) / 60_000.0, 1)
        if worst_adverse_ts is not None else None
    )
    grid_out = {
        f"{g}atr": (
            round((ts - decision_ts_ms) / 60_000.0, 1) if ts is not None else None
        )
        for g, ts in first_touch.items()
    }
    return {
        "mfe_atr": round(best_favorable, 4),
        "time_to_mfe_min": time_to_mfe,
        "mae_atr": round(worst_adverse, 4),
        "time_to_mae_min": time_to_mae,
        "ret_at_offsets": ret_at_offsets,
        "first_passage_grid": grid_out,
    }


async def run_backfill_pass(client: Any, *, now_ms: int, max_rows: int = 200) -> int:
    """Fetch forward paths for elapsed candidates and write D records.

    Returns the number of candidates backfilled. ``client`` is a
    ``HuntCcxtClient`` (or anything exposing the same ``fetch_ohlcv_list``).
    """
    pending = load_pending_backfill(now_ms=now_ms, max_rows=max_rows)
    if not pending:
        return 0
    done = 0
    for row in pending:
        symbol = row["symbol"]
        decision_ts = int(row["decision_ts"])
        h_max_ms = int(row.get("h_max_hours") or 24) * 3600 * 1000
        try:
            ohlcv = await client.fetch_ohlcv_list(
                symbol, "1m", since=decision_ts, limit=1500,
            )
        except Exception:
            _LOG.exception("path_backfill_fetch_failed sym=%s candidate=%s", symbol, row.get("candidate_id"))
            continue
        # fetch_ohlcv_list caps at 1500 bars/call; 24h@1m=1440 fits in one call.
        window = [b for b in ohlcv if decision_ts <= b[0] <= decision_ts + h_max_ms]
        forward_dq: dict[str, Any] = {}
        if not window:
            forward_dq["gaps"] = True
        else:
            # Detect gaps: expected ~1 bar/min.
            expected_bars = h_max_ms // 60_000
            if len(window) < expected_bars * 0.9:
                forward_dq["gaps"] = True
                forward_dq["bars_expected"] = expected_bars
                forward_dq["bars_received"] = len(window)
        atr = float(row.get("atr_entry_tf") or 0.0)
        entry = float(row.get("signal_price") or 0.0)
        derived = compute_derived_from_path(
            direction=row["direction"], entry=entry, atr=atr,
            decision_ts_ms=decision_ts, forward_ohlcv=window,
        )
        record_candidate_forward_path(
            candidate_id=row["candidate_id"], symbol=symbol, decision_ts_ms=decision_ts,
            forward_ohlcv=window, forward_dq=forward_dq, derived=derived,
        )
        done += 1
    if done:
        _LOG.info("path_backfill_pass done=%s pending_seen=%s", done, len(pending))
    return done


async def path_backfill_loop(client: Any, *, interval_s: float = 900.0) -> None:
    """Background task: periodically backfill elapsed candidates' forward paths.

    Runs every ``interval_s`` (default 15 min) — frequent enough that
    intra-day candidates (H_max well under 24h for most gate reasons) get
    backfilled promptly, cheap enough to not compete with the hot path for
    rate-limit budget (only elapsed-window candidates are fetched).
    """
    import asyncio

    from hunt_core.runtime.state import should_stop

    _LOG.info("path_backfill_loop_start interval_s=%s", interval_s)
    while not should_stop():
        try:
            import time as _time

            n = await run_backfill_pass(client, now_ms=int(_time.time() * 1000))
            if n:
                _LOG.info("path_backfill_loop_tick backfilled=%s", n)
        except Exception:
            _LOG.exception("path_backfill_loop_tick_failed")
        for _ in range(int(interval_s)):
            if should_stop():
                break
            await asyncio.sleep(1)


__all__ = ["compute_derived_from_path", "run_backfill_pass", "path_backfill_loop"]
