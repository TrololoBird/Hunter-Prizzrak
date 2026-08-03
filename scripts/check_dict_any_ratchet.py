"""Храповик на ``dict[str, Any]`` в сигнатурах: число не растёт.

ЗАЧЕМ. ADR-0004 переводил тик и глубокую полосу на типизированный позвоночник
(`MarketView`, `NativeAnalystView`), и это уже окупилось: `row: dict[str, Any]` из
тик-пути исчез. Но по дереву нетипизированных словарей всё ещё **773** против
горстки типизированных моделей, и каждый из них — место, где опечатка в ключе
становится молчаливым `None` вместо ошибки. Это тот же корень, что фантомные ключи
(I-6): словарь не знает, какие ключи в нём законны.

Разом это не чинится и чиниться не должно — правка типов и правка поведения обязаны
жить в разных коммитах. Поэтому не запрет, а ХРАПОВИК: текущее число зафиксировано
как потолок, вниз двигать можно свободно, вверх — только осознанно, поправив базу
в этом файле и объяснив в коммите.

⚠ ПОЧЕМУ ЧИСЛО, А НЕ СПИСОК. Список сайтов пришлось бы обновлять при каждом
переносе строки, и он превратился бы в шум, который перестают читать. Число
меняется только когда меняется СУТЬ.

    uv run python scripts/check_dict_any_ratchet.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "hunt_core"

#: Замер 2026-08-03. Опускать вместе с типизацией; ПОДНИМАТЬ — только с обоснованием.
BASELINE_TOTAL = 773
#: Из них в функциях с публичным именем — то есть в межпакетном контракте.
BASELINE_PUBLIC = 388


def _is_dict_str_any(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    if not (isinstance(value, ast.Name) and value.id == "dict"):
        return False
    sl = node.slice
    if not isinstance(sl, ast.Tuple) or len(sl.elts) != 2:
        return False
    key, val = sl.elts
    return (
        isinstance(key, ast.Name)
        and key.id == "str"
        and isinstance(val, ast.Name)
        and val.id == "Any"
    )


def count() -> tuple[int, int, list[str]]:
    total = 0
    public = 0
    worst: list[tuple[int, str]] = []
    for path in sorted(CORE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        per_file = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            annotations = [
                a.annotation for a in node.args.args + node.args.kwonlyargs if a.annotation
            ]
            if node.returns is not None:
                annotations.append(node.returns)
            hits = sum(
                1 for ann in annotations for sub in ast.walk(ann) if _is_dict_str_any(sub)
            )
            total += hits
            per_file += hits
            if hits and not node.name.startswith("_"):
                public += hits
        if per_file:
            worst.append((per_file, str(path.relative_to(ROOT))))
    worst.sort(reverse=True)
    return total, public, [f"{n:>4}  {p}" for n, p in worst[:10]]


def main() -> int:
    total, public, worst = count()
    print(f"dict[str, Any] в сигнатурах: {total} (база {BASELINE_TOTAL})")
    print(f"  из них публичных:          {public} (база {BASELINE_PUBLIC})")
    if total > BASELINE_TOTAL or public > BASELINE_PUBLIC:
        print("\nхудшие файлы:")
        for line in worst:
            print("  " + line)
        print(
            f"\nХРАПОВИК: число ВЫРОСЛО (+{total - BASELINE_TOTAL} всего, "
            f"+{public - BASELINE_PUBLIC} публичных).\n"
            "Новый межпакетный контракт объявляется типом (BaseModel/NamedTuple в domain/),\n"
            "а не словарём. Если рост осознан — поправьте BASELINE_* в этом файле и\n"
            "объясните в коммите, почему тип здесь неуместен."
        )
        return 1
    if total < BASELINE_TOTAL or public < BASELINE_PUBLIC:
        print(
            f"\nчисло СНИЗИЛОСЬ (−{BASELINE_TOTAL - total} всего, "
            f"−{BASELINE_PUBLIC - public} публичных) — опустите BASELINE_* до новых значений,\n"
            "иначе храповик перестанет держать достигнутое."
        )
        # Снижение не ошибка сборки: иначе типизация ломала бы CI до правки базы.
    return 0


if __name__ == "__main__":
    sys.exit(main())
