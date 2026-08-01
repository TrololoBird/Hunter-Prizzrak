"""Найти вызовы ``max()``/``min()`` по итерируемому БЕЗ ``default=`` — падение на пустом входе.

ЗАЧЕМ. Живой прогон 2026-08-01 дал 7 отказов тика вида
``ValueError('max() iterable argument is empty')`` по XAUUSDT/XAGUSDT: символ выпадал из
тика ЦЕЛИКОМ. Это не деградация, а отказ, и он приходит от одной незакрытой ветки —
пустая последовательность там, где код молча предполагает непустую.

Почему не grep. ``max(a, b)`` (несколько аргументов) на пустом входе невозможен и никогда
не падает; падает только форма с ОДНИМ аргументом-итерируемым: ``max(seq)``,
``max(x for x in ...)``, ``max([...])``. Отличить их текстом нельзя — нужен разбор,
поэтому здесь AST, а не регулярка. Ровно на этом различии grep даёт кратный шум.

Что НЕ считается находкой:
  * ``max(a, b, c)`` — форма с несколькими аргументами;
  * есть ``default=`` — автор уже подумал о пустом;
  * литерал непустой последовательности (``max([1, 2])``, ``max((a, b))``) — пустым не бывает.

Инструмент ДИАГНОСТИЧЕСКИЙ, не гард: непустота часто гарантирована выше по коду, и такое
место — ложная тревога. Ноль находок доказывает отсутствие класса; ненулевое требует чтения.

    uv run python scripts/scan_empty_minmax.py                # всё дерево hunt_core
    uv run python scripts/scan_empty_minmax.py prizrak toolkit  # поддеревья
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hunt_core"


def _is_nonempty_literal(node: ast.expr) -> bool:
    """Литерал, который заведомо непуст (``[1, 2]``, ``(a, b)``, ``{x}``)."""
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return len(node.elts) > 0
    return False


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"  ! не разобран {path}: {exc.__class__.__name__}", file=sys.stderr)
        return []
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else ""
        if name not in {"max", "min"}:
            continue
        if len(node.args) != 1:
            continue  # многоаргументная форма — на пустом невозможна
        if any(kw.arg == "default" for kw in node.keywords):
            continue  # автор уже закрыл пустой случай
        arg = node.args[0]
        if _is_nonempty_literal(arg):
            continue
        try:
            src = ast.unparse(node)
        except Exception as exc:  # noqa: BLE001 — нечитаемый узел не должен ронять свип
            src = f"<не восстановлено: {exc.__class__.__name__}>"
        out.append((node.lineno, name, src[:110]))
    return out


def main(subtrees: list[str]) -> int:
    roots = [ROOT / s for s in subtrees] if subtrees else [ROOT]
    files: list[Path] = []
    for r in roots:
        if not r.exists():
            print(f"нет такого поддерева: {r}", file=sys.stderr)
            return 2
        files.extend(sorted(r.rglob("*.py")))

    total = 0
    per_file: dict[Path, list[tuple[int, str, str]]] = {}
    for f in files:
        hits = scan_file(f)
        if hits:
            per_file[f] = hits
            total += len(hits)

    for f, hits in sorted(per_file.items(), key=lambda kv: -len(kv[1])):
        rel = f.relative_to(ROOT.parent)
        print(f"\n{rel}  ({len(hits)})")
        for lineno, name, src in hits:
            print(f"  {lineno:5d}  {name}  {src}")

    print(f"\n{'=' * 70}")
    print(f"файлов просмотрено: {len(files)}   файлов с находками: {len(per_file)}   "
          f"вызовов без default=: {total}")
    print("Ноль = класса нет. Иначе — открывать код: непустота часто гарантирована выше,")
    print("и такое место ложная тревога. Инструмент диагностический, в CI не входит.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main([a for a in sys.argv[1:] if not a.startswith("-")]))
