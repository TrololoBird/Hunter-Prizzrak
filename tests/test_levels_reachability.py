"""Мёртвый код не должен возвращаться: каждый модуль `hunt_core` достижим от точки входа.

Почему тест написан именно так. 2026-07-26 из пакета вычистили 1913 строк, которые не
исполнялись ни разу; самая крупная часть — `levels/levels.py` (1575 → 92). Все проверки,
которые у проекта были, эту гниль пропустили:

* **покрытие** читалось как «недотестировано», а не как «не исполняется» (`levels.py` — 17%);
* **vulture** при `min_confidence = 80` не считает уверенной находкой публичную функцию,
  которую импортирует хотя бы один модуль, — даже если сам этот модуль не импортирует никто
  (`features/fib.py` держал `fib_retracement_levels` «живым» именно так);
* **CLAUDE.md** описывал каталог как действующую ответственность («чистая геометрия SL/TP+fib»).

Поэтому здесь проверяется не список имён, а СВОЙСТВО: граф импортов, построенный от реальных
точек входа (`hunt_core/__main__.py`, `hunt_core/_cli.py`), обязан покрывать весь пакет.
Новый недостижимый модуль роняет тест в тот же день, когда появился, а не через две недели.

⚠ Граф обязан учитывать ОТНОСИТЕЛЬНЫЕ импорты. Первая версия анализатора их не учла и объявила
мёртвым `features/microstructure.py` (805 строк), который жив через `from .microstructure import
add_microstructure_features` в `features/prepare_frame.py`. Удаление словили тесты; проверка,
смотрящая только на `from hunt_core.x.y import`, систематически врёт в большую сторону.
"""

from __future__ import annotations

import ast
import collections
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "hunt_core"
ENTRY_POINTS = ("hunt_core.__main__", "hunt_core._cli")

# Достижимы не от главной точки входа, и это ОСОЗНАННО. Список закрытый: всё, что в нём не
# перечислено и не достижимо, — гниль, и тест обязан упасть.
EXPECTED_UNREACHABLE = {
    # Самостоятельная точка входа: `python -m hunt_core.engine` — отладочный вертикальный срез
    # движка (стримит символы, печатает свежесть). Не часть бота по замыслу.
    "hunt_core.engine.__main__",
    # Построены под гейты движка (ADR-0003 E4a, S8), потребитель так и не подключён. Это не
    # гниль, а работа впереди спроса: код чистый, покрыт тестами, и `docs/engine/library-
    # adoption.md` называет guard-паттерн `funding_stats` эталоном надёжности проекта.
    # Решение — подключить или снять — принимать осознанно, а не молчанием.
    "hunt_core.engine.funding_stats",
    "hunt_core.engine.oi_stats",
}


def _module_name(path: pathlib.Path) -> str:
    name = str(path.relative_to(PKG.parent).with_suffix("")).replace("/", ".")
    return name[:-9] if name.endswith(".__init__") else name


def _import_graph() -> tuple[dict[str, pathlib.Path], dict[str, set[str]]]:
    mods = {
        _module_name(p): p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts
    }
    edges: dict[str, set[str]] = collections.defaultdict(set)

    for name, path in mods.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - защита от битого файла
            continue
        # Пакет, относительно которого разрешаются точки: для __init__.py это он сам.
        base = name if path.name == "__init__.py" else ".".join(name.split(".")[:-1])

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # относительный: `from .x import y`, `from ..x import y`
                    parts = base.split(".")
                    if node.level > 1:
                        parts = parts[: len(parts) - (node.level - 1)]
                    root = ".".join(parts)
                    target = f"{root}.{node.module}" if node.module else root
                elif node.module and node.module.startswith("hunt_core"):
                    target = node.module
                else:
                    continue
                edges[name].add(target)
                for alias in node.names:  # `from pkg import submodule`
                    edges[name].add(f"{target}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("hunt_core"):
                        edges[name].add(alias.name)

    return mods, edges


@pytest.fixture(scope="module")
def reachable() -> set[str]:
    mods, edges = _import_graph()
    seen: set[str] = set()
    stack = list(ENTRY_POINTS)
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for target in edges.get(mod, ()):
            if target in mods and target not in seen:
                stack.append(target)
            parent = ".".join(target.split(".")[:-1])
            if parent in mods and parent not in seen:
                stack.append(parent)
    return seen & set(mods)


def test_no_unexpected_unreachable_module(reachable: set[str]) -> None:
    """Каждый модуль пакета достижим от точки входа — или явно перечислен выше.

    Падение означает одно из двух: (а) появился мёртвый модуль — удаляй его сейчас, пока
    он не оброс тестами и упоминаниями в доках; (б) он мёртв осознанно — впиши его в
    `EXPECTED_UNREACHABLE` с причиной, чтобы решение осталось записанным.
    """
    mods, _ = _import_graph()
    unreachable = set(mods) - reachable - EXPECTED_UNREACHABLE
    assert not unreachable, (
        "недостижимые от точки входа модули: " + ", ".join(sorted(unreachable))
    )


def test_expected_unreachable_list_has_no_stale_entries(reachable: set[str]) -> None:
    """Список исключений не должен переживать свою причину.

    Если модуль из `EXPECTED_UNREACHABLE` подключили, запись обязана уйти — иначе список
    сам превращается в тот тип документации, который врёт молча.
    """
    mods, _ = _import_graph()
    revived = {m for m in EXPECTED_UNREACHABLE if m in reachable}
    missing = {m for m in EXPECTED_UNREACHABLE if m not in mods}
    assert not revived, f"подключены, убери из списка: {sorted(revived)}"
    assert not missing, f"в списке несуществующие модули: {sorted(missing)}"


def test_relative_imports_are_followed() -> None:
    """Граф обязан видеть `from .x import y` — иначе он объявляет мёртвым живое.

    Регрессионная защита: именно на этом первая версия анализатора «похоронила»
    `features/microstructure.py`, живой через `features/prepare_frame.py`.
    """
    _, edges = _import_graph()
    assert "hunt_core.features.microstructure" in edges["hunt_core.features.prepare_frame"]


def test_levels_package_is_only_the_two_sl_floors() -> None:
    """От `levels/` остались ровно два пола дистанции стопа — и один потребитель.

    Остальное (1483 строки) удалено 2026-07-26 как никогда не исполнявшееся. Внутри
    удалённого жила инверсия: `worst` анкерил ЛУЧШИЙ залив вопреки имени, завышая R:R.
    Если каталог снова начнёт расти — сверяй якорь худшего залива с каноном
    `hunt_core/contract.py::worst_entry_edge`, прежде чем на него опираться.
    """
    from hunt_core.levels import levels

    public = {n for n in vars(levels) if not n.startswith("_") and callable(vars(levels)[n])}
    assert public == {"long_min_sl_dist_pct", "short_min_sl_dist_pct"}, (
        f"в levels/ появилось новое публичное API: {sorted(public)}"
    )

    mtf = (PKG / "confluence" / "mtf.py").read_text(encoding="utf-8")
    assert "from hunt_core.levels.levels import" in mtf


@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC/USDT:USDT", "sol-usdt", "", "ЧТО-ТО"])
def test_sl_floors_are_positive_for_any_symbol(symbol: str) -> None:
    """Пол дистанции стопа строго положителен при любом символе.

    Ноль означал бы «стоп вплотную к входу»: риск нулевой, R:R бесконечный. Инвариант I-6 —
    неизвестный символ обязан падать в дефолт, а не в 0.0.
    """
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    assert long_min_sl_dist_pct(symbol) > 0.0
    assert short_min_sl_dist_pct(symbol) > 0.0


def test_sl_floor_symbol_normalization_is_spelling_agnostic() -> None:
    """Один инструмент в разных написаниях даёт один пол — включая unified-форму.

    Найдено 2026-07-26: нормализация снимала `-` и `/`, но не суффикс расчёта, поэтому
    `BTC/USDT:USDT` превращался в `BTCUSDT:USDT`, не совпадал ни с одним якорем и молча
    получал общий пол 1.0 вместо 0.4 — в 2.5 раза шире. Живого ущерба не было: единственный
    прод-вызов нормализует символ сам. Но корректность не должна зависеть от того, повторит
    ли этот шаг каждый следующий вызывающий, — тем более что `BTC/USDT:USDT` и есть
    канонический unified-формат проекта.
    """
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    for fn in (long_min_sl_dist_pct, short_min_sl_dist_pct):
        spellings = {fn(s) for s in ("BTCUSDT", "BTC/USDT:USDT", "BTC-USDT", "btc/usdt:usdt")}
        assert len(spellings) == 1, f"{fn.__name__} даёт разные полы одному символу: {spellings}"


def test_anchor_symbols_get_a_tighter_floor_than_the_rest() -> None:
    """У якорных мажоров пол строго уже общего — иначе ветка якорей мертва по смыслу."""
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    for fn in (long_min_sl_dist_pct, short_min_sl_dist_pct):
        assert fn("BTC/USDT:USDT") < fn("SOMECOINUSDT")
