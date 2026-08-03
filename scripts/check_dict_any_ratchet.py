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

⚠⚠ МЕТОДИКА ПОДСЧЁТА — ЗАФИКСИРОВАНА ЗДЕСЬ, ПОТОМУ ЧТО «СКОЛЬКО ИХ» ЗАВИСИТ ОТ ВОПРОСА.
Первая редакция этого храповика держала одно число (773) и не говорила, что именно
считает. Владелец, независимо считавший то же самое, получил ~910 — и оба числа были
верны, просто отвечали на разные вопросы. Замер 2026-08-03 на одном дереве:

    текстовых вхождений `dict[str, Any]`                     993
    AST, ЛЮБАЯ аннотация                                     988
      из них аннотации переменных и полей классов (AnnAssign) 215
    ТОЛЬКО сигнатуры функций (параметры + возврат)            773
    классов dataclass/BaseModel/NamedTuple/TypedDict           76

Расхождение «993 против 988» — комментарии и строки, куда AST не заглядывает.
Расхождение «988 против 773» — те самые 215 аннотаций переменных и полей.

И это была НАСТОЯЩАЯ ДЫРА, а не разница вкусов: сторожа только сигнатур достаточно,
чтобы протащить словарь мимо гейта, просто объявив его полем класса или переменной
модуля. Поэтому считаются ТРИ величины, и растёт ни одна из них:

  * ``signatures`` — параметры и возврат функций: межпакетный контракт в узком смысле;
  * ``public``     — то же, но только у функций с именем без ``_``;
  * ``annotations``— ВСЕ аннотации, включая поля классов и переменные: закрывает обход.

Не считается намеренно: строки и комментарии (не код), `Dict[str, Any]` из `typing`
(в дереве не используется), вложенные формы вроде `list[dict[str, Any]]` считаются —
обход идёт по всему дереву аннотации, а не по её верхнему узлу.

    uv run python scripts/check_dict_any_ratchet.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "hunt_core"

#: Замер 2026-08-03. Опускать вместе с типизацией; ПОДНИМАТЬ — только с обоснованием.
#: Параметры и возврат функций.
BASELINE_SIGNATURES = 773
#: Из них у функций с публичным именем — межпакетный контракт в узком смысле.
BASELINE_PUBLIC = 388
#: ВСЕ аннотации, включая поля классов и переменные. Держит обход через `x: dict[str, Any]`
#: вместо параметра — без этой величины гейт обходится объявлением поля.
BASELINE_ANNOTATIONS = 988


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


def count() -> tuple[int, int, int, list[str]]:
    """``(сигнатуры, публичные сигнатуры, все аннотации, худшие файлы)``."""
    signatures = 0
    public = 0
    annotations = 0
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
            # ВСЕ аннотации: считается каждый узел дерева, поэтому вложенные формы
            # (`list[dict[str, Any]]`, `dict[str, dict[str, Any]]`) не теряются.
            if _is_dict_str_any(node):
                annotations += 1
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            anns = [a.annotation for a in node.args.args + node.args.kwonlyargs if a.annotation]
            if node.returns is not None:
                anns.append(node.returns)
            hits = sum(1 for ann in anns for sub in ast.walk(ann) if _is_dict_str_any(sub))
            signatures += hits
            per_file += hits
            if hits and not node.name.startswith("_"):
                public += hits
        if per_file:
            worst.append((per_file, str(path.relative_to(ROOT))))
    worst.sort(reverse=True)
    return signatures, public, annotations, [f"{n:>4}  {p}" for n, p in worst[:10]]


def main() -> int:
    signatures, public, annotations, worst = count()
    checks = (
        ("сигнатуры функций", signatures, BASELINE_SIGNATURES),
        ("  из них публичных", public, BASELINE_PUBLIC),
        ("все аннотации", annotations, BASELINE_ANNOTATIONS),
    )
    for label, got, base in checks:
        print(f"{label:<20} {got:>5} (база {base})")

    grown = [(lbl, got, base) for lbl, got, base in checks if got > base]
    if grown:
        print("\nхудшие файлы (по сигнатурам):")
        for line in worst:
            print("  " + line)
        print("\nХРАПОВИК: выросло —")
        for lbl, got, base in grown:
            print(f"  {lbl.strip()}: +{got - base}")
        print(
            "Новый межпакетный контракт объявляется ТИПОМ (BaseModel/NamedTuple в domain/),\n"
            "а не словарём. Если рост осознан — поправьте BASELINE_* в этом файле и\n"
            "объясните в коммите, почему тип здесь неуместен.\n"
            "⚠ Растить `annotations`, не тронув `signatures`, — это обход гейта полем класса."
        )
        return 1

    shrunk = [(lbl, got, base) for lbl, got, base in checks if got < base]
    if shrunk:
        print("\nчисло СНИЗИЛОСЬ:")
        for lbl, got, base in shrunk:
            print(f"  {lbl.strip()}: −{base - got}")
        print(
            "опустите BASELINE_* до новых значений, иначе храповик перестанет держать\n"
            "достигнутое (снижение не роняет сборку: типизация не должна ломать CI до\n"
            "правки базы)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
