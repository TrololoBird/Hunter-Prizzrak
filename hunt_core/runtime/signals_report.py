"""Telegram /signals — watchlist snapshot + tracker status.

REST snapshot of watched symbols: lifecycle, regime, candidates.
Point query: ``/signal SYM``. Live pre_*: watch loop.
"""
from __future__ import annotations



import asyncio
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hunt_core.deliver.telegram import TelegramBroadcaster



from hunt_core.data.universe import load_watchlist_symbols
from hunt_core.scanner.detect.delivery_support import (
    collect_report_blockers,
    evaluate_alert_gate,
    evaluate_stale_advice,
    primary_block_for_report,
)
from hunt_core.paths import WATCHLIST
from hunt_core.runtime.symbol_probe import normalize_symbol, probe_symbol_signal
from hunt_core.track.tracker import load_tracker_state

_PROBE_RETRIES = 3
_PROBE_RETRY_DELAY_S = 1.5
_MAX_SYMBOLS = 18

_STRONG_PHASES = frozenset(
    {"dump_active", "dump_confirmed", "distribution", "exhaustion_at_high"}
)
_LONG_STRONG_PHASES = frozenset(
    {"recovery", "impulse_active", "impulse_initiating", "breakout_arming"}
)


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.6f}"


def _human_probe_error(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).lower()
    if "incompleteread" in name.lower() or "timeout" in name.lower():
        return "Сбой сети Binance — повтори /signals через 1–2 мин"
    if "connection" in text or "proxy" in text:
        return "Сеть недоступна — повтори позже"
    return f"Probe failed: {name}"


async def _probe_with_retry(symbol: str) -> dict[str, Any]:
    last_exc: BaseException | None = None
    for attempt in range(_PROBE_RETRIES):
        try:
            row = await probe_symbol_signal(symbol, auto_watchlist=False, stagger_ms=120)
            if row.get("error"):
                return row
            return row
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < _PROBE_RETRIES:
                await asyncio.sleep(_PROBE_RETRY_DELAY_S * (attempt + 1))
    return {
        "symbol": symbol,
        "error": _human_probe_error(last_exc or RuntimeError("probe_failed")),
    }


def resolve_signals_universe(explicit: list[str] | None = None) -> list[str]:
    """Symbols: args → watchlist → tracker active."""
    if explicit:
        out: list[str] = []
        for raw in explicit:
            sym = normalize_symbol(raw)
            if sym and sym not in out:
                out.append(sym)
        return out[:_MAX_SYMBOLS]

    symbols: list[str] = []
    for sym in load_watchlist_symbols(WATCHLIST):
        if sym not in symbols:
            symbols.append(sym)

    tracker = load_tracker_state()
    for key, sig in (tracker.get("signals") or {}).items():
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        sym = str(key).split(":", 1)[0].upper()
        if sym and sym not in symbols:
            symbols.append(sym)

    return symbols[:_MAX_SYMBOLS]




def _format_symbol_snapshot(
    sym: str,
    row: dict[str, Any],
) -> str:
    sym_label = html.escape(sym.replace("USDT", "-USDT"))
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    lc_phase = html.escape(str(lc.get("phase") or "—"))
    bias = lc.get("recommended_bias")
    emoji = "🔴" if bias == "short" else "🟢" if bias == "long" else "⚖️"

    lines = [
        f"{emoji} <b>{sym_label}</b> · <code>{_fmt_price(price)}</code> · "
        f"lc <code>{lc_phase}</code>",
    ]

    return "\n".join(lines)


def _signal_rating(fuel: float, phase: str, direction: str) -> tuple[str, str]:
    """Returns (emoji, label) based on fuel + lifecycle phase."""
    strong = _STRONG_PHASES if direction == "short" else _LONG_STRONG_PHASES
    if fuel >= 80 and phase in strong:
        return "🔥", "СИЛЬНЫЙ"
    if fuel >= 65 and phase in strong:
        return "✅", "УВЕРЕННЫЙ"
    if fuel >= 50:
        return "⚠️", "СРЕДНИЙ"
    return "📊", "СЛАБЫЙ"


def _tp_progress(entry_lo: float, entry_hi: float, tp1: float, ext_extreme: float, direction: str) -> str:
    mid = (entry_lo + entry_hi) / 2.0 if entry_lo and entry_hi else (entry_lo or entry_hi)
    if mid <= 0 or tp1 <= 0:
        return ""
    if direction == "short":
        total = (mid - tp1) / mid * 100.0
        traveled = (mid - ext_extreme) / mid * 100.0 if ext_extreme > 0 else 0.0
        remaining = (ext_extreme - tp1) / ext_extreme * 100.0 if ext_extreme > 0 else total
    else:
        total = (tp1 - mid) / mid * 100.0
        traveled = (ext_extreme - mid) / mid * 100.0 if ext_extreme > 0 else 0.0
        remaining = (tp1 - ext_extreme) / ext_extreme * 100.0 if ext_extreme > 0 else total
    if total <= 0:
        return ""
    pct_done = max(0.0, min(1.0, traveled / total))
    filled = int(pct_done * 8)
    bar = "█" * filled + "░" * (8 - filled)
    rem_s = f"{remaining:.1f}%" if remaining > 0 else "TP1!"
    return f"[{bar}] осталось {rem_s}"


def _duration_human(opened_at: str | None) -> str:
    if not opened_at:
        return "—"
    try:
        opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        delta = datetime.now(UTC) - opened
        total_min = int(delta.total_seconds() / 60)
        if total_min < 60:
            return f"{total_min}м"
        h, m = divmod(total_min, 60)
        return f"{h}ч {m}м" if m else f"{h}ч"
    except (TypeError, ValueError):
        return "—"


@dataclass(slots=True)
class _ReportRollup:
    n_plus: int = 0
    n_tp1: int = 0
    n_realert: int = 0
    n_stale: int = 0
    n_bias_conflict: int = 0
    n_probe_fail: int = 0


def _pnl_pct(sig: dict[str, Any], direction: str, price: float) -> float | None:
    lo = float(sig.get("entry_lo") or 0)
    hi = float(sig.get("entry_hi") or 0)
    mid = (lo + hi) / 2.0 if lo > 0 and hi > 0 else (lo or hi)
    if mid <= 0 or price <= 0:
        return None
    raw = (price - mid) / mid * 100.0
    return round(-raw if direction == "short" else raw, 2)


def _rollup_touch(
    rollup: _ReportRollup,
    *,
    key: str,
    sig: dict[str, Any],
    direction: str,
    row: dict[str, Any],
) -> None:
    sym = key.partition(":")[0]
    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    lc = row.get("lifecycle") or {}
    price = float(row.get("price") or 0)
    pnl = _pnl_pct(sig, direction, price)
    if pnl is not None and pnl > 0:
        rollup.n_plus += 1
    if sig.get("tp1_hit"):
        rollup.n_tp1 += 1
    if evaluate_alert_gate(setup, direction=direction, symbol=sym, lifecycle=lc, row=row).ok:
        rollup.n_realert += 1
    phase = str(lc.get("phase") or "")
    if phase == "no_setup":
        rollup.n_stale += 1
    bias = str(lc.get("recommended_bias") or "")
    if (direction == "short" and bias == "long") or (direction == "long" and bias == "short"):
        rollup.n_bias_conflict += 1


def _format_summary(
    rollup: _ReportRollup,
    *,
    n_active: int,
) -> str:
    return (
        f"<b>Сводка:</b> "
        f"tracker active <code>{n_active}</code> · {rollup.n_plus} в плюсе · "
        f"{rollup.n_tp1} TP1 · {rollup.n_realert} re-alert · "
        f"{rollup.n_stale} stale · {rollup.n_bias_conflict} bias-конфликт"
        + (f" · {rollup.n_probe_fail} probe fail" if rollup.n_probe_fail else "")
    )


def _format_active_block(
    *,
    key: str,
    sig: dict[str, Any],
    row: dict[str, Any],
) -> str:
    sym, _, direction = key.partition(":")
    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    lc = row.get("lifecycle") or {}
    price = float(row.get("price") or 0)
    pnl = _pnl_pct(sig, direction, price)

    primary = primary_block_for_report(
        setup, direction=direction, symbol=sym, lifecycle=lc, row=row
    )
    extra = collect_report_blockers(
        setup, direction=direction, symbol=sym, lifecycle=lc, row=row
    )
    secondary = [b for b in extra if b.code != primary.code][:2] if primary else extra[:2]
    advice = evaluate_stale_advice(
        symbol=sym, direction=direction, lifecycle=lc, setup=setup, sig=sig
    )

    sym_label = html.escape(sym.replace("USDT", "-USDT"))
    dir_u = direction.upper()
    dir_emoji = "🔴" if direction == "short" else "🟢"
    fuel = float(sig.get("fuel") or 0)
    lc_phase = str(lc.get("phase") or sig.get("entry_lifecycle_phase") or "")
    rating_emoji, rating_label = _signal_rating(fuel, lc_phase, direction)
    pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"
    duration = _duration_human(sig.get("opened_at"))

    sl_label = _fmt_price(sig.get("stop_loss"))
    if sig.get("sl_at_breakeven"):
        sl_label = f"{sl_label} 🔒BE"

    tp1 = float(sig.get("tp1") or 0)
    tp2 = float(sig.get("tp2") or 0)
    entry_lo = float(sig.get("entry_lo") or 0)
    entry_hi = float(sig.get("entry_hi") or 0)
    ext_extreme = float(sig.get("extreme_lo" if direction == "short" else "extreme_hi") or 0)

    lines = [
        f"{dir_emoji} <b>ВХОД ВЗЯТ · {sym_label} {dir_u}</b>  {rating_emoji} <b>{rating_label}</b>",
        (
            f"📍 Вход: <code>{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}</code>  "
            f"Стоп: <code>{sl_label}</code>"
        ),
        (
            f"🎯 TP1: <code>{_fmt_price(tp1)}</code>"
            + ("  ✅" if sig.get("tp1_hit") else "")
            + f"  TP2: <code>{_fmt_price(tp2)}</code>"
            + ("  ✅" if sig.get("tp2_hit") else "")
        ),
    ]
    progress = _tp_progress(entry_lo, entry_hi, tp1, ext_extreme, direction)
    if progress:
        lines.append(f"📊 {progress}")
    if sig.get("tp1_hit"):
        pct = sig.get("partial_fixed_pct") or 80
        lines.append(
            f"✅ <b>TP1 достигнут</b> — зафиксируй <code>{pct}%</code>"
            + (" · стоп на безубыток" if sig.get("sl_at_breakeven") else "")
        )
    lines.append(
        f"💰 PnL: <code>{pnl_s}</code>  ⏱ {duration}  "
        f"Score: <code>{sig.get('score') or '—'}</code>  Fuel: <code>{int(fuel) if fuel else '—'}</code>"
    )
    lines.append(
        f"Сейчас <code>{_fmt_price(price)}</code> · "
        f"фаза <code>{html.escape(lc_phase or '—')}</code>"
    )
    realert_blockers = [
        b for b in extra if b.code not in {"not_confirmed"} or not sig.get("tp1_hit")
    ]
    if primary is not None and primary.ok:
        lines.append("✅ <b>Re-alert</b> сейчас прошёл бы")
    elif realert_blockers:
        lines.append(
            f"<i>Новый вход (re-alert): {html.escape(realert_blockers[0].message)}</i>"
        )
    if advice:
        lines.append(html.escape(advice))
    if secondary:
        more = "; ".join(b.message for b in secondary)
        lines.append(f"<i>Ещё: {html.escape(more)}</i>")
    return "\n".join(lines)


async def build_signals_report_text(symbols: list[str] | None = None) -> str:
    universe = resolve_signals_universe(symbols)

    state = load_tracker_state()
    tracker_signals = state.get("signals") or {}
    active_tracker = [
        (k, v)
        for k, v in tracker_signals.items()
        if isinstance(v, dict) and v.get("status") == "active"
    ]

    blocks: list[str] = [
        f"📋 <b>/signals</b> · {datetime.now(UTC).strftime('%H:%M')} UTC",
        "<i>Снимок watchlist + tracker. Точка: <code>/signal SYM</code>.</i>",
    ]

    if not universe:
        blocks.append(
            "<b>Нет монет</b> — добавь в watchlist или <code>/signals BTC ETH</code>"
        )
        if active_tracker:
            blocks.append(f"<b>Tracker active</b> · <code>{len(active_tracker)}</code> поз.")
        return "\n\n".join(blocks)

    blocks.append(f"<b>Монеты</b> · <code>{len(universe)}</code>")

    n_fail = 0
    row_cache: dict[str, dict[str, Any]] = {}
    rollup = _ReportRollup()

    for sym in universe:
        row = await _probe_with_retry(sym)
        if row.get("error"):
            n_fail += 1
            rollup.n_probe_fail += 1
            blocks.append(
                f"⚠️ <b>{html.escape(sym.replace('USDT', '-USDT'))}</b>\n"
                f"<i>{html.escape(str(row['error']))}</i>"
            )
            continue
        row_cache[sym] = row
        blocks.append(
            _format_symbol_snapshot(sym, row)
        )

    if active_tracker:
        blocks.append(f"<b>Active tracker</b> · <code>{len(active_tracker)}</code> поз.")
        for key, sig in sorted(active_tracker, key=lambda x: x[0]):
            sym = key.partition(":")[0]
            if sym not in row_cache:
                row_cache[sym] = await _probe_with_retry(sym)
            row = row_cache[sym]
            if row.get("error"):
                rollup.n_probe_fail += 1
                sym_label = html.escape(sym.replace("USDT", "-USDT"))
                blocks.append(
                    f"⚠️ <b>{sym_label}</b> (tracker)\n"
                    f"<i>{html.escape(str(row['error']))}</i>"
                )
                continue
            direction = key.partition(":")[2] or "short"
            _rollup_touch(rollup, key=key, sig=sig, direction=direction, row=row)
            blocks.append(_format_active_block(key=key, sig=sig, row=row))

    blocks.append(
        _format_summary(
            rollup,
            n_active=len(active_tracker),
        )
    )
    blocks.append("<i>REST snapshot · pre_* в watch отдельно от /signal</i>")
    blocks.append("<i>Hunt tracker · не auto-trade</i>")
    return "\n\n".join(blocks)


async def deliver_signals_report(
    broadcaster: TelegramBroadcaster,
    *,
    symbols: list[str] | None = None,
) -> None:
    from hunt_core.runtime.cycle._cycle_reconcile import _split_telegram

    text = await build_signals_report_text(symbols)
    for part in _split_telegram(text):
        await broadcaster.send_html(part, no_split=True)
