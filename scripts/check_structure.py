"""Структурные проверки, которые живой прогон не заменяет.

Каталог `tests/` удалён 2026-07-27 по решению владельца, и решение обосновано замером: за
день проверка примитивов нашла сдвиг часов на 43.4 с, форминг-бар в 72% выдач, NATR и Aroon
в 100 раз, отравленный леджер — **903 теста не поймали ни одного**. Хуже, часть тестов
активно защищала дефекты (`test_prizrak_step0_render` требовал буквальный текст
`= "in_entry_zone"` ровно дважды, `test_indicator_reference_values` пинил `adx[0] == 0.0`
как «never null»), а контаминация леджера на 3423 строки БЫЛА ВЫЗВАНА тестами.

Но две проверки живой прогон не делает по построению — они про ГРАФ ИМПОРТОВ, а не про
поведение. Их сюда и перенесли:

1. **Граница модулей.** Ни один `research/backtest_*.py` не имеет права импортировать
   `hunt_core.prizrak`: бэктест покрывает ТОЛЬКО манипуляции, и прогон после правки призрака
   вернёт то же число — это отсутствие измерения, выданное за «регрессий нет». И два модуля
   не имеют права импортировать друг друга.
2. **Достижимость.** Каждый модуль `hunt_core/` обязан быть достижим по относительным
   импортам от точки входа. Недостижимый модуль неотличим от мёртвого кода, а покрытие и
   vulture его не ловят (замер 2026-07-26: 1913 строк, не исполнявшихся ни разу).

Запуск:
    uv run python scripts/check_structure.py
Возвращает ненулевой код при нарушении — годится для pre-commit и CI.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "research"
CORE = ROOT / "hunt_core"
# Подстроки, означающие «файл дотягивается до пути детекта/доставки манипуляций».
_MANIP_MARKERS = ("advance_manipulation_scales", "manipulation_delivery")
_ENTRYPOINTS = ("_cli.py", "__main__.py")


def _imports(path: pathlib.Path) -> set[str]:
    """Абсолютные и относительные импорты файла, приведённые к точечным именам."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module)
            # ⚠ `from hunt_core import clock, serde` кладёт САМИ МОДУЛИ в `names`, а не в
            # `module`. Первая редакция обходчика читала только `module` и объявила
            # недостижимыми clock.py и serde.py, которые импортирует половина дерева.
            # Ровно та ошибка, из-за которой прежний сканер мёртвого кода однажды удалил
            # живое: граф обязан идти и по именам тоже.
            base = node.module or ""
            for alias in node.names:
                out.add(f"{base}.{alias.name}" if base else alias.name)
    return out


def check_module_boundary() -> list[str]:
    """Бэктест не трогает призрака; модули не импортируют друг друга."""
    bad: list[str] = []
    backtests = sorted(RESEARCH.glob("backtest_*.py"))
    if not backtests:
        bad.append("research/backtest_*.py не найдено — граница проверяется вхолостую")
    for path in backtests:
        src = path.read_text(encoding="utf-8")
        if "hunt_core.prizrak" in src:
            bad.append(f"{path.name}: импортирует hunt_core.prizrak — бэктест покрывает ТОЛЬКО манипуляции")
        if not any(m in src for m in _MANIP_MARKERS):
            bad.append(f"{path.name}: не дотягивается до пути манипуляций ({_MANIP_MARKERS})")
    for mod, foreign in (("prizrak", "hunt_core.scanner"), ("scanner", "hunt_core.prizrak")):
        for path in (CORE / mod).rglob("*.py"):
            if foreign in path.read_text(encoding="utf-8"):
                bad.append(f"{path.relative_to(ROOT)}: импортирует {foreign} — модули независимы")
    return bad


def check_reachability() -> list[str]:
    """Каждый модуль hunt_core достижим от точки входа по относительным импортам."""
    seen: set[pathlib.Path] = set()
    queue = [CORE / e for e in _ENTRYPOINTS if (CORE / e).exists()]
    by_name = {
        p.relative_to(ROOT).with_suffix("").as_posix().replace("/", "."): p
        for p in CORE.rglob("*.py")
    }
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for name in _imports(cur):
            # Относительный импорт `from .x import y` даёт module="x" — разрешаем от пакета.
            for cand in (name, f"{cur.parent.relative_to(ROOT).as_posix().replace('/', '.')}.{name}"):
                target = by_name.get(cand) or by_name.get(f"{cand}.__init__")
                if target is not None and target not in seen:
                    queue.append(target)
                # ⚠ `__init__.py` КАЖДОГО пакета по пути исполняется при импорте подмодуля,
                # поэтому его импорты — тоже рёбра графа. Без этого модуль, который тянет
                # только `maps/__init__.py`, объявлялся мёртвым: так ложно всплыли
                # toolkit/forecast.py и toolkit/archetypes.py.
                parts = cand.split(".")
                for depth in range(1, len(parts)):
                    pkg = by_name.get(".".join(parts[:depth]) + ".__init__")
                    if pkg is not None and pkg not in seen:
                        queue.append(pkg)
    unreachable = sorted(
        p.relative_to(ROOT).as_posix()
        for p in CORE.rglob("*.py")
        if p not in seen and p.name != "__init__.py"
    )
    # Осознанные исключения — вторые точки входа, вызываемые вручную.
    allowed = {"hunt_core/engine/__main__.py"}
    return [f"недостижим от точки входа: {u}" for u in unreachable if u not in allowed]


def main() -> int:
    problems = check_module_boundary()
    reach = check_reachability()
    print(f"граница модулей: {'OK' if not problems else f'{len(problems)} нарушений'}")
    for p in problems:
        print(f"  ✗ {p}")
    print(f"достижимость:    {'OK' if not reach else f'{len(reach)} недостижимых'}")
    for r in reach[:20]:
        print(f"  ✗ {r}")
    if len(reach) > 20:
        print(f"  … ещё {len(reach) - 20}")
    return 1 if (problems or reach) else 0


if __name__ == "__main__":
    sys.exit(main())
