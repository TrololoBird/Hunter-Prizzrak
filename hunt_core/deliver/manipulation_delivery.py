"""Deliver manipulation reversal setups (scanner/detect/patterns.py).

Scanner's sole signal path — two patterns:
- Pattern A (long): impulse→absorption→bokovik→sweep→break
- Pattern B (short): HTF trend→sweep→fade→LTF_confirm

Non-pinned universe only. Pinned symbols are Prizrak's Deep module exclusively.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Any

from hunt_core.deliver._labels import fmt_price
from hunt_core.deliver.lab import send_lane_html
from hunt_core.paths import SCANNER_STATE
from hunt_core.scanner.detect.patterns import ManipulationSetup, advance_manipulation_scales
from hunt_core.scanner.detect.state import load_scanner_state, save_scanner_state
from hunt_core.track._cooldowns import (
    global_confirm_burst_cap_reached,
    recent_stop_hit_cooldown,
    record_confirm_burst,
    symbol_daily_tg_cap_reached,
    symbol_loss_streak_cooldown,
    symbol_repeat_loser_blocked,
)
from hunt_core.track.tracker import has_active_signal, register_signal_open

_LOG = logging.getLogger(__name__)

_MIN_RR = 1.2
# We deliberately hunt VERY large manipulation moves (pump/dump of 40-80%+), so a
# 20% target cap rejected exactly the biggest, most valuable setups. 60% keeps out
# only physically absurd targets; RR (_MIN_RR) + the structural target still gate quality.
_MAX_TARGET_PCT = 60.0

_TIMEFRAMES = ("1d", "4h", "1h", "15m", "5m")
_LOOKBACK_BY_TF = {"1d": 220, "4h": 120, "1h": 120, "15m": 700, "5m": 1000}
_MAX_STALE_MS_BY_TF = {
    "1d": 86400_000 * 2,   # 2 days
    "4h": 14400_000 * 2,   # 8 hours
    "1h": 3600_000 * 2,    # 2 hours
    "15m": 900_000 * 2,    # 30 minutes
    "5m": 300_000 * 2,     # 10 minutes
}
# Bar duration per TF — used to drop the still-forming candle from the list path
# (fetch_ohlcv_list bypasses finalize_kline_frame's incomplete-tail drop, so ccxt's
# in-progress last kline would otherwise reach the detectors and repaint).
_INTERVAL_MS = {
    "1d": 86400_000,
    "4h": 14400_000,
    "1h": 3600_000,
    "15m": 900_000,
    "5m": 300_000,
}


# User's explicit correction: don't wait for the dump/pump to already be
# running (it "may pass in three minutes") — enter as the impulse starts
# EXHAUSTING, deliberately BEFORE full confirmation, which means price can
# still move a bit further against the position before genuinely turning.
# The stop already accounts for that (anchored beyond the full sweep extreme,
# not the entry) — this adds the other missing piece: an explicit averaging
# ("довор") level between entry and stop, and a market/limit order-type label
# matching Prizrak's own format, instead of a single bare "entry price".
_AVERAGING_FRACTION = 0.5  # how far from entry toward stop the averaging limit sits


def _stop_buffer(meso_bars: list[list[float]], *, pattern_a3: bool = False) -> float:
    """ATR-adaptive stop buffer: 0.3 × ATR%, min 3.0%, max 5%.

    For Pattern A3 (no sweep — stop anchored at entry level) the minimum is
    raised to 5.0% to compensate for the missing structural gap between
    entry and stop anchor (A3 sets sweep_extreme = lo, so the entire risk
    comes from this buffer alone — there is zero structural breathing room).
    """
    from hunt_core.scanner.detect.events import ohlcv_to_df, atr
    min_buf = 0.05 if pattern_a3 else 0.03
    try:
        df = ohlcv_to_df(meso_bars)
        atr_val = atr(df, 14)
    except Exception:
        atr_val = 0.0
    if atr_val <= 0:
        return min_buf
    last_close = float(df["close"][-1])
    if last_close <= 0:
        return min_buf
    atr_pct = atr_val / last_close
    return min(max(min_buf, atr_pct * 0.3), 0.05)


def _geometry(setup: ManipulationSetup, *, price: float, stop_buffer: float | None = None) -> dict[str, Any] | None:
    if setup.target is None:
        return None  # no real structural target — abstain, never fabricate one
    # Full structural ladder (course: пулы ликвидности снизу/сверху). R:R is measured
    # to the DEEPEST reachable pool within _MAX_TARGET_PCT — the «среднесрочная цель»
    # — while the nearer levels are shown as partial take-profits. A wide stop above
    # the manipulation extreme only pays off against the whole move, not the first
    # tiny bounce (that is why a single 30% retrace under-stated R and filtered the
    # real EVAA short).
    ladder = [t for t in (setup.target_ladder or ()) if t and t > 0]
    if setup.direction == "short":
        ladder = [t for t in ladder if t < price and abs(price - t) / price * 100.0 <= _MAX_TARGET_PCT]
    else:
        ladder = [t for t in ladder if t > price and abs(price - t) / price * 100.0 <= _MAX_TARGET_PCT]
    primary_target = (min(ladder) if setup.direction == "short" else max(ladder)) if ladder else setup.target
    target_dist_pct = abs(price - primary_target) / price * 100.0
    if target_dist_pct > _MAX_TARGET_PCT:
        return None  # цель нереалистично далеко — пропускаем
    # Стоп «за хая/лоу манипуляции С ЗАПАСОМ» (транскрипт). Бэктест на реальных барах
    # EVAA (памп 3.85 → дамп): фикс-2-3% от НАСТОЯЩЕГО экстремума манипуляции даёт
    # лучший R (1.3), а «соизмеримо с движением» (0.25×дистанция) переширяет стоп и
    # роняет R до 1.0. Значит запас — фикс _SL_BUFFER_PCT; ключевое — что
    # sweep_extreme это истинный экстремум манипуляции (памп-хай / лоу боковика),
    # НЕ локальный фитиль (иначе стоп внутри шума — баг ZRO).
    buf = 0.03 if stop_buffer is None else stop_buffer
    if setup.direction == "short":
        stop = setup.sweep_extreme * (1 + buf)
        risk = stop - price
        reward = price - primary_target
    else:
        stop = setup.sweep_extreme * (1 - buf)
        risk = price - stop
        reward = primary_target - price
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < _MIN_RR:
        return None
    averaging_price = price + (stop - price) * _AVERAGING_FRACTION
    return {
        "entry_lo": min(price, averaging_price),
        "entry_hi": max(price, averaging_price),
        "averaging_price": averaging_price,
        "stop": stop,
        "rr": rr,
        "primary_target": primary_target,
        "ladder": ladder,
    }


def _format_manipulation_signal(symbol: str, setup: ManipulationSetup, *, price: float, geo: dict[str, Any]) -> str:
    sym = html.escape(symbol.replace("USDT", "-USDT"))
    side_label = "SHORT" if setup.direction == "short" else "LONG"
    emoji = "🔴" if setup.direction == "short" else "🟢"
    pattern_label = f"Pattern {setup.pattern_type}"
    micro_line = ""
    if setup.micro_tf:
        tag = "подтверждён" if setup.micro_confirmed else "не найден"
        micro_line = f"Разворот на {setup.micro_tf}: <b>{tag}</b>\n"
    lines = [
        f"{emoji} <b>Манипуляция {pattern_label}</b> · <code>{sym}</code> · <b>{side_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Score: <b>{setup.score:.0%}</b> · Шаги: {setup.steps_covered}/{setup.total_steps}",
        f"Свип уровня {setup.macro_tf} <code>{fmt_price(setup.swept_level)}</code> → "
        f"экстремум <code>{fmt_price(setup.sweep_extreme)}</code> ({setup.meso_tf})",
        micro_line.rstrip("\n") if micro_line else "",
        f"📍 Вход (рыночный / лимит): <code>{fmt_price(geo['entry_lo'])} — {fmt_price(geo['entry_hi'])}</code>",
        f"➕ Довор (если пойдёт против ещё): <code>{fmt_price(geo['averaging_price'])}</code>",
        f"🛑 Стоп (за структуру): <code>{fmt_price(geo['stop'])}</code>",
        _tp_ladder_line(geo, setup),
        f"R:R ≈ <code>{geo['rr']:.2f}</code> <i>(до среднесрочной {fmt_price(geo.get('primary_target') or setup.target)})</i>",
        (f"⚡ Фандинг: {html.escape(setup.funding_note)}" if setup.funding_note else ""),
        f"<i>почему: {html.escape(', '.join(setup.evidence))}</i>",
        "<i>Тейки частями по лестнице, держим до цели/стопа — не микро-триггер</i>",
    ]
    return "\n".join(line for line in lines if line)


def _tp_ladder_line(geo: dict[str, Any], setup: ManipulationSetup) -> str:
    """Full structural take-profit ladder (course: пулы ликвидности, тейки частями)."""
    ladder = geo.get("ladder") or list(setup.target_ladder or ())
    ladder = [t for t in ladder if t and t > 0]
    if not ladder:
        return f"🎯 Цель (структурная зона): <code>{fmt_price(setup.target)}</code>"
    ladder = sorted(ladder, reverse=(setup.direction == "short"))
    tps = " · ".join(f"TP{i+1} <code>{fmt_price(t)}</code>" for i, t in enumerate(ladder[:6]))
    return f"🎯 Тейки (пулы ликвидности): {tps}"


_PARALLEL_SEMAPHORE = 10


async def _funding_ctx(client: Any, symbol: str) -> dict[str, float] | None:
    """Recent funding context for the manipulation-short conviction/timing signal.

    Returns {"rate": last 8h funding, "peak": max over ~3 days}. Elevated positive
    funding = crowded longs = squeeze-short fuel; a rollover from that peak times
    the "основной слив" (see modeled EVAA/THE analysis + _funding_short_signal).
    Cached 900s in the client, so per-scan cost is ~1 REST call per symbol."""
    try:
        hist = await client.fetch_funding_rate_history(symbol, limit=10)
    except Exception:
        _LOG.debug("manipulation_funding_failed sym=%s", symbol, exc_info=True)
        return None
    rates = [float(r.get("fundingRate") or 0.0) for r in (hist or [])]
    if not rates:
        return None
    return {"rate": rates[-1], "peak": max(rates)}


async def _fetch_symbol_data(
    client: Any, symbol: str, sem: asyncio.Semaphore,
) -> tuple[str, dict[str, list[list[float]]], dict[str, float] | None]:
    """Parallel OHLCV + funding fetch for one symbol. CCXT Pro async REST."""
    import time
    ohlcv_by_tf: dict[str, list[list[float]]] = {}
    now_ms = time.time() * 1000
    async def _fetch(tf: str) -> tuple[str, list[list[float]] | None]:
        try:
            # Cached: the scanner re-runs every cycle; interval-aware TTL means a 1d
            # frame is refetched ~hourly, not every cycle (was the dominant REST sink
            # → 418 ban). Only 5m refreshes fast. WS-fed frames supersede this next.
            bars = await client.fetch_ohlcv_list_cached(symbol, tf, limit=_LOOKBACK_BY_TF[tf])
            if bars and len(bars) >= 2:
                interval_ms = _INTERVAL_MS.get(tf)
                if interval_ms is not None and int(bars[-1][0]) + interval_ms > now_ms:
                    # Drop the still-forming candle (list path skips finalize_kline_frame)
                    # so detect_impulse et al. never fire on an unclosed bar.
                    bars = bars[:-1]
            if bars and len(bars) > 0:
                last_ts = int(bars[-1][0])
                stale_ms = _MAX_STALE_MS_BY_TF.get(tf, 3600_000)
                if now_ms - last_ts > stale_ms:
                    return tf, None
            return tf, bars
        except Exception:
            _LOG.debug("manipulation_fetch_failed sym=%s tf=%s", symbol, tf, exc_info=True)
            return tf, None
    # Parallel per-TF fetch + funding within symbol
    async with sem:
        tfs = await asyncio.gather(*[_fetch(tf) for tf in _TIMEFRAMES], return_exceptions=True)
        funding = await _funding_ctx(client, symbol)
    for item in tfs:
        if isinstance(item, BaseException):
            continue
        tf, bars = item
        if isinstance(tf, str) and bars:
            ohlcv_by_tf[tf] = bars
    return symbol, ohlcv_by_tf, funding


async def deliver_manipulation_setups(
    symbols: list[str],
    client: Any,
    broadcaster: Any,
    *,
    tracker_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan ``symbols`` for manipulation reversal setups — parallel REST + Polars detection.

    Uses asyncio.gather with semaphore for CCXT Pro async REST.
    Detection runs on Polars DataFrames via scanner/detect/patterns.py.
    """
    results: list[dict[str, Any]] = []
    now_dt = datetime.now(timezone.utc)
    now_ms = now_dt.timestamp() * 1000
    sem = asyncio.Semaphore(_PARALLEL_SEMAPHORE)
    fetch_tasks = [_fetch_symbol_data(client, s, sem) for s in symbols]
    outcomes = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    scanner_states = load_scanner_state(SCANNER_STATE)
    states_changed = False

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            _LOG.warning("manipulation_gather_exception", error=repr(outcome))
            continue
        symbol, ohlcv_by_tf, funding_ctx = outcome
        if not ohlcv_by_tf.get("1d"):
            continue

        # Multi-scale: run every TF ladder (1d/4h, 4h/1h, 1h/15m), each with its
        # own persisted A/B state — a manipulation at any scale is caught by its
        # matching ladder (see advance_manipulation_scales). Per-symbol state is
        # the dict of per-ladder states.
        prior_state = scanner_states.get(symbol)
        new_state, setup = advance_manipulation_scales(
            symbol, ohlcv_by_tf, prior_state, now_ms=now_ms, funding_ctx=funding_ctx
        )
        if setup is None:
            # Stage advanced but no completed signal yet — persist the progress
            # immediately so it isn't lost (Bug 2: NON-completed path keeps its
            # current behavior and commits right away).
            if new_state != prior_state:
                scanner_states[symbol] = new_state
                states_changed = True
            continue

        if tracker_state is not None:
            if has_active_signal(tracker_state, symbol=symbol, direction=setup.direction):
                continue  # course: runs to completion — don't re-fire while the prior call is still open

            # Bug 1: enforce the same rate-limit / cooldown gates the main confirm
            # path uses. The manipulation path previously skipped ALL of them. Skip
            # delivery (continue) if ANY gate trips. Gates use tz-aware UTC datetimes.
            if (
                global_confirm_burst_cap_reached(tracker_state, now=now_dt)
                or recent_stop_hit_cooldown(tracker_state, symbol=symbol, direction=setup.direction, now=now_dt)
                or symbol_loss_streak_cooldown(tracker_state, symbol=symbol, direction=setup.direction, now=now_dt)
                or symbol_daily_tg_cap_reached(tracker_state, symbol=symbol, direction=setup.direction, now=now_dt)
                or symbol_repeat_loser_blocked(tracker_state, symbol=symbol, now=now_dt)
            ):
                continue

        # setup.entry_ref anchors to the confirmation candle's own close when
        # micro confirmation fired — falls back to the last meso close only
        # when there was no micro confirmation to anchor to.
        meso_bars = ohlcv_by_tf.get(setup.meso_tf) or ohlcv_by_tf["1d"]
        if setup.entry_ref is not None and setup.entry_ref > 0:
            price = setup.entry_ref
        else:
            price = float(meso_bars[-1][4])
        if price <= 0:
            continue
        stop_buffer = _stop_buffer(meso_bars, pattern_a3=(setup.pattern_type == "A3"))
        geo = _geometry(setup, price=price, stop_buffer=stop_buffer)
        if geo is None:
            continue

        text = _format_manipulation_signal(symbol, setup, price=price, geo=geo)
        try:
            result = await send_lane_html(broadcaster, text)
        except Exception:
            _LOG.exception("manipulation_delivery_send_failed sym=%s", symbol)
            continue

        # Bug 2: only commit the completed (reset) state AFTER a successful send,
        # so a transient Telegram failure leaves scanner_states[symbol] == prior_state
        # and the still-armed pattern retries on the next cycle instead of being lost.
        message_id = getattr(result, "message_id", None)
        if new_state != prior_state:
            scanner_states[symbol] = new_state
            states_changed = True

        if tracker_state is not None:
            setup_dict = {
                "stop_loss": geo["stop"],
                "tp1": setup.target,
                "entry_zone": [geo["entry_lo"], geo["entry_hi"]],
                "averaging_price": geo["averaging_price"],
                "entry_type": f"manipulation_{setup.pattern_type}",
                "risk_reward": geo["rr"],
                "level_source": "manipulation_structural",
                "telegram_sent": True,
                "delivery_tier": "triggered",
                "phase": "manipulation",
                "pattern_type": setup.pattern_type,
                "score": setup.score,
                "steps": f"{setup.steps_covered}/{setup.total_steps}",
                "dump_score": 0,
                "dump_fuel": 0,
                "long_score": 0,
                "long_fuel": 0,
                "confirm_hard": [],
            }
            lifecycle = {"phase": "pre_dump" if setup.direction == "short" else "pre_pump"}
            try:
                register_signal_open(
                    tracker_state,
                    symbol=symbol,
                    direction=setup.direction,
                    price=price,
                    setup=setup_dict,
                    lifecycle=lifecycle,
                    now=now_dt,
                    entry_message_id=message_id,
                )
            except Exception:
                _LOG.exception("manipulation_tracker_register_failed sym=%s", symbol)

            # Track the burst window only AFTER a real send (failed sends must not
            # count toward the global burst cap). MUTATES tracker_state["confirm_burst_ts"].
            record_confirm_burst(tracker_state, now=now_dt)

        results.append({"symbol": symbol, "direction": setup.direction, "message_id": message_id,
                         "pattern_type": setup.pattern_type, "score": setup.score})
        _LOG.info(
            "manipulation_delivered sym=%s dir=%s pattern=%s score=%.2f target=%s rr=%.2f steps=%d/%d",
            symbol, setup.direction, setup.pattern_type, setup.score,
            setup.target, geo["rr"], setup.steps_covered, setup.total_steps,
        )

    if states_changed:
        save_scanner_state(scanner_states, SCANNER_STATE)

    return results


__all__ = ["deliver_manipulation_setups"]
