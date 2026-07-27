"""Единственная реализация формулы результата сделки — общий слой.

Зачем отдельный модуль. Формула жила в `tracker.close_signal`, а `track/equity.py` считал R
как `хранимый pnl / дистанция стопа`, где дистанция меряется от ХУДШЕЙ кромки зоны. Замер
2026-07-27 по 283 записям: у **157 из 168** невырожденных зон хранимый `pnl` писался от
СЕРЕДИНЫ, от худшей кромки — у нуля. То есть числитель и знаменатель R брались от разных
точек отсчёта, и в каждый R впрыснута половина ширины зоны. Потребитель — `_cooldowns.net_r`,
то есть ЖИВОЙ гейт допуска символа.

Глубже: в колонке `pnl_pct` смешаны ТРИ поколения формулы. Пересчёт всех 283 записей по
текущей конвенции даёт **+976.2%** против хранимых **+1575.2%** — расхождение 599 п.п.

Принцип, из которого это следует: **хранить факты, выводить производные.** `exit_price`,
кромки зоны, `tp1`, `partial_fixed_pct` — наблюдения; `pnl_pct` — вывод той версии кода, что
исполнялась в тот день. Поэтому потребитель обязан считать сам, а не доверять хранимому
числу, и считать ОДНОЙ формулой — этой.

⚠ Держать вторую копию формулы где бы то ни было запрещено. Две реализации одного понятия —
ровно тот дефект, который здесь чинится.
"""
from __future__ import annotations

from typing import Any

# Точка отсчёта и способ — раздельно, потому что разошлись именно они. Прежние значения
# (`full_position` / `partial_fix_at_tp1`) называли способ и молчали о базе, из-за чего
# поколение формулы было неотличимо по данным.
BASIS_FULL = "worst_edge/full_position"
BASIS_PARTIAL = "worst_edge/partial_fix_at_tp1"


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0.0 else None


def entry_base(row: dict[str, Any], *, direction: str) -> float | None:
    """Кромка входа, от которой считается и стоп, и результат: ХУДШАЯ из полосы.

    Лонг — `entry_hi` (заплатил дороже всех), шорт — `entry_lo` (продал дешевле всех). По
    лимитной лестнице неизвестно, где именно исполнилось, и приписывать себе лучшую половину
    полосы нельзя (I-6). Та же кромка, от которой `_trailing.py::_worst_entry` ставит
    безубыток — две разные базы в одной сделке давали прибыль из ничего в половину ширины.
    """
    lo, hi = _num(row.get("entry_lo")), _num(row.get("entry_hi"))
    if lo and hi:
        return hi if str(direction).lower() == "long" else lo
    return lo or hi


def realized_pct(
    row: dict[str, Any],
    *,
    direction: str | None = None,
    exit_price: float | None = None,
) -> tuple[float, str] | None:
    """Результат сделки в процентах и НАЗВАНИЕ базы, которой он посчитан.

    Формула повторяет денежную механику метода: часть банкуется на TP1, остаток едет дальше.
    Отметить всю позицию по цене выхода значило бы показать сделку, взявшую +20% на половине
    и вернувшуюся в безубыток, как PnL 0.00% — то есть стереть настоящую прибыль.

    Args:
        row: Запись сделки (кромки зоны, `tp1`, `partial_fixed_pct`, `tp1_hit`).
        direction: Направление; берётся из строки, если не передано.
        exit_price: Цена выхода; берётся из строки, если не передана.

    Returns:
        ``(процент, базис)`` либо ``None``, когда геометрии не хватает. None — это «не
        измерено», и вызывающий обязан такую строку ИСКЛЮЧИТЬ и посчитать, а не подставить
        ноль (I-6).
    """
    direc = str(direction if direction is not None else row.get("direction") or "").lower()
    px = _num(exit_price if exit_price is not None else row.get("exit_price"))
    base = entry_base(row, direction=direc)
    if px is None or base is None or base <= 0.0:
        return None

    def _leg(price: float) -> float:
        raw = (price - base) / base * 100.0
        return raw if direc == "long" else -raw

    tp1 = _num(row.get("tp1"))
    fixed_pct = float(row.get("partial_fixed_pct") or 0.0)
    if row.get("tp1_hit") and tp1 and 0.0 < fixed_pct < 100.0:
        frac = fixed_pct / 100.0
        return round(frac * _leg(tp1) + (1.0 - frac) * _leg(px), 2), BASIS_PARTIAL
    return round(_leg(px), 2), BASIS_FULL


def stop_distance_pct(row: dict[str, Any], *, direction: str | None = None) -> float | None:
    """Дистанция ПЕРВОНАЧАЛЬНОГО стопа в процентах — единица риска сделки.

    Берётся `original_stop_loss`, а не текущий: размер позиции решается на входе, и
    подтянутый позже безубыток риска не меняет. Считать риск по сдвинутому стопу значило бы
    задним числом объявить сделку тем крупнее, чем удачнее она шла.
    """
    direc = str(direction if direction is not None else row.get("direction") or "").lower()
    base = entry_base(row, direction=direc)
    stop = _num(row.get("original_stop_loss")) or _num(row.get("stop_loss"))
    if not base or not stop:
        return None
    return abs(base - stop) / base * 100.0


__all__ = [
    "BASIS_FULL",
    "BASIS_PARTIAL",
    "entry_base",
    "realized_pct",
    "stop_distance_pct",
]
