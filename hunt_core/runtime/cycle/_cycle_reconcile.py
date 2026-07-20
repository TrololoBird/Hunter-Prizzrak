"""Orphan/in-watch signal reconcile + follow-up TG delivery."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from hunt_core.data.lake import buffer_tracker_state
from hunt_core.engine import rest
from hunt_core.runtime.state import LOG
from hunt_core.track.events import append_signal_event
from hunt_core.track.pump_history import record_signal_outcome
from hunt_core.track.tracker import (
    mark_close_notified,
    mark_followups_sent,
    reconcile_signal,
)

# Orphan signals (symbol no longer in watchlist) are re-checked via REST klines.
ORPHAN_RECONCILE_MINUTES = 2
INWATCH_KLINE_RECONCILE_SECONDS = 45


def _unified(compact: str) -> str:
    """Compact tracker key ``BTCUSDT`` → ccxt-unified ``BTC/USDT:USDT`` for the engine exchange."""
    base = compact.upper().replace("/", "").replace(":USDT", "")
    if base.endswith("USDT"):
        base = base[:-4]
    return f"{base}/USDT:USDT"


async def _kline_extremes_between(
    exchange: Any, compact_symbol: str, *, anchor: datetime, now: datetime
) -> tuple[float, float, float] | None:
    """Engine-REST 5m ``(hi, lo, last_close)`` for the closed window, or ``None`` fail-loud.

    ADR-0004: the reconcile pollers fetch their own detection frames off the ENGINE REST tail
    (``fetch_ohlcv_between`` over ``rt.multi.primary.exchange``), not the deleted legacy client.
    """
    try:
        bars = await rest.fetch_ohlcv_between(
            exchange,
            _unified(compact_symbol),
            "5m",
            start_ms=int(anchor.timestamp() * 1000),
            end_ms=int(now.timestamp() * 1000),
        )
    except Exception as exc:  # noqa: BLE001 — REST tail is best-effort; a stale anchor just retries
        LOG.warning("reconcile_klines_failed", symbol=compact_symbol, error=repr(exc))
        return None
    if not bars:
        return None
    hi = max(float(b[2]) for b in bars)
    lo = min(float(b[3]) for b in bars)
    last_price = float(bars[-1][4])
    return hi, lo, last_price


async def _reconcile_inwatch_active(
    exchange: Any,
    tracker_state: dict[str, Any],
    *,
    symbol: str,
    now: datetime,
) -> list[Any]:
    """5m kline hi/lo since last_checked_at for active signals still in the watchlist."""
    events: list[Any] = []
    signals = tracker_state.get("signals") or {}
    sym_u = symbol.upper()
    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        o_sym, _, o_dir = key.partition(":")
        if o_sym != sym_u:
            continue
        anchor_raw = sig.get("last_checked_at") or sig.get("opened_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
        except (TypeError, ValueError):
            anchor = now
        if (now - anchor).total_seconds() < INWATCH_KLINE_RECONCILE_SECONDS:
            continue
        extremes = await _kline_extremes_between(exchange, o_sym, anchor=anchor, now=now)
        if extremes is None:
            sig["last_checked_at"] = now.isoformat()
            continue
        hi, lo, last_price = extremes
        events.extend(
            reconcile_signal(
                tracker_state,
                symbol=o_sym,
                direction=o_dir,
                hi=hi,
                lo=lo,
                last_price=last_price,
                ts=now,
            )
        )
    return events


async def _reconcile_orphan_signals(
    exchange: Any,
    tracker_state: dict[str, Any],
    *,
    seen_symbols: set[str],
    now: datetime,
) -> list[Any]:
    events: list[Any] = []
    signals = tracker_state.get("signals") or {}
    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        o_sym, _, o_dir = key.partition(":")
        if not o_sym or not o_dir or o_sym in seen_symbols:
            continue
        anchor_raw = sig.get("last_checked_at") or sig.get("opened_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
        except (TypeError, ValueError):
            anchor = now
        if (now - anchor).total_seconds() < ORPHAN_RECONCILE_MINUTES * 60:
            continue
        extremes = await _kline_extremes_between(exchange, o_sym, anchor=anchor, now=now)
        if extremes is None:
            sig["last_checked_at"] = now.isoformat()
            continue
        hi, lo, last_price = extremes
        events.extend(
            reconcile_signal(
                tracker_state,
                symbol=o_sym,
                direction=o_dir,
                hi=hi,
                lo=lo,
                last_price=last_price,
                ts=now,
            )
        )
    return events


async def _deliver_followup(
    broadcaster: Any,
    fu: Any,
    row: dict[str, Any],
    tracker_state: dict[str, Any],
    *,
    now: datetime,
    send_telegram: bool,
) -> bool:
    """Send one follow-up; mark + persist immediately on success."""
    announced = bool((fu.payload or {}).get("announced", True))
    if not send_telegram or broadcaster is None or not announced:
        return False
    from hunt_core.deliver.templates import format_followup_telegram_message

    msg = format_followup_telegram_message(fu, row)
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "watch_followup_send_failed",
            symbol=fu.symbol,
            followup_event=fu.event,
            status=result.status,
            reason=result.reason,
        )
        return False
    mark_followups_sent(tracker_state, [fu], now=now)
    if fu.event == "invalidate":
        mark_close_notified(
            tracker_state,
            symbol=fu.symbol,
            direction=fu.direction,
            message_key=fu.message_key,
            now=now,
        )
    buffer_tracker_state(tracker_state)
    LOG.info(
        "watch_followup_sent",
        symbol=fu.symbol,
        followup_event=fu.event,
        message_id=result.message_id,
    )
    return True


def _record_followup_side_effects(
    followups: list[Any],
    *,
    sent_keys: set[str],
    now: datetime,
    pump_store: Any | None,
) -> None:
    """Append signal_events / pump_history only for follow-ups that shipped."""
    for fu in followups:
        if fu.message_key not in sent_keys:
            continue
        if fu.event == "invalidate":
            append_signal_event(
                "invalidate",
                symbol=fu.symbol,
                direction=str(fu.direction or (fu.payload or {}).get("direction") or ""),
                detail=str(fu.detail or ""),
                payload=fu.payload or {},
            )
        if fu.event == "early_breakeven":
            append_signal_event(
                "followup",
                symbol=fu.symbol,
                direction=str(fu.direction or ""),
                detail=f"early_breakeven:{fu.detail or ''}",
                payload=fu.payload or {},
            )
        if pump_store is None:
            continue
        if fu.event == "fix_profit_tp1":
            record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp1", now=now)
        elif fu.event == "fix_profit_tp2":
            record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp2", now=now)
        elif fu.event == "invalidate":
            record_signal_outcome(
                pump_store, symbol=fu.symbol, outcome="invalidate", now=now
            )


def _split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    from hunt_core.deliver.telegram import _split_telegram_text

    return _split_telegram_text(text, limit=limit)


__all__ = [
    "INWATCH_KLINE_RECONCILE_SECONDS",
    "ORPHAN_RECONCILE_MINUTES",
    "_deliver_followup",
    "_reconcile_inwatch_active",
    "_reconcile_orphan_signals",
    "_record_followup_side_effects",
    "_split_telegram",
]
