"""Публичные символы `hunt_core`, на которые НЕТ ни одной ссылки во всём дереве.

Зачем отдельный инструмент, когда есть vulture и покрытие.

* **Покрытие** читается как «недотестировано», а не «не исполняется». При 17% строк без прогона
  мёртвое неотличимо от непокрытого.
* **vulture при `conf 80`** не считает находкой публичную функцию, которую импортирует хотя бы
  один модуль — **даже если сам этот модуль не импортирует никто**. Так `features/fib.py` держал
  `fib_retracement_levels`, а `delivery_support.py` — шесть функций разом: их держал собственный
  `__all__`.
* **`tests/test_levels_reachability.py`** доказывает достижимость МОДУЛЯ от `__main__`. Внутрь
  живого модуля он не смотрит, поэтому `levels/levels.py` спокойно нёс 1483 неисполнявшиеся
  строки, а `deliver/geometry.py` — гео-вето, которое не могло сработать.

Этот свип отвечает на третий, самый дешёвый вопрос: **есть ли на символ хоть одна ссылка вне
файла, где он объявлен**. Ноль ссылок — почти всегда мёртвый код; исключения перечислены ниже и
требуют явного обоснования.

⚠ Инструмент ДИАГНОСТИЧЕСКИЙ, не гард. Он намеренно НЕ падает и НЕ входит в CI: ноль ссылок —
сильная улика, но не доказательство (символ может зваться через `getattr`, быть контрактом
сериализации или точкой входа плагина). Прежде чем удалять — открой код и проверь; массовое
удаление по этому списку один раз уже стоило восстановления 805 строк.

    uv run python scripts/dead_symbol_sweep.py            # весь hunt_core
    uv run python scripts/dead_symbol_sweep.py deliver    # только поддерево
"""
from __future__ import annotations

import ast
import collections
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "hunt_core"
# Ссылки ищем и за пределами пакета: тест, скрипт или research-прогон — тоже читатель.
SEARCH_ROOTS = ("hunt_core", "tests", "scripts", "research")

# Символы, у которых ноль ссылок ОСОЗНАННО. Каждая запись обязана нести причину — иначе список
# превратится в свалку, глушащую ровно те находки, ради которых свип написан.
ALLOWED: dict[str, str] = {
    "hunt_core/engine/__main__.py::main": (
        "вторая точка входа `python -m hunt_core.engine` — диагностика «двигает ли WS кадр»; "
        "вызывается человеком из терминала, ссылки в коде быть и не должно"
    ),
}


def _iter_py(root: pathlib.Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def collect_public_defs(subtree: str = "") -> dict[tuple[pathlib.Path, str], int]:
    out: dict[tuple[pathlib.Path, str], int] = {}
    base = PKG / subtree if subtree else PKG
    for path in _iter_py(base):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    out[(path, node.name)] = node.lineno
    return out


def load_corpus() -> dict[pathlib.Path, str]:
    corpus: dict[pathlib.Path, str] = {}
    for name in SEARCH_ROOTS:
        root = REPO / name
        if root.is_dir():
            for path in _iter_py(root):
                corpus[path] = path.read_text(encoding="utf-8")
    return corpus


def main() -> int:
    subtree = sys.argv[1] if len(sys.argv) > 1 else ""
    defs = collect_public_defs(subtree)
    corpus = load_corpus()

    dead: list[tuple[pathlib.Path, str, int]] = []
    for (path, name), lineno in sorted(defs.items()):
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if any(q != path and pattern.search(text) for q, text in corpus.items()):
            continue
        # Ссылка внутри своего же файла считается — кроме строки объявления и строкового
        # упоминания в `__all__` (экспорт сам по себе читателем не является: ровно на этом
        # vulture и слеп).
        own = [
            i
            for i, line in enumerate(corpus[path].splitlines(), 1)
            if pattern.search(line)
            and i != lineno
            and f'"{name}"' not in line
            and f"'{name}'" not in line
        ]
        if own:
            continue
        key = f"{path.relative_to(REPO)}::{name}"
        if key in ALLOWED:
            continue
        dead.append((path, name, lineno))

    if not dead:
        print("публичных символов без единой ссылки не найдено")
        return 0

    by_file: dict[pathlib.Path, list[tuple[str, int]]] = collections.defaultdict(list)
    for path, name, lineno in dead:
        by_file[path].append((name, lineno))

    print(f"ПУБЛИЧНЫХ СИМВОЛОВ БЕЗ ЕДИНОЙ ССЫЛКИ: {len(dead)} в {len(by_file)} файлах")
    print("(улика, не приговор — открой код прежде чем удалять)\n")
    for path in sorted(by_file):
        print(f"{path.relative_to(REPO)}:")
        for name, lineno in sorted(by_file[path]):
            print(f"    {name}  (:{lineno})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
