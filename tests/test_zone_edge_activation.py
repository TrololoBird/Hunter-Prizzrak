"""Гард: кромочный кандидат не объявляет вход исполненным без проверки.

Дефект. `orchestrator.py::_zone_edge_candidate` ставил `summary["activation"] = "in_entry_zone"`
**БЕЗУСЛОВНО**, тогда как гейт выше пускает цену на `_ZONE_EDGE_BAND` = 35% вглубь бокса, а
регистрируемая полоса — всего `entry ± _ENTRY_BAND_PCT` = ±0.2%. Лонг ловится от `lo`, но цена
может стоять у `lo + 0.35 × ширина`.

Цепочка последствий целиком: `runtime/emitter.py` переводит `activation == "in_entry_zone"` в
`delivery_tier = "triggered"` → `tracker.register_signal_open` засевает `extreme_hi/lo` ЦЕНОЙ, а
PnL считает от кромки полосы → разница мгновенно оказывается MFE. При
`accumulation_max_width_pct = 12.0` это до **~4.0% на тике регистрации** против порога взвода
трейла `min_trail_mfe_pct = 2.5` — то есть трейл мог взводиться сразу.

Соседняя ветка `_zone_candidate` эту дисциплину соблюдала всегда
(`near = abs(price - catalyst) / catalyst <= _ENTRY_BAND_PCT`), кромочная — нет, хотя обе
эмитируют один и тот же лимит от уровня. Это и выдаёт пропуск за случайный, а не за решение.

⚠ ЧЕСТНО О ПРОИСХОЖДЕНИИ ЧИСЕЛ. Дефект установлен чтением кода и арифметикой границ, а НЕ
наблюдением кромочной ветки на живом рынке: в срезе 2026-07-27 по 8 символам × 5 ТФ она не
сработала ни разу (все 4 кандидата были `approaching`, что корректно уходит в `armed`).
Поэтому гард проверяет ИНВАРИАНТ структурно, а не воспроизводит сработку на фикстуре —
фикстура здесь доказывала бы только саму себя.

Второй слой защиты — `tracker.register_signal_open`, который с 2026-07-27 проверяет фил
независимо от того, что заявил продюсер (`tests/test_register_verifies_fill.py`).
"""
from __future__ import annotations

import ast
import pathlib

from hunt_core.contract import price_in_entry_zone

_SRC = pathlib.Path("hunt_core/prizrak/orchestrator.py")


def _activation_assignments(func_name: str) -> list[tuple[ast.expr, str]]:
    """Всё, что присваивается `summary["activation"]` внутри функции: (узел, исходник).

    Разбор по AST, а не grep: рядом лежит объяснительный комментарий, содержащий и
    `in_entry_zone`, и `near_entry`, — текстовый поиск был бы зелёным по нему.

    ⚠ Проверять надо ТИП УЗЛА, а не префикс строки. У тернарного выражения истинная ветка
    стоит ПЕРВОЙ, поэтому исходник корректной формы тоже начинается с `"in_entry_zone"` —
    первая редакция этого гарда на том и провалилась, объявив исправленный код дефектным.
    """
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[tuple[ast.expr, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value == "activation"
                ):
                    out.append((sub.value, ast.get_source_segment(src, sub.value) or ""))
    return out


def test_edge_candidate_activation_is_conditional() -> None:
    """Активация обязана вычисляться, а не назначаться константой."""
    found = _activation_assignments("_zone_edge_candidate")
    assert found, "_zone_edge_candidate больше не задаёт activation — проверь цепочку тира"
    for node, src in found:
        assert not isinstance(node, ast.Constant), (
            "activation снова объявляется безусловно — цена может быть на 35% вглубь бокса "
            "при полосе ±0.2%"
        )
        assert "price_in_entry_zone" in src, (
            "активация задаётся не проверкой попадания цены в зарегистрированную полосу"
        )


def test_retest_branch_keeps_its_own_discipline() -> None:
    """Ветка ретеста всегда проверяла попадание — она не должна это потерять."""
    found = _activation_assignments("_zone_candidate")
    assert found, "_zone_candidate больше не задаёт activation"
    for node, _src in found:
        assert not isinstance(node, ast.Constant), (
            "ветка ретеста стала объявлять активацию безусловно"
        )


def test_contract_reads_entry_zone_not_entry_lo_hi() -> None:
    """Форма аргумента, на которой я сам чуть не ошибся.

    `price_in_entry_zone` читает ключ `entry_zone`; `_base_summary` кладёт `entry_lo`/
    `entry_hi`. Передав summary напрямую, получишь False ВСЕГДА — кромочная ветка тихо
    станет вечным `near_entry`. Отказ в безопасную сторону, но такой же молчаливый.
    """
    summary_shape = {"entry_lo": 99.0, "entry_hi": 101.0}
    assert not price_in_entry_zone(summary_shape, price=100.0, direction="long")
    assert price_in_entry_zone(
        {"entry_zone": [99.0, 101.0]}, price=100.0, direction="long"
    )


def test_edge_candidate_passes_the_right_shape() -> None:
    """Проверка обязана идти по entry_zone, собранному из кромок summary."""
    expr = " ".join(src for _n, src in _activation_assignments("_zone_edge_candidate"))
    assert "entry_zone" in expr, (
        "в price_in_entry_zone передана не та форма — контракт вернёт False всегда"
    )
    assert "entry_lo" in expr and "entry_hi" in expr, (
        "полоса собирается не из кромок summary — проверка пойдёт по чужим числам"
    )
