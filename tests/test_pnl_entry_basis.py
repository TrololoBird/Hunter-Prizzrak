"""Гард: PnL считается от ТОЙ ЖЕ кромки входа, от которой ставится стоп.

Дефект. `_trailing.py::_worst_entry` ставит безубыток и трейл от ХУДШЕЙ кромки зоны (лонг —
`entry_hi`, шорт — `entry_lo`), а `close_signal` считал прибыль от СЕРЕДИНЫ. Две точки отсчёта
в одной сделке означают, что выход «в безубыток» книжится с прибылью в половину ширины зоны —
из ничего.

Замер по 3682 записям истории: 88.5% зон вырождены (нулевая ширина), там разницы нет. У
остальных 423 медиана ширины 1.010%, p90 3.547%, максимум 13.26% — до **6.63%** фантомной
прибыли на сделку, что больше всего буфера стопа.

⚠ Отдельно фиксирую собственную ошибку, чтобы её не повторили. Я обвинил в фальши 397
прибыльных `stop_hit` («стоп ниже входа — прибыль невозможна»). Проверка показала обратное:
406 из 444 закрыты по формуле `partial_fix_at_tp1` (банк на TP1 + раннер до безубытка — это
законно и есть суть метода), а у остальных 38 формула сходится копейка в копейку. Ошибка была
моя: я применил логику лонга к шортам и не учёл частичную фиксацию.
"""
from __future__ import annotations

from datetime import UTC, datetime

from hunt_core.track import tracker


def _closed(direction: str, *, lo: float, hi: float, exit_px: float) -> dict:
    state: dict = {"signals": {}}
    key = f"TESTUSDT:{direction}"
    state["signals"][key] = {
        "symbol": "TESTUSDT", "direction": direction,
        "entry_lo": lo, "entry_hi": hi,
        "opened_at": datetime.now(UTC).isoformat(),
        "status": "triggered", "phase": "triggered",
    }
    tracker.close_signal(
        state, symbol="TESTUSDT", direction=direction,
        reason="stop_hit", exit_price=exit_px,
        now=datetime.now(UTC), archive=False,
    )
    return state["signals"][key]


def test_breakeven_exit_at_worst_edge_is_zero_not_half_the_zone() -> None:
    """Выход по худшей кромке — это РОВНО ноль. Раньше книжилось +половина ширины."""
    long_sig = _closed("long", lo=100.0, hi=102.0, exit_px=102.0)
    assert abs(float(long_sig["pnl_pct"])) < 0.01, (
        f"выход в безубыток дал pnl={long_sig['pnl_pct']} — база снова середина зоны"
    )
    short_sig = _closed("short", lo=100.0, hi=102.0, exit_px=100.0)
    assert abs(float(short_sig["pnl_pct"])) < 0.01


def test_real_loss_and_gain_still_measured() -> None:
    """Гард не имеет права обнулять настоящий результат."""
    loss = _closed("long", lo=100.0, hi=102.0, exit_px=99.0)
    assert float(loss["pnl_pct"]) < -2.0, "настоящий убыток пропал"
    gain = _closed("long", lo=100.0, hi=102.0, exit_px=110.0)
    assert float(gain["pnl_pct"]) > 7.0, "настоящая прибыль занижена сверх меры"


def test_degenerate_zone_is_unaffected() -> None:
    """У 88.5% записей зона нулевой ширины — поведение обязано остаться прежним."""
    sig = _closed("long", lo=100.0, hi=100.0, exit_px=105.0)
    assert abs(float(sig["pnl_pct"]) - 5.0) < 0.01


def test_short_uses_lower_edge_as_base() -> None:
    """Для шорта худшая кромка — нижняя: продать дешевле хуже."""
    sig = _closed("short", lo=100.0, hi=102.0, exit_px=95.0)
    assert abs(float(sig["pnl_pct"]) - 5.0) < 0.01, (
        f"шорт от 100 до 95 обязан дать +5%, получено {sig['pnl_pct']}"
    )
