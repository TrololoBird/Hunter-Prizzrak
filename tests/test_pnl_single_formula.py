"""Гард: формула результата сделки — ОДНА на весь проект, и метрики считают по ней.

Дефект. Формула жила в `tracker.close_signal`, а `track/equity.py` брал ХРАНИМЫЙ `pnl_pct` и
делил его на дистанцию стопа, измеренную от ХУДШЕЙ кромки зоны.

ЗАМЕР 2026-07-27 по 283 записям очищенного леджера:
* у **157 из 168** невырожденных зон хранимый `pnl` писался от СЕРЕДИНЫ зоны, от худшей
  кромки — у **нуля**. Числитель и знаменатель R брались от разных точек отсчёта, и в каждый
  R впрыснута половина ширины зоны;
* в колонке смешаны ТРИ поколения формулы: пересчёт по текущей конвенции даёт **+976.2%**
  против хранимых **+1575.2%** — расхождение 599 п.п.

Потребитель рассогласования — `_cooldowns.net_r`, то есть ЖИВОЙ гейт допуска символа, а не
только отчёт.

ЭФФЕКТ ПРАВКИ на тех же данных: валовый R **+459.6 → +285.6**, чистый **+437.0 → +263.0**,
матожидание **+1.561R → +0.939R**, медиана **+0.051R → −0.073R** (типичная сделка уходит в
минус), нижняя граница бутстрэпа **+0.548 → +0.147**.

Принцип, который это закрепляет: **хранить факты, выводить производные.** `exit_price`,
кромки зоны, `tp1`, `partial_fixed_pct` — наблюдения; `pnl_pct` — вывод той версии кода, что
исполнялась в тот день.
"""
from __future__ import annotations

import ast
import pathlib

from hunt_core.track.pnl import BASIS_FULL, BASIS_PARTIAL, entry_base, realized_pct


def test_long_and_short_measure_from_the_worst_edge() -> None:
    """Лонг — от верхней кромки, шорт — от нижней: приписать себе лучший залив нельзя."""
    row = {"entry_lo": 100.0, "entry_hi": 102.0}
    assert entry_base(row, direction="long") == 102.0
    assert entry_base(row, direction="short") == 100.0


def test_breakeven_exit_at_worst_edge_is_exactly_zero() -> None:
    """Выход по худшей кромке — ровно ноль, а не половина ширины зоны."""
    long_row = {"entry_lo": 100.0, "entry_hi": 102.0, "direction": "long", "exit_price": 102.0}
    pct, basis = realized_pct(long_row)  # type: ignore[misc]
    assert abs(pct) < 0.01, f"выход в безубыток дал {pct} — база снова середина зоны"
    assert basis == BASIS_FULL

    short_row = {"entry_lo": 100.0, "entry_hi": 102.0, "direction": "short", "exit_price": 100.0}
    pct, _ = realized_pct(short_row)  # type: ignore[misc]
    assert abs(pct) < 0.01


def test_partial_fix_is_not_marked_as_full_position() -> None:
    """Частичная фиксация на TP1 — другой способ И другая метка."""
    row = {
        "entry_lo": 100.0, "entry_hi": 100.0, "direction": "long",
        "tp1": 120.0, "tp1_hit": True, "partial_fixed_pct": 50.0, "exit_price": 100.0,
    }
    pct, basis = realized_pct(row)  # type: ignore[misc]
    assert basis == BASIS_PARTIAL
    assert abs(pct - 10.0) < 0.01, "0.5×(+20%) + 0.5×(0%) = +10%"


def test_basis_names_the_anchor_not_only_the_method() -> None:
    """Метка обязана называть ТОЧКУ ОТСЧЁТА.

    Прежние `full_position` / `partial_fix_at_tp1` молчали о базе — из-за этого поколение
    формулы было неотличимо по данным и его пришлось восстанавливать пересчётом 283 записей.
    """
    assert "worst_edge" in BASIS_FULL
    assert "worst_edge" in BASIS_PARTIAL


def test_missing_geometry_is_none_not_zero() -> None:
    """Нет геометрии — «не измерено», а не «ноль» (I-6).

    Ноль здесь означал бы безубыточную сделку и тихо попал бы в среднее.
    """
    assert realized_pct({"direction": "long", "exit_price": 100.0}) is None
    assert realized_pct({"entry_lo": 100.0, "entry_hi": 100.0, "direction": "long"}) is None


def test_equity_does_not_read_the_stored_pnl() -> None:
    """`build_trade_frame` обязан считать сам, а не доверять колонке.

    Разбор по AST: в файле рядом лежит комментарий, где `pnl_pct` упоминается как раз в
    объяснении, почему его не читают, — grep был бы зелёным по нему.
    """
    src = pathlib.Path("hunt_core/track/equity.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "build_trade_frame":
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get"
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and sub.args[0].value == "pnl_pct"
            ):
                raise AssertionError(
                    "build_trade_frame снова читает хранимый pnl_pct — в колонке три "
                    "поколения формулы, и её нельзя смешивать с риском от худшей кромки"
                )
        return
    raise AssertionError("build_trade_frame не найден")


def test_formula_lives_in_exactly_one_place() -> None:
    """Вторая копия формулы запрещена — расхождение двух копий и есть исходный дефект."""
    tracker_src = pathlib.Path("hunt_core/track/tracker.py").read_text(encoding="utf-8")
    assert "realized_pct(" in tracker_src, "трекер перестал пользоваться общей формулой"
    equity_src = pathlib.Path("hunt_core/track/equity.py").read_text(encoding="utf-8")
    assert "realized_pct(" in equity_src, "метрики перестали пользоваться общей формулой"
    # Собственных реализаций базы входа быть не должно ни там, ни там.
    for path in ("hunt_core/track/tracker.py", "hunt_core/track/equity.py"):
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "entry_base" not in names, f"{path} завёл собственную базу входа"
        assert "risk_base" not in names, f"{path} завёл собственную базу риска"
