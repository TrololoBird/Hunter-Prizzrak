"""Смоук-импорт: каждый модуль `hunt_core` обязан импортироваться.

ЗАЧЕМ. Гейты проекта не ловят удаление живого модуля — это измерено, а не предположено.
При вырезе модуля манипуляций 2026-07-31 `deliver/digest.py` был снесён целиком, хотя
главный тик импортирует из него `get_advisory_digest`. ruff, vulture, mypy и
`check_structure.py` прошли ЗЕЛЁНЫМИ: mypy молчал из-за глобального
`ignore_missing_imports = true`, остальные такое не ловят по построению. Поймал только
живой прогон `watch --once`, упав на `ModuleNotFoundError`.

⚠ ПОЧЕМУ МОДУЛИ, А НЕ ПАКЕТЫ. Импорт одних только `__init__.py` слабее: пакет
импортируется, а пропавший подмодуль внутри него замечен не будет, пока кто-то не позовёт
функцию с отложенным импортом (а таких обходов циклов в дереве 204). Обходим ВСЕ `*.py`.

⚠ ЧТО ЭТА ПРОВЕРКА НЕ ЗНАЧИТ. Успешный импорт не означает, что бот стартует: отложенные
импорты внутри функций исполняются только при вызове. Это нижняя граница, а не гарантия;
верхняя остаётся за живым прогоном `watch --once --no-telegram`.

    uv run python scripts/check_imports.py
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "hunt_core"
sys.path.insert(0, str(ROOT))


def module_names() -> list[str]:
    """Все импортируемые имена модулей под `hunt_core`, в детерминированном порядке."""
    names: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not parts:
            continue
        # `__main__.py` при импорте исполняет точку входа — его пропускаем осознанно.
        if parts[-1] == "__main__":
            continue
        names.append(".".join(parts))
    return names


def main() -> int:
    names = module_names()
    failures: list[tuple[str, str]] = []
    for name in names:
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001 — падение ЛЮБОГО рода здесь есть находка
            failures.append((name, traceback.format_exc()))

    if failures:
        for name, tb in failures:
            print(f"\n=== НЕ ИМПОРТИРУЕТСЯ: {name} ===")
            print(tb.rstrip())
        print(f"\nсмоук-импорт: ПРОВАЛ — {len(failures)} из {len(names)} модулей")
        return 1

    # Охват печатается всегда: молчаливое «OK» без числа неотличимо от «ничего не просмотрено»
    # (тот же урок, что и в check_structure.py).
    print(f"смоук-импорт: OK (импортировано {len(names)} модулей hunt_core)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
