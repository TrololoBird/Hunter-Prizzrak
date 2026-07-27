"""TG delivery cooldowns and symbol-level repeat-loser caps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hunt_core import serde

POST_SL_REENTRY_COOLDOWN_MINUTES = 90
SYMBOL_LOSS_STREAK_MIN = 2
SYMBOL_LOSS_STREAK_WINDOW_HOURS = 24.0
SYMBOL_LOSS_STREAK_COOLDOWN_HOURS = 6.0
SYMBOL_DAILY_TG_MAX = 2
GLOBAL_CONFIRM_BURST_MAX = 2
GLOBAL_CONFIRM_BURST_WINDOW_MINUTES = 5.0
SYMBOL_REPEAT_LOSER_LOOKBACK = 10
# ⚠ ПОРОГ В R, А НЕ В ПРОЦЕНТАХ. Раньше здесь стояло -8.0 «процентов», и гейт складывал
# `pnl_pct` разных сделок — величину без смысла: сделка со стопом 0.3% и сделка со стопом 30%
# входили в сумму одинаковым весом, хотя это ставки, различающиеся в сто раз.
#
# ЗАМЕР 2026-07-27 по 283 настоящим записям: два символа с суммой **-2.63R** показывали в
# процентах **-0.09%** и проходили гейт насквозь. Их убытки случились на тесных стопах, где
# процент мал по построению, — то есть правило было слепо ровно к тому классу, ради которого
# существует.
#
# Числа не подобраны, а ПЕРЕСЧИТАНЫ из прежних при измеренной медиане стопа 2.42%:
# -8.0% / 2.42 = -3.3R и -15.0% / 2.42 = -6.2R. Строгость политики сохранена, исправлена
# единица. Подгонять их под выборку нельзя: символов с ≥5 сделками здесь всего 4.
SYMBOL_REPEAT_LOSER_NET_R = -3.0
SYMBOL_REPEAT_LOSER_LIFETIME_R = -6.0
SYMBOL_REPEAT_LOSER_LIFETIME_MIN_SAMPLES = 15
SYMBOL_REPEAT_LOSER_MIN_SAMPLES = 5
SYMBOL_REPEAT_LOSER_COOLDOWN_HOURS = 24.0


def recent_stop_hit_cooldown(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    now: datetime,
    minutes: int = POST_SL_REENTRY_COOLDOWN_MINUTES,
) -> bool:
    """True when a stop_hit closed within *minutes* — block fresh confirm TG."""
    sym = symbol.upper()
    direc = direction.lower()
    cutoff = now - timedelta(minutes=minutes)
    for rec in reversed(state.get("closed_history") or []):
        if not isinstance(rec, dict):
            continue
        if str(rec.get("symbol") or "").upper() != sym:
            continue
        if str(rec.get("direction") or "").lower() != direc:
            continue
        if str(rec.get("close_reason") or "") != "stop_hit":
            continue
        pnl = rec.get("pnl_pct")
        if pnl is not None:
            try:
                if float(pnl) >= 0:
                    continue
            except (TypeError, ValueError):
                pass
        raw = rec.get("closed_at")
        if not raw:
            continue
        try:
            closed = datetime.fromisoformat(str(raw))
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if closed >= cutoff:
            return True
    return False


def record_confirm_burst(state: dict[str, Any], *, now: datetime) -> None:
    """Track recent TG confirms for global burst cap (correlated alt-dump bursts)."""
    buf = list(state.get("confirm_burst_ts") or [])
    buf.append(now.isoformat())
    cutoff = now - timedelta(minutes=GLOBAL_CONFIRM_BURST_WINDOW_MINUTES)
    kept: list[str] = []
    for raw in buf:
        try:
            ts = datetime.fromisoformat(str(raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(raw)
    state["confirm_burst_ts"] = kept[-30:]


def global_confirm_burst_cap_reached(
    state: dict[str, Any],
    *,
    now: datetime,
    max_confirms: int = GLOBAL_CONFIRM_BURST_MAX,
    window_minutes: float = GLOBAL_CONFIRM_BURST_WINDOW_MINUTES,
) -> bool:
    """True when too many confirms shipped in a short window."""
    cutoff = now - timedelta(minutes=window_minutes)
    count = 0
    for raw in reversed(state.get("confirm_burst_ts") or []):
        try:
            ts = datetime.fromisoformat(str(raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            count += 1
        else:
            break
    return count >= max_confirms


def symbol_loss_streak_cooldown(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    now: datetime,
    min_losses: int = SYMBOL_LOSS_STREAK_MIN,
    window_hours: float = SYMBOL_LOSS_STREAK_WINDOW_HOURS,
    cooldown_hours: float = SYMBOL_LOSS_STREAK_COOLDOWN_HOURS,
) -> bool:
    """Block re-entry after *min_losses* losing stop_hit within *window_hours*."""
    sym = symbol.upper()
    direc = direction.lower()
    window_cutoff = now - timedelta(hours=window_hours)
    cooldown_cutoff = now - timedelta(hours=cooldown_hours)
    losses_in_window = 0
    last_loss_at: datetime | None = None
    for rec in reversed(state.get("closed_history") or []):
        if not isinstance(rec, dict):
            continue
        if str(rec.get("symbol") or "").upper() != sym:
            continue
        if str(rec.get("direction") or "").lower() != direc:
            continue
        if str(rec.get("close_reason") or "") != "stop_hit":
            continue
        pnl = rec.get("pnl_pct")
        if pnl is not None:
            try:
                if float(pnl) >= 0:
                    continue
            except (TypeError, ValueError):
                pass
        raw = rec.get("closed_at")
        if not raw:
            continue
        try:
            closed = datetime.fromisoformat(str(raw))
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if closed < window_cutoff:
            break
        losses_in_window += 1
        if last_loss_at is None:
            last_loss_at = closed
    if losses_in_window < min_losses or last_loss_at is None:
        return False
    return last_loss_at >= cooldown_cutoff


def telegram_outcome_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge in-memory closed_history with archived signal_history.jsonl."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(rec: dict[str, Any]) -> None:
        if not rec.get("telegram_sent"):
            return
        key = (
            str(rec.get("symbol") or "").upper(),
            str(rec.get("direction") or "").lower(),
            str(rec.get("opened_at") or ""),
            str(rec.get("closed_at") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        out.append(rec)

    for rec in state.get("closed_history") or []:
        if isinstance(rec, dict):
            _add(rec)
    try:
        from hunt_core.paths import SIGNAL_HISTORY

        if SIGNAL_HISTORY.exists():
            for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = serde.loads(line)
                except serde.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    _add(rec)
    except OSError:
        pass
    out.sort(key=lambda r: str(r.get("closed_at") or r.get("opened_at") or ""))
    return out


def symbol_daily_tg_cap_reached(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    now: datetime,
    max_per_day: int = SYMBOL_DAILY_TG_MAX,
) -> bool:
    """Limit TG confirm spam per symbol."""
    sym = symbol.upper()
    direc = direction.lower()
    cutoff = now - timedelta(hours=24.0)
    count = 0
    for rec in reversed(telegram_outcome_records(state)):
        if str(rec.get("symbol") or "").upper() != sym:
            continue
        if str(rec.get("direction") or "").lower() != direc:
            continue
        raw = rec.get("closed_at") or rec.get("opened_at")
        if not raw:
            continue
        try:
            opened = datetime.fromisoformat(str(raw))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if opened < cutoff:
            break
        count += 1
    return count >= max_per_day


def net_r(records: list[dict[str, Any]]) -> float:
    """Суммарный результат в долях риска (R) по списку закрытых сделок.

    R сопоставим между сделками, а процент — нет: он измеряет ход цены, а не размер ставки.
    Записи, по которым риск не восстановим (нет геометрии либо стоп ниже шумового порога),
    в сумму НЕ ВХОДЯТ — приписать им ноль значило бы объявить их безрисковыми (I-6).
    """
    from hunt_core.track.equity import stop_distance_pct

    total = 0.0
    for rec in records:
        pnl = rec.get("pnl_pct")
        dist = stop_distance_pct(rec)
        if pnl is None or not dist:
            continue
        try:
            total += float(pnl) / dist
        except (TypeError, ValueError):
            continue
    return total


def symbol_repeat_loser_blocked(
    state: dict[str, Any],
    *,
    symbol: str,
    now: datetime,
    lookback: int = SYMBOL_REPEAT_LOSER_LOOKBACK,
    net_floor: float = SYMBOL_REPEAT_LOSER_NET_R,
    min_samples: int = SYMBOL_REPEAT_LOSER_MIN_SAMPLES,
    cooldown_hours: float = SYMBOL_REPEAT_LOSER_COOLDOWN_HOURS,
) -> bool:
    """Block symbols with chronic TG losses, measured in risk units."""
    sym = symbol.upper()
    tg_closed = [
        r for r in telegram_outcome_records(state) if str(r.get("symbol") or "").upper() == sym
    ]
    recent = tg_closed[-lookback:]
    if len(tg_closed) >= SYMBOL_REPEAT_LOSER_LIFETIME_MIN_SAMPLES:
        if net_r(tg_closed) < SYMBOL_REPEAT_LOSER_LIFETIME_R:
            return True
    if len(recent) < min_samples:
        return False
    if net_r(recent) >= net_floor:
        return False
    raw = recent[-1].get("closed_at")
    if not raw:
        return True
    try:
        closed = datetime.fromisoformat(str(raw))
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return True
    return (now - closed) < timedelta(hours=cooldown_hours)


__all__ = [
    "GLOBAL_CONFIRM_BURST_MAX",
    "GLOBAL_CONFIRM_BURST_WINDOW_MINUTES",
    "POST_SL_REENTRY_COOLDOWN_MINUTES",
    "SYMBOL_DAILY_TG_MAX",
    "SYMBOL_LOSS_STREAK_COOLDOWN_HOURS",
    "SYMBOL_LOSS_STREAK_MIN",
    "SYMBOL_LOSS_STREAK_WINDOW_HOURS",
    "SYMBOL_REPEAT_LOSER_COOLDOWN_HOURS",
    "SYMBOL_REPEAT_LOSER_LIFETIME_MIN_SAMPLES",
    "SYMBOL_REPEAT_LOSER_LIFETIME_R",
    "SYMBOL_REPEAT_LOSER_LOOKBACK",
    "SYMBOL_REPEAT_LOSER_MIN_SAMPLES",
    "SYMBOL_REPEAT_LOSER_NET_R",
    "global_confirm_burst_cap_reached",
    "net_r",
    "recent_stop_hit_cooldown",
    "record_confirm_burst",
    "symbol_daily_tg_cap_reached",
    "symbol_loss_streak_cooldown",
    "symbol_repeat_loser_blocked",
    "telegram_outcome_records",
]
