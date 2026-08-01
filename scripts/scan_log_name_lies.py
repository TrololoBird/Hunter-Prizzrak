"""Найти лог-сообщения об ОТКАЗЕ, стоящие на пути УСПЕХА — ложь в логе.

ЗАЧЕМ. 2026-08-01 в `runtime/cycle/_cycle_loop.py` найдено:

    from hunt_core.params.store import invalidate_calibration_cache   # импорт удался
    invalidate_calibration_cache()                                    # вызов выполнен
    LOG.debug("hunt_calibration_rebuild_skipped", reason="module_unavailable")

Строка печаталась при КАЖДОМ старте и лгала дважды: ничего не пропущено, модуль доступен.
Разбор в тот день начался с попытки понять, какой модуль недоступен, — то есть ложь стоила
реального времени. Молчание заставляет посмотреть; ложь заставляет посмотреть НЕ ТУДА,
и потому дороже.

Это частный случай name-lie — класса дефектов, который CLAUDE.md называет фирменным
(«имя врёт про содержимое»). Здесь имя события врёт про то, что произошло.

ЧТО ИЩЕТ СКАНЕР. Вызовы логгера, у которых первый аргумент (имя события) содержит слово
отказа, но сам вызов НЕ находится ни в одном `except`-блоке и не стоит после `raise`.

⚠ ЧЕГО ОН НЕ УМЕЕТ, и это надо знать. Отказ бывает законно обнаружен БЕЗ исключения:
проверка кода ответа, `if not ok:`, вернувшийся `None`. Такие места — законные, и сканер
пометит их тоже. Инструмент ДИАГНОСТИЧЕСКИЙ: он сужает 700 вызовов логгера до десятков,
дальше читает человек. Ноль находок доказывает отсутствие класса; ненулевое — повод открыть
файл, а не повод править.

    uv run python scripts/scan_log_name_lies.py
    uv run python scripts/scan_log_name_lies.py runtime engine
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hunt_core"

# Слова, которыми называют ОТКАЗ. Сопоставление ПОСЛОВНОЕ, а не подстрочное.
#
# ⚠ Первая редакция сравнивала подстроками и немедленно поймала саму себя: событие
# `hunt_calibration_cache_invalidated` (успех!) совпало со словом `invalid`. Инструмент,
# ищущий ложь в именах, не должен врать сам — отсюда разбиение имени на слова.
_FAILURE_WORDS = frozenset({
    "failed", "failure", "fail", "error", "errors", "unavailable", "missing",
    "skipped", "skip", "aborted", "abort", "denied", "rejected", "reject",
    "timeout", "timedout", "broken", "lost", "dropped", "drop", "stale",
    "invalid", "unreachable", "degraded", "disabled", "blocked",
})


def _event_words(event: str) -> set[str]:
    """Имя события → множество слов. Разделители — `_`, пробел, `|`, `-`, `:`."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in event.lower())
    return set(cleaned.split())
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}


def _is_log_call(node: ast.Call) -> str | None:
    """Имя события, если это вызов логгера с литеральным первым аргументом."""
    fn = node.func
    if not isinstance(fn, ast.Attribute) or fn.attr not in _LOG_ATTRS:
        return None
    owner = fn.value
    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    if "log" not in str(name).lower():
        return None
    if not node.args:
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


class _Scanner(ast.NodeVisitor):
    """Обходит дерево, помня глубину вложенности в `except` и `finally`."""

    def __init__(self) -> None:
        self.in_handler = 0
        self.hits: list[tuple[int, str, str]] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        self.in_handler += 1
        self.generic_visit(node)
        self.in_handler -= 1

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        for stmt in node.body:
            self.visit(stmt)
        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.orelse:
            self.visit(stmt)
        # `finally` исполняется и на успехе, и на отказе — сообщение об отказе там
        # утверждает больше, чем известно, поэтому считается подозрительным.
        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        event = _is_log_call(node)
        if event and not self.in_handler and (_event_words(event) & _FAILURE_WORDS):
            kwargs = ", ".join(f"{k.arg}=..." for k in node.keywords if k.arg)
            self.hits.append((node.lineno, event, kwargs))
        self.generic_visit(node)


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"  ! не разобран {path}: {exc.__class__.__name__}", file=sys.stderr)
        return []
    scanner = _Scanner()
    scanner.visit(tree)
    return scanner.hits


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
        print(f"\n{f.relative_to(ROOT.parent)}  ({len(hits)})")
        for lineno, event, kwargs in hits:
            tail = f"  [{kwargs}]" if kwargs else ""
            print(f"  {lineno:5d}  {event}{tail}")

    print(f"\n{'=' * 74}")
    print(f"файлов просмотрено: {len(files)}   с находками: {len(per_file)}   "
          f"сообщений об отказе вне except: {total}")
    print("Ноль = класса нет. Иначе — ОТКРЫВАТЬ КОД: отказ бывает законно обнаружен без")
    print("исключения (код ответа, `if not ok`, вернувшийся None), и такое место корректно.")
    print("Ложью является только сообщение об отказе НА ПУТИ УСПЕХА.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main([a for a in sys.argv[1:] if not a.startswith("-")]))
