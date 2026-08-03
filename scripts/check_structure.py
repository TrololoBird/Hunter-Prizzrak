"""Структурные проверки, которые живой прогон не заменяет.

Каталог `tests/` удалён 2026-07-27 по решению владельца, и решение обосновано замером: за
день проверка примитивов нашла сдвиг часов на 43.4 с, форминг-бар в 72% выдач, NATR и Aroon
в 100 раз, отравленный леджер — **903 теста не поймали ни одного**. Хуже, часть тестов
активно защищала дефекты (`test_prizrak_step0_render` требовал буквальный текст
`= "in_entry_zone"` ровно дважды, `test_indicator_reference_values` пинил `adx[0] == 0.0`
как «never null»), а контаминация леджера на 3423 строки БЫЛА ВЫЗВАНА тестами.

Но одну проверку живой прогон не делает по построению — она про ГРАФ ИМПОРТОВ, а не про
поведение. Её сюда и перенесли:

**Достижимость.** Каждый модуль `hunt_core/` обязан быть достижим по относительным
импортам от точки входа. Недостижимый модуль неотличим от мёртвого кода, а покрытие и
vulture его не ловят (замер 2026-07-26: 1913 строк, не исполнявшихся ни разу).

⚠ Проверка «граница модулей» снята 2026-07-31 вместе с модулем МАНИПУЛЯЦИИ. Она следила,
что `research/backtest_*.py` не тянет призрака и что два модуля не импортируют друг друга;
модуль остался ОДИН, границы больше нет, и проверка выродилась в тавтологию. Гейт, которому
нечего запрещать, не гейт — он лишь создаёт видимость контроля. Сама достижимость свою
работу сделала при вырезе: поймала `data/baseline_store.py`, ставший писателем без читателей
(единственным читателем был `scanner/detect/expansion_readiness.py`).

Запуск:
    uv run python scripts/check_structure.py
Возвращает ненулевой код при нарушении — годится для pre-commit и CI.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "hunt_core"
_ENTRYPOINTS = ("_cli.py", "__main__.py")


def _resolve_relative(pkg: str, level: int, module: str | None) -> str | None:
    """``from ..x import y`` внутри пакета ``a.b.c`` → ``a.b.x``.

    ⚠ УРОВЕНЬ ОТНОСИТЕЛЬНОСТИ ЧИТАЕТСЯ ИЗ AST, А НЕ УГАДЫВАЕТСЯ. До 2026-08-02 обходчик
    брал только ``node.module`` и пробовал два кандидата: голое имя и «имя от родительского
    пакета». Для ``from .x import y`` это случайно совпадало с истиной, а для ``from ..x``
    давало мимо: модуль, достижимый ТОЛЬКО таким импортом, был бы объявлен мёртвым. Именно
    такая ошибка (граф, не знающий про относительные импорты) однажды уже привела к
    удалению живого кода. Сейчас в дереве 3 файла с ``from ..`` — они достижимы и другими
    путями, поэтому ложного срабатывания ещё не случилось; это везение, а не свойство.
    """
    if level == 0:
        return module or ""
    parts = pkg.split(".") if pkg else []
    if level - 1 > len(parts):
        return None  # импорт выше корня пакета — такого модуля не существует
    base_parts = parts[: len(parts) - (level - 1)]
    if module:
        base_parts = [*base_parts, *module.split(".")]
    return ".".join(base_parts)


def _imports(path: pathlib.Path) -> set[str]:
    """Импорты файла, приведённые к АБСОЛЮТНЫМ точечным именам."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    pkg = path.parent.relative_to(ROOT).as_posix().replace("/", ".")
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(pkg, node.level, node.module)
            if base is None:
                continue
            if base:
                out.add(base)
            # ⚠ `from hunt_core import clock, serde` кладёт САМИ МОДУЛИ в `names`, а не в
            # `module`. Первая редакция обходчика читала только `module` и объявила
            # недостижимыми clock.py и serde.py, которые импортирует половина дерева.
            # Ровно та ошибка, из-за которой прежний сканер мёртвого кода однажды удалил
            # живое: граф обязан идти и по именам тоже.
            for alias in node.names:
                out.add(f"{base}.{alias.name}" if base else alias.name)
    return out


def check_reachability(coverage: dict[str, int] | None = None) -> list[str]:
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
        for cand in _imports(cur):
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
    all_modules = list(CORE.rglob("*.py"))
    unreachable = sorted(
        p.relative_to(ROOT).as_posix()
        for p in all_modules
        if p not in seen and p.name != "__init__.py"
    )
    # Осознанные исключения — вторые точки входа, вызываемые вручную.
    allowed = {"hunt_core/engine/__main__.py"}
    if coverage is not None:
        coverage["total"] = len(all_modules)
        coverage["visited"] = len(seen)
        coverage["entrypoints"] = sum(1 for e in _ENTRYPOINTS if (CORE / e).exists())
    return [f"недостижим от точки входа: {u}" for u in unreachable if u not in allowed]


def main() -> int:
    # ⚠ ОХВАТ — ЧАСТЬ ОТВЕТА, А НЕ УКРАШЕНИЕ. «Нарушений нет» без числа просмотренных
    # объектов неотличимо от «ничего не просмотрено»: заниженный охват выглядит как чистый
    # результат. Здесь это уже кусалось — сканер, не знавший про относительные импорты,
    # объявлял живое мёртвым, и наоборот.
    coverage: dict[str, int] = {}
    reach = check_reachability(coverage)
    print(
        f"достижимость: {'OK' if not reach else f'{len(reach)} недостижимых'} "
        f"(обойдено {coverage.get('visited', 0)} из {coverage.get('total', 0)} модулей "
        f"от {coverage.get('entrypoints', 0)} точек входа)"
    )
    for r in reach[:20]:
        print(f"  ✗ {r}")
    if len(reach) > 20:
        print(f"  … ещё {len(reach) - 20}")
    return 1 if reach else 0


if __name__ == "__main__":
    sys.exit(main())
