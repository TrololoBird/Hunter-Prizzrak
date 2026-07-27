"""Гард: напечатанный ТВХ обязан быть той ценой, по которой построен план.

Дефект (замер на живых данных 2026-07-27, 10 символов × 4 ТФ). Карточка печатала
`ТВХ ★<цена>`, беря ключевую линию ордерной сетки — для ЛЮБОГО вида зоны. Но правило
анкеровки в `setups.py` разное:

* `_zone_view` (добор/шорт) — ключевая линия ДЕЙСТВИТЕЛЬНО вытесняет ПОК:
  `edge = key_px if key_px is not None else (poc … else кромка)`;
* `_perezakup_view` (перезакуп) — намеренно НЕТ. Якорь даёт ПОК крупной базы (курс стр.30),
  и функция это прямо документирует: сетка «показывает, как база дробится на ордера, но не
  переопределяет вход».

Форматтер применял одно правило к обоим видам. Результат: **21 из 21** перезакупа с
ключевой линией печатал ТВХ, которого модуль не считал (добор 8 из 8 и шорт 4 из 4 —
совпадали). Медиана разрыва 0.11%, максимум 1.29%. На XAG карточка показывала ★58.4968 при
посчитанном `entry` 58.678.

Почему это дороже, чем разница в 0.11%: `setups.py::_anchor`, цели и RR берут `entry`. То
есть весь план строился по одной цене, а читателю печаталась другая — и сверить их он не мог.

⚠ Это НЕ про неустойчивость ПОКа: у 15 из 21 расхождения `poc_unstable=False`. Флаг
бимодальности здесь ни при чём, расхождение систематическое.
"""
from __future__ import annotations

import ast
import pathlib


def _plan_zone_reads_entry() -> bool:
    """ТВХ обязан читаться из `zone["entry"]`, а не из `lines[key]`.

    Проверка по AST, а не по тексту: grep по исходнику ловил бы собственный комментарий
    этого гарда и был бы зелёным по ошибке.
    """
    src = pathlib.Path("hunt_core/prizrak/format_post.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "ТВХ ★" not in seg:
            continue
        # В функции, печатающей ТВХ, key_px обязан присваиваться из zone.get("entry").
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            targets = {t.id for t in sub.targets if isinstance(t, ast.Name)}
            if "key_px" not in targets:
                continue
            if "entry" in (ast.get_source_segment(src, sub.value) or ""):
                return True
    return False


def test_tvh_is_the_computed_entry_not_the_grid_line() -> None:
    """ТВХ печатается из посчитанного `entry`."""
    assert _plan_zone_reads_entry(), (
        "ТВХ снова берётся из ключевой линии сетки — у перезакупа это НЕ вход "
        "(setups.py::_perezakup_view, курс стр.30)"
    )


def test_perezakup_entry_ignores_the_grid_key_line() -> None:
    """Контракт `setups.py`: у перезакупа сетка не переопределяет вход, у добора — да.

    Если это правило однажды сделают одинаковым для обоих видов, форматтер надо менять
    вместе с ним — гард обязан упасть и потребовать решения, а не молча разойтись снова.
    """
    src = pathlib.Path("hunt_core/prizrak/setups.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {
        n.name: (ast.get_source_segment(src, n) or "")
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    perezakup = funcs.get("_perezakup_view", "")
    zone_view = funcs.get("_zone_view", "")
    assert perezakup, "_perezakup_view исчез — контракт ТВХ надо пересмотреть"
    assert 'anchor = poc if' in perezakup, (
        "_perezakup_view больше не якорится на ПОК — проверь, что печатает форматтер"
    )
    assert "key_px if key_px is not None" in zone_view, (
        "_zone_view больше не отдаёт приоритет ключевой линии — правила видов сошлись, "
        "форматтер надо привести в соответствие"
    )
