"""SL/TP intrabar evaluation and lifecycle stale invalidation (Phase 8 split)."""
from __future__ import annotations

import structlog
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from hunt_core.params.store import tracker_thresholds, tp1_partial_fix_pct as _tp1_pct
from hunt_core.track._trailing import (
    _mfe_pct,
    _stop_in_profit_zone,
    _update_trailing_stop,
    _worst_entry,
)
from hunt_core.track.pnl import realized_pct

if TYPE_CHECKING:
    from hunt_core.track.tracker import HuntFollowUp

_LOG = structlog.get_logger(__name__)
# Stale lifecycle phase sets (mirrored from tracker)
_SHORT_STALE_PHASES = frozenset(
    {
        "no_setup",
        "post_dump_bounce",
        "recovery",
        "accumulation",
        "breakout_arming",
        "impulse_initiating",
    },
)
_LONG_STALE_PHASES = frozenset(
    {"distribution", "exhaustion_at_high", "dump_active"},
)

def _tracker_ref():
    from hunt_core.track import tracker as tr
    return tr

SNIPER_HOLD_TO_TARGET = __import__("os").environ.get("HUNT_SNIPER_MODE", "1") not in {"0", "false", "False"}
SIGNAL_TIMEOUT_HOURS = 48.0  # default / SHORT: pump-absorption is fast (hours–2-3 days)
# The LONG manipulation types are MEDIUM-TERM (accumulation → 100-400% over WEEKS; the
# trader "пересиживает" and holds — research/manipulations_corpus/long_manip_3types). A
# 48h close cut those winners before they ran; the dataset_v10 backtest is net-NEGATIVE
# with a 4-5 d horizon on longs but +0.54R/trade with a 21 d horizon. So give longs a
# medium-term leash. Shorts keep the fast timeout.
SIGNAL_TIMEOUT_HOURS_LONG = float(__import__("os").environ.get("HUNT_LONG_TIMEOUT_H", "504") or 504)  # 21 d


def _signal_timeout_hours(direction: str) -> float:
    return SIGNAL_TIMEOUT_HOURS_LONG if direction == "long" else SIGNAL_TIMEOUT_HOURS

_BAR_MIN_AGE_MIN = {"1m": 0.0, "1m_closed": 2.0, "5m": 6.0, "5m_closed": 11.0}
def _bar_extremes(
    row: dict[str, Any], active: dict[str, Any], *, price: float, ts: datetime
) -> tuple[float, float]:
    """Экстремумы для проверки SL/TP — накопленные С МОМЕНТА ПОСЛЕДНЕГО СДВИГА СТОПА.

    ⚠ ПОЧЕМУ НЕ ЗА ВСЮ ЖИЗНЬ СДЕЛКИ. Раньше здесь копились экстремумы от самого открытия, и
    ПОДВИНУТЫЙ стоп проверялся против ЗАМОРОЖЕННОГО минимума — то есть срабатывал мгновенно,
    на следующем же опросе. Воспроизведено на живом коде (лонг, цена только росла и ни разу
    не возвращалась):

        тик 0  px=100.00  lo=100.00  stop= 94.0
        тик 1  px=103.00  lo=100.00  stop=101.5   трейл сдвинулся
        тик 2  px=106.00  lo=100.00  stop=104.5   TP1 защёлкнут
        тик 3  px=105.50  lo=100.00  stop=104.5 → ЗАКРЫТ trailing_stop_profit +5.70 → «win»

    `lo = 100.0` — это цена регистрации, из времени, когда стопа на 104.5 ещё не существовало.
    Рынок ниже 104.5 после сдвига не торговался НИ РАЗУ. Следствия: раннера не существует
    вовсе (любая сделка со сдвинувшимся стопом закрывается на следующем опросе), TP2 через
    этот путь недостижим, а выход книжится по цене стопа — которая для лонга стоит ВЫШЕ
    рынка, — то есть результат завышен систематически, и `outcome_kind` пишет это в победы.

    Автор класс видел и закрыл ровно один тик (`stop_hit and trail_updated and
    _stop_in_profit_zone`), но `trail_updated` на СЛЕДУЮЩЕМ опросе уже False, и гард отпускал.

    Пожизненные `extreme_hi`/`extreme_lo` сохранены и по-прежнему считают MFE — там окно от
    открытия и есть правильное. Для SL/TP ведётся отдельная пара, которую сбрасывает
    `reset_stop_window` при каждой записи `stop_loss`.
    """
    trk = _tracker_ref()
    hi = lo = price
    age = trk._signal_age_min(active, ts)
    timeframes = row.get("timeframes") or {}
    for tf_key, min_age in _BAR_MIN_AGE_MIN.items():
        if age < min_age:
            continue
        candle = (timeframes.get(tf_key) or {}).get("candle") or {}
        try:
            c_hi = float(candle.get("high") or 0)
            c_lo = float(candle.get("low") or 0)
        except (TypeError, ValueError):
            continue
        if c_hi > 0:
            hi = max(hi, c_hi)
        if c_lo > 0:
            lo = min(lo, c_lo)
    # Пожизненные экстремумы — ТОЛЬКО для MFE (окно от открытия там и есть верное).
    life_hi, life_lo = hi, lo
    try:
        life_hi = max(life_hi, float(active.get("extreme_hi") or price))
        life_lo = min(life_lo, float(active.get("extreme_lo") or price))
    except (TypeError, ValueError):
        pass
    active["extreme_hi"] = life_hi
    active["extreme_lo"] = life_lo

    # Окно ДЛЯ SL/TP — с последнего сдвига стопа. Отсутствие ключей = окно только что
    # сброшено (или сделка новая): начинаем с того, что видно на этом опросе.
    try:
        hi = max(hi, float(active.get("sl_window_hi") or hi))
        lo = min(lo, float(active.get("sl_window_lo") or lo))
    except (TypeError, ValueError):
        pass
    active["sl_window_hi"] = hi
    active["sl_window_lo"] = lo
    return hi, lo


def _stale_lifecycle_invalidate(
    state: dict[str, Any],
    active: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    price: float,
    ts: datetime,
    announced: bool,
    archive: bool = True,
) -> HuntFollowUp | None:
    trk = _tracker_ref()
    """Close tracker position when lifecycle structurally contradicts the open thesis.

    ``archive`` is threaded to the terminal ``close_signal`` so verify/test callers
    (``archive=False``) never append rows to the production ``signal_history.jsonl``.
    """
    if trk._close_already_notified(state, symbol, direction):
        return None
    k = trk._key(symbol, direction)
    lc_phase = str(lifecycle.get("phase") or "")
    lc_bias = str(lifecycle.get("recommended_bias") or "")
    session = row.get("session") or {}
    pos = float(session.get("pos_in_range") or 0.5)

    contra = False
    tr = tracker_thresholds(symbol)
    ticks_needed = int(tr.get("stale_lc_ticks_default", 3))
    near_tp1_ticks = int(tr.get("stale_lc_ticks_near_tp1", 8))
    near_tp1_pct = float(tr.get("near_tp1_remaining_pct", 3.0))
    detail = ""

    if direction == "short":
        opened_phase = str(
            active.get("entry_lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )
        # Phase unchanged since entry — not a lifecycle transition (SPACEUSDT post-mortem:
        # short opened in impulse_initiating, stale fired 3 ticks later on same phase).
        if opened_phase and lc_phase == opened_phase:
            active["stale_lc_ticks"] = 0
            return None
        if active.get("tp1_managed") or active.get("tp1_hit") or active.get("sl_at_breakeven"):
            active["stale_lc_ticks"] = 0
            return None
        if lc_phase in _SHORT_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "post_dump_bounce" and active.get("tp1_hit"):
                ticks_needed = 1
        elif lc_bias == "long":
            contra = True
            detail = f"lifecycle_stale:bias_long:{lc_phase}"
    else:
        opened_phase = str(
            active.get("entry_lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )
        if opened_phase and lc_phase == opened_phase:
            active["stale_lc_ticks"] = 0
            return None
        if active.get("tp1_managed") or active.get("tp1_hit") or active.get("sl_at_breakeven"):
            active["stale_lc_ticks"] = 0
            return None
        if lc_phase in _LONG_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "distribution" and pos >= 0.82:
                ticks_needed = 2

    if not contra:
        active["stale_lc_ticks"] = 0
        return None

    mfe = _mfe_pct(active, direction=direction)
    if SNIPER_HOLD_TO_TARGET and (
        active.get("trailing_active")
        or mfe >= float(tr.get("sniper_hold_min_mfe_pct", 2.0))
    ):
        active["stale_lc_ticks"] = 0
        active["hold_reason"] = "sniper_hold"
        return None

    # Near-TP1 grace: if MFE is within 3% of TP1 distance, hold 8 ticks instead
    # of closing early. HUSDT/ARMUSDT were 1-2% from TP1 when stale fired at 3 ticks.
    if ticks_needed == int(tr.get("stale_lc_ticks_default", 3)) and not active.get("tp1_hit"):
        tp1 = float(active.get("tp1") or 0)
        entry_lo = float(active.get("entry_lo") or 0)
        entry_hi = float(active.get("entry_hi") or 0)
        entry_mid = (entry_lo + entry_hi) / 2.0 if entry_lo and entry_hi else (entry_lo or entry_hi)
        if tp1 > 0 and entry_mid > 0:
            if direction == "short":
                tp1_dist = (entry_mid - tp1) / entry_mid * 100.0
                mfe = (entry_mid - float(active.get("extreme_lo") or entry_mid)) / entry_mid * 100.0
            else:
                tp1_dist = (tp1 - entry_mid) / entry_mid * 100.0
                mfe = (float(active.get("extreme_hi") or entry_mid) - entry_mid) / entry_mid * 100.0
            remaining = tp1_dist - mfe
            if 0 < remaining <= near_tp1_pct:
                ticks_needed = near_tp1_ticks

    n = int(active.get("stale_lc_ticks") or 0) + 1
    active["stale_lc_ticks"] = n
    if n < ticks_needed:
        return None

    trk.close_signal(
        state,
        symbol=symbol,
        direction=direction,
        reason="lifecycle_stale",
        exit_price=price,
        now=ts,
        archive=archive,
    )
    msg_key = f"{k}:invalidate:lifecycle_stale:{lc_phase}"
    if not trk._followup_allowed(state, msg_key, now=ts):
        return None
    return trk.HuntFollowUp(
        event="invalidate",
        symbol=symbol,
        direction=direction,
        message_key=msg_key,
        detail=detail,
        price=price,
        payload={
            **trk._latched_levels_payload(active),
            "announced": announced,
            "reason": "lifecycle_stale",
            "phase": lc_phase,
            "stale_ticks": n,
            "pos_in_range": round(pos, 3),
            **trk._followup_trade_metrics(active, direction=direction, price=price, ts=ts),
        },
    )


def evaluate_levels(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    price: float,
    hi: float,
    lo: float,
    ts: datetime,
    row: dict[str, Any] | None = None,
) -> list[HuntFollowUp]:
    trk = _tracker_ref()
    """Latched SL/TP state machine against intrabar extremes.

    State transitions ALWAYS happen; the followup cooldown only dedupes
    messages. Transport flags (telegram_sent / entry_message_id) never gate
    state — they only mark events as announced for the sender.
    """
    events: list[HuntFollowUp] = []
    k = trk._key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or not trk._is_signal_active(active):
        return events
    if trk._close_already_notified(state, symbol, direction):
        return events
    # An ARMED signal is a resting limit that has NOT filled — there is no
    # position to manage. Running the SL/TP machine on it books outcomes for a
    # trade that never opened: extremes accumulate from spot, so MFE is the
    # unfilled distance to the zone, trailing ratchets, and a TP that sits
    # between spot and the zone "hits" on the registration tick. Promotion to
    # TRIGGERED happens in `_maybe_armed_to_triggered` (followups) once price
    # actually enters the latched entry zone; only then does management start.
    # ⚠ ГАРД `armed` ПЕРЕЕХАЛ НИЖЕ БАРЬЕРА ПРОТУХАНИЯ. Он стоял ЗДЕСЬ, то есть выходил
    # раньше проверки orphan-TTL, и неисполнившийся лимит не истекал НИКОГДА: запись
    # оставалась в `signals` бессрочно, потому что единственный путь её снять лежал за
    # этим `return`.
    #
    # До 2026-08-01 это была «просто» вечная запись. С гардом занятого направления в
    # `tracker.py::register_signal_open` (добавлен тогда же) цена выросла: висящий armed
    # БЛОКИРУЕТ все будущие сигналы по этому `SYMBOL:direction`. То есть безобидная утечка
    # состояния превратилась бы в тихую пробку на канале.
    #
    # Что применимо к armed, а что нет: TTL — ДА, лимит, к которому никто не возвращался
    # сутки, надо снимать. Машина SL/TP и MFE-stall — НЕТ: позиции не существует, экстремумы
    # копятся от спота, и любой их разбор книжит исход сделке, которой не было (ровно то,
    # ради чего гард и заводился). Поэтому TTL считается для всех, а всё остальное — после
    # выхода armed.
    announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))
    is_armed = str(active.get("delivery_tier") or "").lower() == "armed"

    tr = tracker_thresholds(symbol)
    # ⚠ ЗДЕСЬ БЫЛ КЛЮЧ-ОБМАНКА. Читалось `orphan_ttl_hours` (дефолт 24), а дальше стояло
    # `12.0 if direction == "short" else max(base * 2.0, 48.0)` — то есть для шортов
    # прочитанное значение ИГНОРИРОВАЛОСЬ полностью, а для лонгов при дефолте 24 всегда
    # получалось 48 (`max(48, 48)`). Ключ выглядел настраиваемым и не управлял ничем:
    # правка TOML молча ничего не делала — ровно тот класс, о котором предупреждает README.
    #
    # Теперь два честных ключа. ЭФФЕКТИВНЫЕ ЧИСЛА СОХРАНЕНЫ (48/12), чтобы правка не
    # меняла поведение заодно с исправлением интерфейса.
    #
    # ⚠ АСИММЕТРИЯ 48/12 НЕ ИЗМЕРЕНА — это нарушение I-7, и оно остаётся открытым.
    # Замер 2026-08-03 по всем трём леджерам: событий `orphan_expired` в истории **одно**
    # (long), то есть обосновать или опровергнуть разницу между лонгом и шортом сейчас
    # физически не на чем. Числа перенесены как есть и помечены как унаследованные;
    # менять их — только по выборке, а не «разумным значением».
    orphan_ttl_h = float(
        tr.get("orphan_ttl_short_h", 12.0)
        if direction == "short"
        else tr.get("orphan_ttl_long_h", 48.0)
    )
    # (removed: the >50%-toward-TP1 orphan-TTL extension read active["entry_price"]/
    # ["entry_reference"] — phantom keys with no producer, so entry was always 0 and the
    # block never fired — G-68. Reviving it means deriving entry from entry_lo/entry_hi.)
    price = float(price or 0)
    last_rec_raw = active.get("last_reconcile_ts") or active.get("opened_at")
    try:
        last_rec = datetime.fromisoformat(str(last_rec_raw))
        if last_rec.tzinfo is None:
            last_rec = last_rec.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        last_rec = ts
    orphan_age_h = (ts - last_rec).total_seconds() / 3600.0
    if orphan_age_h >= orphan_ttl_h:
        _LOG.warning(
            "orphan_expired %s:%s — last reconcile %.1fh ago (ttl=%.0fh)",
            symbol,
            direction,
            orphan_age_h,
            orphan_ttl_h,
        )
        trk.close_signal(
            state,
            symbol=symbol,
            direction=direction,
            reason="orphan_expired",
            exit_price=price,
            now=ts,
        )
        msg_key = f"{k}:invalidate:orphan_expired"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"orphan TTL {orphan_ttl_h:.0f}h · "
                        f"last reconcile {orphan_age_h:.1f}h ago"
                    ),
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "reason": "orphan_expired",
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    # Отсюда и ниже — управление ПОЗИЦИЕЙ. У armed её нет: это отдыхающий лимит, который
    # ещё не залился. Прогон машины SL/TP по нему книжит исход сделке, которой не было —
    # экстремумы копятся от спота, поэтому MFE равен всей незалитой дистанции до зоны,
    # трейл храповит, а цель между спотом и зоной «срабатывает» на тике регистрации.
    # Повышение до TRIGGERED делает `_maybe_armed_to_triggered` (followups), когда цена
    # реально войдёт в защёлкнутую зону входа; только тогда начинается управление.
    if is_armed:
        return events

    # Longs accumulate and legitimately sit flat/red for days before the pump
    # ("пересидеть") — the 8h/1%-MFE stall was a short-trade tuning that killed medium-
    # term longs early. Scale the stall window for longs (env HUNT_LONG_STALL_H).
    _stall_default = float(tr.get("mfe_stall_hours", 8.0))
    if direction == "long":
        stall_h = float(__import__("os").environ.get("HUNT_LONG_STALL_H", "120") or 120)  # 5 d
    else:
        stall_h = _stall_default
    stall_min_mfe = float(tr.get("mfe_stall_min_pct", 1.0))
    signal_timeout_h = _signal_timeout_hours(direction)
    age_min = trk._signal_age_min(active, ts)
    if (
        not active.get("tp1_hit")
        and age_min >= stall_h * 60.0
        and age_min < signal_timeout_h * 60.0
        and _mfe_pct(active, direction=direction) < stall_min_mfe
    ):
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason="time_stall", exit_price=price, now=ts,
        )
        msg_key = f"{k}:invalidate:time_stall"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"time stall {stall_h:.0f}h · MFE "
                        f"{_mfe_pct(active, direction=direction):.1f}% < {stall_min_mfe:.1f}%"
                    ),
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "reason": "time_stall",
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    if age_min >= signal_timeout_h * 60.0:
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason="timeout", exit_price=price, now=ts,
        )
        msg_key = f"{k}:invalidate:timeout"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=f"timeout {signal_timeout_h:.0f}h без SL/TP",
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "reason": "timeout",
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    be_locked = trk._apply_early_breakeven_lock(active, direction=direction, symbol=symbol)
    if be_locked:
        stop = float(active.get("stop_loss") or 0)
        mfe = _mfe_pct(active, direction=direction)
        phase = str(active.get("entry_lifecycle_phase") or "")
        msg_key = f"{k}:early_be:{stop:.6f}"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="early_breakeven",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"Early BE · MFE {mfe:.1f}% · SL → {trk._fmt(stop)} "
                        f"({phase or '—'})"
                    ),
                    price=price,
                    payload={
                        **trk._latched_levels_payload(active),
                        "announced": announced,
                        "sl_at_breakeven": True,
                        "entry_lifecycle_phase": phase,
                        "mfe_pct": round(mfe, 2),
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
    trail_updated, prev_stop = _update_trailing_stop(
        active, direction=direction, row=row, symbol=symbol, ts=ts
    )

    tp1 = float(active.get("tp1") or 0)
    tp2 = float(active.get("tp2") or 0)
    stop = float(active.get("stop_loss") or 0)
    latch = trk._latched_levels_payload(active)
    latch["announced"] = announced

    if trail_updated and stop > 0:
        protected = round(trk._pnl_at_price(active, direction, stop), 2)
        msg_key = f"{k}:trailing:{stop:.6f}"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="trailing_updated",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"Trailing SL → {trk._fmt(stop)} · защита ~{protected:.1f}%"
                    ),
                    price=price,
                    payload={
                        **latch,
                        "stop_loss": stop,
                        "prev_stop": prev_stop,
                        "protected_pnl_pct": protected,
                        "trailing_active": True,
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )

    if active.get("tp1_hit") and not active.get("tp1_managed"):
        trk.on_tp1_reached(active, direction=direction, symbol=symbol, row=row)
        latch = trk._latched_levels_payload(active)
        latch["announced"] = announced
        stop = float(active.get("stop_loss") or 0)

    if direction == "short":
        stop_hit = stop > 0 and hi >= stop
        tp1_touch = tp1 > 0 and lo <= tp1
        tp2_touch = tp2 > 0 and lo <= tp2
        near_stop = stop > 0 and hi >= stop * 0.998
    else:
        stop_hit = stop > 0 and lo <= stop
        tp1_touch = tp1 > 0 and hi >= tp1
        tp2_touch = tp2 > 0 and hi >= tp2
        near_stop = stop > 0 and lo <= stop * 1.002

    # Same-tick guard: trailing into profit zone must not instant-close on stale hi/lo.
    if (
        stop_hit
        and trail_updated
        and _stop_in_profit_zone(active, direction=direction, stop=stop)
    ):
        stop_hit = False

    # Stop first: a wick through SL ends the signal even if TP printed later.
    if stop_hit:
        # ⚠ Признак «это не стоп-аут» — СТОП БЫЛ СДВИНУТ, а не «работает трейл».
        #
        # Условие держалось на одном `trailing_active`, который ставит только
        # `_trailing._update_trailing_stop`. Но безубыток после первой цели ставит
        # `apply_tp1_management`, и он пишет ДРУГОЙ флаг — `sl_at_breakeven`. Итог на живом
        # канале 2026-07-27: 4 закрытия из 6 ушли как «🔴 Стоп · Стоп-лосс пробит · Позиция
        # закрылась по стопу» с ПОЛОЖИТЕЛЬНЫМ PnL в той же строке (BEAT +52.5%, DIA, AKE, BTW).
        # Читатель видит взаимоисключающие утверждения и не может понять исход сделки.
        #
        # Второе изменение — вердикт по РЕЗУЛЬТАТУ СДЕЛКИ (`realized_pct`), а не по ходу
        # последней ноги: после частичной фиксации на TP1 остаток штатно выходит в ноль, и
        # ход ноги == 0 при взятых +65% на первой цели — это победа, а не «стоп».
        # ⚠ Без запасного значения. `realized_pct` возвращает None ровно тогда, когда у сделки
        # нет геометрии входа, и подстановка туда «хода ноги» дала бы 0.0 (`_pnl_at_price`
        # падает в ноль по ТОМУ ЖЕ условию) — то есть `0.0 >= 0` объявил бы сделку без единой
        # известной кромки «выходом в безубыток». Не измерено — значит обычный стоп (I-6).
        realized = realized_pct(active, direction=direction, exit_price=stop)
        stop_was_moved = bool(active.get("trailing_active") or active.get("sl_at_breakeven"))
        if realized is not None and stop_was_moved and realized[0] >= 0:
            close_reason = "trailing_stop_profit"
            detail_msg = (
                f"Стоп в безубытке/прибыли {trk._fmt(stop)} · фиксация {realized[0]:+.1f}%"
            )
        else:
            close_reason = "stop_hit"
            detail_msg = f"SL {trk._fmt(stop)} пробит (intrabar)"
        trk._transition(
            active,
            trk._coerce_signal_phase(active),
            trk.SignalPhase.INVALIDATED,
            strict=False,
        )
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason=close_reason, exit_price=stop, now=ts,
        )
        msg_key = f"{k}:invalidate:{close_reason}"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=detail_msg,
                    price=price,
                    payload={
                        **latch,
                        "reason": close_reason,
                        "trailing_active": bool(active.get("trailing_active")),
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=stop, ts=ts
                        ),
                    },
                )
            )
        return events

    if tp2_touch:
        skipped = not active.get("tp1_hit")
        active["tp1_hit"] = True
        active["tp2_hit"] = True
        trk.close_signal(
            state, symbol=symbol, direction=direction,
            reason="tp2", exit_price=tp2, now=ts,
        )
        msg_key = f"{k}:tp2"
        if trk._followup_allowed(state, msg_key, now=ts):
            detail = f"TP1+TP2 (пролёт) · TP2 {trk._fmt(tp2)}" if skipped else f"TP2 {trk._fmt(tp2)}"
            events.append(
                trk.HuntFollowUp(
                    event="fix_profit_tp2",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=detail,
                    price=price,
                    # Единственная ветка закрытия БЕЗ `_followup_trade_metrics`: форматтер падал
                    # в свой запасной расчёт (полная позиция от кромки) и печатал не то число,
                    # что `close_signal` только что записал в леджер (там частичная фиксация на
                    # TP1 учтена). Сегодня расхождения нет — все три `tp2`-строки леджера без
                    # частичной фиксации, — но это совпадение данных, а не свойство кода.
                    payload={
                        **latch,
                        "tp2": tp2,
                        "tp1_skipped": skipped,
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=tp2, ts=ts
                        ),
                    },
                )
            )
        return events

    if tp1_touch and not active.get("tp1_hit"):
        active["tp1_hit"] = True
        trk.on_tp1_reached(active, direction=direction, symbol=symbol, row=row)
        latch = {**trk._latched_levels_payload(active), "announced": announced, "tp1": tp1}
        _worst_entry(active, direction=direction)
        fix_pct = int(active.get("partial_fixed_pct") or _tp1_pct(symbol))
        # Persist the banked fraction on the signal: the close-time PnL must know that
        # part of the position was already realised at TP1 (see tracker._close).
        active["partial_fixed_pct"] = fix_pct
        msg_key = f"{k}:tp1"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="fix_profit_tp1",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"TP1 {trk._fmt(tp1)} · зафиксируй {fix_pct}% · "
                        f"SL → {trk._fmt(active.get('stop_loss'))} (BE+buf)"
                    ),
                    price=price,
                    payload={
                        **latch,
                        "partial_fixed_pct": fix_pct,
                        "sl_at_breakeven": True,
                        **trk._followup_trade_metrics(
                            active, direction=direction, price=tp1, ts=ts
                        ),
                    },
                )
            )

    if near_stop and not active.get("stop_warned"):
        active["stop_warned"] = True
        msg_key = f"{k}:stop_warn"
        if trk._followup_allowed(state, msg_key, now=ts):
            events.append(
                trk.HuntFollowUp(
                    event="stop_warning",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=f"near SL {trk._fmt(stop)}",
                    price=price,
                    payload={**latch, "stop": stop},
                )
            )
    return events


__all__ = ["evaluate_levels", "_stale_lifecycle_invalidate", "_bar_extremes"]
