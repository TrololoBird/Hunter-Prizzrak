"""Deliver manipulation reversal setups (scanner/detect/patterns.py).

Scanner's sole signal path — two patterns:
- Pattern A (long): impulse→absorption→bokovik→sweep→break
- Pattern B (short): HTF trend→sweep→fade→LTF_confirm

Non-pinned universe only. Pinned symbols are Prizrak's Deep module exclusively.
"""
from __future__ import annotations

import asyncio
import html
import os
import structlog
from datetime import datetime, timezone
from typing import Any

from hunt_core.deliver._labels import fmt_price
from hunt_core.deliver.lab import send_lane_html
from hunt_core.paths import SCANNER_STATE
from hunt_core.scanner.detect.patterns import ManipulationSetup, advance_manipulation_scales
from hunt_core.scanner.detect.state import load_scanner_state, save_scanner_state
from hunt_core.scanner.feed import ScannerFeed
from hunt_core.track._cooldowns import (
    global_confirm_burst_cap_reached,
    recent_stop_hit_cooldown,
    symbol_daily_tg_cap_reached,
    symbol_loss_streak_cooldown,
    symbol_repeat_loser_blocked,
)
from hunt_core.track.tracker import has_active_signal, register_signal_open

_LOG = structlog.get_logger(__name__)

# ── Manipulation geometry — calibration surface (code-intent, deliberately NOT TOML) ──
# G-30 disposition: unlike the maps OB/VP thresholds (which moved to config.defaults.toml),
# these constants stay as documented code. Per the spec §5 drift-resolution precedent —
# code-intent wins over TOML when the code carries the rationale — every number below is
# grounded on the manipulation corpus itself (sweep depths, ESPORTS/BSB/ZEREBRO move
# sizes, the трети/две-трети добор ladder), i.e. they are METHOD INVARIANTS, not deployment
# knobs a operator would retune. Two further reasons: (1) the deliver path threads no config
# object, so relocating would mean plumbing cfg through ~8 hot-path functions purely to move
# a literal; (2) exposing corpus-grounded numbers as TOML invites blind retuning of the
# method. Relocation was CONSIDERED and rejected here — same disposition the OB absolute-USD
# floors carry ("making them relative is a calibration decision, not a config move").
_MIN_RR = 1.2
# Advisory («ОЖИДАНИЕ подтверждения — НЕ вход») resend throttle: same card re-sends
# only after this many seconds unless the setup progressed (steps_covered grew).
# In-process only — a restart re-sends once, which is acceptable.
_ADVISORY_RESEND_S = float(os.getenv("HUNT_MANIP_ADVISORY_RESEND_S", "21600") or 21600)
_ADVISORY_SENT: dict[tuple[str, str, str], tuple[float, int]] = {}
# Minimum structural sweep depth (|swept_level − sweep_extreme| / swept_level) for a
# dip to count as a liquidity-grab «свип». Grounded on real bars: the corpus winners
# swept ≥1%, while the junk O/USDT A-long «swept» only 0.07–0.34% (chart noise). 0.5%
# sits in the clean gap between the two. Pattern A3 is exempt (no real sweep).
_MIN_SWEEP_DEPTH_PCT = 0.005
# Measured-move fallback target, per detection frame. When the structural ladder has NO
# pool within the cap (only far dead peaks after a round-trip), abstaining drops real
# author trades whose target is a MEASURED % move, not a structural pool (grounded vs the
# «Owner of SHORT» channel: ESPORTS +160%, UNI, VELVET, SAFE — «10-20% чистого»). Project
# a frame-scaled measured target as the fallback. Only fires when NO structural target
# exists → signals that already have reachable pools (e.g. the O/USDT case) are unchanged.
# The R:R gate below still filters it.
_MEASURED_MOVE_BY_TF: dict[str, float] = {
    "1w": 0.60, "1d": 0.50, "4h": 0.30, "1h": 0.18, "15m": 0.12, "5m": 0.10,
}
_MEASURED_MOVE_DEFAULT = 0.15
# We deliberately hunt VERY large manipulation moves, so a 20% target cap rejected
# exactly the biggest, most valuable setups; it was raised to 60%. But a single cap
# cannot serve every scale: the method's own numbers are 100%, 160%, «больше 400%»
# on DAILY structures («в среднесроке эта сделка показала 250%»), while a 15m-scale
# manipulation is a few percent. A flat 60% silently discarded any setup whose
# nearest structural pool sat further out — i.e. precisely the daily-scale trades the
# (1w, 1d, 4h) ladder was added to find. Cap by the detection frame instead.
# RR (_MIN_RR, measured to TP1) + the structural target still gate quality.
_MAX_TARGET_PCT_BY_TF: dict[str, float] = {
    "1d": 300.0,
    "4h": 150.0,
    "1h": 80.0,
}
_MAX_TARGET_PCT = 60.0  # floor for the fast frames (15m/5m) and unknown TFs


def _max_target_pct(meso_tf: str | None) -> float:
    return _MAX_TARGET_PCT_BY_TF.get(str(meso_tf or ""), _MAX_TARGET_PCT)

# The detection-frame fetch (TF ladder, per-TF lookback/staleness, forming-bar drop, funding
# context) now lives in hunt_core.scanner.feed — the native ScannerFeed on the engine (ADR-0004 S7).


# User's explicit correction: don't wait for the dump/pump to already be
# running (it "may pass in three minutes") — enter as the impulse starts
# EXHAUSTING, deliberately BEFORE full confirmation, which means price can
# still move a bit further against the position before genuinely turning.
# The stop already accounts for that (anchored beyond the full sweep extreme,
# not the entry) — this adds the other missing piece: an explicit averaging
# ("довор") level between entry and stop, and a market/limit order-type label
# matching Prizrak's own format, instead of a single bare "entry price".
_AVERAGING_FRACTION = 0.5  # how far from entry toward stop the averaging limit sits
# Лесенка доборов (Prizrak/Влад SHORT: «доборы страховочные чуть ниже», «добирал…
# усреднил… сместил средний»). Вместо одного добора на 50% к стопу — несколько
# структурных доборов между входом и стопом. Доли отсчитываются от дистанции вход→стоп;
# 0.5 сохранён для обратной совместимости (_AVERAGING_FRACTION == средний рунг).
_DOBOR_FRACTIONS = (0.33, 0.66)  # два добора: треть и две трети пути к стопу
# Полоса входа шире этого — не ошибка (широкий стоп метода → широкая полоса), но исход
# начинает определяться филом, а не сетапом. Помечаем в сообщении, НЕ подавляем сигнал.
_WIDE_ENTRY_BAND_PCT = 5.0


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
    # A real «свип» must actually snag liquidity («снизу ликвидность сняли»). A sub-
    # _MIN_SWEEP_DEPTH_PCT dip below the level is chart noise, not a sweep — treating
    # it as one bolts a fixed stop buffer (3%) onto a 0.07–0.34% wiggle, so the stop
    # sits 15–45× the sweep depth below entry and every добор lands under the extreme
    # (grounded by replaying the real detector on live O/USDT bars). The genuine corpus
    # winners all swept ≥1% (ZEREBRO 1.0–2.6%, ESPORTS 2–27%, BSB 4–11%), so this
    # gate separates junk from the real liquidity grabs. Pattern A3 has no real sweep
    # (its extreme is a synthetic ATR offset below the accumulation low) → exempt.
    if setup.pattern_type != "A3" and setup.swept_level > 0:
        sweep_depth = abs(setup.swept_level - setup.sweep_extreme) / setup.swept_level
        if sweep_depth < _MIN_SWEEP_DEPTH_PCT:
            return None
    # Full structural ladder (course: пулы ликвидности снизу/сверху). R:R is measured
    # to the DEEPEST reachable pool within the scale's target cap — the «среднесрочная
    # цель» — while the nearer levels are shown as partial take-profits. A wide stop
    # above the manipulation extreme only pays off against the whole move, not the
    # first tiny bounce (that is why a single 30% retrace under-stated R and filtered
    # the real EVAA short).
    max_target_pct = _max_target_pct(setup.meso_tf)
    ladder = [t for t in (setup.target_ladder or ()) if t and t > 0]
    if setup.direction == "short":
        ladder = [t for t in ladder if t < price and abs(price - t) / price * 100.0 <= max_target_pct]
    else:
        ladder = [t for t in ladder if t > price and abs(price - t) / price * 100.0 <= max_target_pct]
    projected = False
    if ladder:
        # Ближайшая цель = первая частичная фиксация (TP1). Именно она определяет,
        # окупается ли риск на первом же снятии — R:R до дальнего пула этого не показывает.
        primary_target = min(ladder) if setup.direction == "short" else max(ladder)
        nearest_target = max(ladder) if setup.direction == "short" else min(ladder)
    else:
        st = setup.target
        st_reachable = st is not None and st > 0 and abs(price - st) / price * 100.0 <= max_target_pct
        if st_reachable:
            primary_target = nearest_target = st  # reachable structural target — unchanged
        else:
            # setup.target is absent or a far dead peak beyond cap → project a frame-scaled
            # MEASURED move (the author's %-target) instead of abstaining. See
            # _MEASURED_MOVE_BY_TF. This does NOT override a reachable structural level.
            mm = _MEASURED_MOVE_BY_TF.get(str(setup.meso_tf or ""), _MEASURED_MOVE_DEFAULT)
            primary_target = nearest_target = price * (1 + mm) if setup.direction == "long" else price * (1 - mm)
            ladder = [primary_target]
            projected = True
    target_dist_pct = abs(price - primary_target) / price * 100.0
    if target_dist_pct > max_target_pct:
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
        reward_tp1 = price - nearest_target
    else:
        stop = setup.sweep_extreme * (1 - buf)
        risk = price - stop
        reward = primary_target - price
        reward_tp1 = nearest_target - price
    if risk <= 0 or reward <= 0 or reward_tp1 <= 0:
        return None
    rr = reward / risk
    rr_tp1 = reward_tp1 / risk
    # Гейт качества считается по БЛИЖНЕЙ цели, а не по дальнему пулу. Тейки берутся
    # частями: первая фиксация происходит на TP1, и именно она должна окупать риск.
    # Гейт по дальней цели пропускал сетапы, у которых TP1 не отбивает стоп, — глубокий
    # пул «вытягивал» проверку за геометрию, до которой сделка почти не доживает.
    # rr_tp1 <= rr всегда, поэтому этот гейт строго строже прежнего (при одной цели
    # ближняя == дальняя и поведение не меняется).
    if rr_tp1 < _MIN_RR:
        return None
    averaging_price = price + (stop - price) * _AVERAGING_FRACTION
    # Лесенка доборов между входом и стопом (широкий стоп «за структуру» остаётся общим
    # для всей позиции). Аддитивно к геометрии — гейты rr/target выше не тронуты, поэтому
    # набор живых сигналов не меняется, богаче только управление позицией.
    dobor_ladder = [price + (stop - price) * f for f in _DOBOR_FRACTIONS]
    entry_lo = min(price, averaging_price)
    entry_hi = max(price, averaging_price)

    # WO#3 — «вход ниже реклейма». Доборы ВНИЗ — это метод (усреднение/пересиживание
    # под широким стопом), поэтому ширину полосы мы НЕ режем. Но Pattern C — это
    # «закреп ВЫШЕ предыдущего максимума»: цена под реклеймнутым уровнем означает, что
    # закрепа больше нет (тезис сломан), а не что подвернулась цена получше. Такой добор
    # покупает продолжение свипа. Поэтому для C нижний край полосы не опускается ниже
    # реклейм-уровня (swept_level = prior_high; entry_ref = закреп-клоуз всегда выше него).
    reclaim_clamped = False
    if setup.pattern_type == "C" and setup.direction == "long" and setup.swept_level > 0:
        if entry_lo < setup.swept_level < entry_hi:
            entry_lo = setup.swept_level
            reclaim_clamped = True
        dobor_ladder = [d for d in dobor_ladder if d >= setup.swept_level]

    # Ширина полосы — не гейт, а честная метка: при широком стопе полоса широкая
    # ЗАКОНОМЕРНО, и исход тогда сильно зависит от фила. Помечаем, не подавляем.
    band_width_pct = (entry_hi - entry_lo) / price * 100.0 if price > 0 else 0.0
    return {
        "entry_lo": entry_lo,
        "entry_hi": entry_hi,
        "averaging_price": averaging_price,
        "dobor_ladder": dobor_ladder,
        "stop": stop,
        "rr": rr,
        "rr_tp1": rr_tp1,
        "primary_target": primary_target,
        "nearest_target": nearest_target,
        "ladder": ladder,
        "projected": projected,
        "band_width_pct": band_width_pct,
        "band_wide": band_width_pct > _WIDE_ENTRY_BAND_PCT,
        "reclaim_clamped": reclaim_clamped,
    }


# Метки, которые описывают НЕДОСТАЮЩЕЕ подтверждение или встречный контекст.
# Они не аргументы за вход и не должны попадать в строку «почему».
_RISK_EVIDENCE: dict[str, str] = {
    "ltf_pending": "разворот на младшем ТФ ещё не подтверждён",
    "volume_pending": "нет бычьих объёмов",
    "htf_bear": "старший ТФ медвежий (вход против тренда)",
    "htf_bull": "старший ТФ бычий (вход против тренда)",
}


def _split_evidence(setup: ManipulationSetup) -> tuple[list[str], list[str]]:
    """Разделить evidence на аргументы «за» и факторы риска.

    ``htf_bull``/``htf_bear`` — риск только когда они ПРОТИВ направления входа;
    по тренду это подтверждение.
    """
    supporting: list[str] = []
    risks: list[str] = []
    for tag in setup.evidence:
        against_htf = (tag == "htf_bear" and setup.direction == "long") or (
            tag == "htf_bull" and setup.direction == "short"
        )
        if tag in ("ltf_pending", "volume_pending") or against_htf:
            risks.append(_RISK_EVIDENCE.get(tag, tag))
        else:
            supporting.append(tag)
    return supporting, risks


def _format_manipulation_signal(symbol: str, setup: ManipulationSetup, *, price: float, geo: dict[str, Any]) -> str:
    sym = html.escape(symbol.replace("USDT", "-USDT"))
    side_label = "SHORT" if setup.direction == "short" else "LONG"
    emoji = "🔴" if setup.direction == "short" else "🟢"
    pattern_label = f"Pattern {setup.pattern_type}"
    micro_line = ""
    if setup.micro_tf:
        tag = "подтверждён" if setup.micro_confirmed else "не найден"
        micro_line = f"Разворот на {setup.micro_tf}: <b>{tag}</b>\n"
    supporting, risks = _split_evidence(setup)
    # Micro-confirmation gates ACTIONABILITY of the plan, not just the score. Before the
    # LTF reversal (bos/choch) is found, a full «📍 Вход» plan reads as go-now and the
    # trader enters into an unconfirmed continuation of the sweep (realized downside:
    # ALLO −13.55%). When unconfirmed, disavow the entry: show the levels as REFERENCE
    # only, explicitly «ожидание подтверждения — не вход». (Workorder #1, minimal variant.)
    confirmed = bool(getattr(setup, "micro_confirmed", False))
    band_note = ""
    if geo.get("reclaim_clamped"):
        # Полоса упиралась бы ниже реклейма — там закреп уже сломан, это не добор.
        band_note += f"\n   <i>нижний край подтянут к реклейму {fmt_price(setup.swept_level)} — ниже него закрепа нет</i>"
    if geo.get("band_wide"):
        band_note += (
            f"\n   <i>⚠️ широкая зона ({float(geo.get('band_width_pct') or 0):.1f}%) — "
            f"исход сильно зависит от фила</i>"
        )
    if confirmed:
        entry_line = (
            f"📍 Вход (рыночный / лимит): "
            f"<code>{fmt_price(geo['entry_lo'])} — {fmt_price(geo['entry_hi'])}</code>{band_note}"
        )
    else:
        entry_line = (
            f"⏳ <b>ОЖИДАНИЕ подтверждения разворота ({setup.micro_tf or 'LTF'}) — НЕ вход.</b>\n"
            f"   <i>ориентиры (входить только ПОСЛЕ подтверждения): "
            f"{fmt_price(geo['entry_lo'])} — {fmt_price(geo['entry_hi'])}</i>"
        )
    lines = [
        f"{emoji} <b>Манипуляция {pattern_label}</b> · <code>{sym}</code> · <b>{side_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Score: <b>{setup.score:.0%}</b> · Шаги: {setup.steps_covered}/{setup.total_steps}",
        f"Свип уровня {setup.macro_tf} <code>{fmt_price(setup.swept_level)}</code> → "
        f"экстремум <code>{fmt_price(setup.sweep_extreme)}</code> ({setup.meso_tf})",
        micro_line.rstrip("\n") if micro_line else "",
        entry_line,
        _dobor_ladder_line(geo) if confirmed else "",
        f"🛑 Стоп (за структуру): <code>{fmt_price(geo['stop'])}</code>",
        _tp_ladder_line(geo, setup),
        _rr_line(geo, setup),
        (f"⚡ Фандинг: {html.escape(setup.funding_note)}" if setup.funding_note else ""),
        (f"<i>почему: {html.escape(', '.join(supporting))}</i>" if supporting else ""),
        (f"⚠️ <i>риски: {html.escape('; '.join(risks))}</i>" if risks else ""),
        "<i>Тейки частями по лестнице, держим до цели/стопа — не микро-триггер</i>",
    ]
    return "\n".join(line for line in lines if line)


def _rr_line(geo: dict[str, Any], setup: ManipulationSetup) -> str:
    """R:R до ближней и до дальней цели.

    Только дальний R:R завышает ожидаемую отдачу: дойти до самого глубокого пула
    заметно менее вероятно, чем до первой частичной фиксации.
    """
    far = geo["rr"]
    far_target = geo.get("primary_target") or setup.target
    near = geo.get("rr_tp1") or 0.0
    near_target = geo.get("nearest_target")
    if near > 0 and near_target and near_target != far_target:
        return (
            f"R:R ≈ <code>{near:.2f}</code> <i>(до TP1 {fmt_price(near_target)})</i>"
            f" · <code>{far:.2f}</code> <i>(до среднесрочной {fmt_price(far_target)})</i>"
        )
    return f"R:R ≈ <code>{far:.2f}</code> <i>(до среднесрочной {fmt_price(far_target)})</i>"


def _dobor_ladder_line(geo: dict[str, Any]) -> str:
    """Лесенка страховочных доборов между входом и стопом (усреднение по структуре).

    Метод автора — не один довор, а несколько: усредняем позицию по мере хода против,
    удерживая общий широкий стоп «за структуру». Fallback на одиночный ``averaging_price``.
    """
    rungs = [d for d in (geo.get("dobor_ladder") or ()) if d and d > 0]
    if not rungs:
        avg = geo.get("averaging_price")
        return f"➕ Довор (если пойдёт против ещё): <code>{fmt_price(avg)}</code>" if avg else ""
    tags = " · ".join(f"Д{i+1} <code>{fmt_price(d)}</code>" for i, d in enumerate(rungs))
    return f"➕ Доборы (усреднение к стопу): {tags}"


def _tp_ladder_line(geo: dict[str, Any], setup: ManipulationSetup) -> str:
    """Full structural take-profit ladder (course: пулы ликвидности, тейки частями)."""
    ladder = geo.get("ladder") or list(setup.target_ladder or ())
    ladder = [t for t in ladder if t and t > 0]
    if not ladder:
        return f"🎯 Цель (структурная зона): <code>{fmt_price(setup.target)}</code>"
    ladder = sorted(ladder, reverse=(setup.direction == "short"))
    tps = " · ".join(f"TP{i+1} <code>{fmt_price(t)}</code>" for i, t in enumerate(ladder[:6]))
    if geo.get("projected"):
        # No structural pool in reach — this is a measured %-move projection, not a pool.
        return f"🎯 Цель (проекция движения, нет структурного пула): {tps}"
    return f"🎯 Тейки (пулы ликвидности): {tps}"


async def deliver_manipulation_setups(
    symbols: list[str],
    feed: ScannerFeed,
    broadcaster: Any,
    *,
    tracker_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan ``symbols`` for manipulation reversal setups — engine-fed frames + Polars detection.

    Detection frames (closed-only OHLCV per TF + funding context) come from ``feed`` — the native
    :class:`~hunt_core.scanner.feed.ScannerFeed` on the engine (ADR-0004 S7), or the legacy client
    behind the same interface while the coexistence flag is OFF. Detection runs on Polars
    DataFrames via ``scanner/detect/patterns.py``.
    """
    results: list[dict[str, Any]] = []
    now_dt = datetime.now(timezone.utc)
    now_ms = now_dt.timestamp() * 1000
    outcomes = await asyncio.gather(
        *(feed.detection_data(s, now_ms=now_ms) for s in symbols), return_exceptions=True
    )

    scanner_states = load_scanner_state(SCANNER_STATE)
    states_changed = False

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            _LOG.warning("manipulation_gather_exception error=%s", repr(outcome))
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

        # SHORT selectivity gate (evidence-driven, source-faithful). The GTC transcript
        # takes the short only AFTER the lower-TF reversal confirms ("увидели подтверждение
        # на младшем таймфрейме"). Empirically (dataset_v10) the UNconfirmed (ltf_pending)
        # shorts are the loss engine: 61 setups at −0.71R; the confirmed subset is the only
        # non-losing one. Longs are the real edge (+0.54R) and are unaffected. So suppress
        # delivery of pre-confirmation shorts — persist state so the ladder keeps tracking
        # and can still fire once the LTF break lands. Override: HUNT_MANIP_SHORT_REQUIRE_LTF=0.
        if (
            setup.direction == "short"
            and not getattr(setup, "micro_confirmed", False)
            and os.getenv("HUNT_MANIP_SHORT_REQUIRE_LTF", "1") not in {"0", "false", "False"}
        ):
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

        # Advisory (unconfirmed) cards never register a tracker signal (WO#1), so
        # has_active_signal cannot throttle them — without this dedup the SAME
        # «ОЖИДАНИЕ подтверждения» card re-sends every scan cycle (~5 min) while
        # the pattern stays armed (live case: EPICUSDT ×14/hour, 2026-07-15).
        # Re-send only when the setup PROGRESSES (steps_covered grows — e.g. the
        # LTF confirm lands) or the cooldown lapses. Confirmed setups are exempt:
        # they register with the tracker and are throttled by has_active_signal.
        if not bool(getattr(setup, "micro_confirmed", False)):
            adv_key = (symbol, setup.direction, setup.pattern_type)
            prev = _ADVISORY_SENT.get(adv_key)
            now_mono = asyncio.get_event_loop().time()
            if (
                prev is not None
                and setup.steps_covered <= prev[1]
                and (now_mono - prev[0]) < _ADVISORY_RESEND_S
            ):
                if new_state != prior_state:
                    scanner_states[symbol] = new_state
                    states_changed = True
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
        if not bool(getattr(setup, "micro_confirmed", False)):
            _ADVISORY_SENT[(symbol, setup.direction, setup.pattern_type)] = (
                asyncio.get_event_loop().time(),
                setup.steps_covered,
            )
        message_id = getattr(result, "message_id", None)
        if new_state != prior_state:
            scanner_states[symbol] = new_state
            states_changed = True

        # WO#1: an UNCONFIRMED setup is rendered as «ОЖИДАНИЕ подтверждения — НЕ вход»
        # (advisory watch card, see _format_manipulation_signal). It must NOT become a
        # tracked open position: registering it made auto_resolve fire TRIGGERED/TP/stop
        # follow-ups on a non-entry, wrote its outcome to the ledger, burned the global
        # confirm-burst budget, and — worst — made has_active_signal SWALLOW the later
        # CONFIRMED signal on the same symbol (the LTF break finally landing). So the
        # tracker only opens a position for confirmed setups; the advisory card was
        # already sent above. (Short unconfirmed setups are suppressed earlier, ~471.)
        if tracker_state is not None and bool(getattr(setup, "micro_confirmed", False)):
            # The message shows the whole pool ladder and promises «тейки частями …
            # держим до цели/стопа», but the tracker used to receive tp1 ONLY (the
            # nearest pool) and no tp2 — so auto_resolve_active_signals closed the
            # whole position on the first touch and the «среднесрочная цель» was
            # never tracked. Hand the tracker the same ladder the operator sees.
            tps = sorted(geo["ladder"] or [setup.target], reverse=(setup.direction == "short"))
            setup_dict = {
                "stop_loss": geo["stop"],
                "tp1": tps[0],
                "tp2": tps[1] if len(tps) > 1 else None,
                "tp3": tps[2] if len(tps) > 2 else None,
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

            # NB: the burst window is recorded ONCE inside register_signal_open
            # (tracker.py) — matching the main lane. A second record_confirm_burst here
            # double-counted every confirmed manip signal, tripping the burst cap of 2
            # after a single ping.

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
