"""`hunt_core/levels/` почти целиком недостижим из прода — этот факт зафиксирован механически.

Как это выяснилось (2026-07-26). Ревью показало покрытие `levels/levels.py` в 17% и
рекомендовало «закрыть тестами чистую геометрию». При написании тех тестов независимый
пересчёт R:R разошёлся с полем модуля (2.72 против 1.97), и разбор показал корень:

    levels.py:1191   worst = entry_lo  # long fills at the bottom of the zone
    levels.py:979    worst = entry_hi  # short fills at the top of the zone

Оба комментария утверждают обратное истине: худший залив лонга — это ВЕРХ полосы (заплатил
больше), шорта — НИЗ (продал дешевле). То есть `worst` анкерит ЛУЧШИЙ залив, завышая и
печатаемый R:R, и полы TP, и вето `sl_nominal_too_wide`. Канон в
`hunt_core/contract.py::worst_entry_edge` описывает ровно эту инверсию как УЖЕ однажды
исправленную — здесь она пережила ту правку.

Живого ущерба нет, и вот почему: **у обоих построителей нет ни одного вызова в проде.**
Замер тем же днём — 863 из 1563 строк `levels.py` без потребителя, плюс приватные хелперы,
которые обслуживают только их. Поэтому тестами это покрывать НЕЛЬЗЯ: тест на мёртвый код
цементирует его и создаёт ложное впечатление проверенной геометрии — та самая «ложная
безопасность», против которой написан `tests/test_module_boundary.py`.

Вместо тестов геометрии здесь зафиксирована ДОСТИЖИМОСТЬ. Если функция из списка мёртвых
получит вызов, тест упадёт, и тот, кто её оживляет, обязан сперва починить инверсию `worst`.
Если наоборот — мёртвое удалят, список сократится осознанно, а не молча.

Почему этого не поймал vulture: он гоняется с `min_confidence = 80`, а публичная функция,
импортируемая хотя бы одним модулем (`features/fib.py` импортирует `fib_retracement_levels`
и сам при этом никем не импортируется), уверенной находкой не считается.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "hunt_core"

# Единственный живой вход в пакет на 2026-07-26 — confluence/mtf.py.
LIVE_SURFACE = {"long_min_sl_dist_pct", "short_min_sl_dist_pct"}

# Публичные функции levels/ без единого вызова из hunt_core/ на 2026-07-26.
UNREACHABLE = {
    "adaptive_level_params",
    "apply_liquidity_tp_ladder_long",
    "apply_liquidity_tp_ladder_short",
    "build_liquidity_context",
    "continuation_short_targets",
    "reanchor_setup_levels",
    "resolve_volume_profile_from_parts",
    "structural_long_levels",
    "structural_short_levels",
}


def _production_sources() -> list[pathlib.Path]:
    """Все .py пакета, КРОМЕ самого levels/ (внутренние вызовы не делают функцию живой)."""
    return [
        p
        for p in PKG.rglob("*.py")
        if "__pycache__" not in p.parts and "levels" not in p.relative_to(PKG).parts
    ]


def _called_names(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - защита от битого файла
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


@pytest.fixture(scope="module")
def production_calls() -> set[str]:
    calls: set[str] = set()
    for p in _production_sources():
        calls |= _called_names(p)
    return calls


@pytest.mark.parametrize("name", sorted(UNREACHABLE))
def test_dead_level_builder_is_still_uncalled(name: str, production_calls: set[str]) -> None:
    """Мёртвая функция levels/ не должна получить вызов, пока в ней живёт инверсия `worst`.

    Падение здесь — не «сломался тест». Это значит: код оживили, и прежде чем на него
    опираться, надо починить анкер худшего залива (`levels.py:979` и `:1191`) и сверить
    его с `hunt_core/contract.py::worst_entry_edge`.
    """
    assert name not in production_calls, (
        f"`{name}` получила вызов в проде. Прежде чем на неё опираться: в levels.py "
        f"`worst` анкерит ЛУЧШИЙ залив (long→entry_lo, short→entry_hi), что завышает R:R "
        f"и ослабляет вето sl_nominal_too_wide. Канон — contract.py::worst_entry_edge."
    )


def test_live_surface_is_exactly_the_two_sl_floors(production_calls: set[str]) -> None:
    """Живой вход в levels/ — ровно два пола дистанции стопа, и только из confluence/mtf.py.

    Если появится третий, список выше пора пересматривать вместе с описанием каталога
    `levels/` в CLAUDE.md — оно утверждает «чистая геометрия SL/TP+fib» как действующую
    ответственность, что на 2026-07-26 неверно.
    """
    mtf = (PKG / "confluence" / "mtf.py").read_text(encoding="utf-8")
    assert "from hunt_core.levels.levels import" in mtf
    for fn in LIVE_SURFACE:
        assert fn in mtf, f"{fn} перестала импортироваться в mtf.py — обнови LIVE_SURFACE"
        assert fn in production_calls


@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC/USDT:USDT", "sol-usdt", "", "ЧТО-ТО"])
def test_sl_floors_are_positive_for_any_symbol(symbol: str) -> None:
    """Пол дистанции стопа обязан быть строго положительным при любом символе.

    Ноль здесь означал бы «стоп вплотную к входу» — риск нулевой, R:R бесконечный.
    Это инвариант I-6: неизвестный символ должен падать в дефолт, а не в 0.0.
    """
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    assert long_min_sl_dist_pct(symbol) > 0.0
    assert short_min_sl_dist_pct(symbol) > 0.0


def test_sl_floor_symbol_normalization_is_spelling_agnostic() -> None:
    """Один инструмент в разных записях обязан давать один пол — включая unified-форму.

    Найдено этим тестом 2026-07-26: нормализация снимала `-` и `/`, но НЕ суффикс
    расчёта, поэтому `BTC/USDT:USDT` превращался в `BTCUSDT:USDT`, не совпадал ни с одним
    якорем и молча получал общий пол (1.0 вместо 0.4 — в 2.5 раза шире).

    Живого ущерба не было: единственный прод-вызов,
    `confluence/mtf.py::build_mtf_confluence_native`, нормализует символ сам. Но это
    делало корректность заложницей того, что КАЖДЫЙ будущий вызывающий повторит тот шаг,
    — при том что `BTC/USDT:USDT` и есть канонический unified-формат проекта.
    """
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    for fn in (long_min_sl_dist_pct, short_min_sl_dist_pct):
        spellings = {fn(s) for s in ("BTCUSDT", "BTC/USDT:USDT", "BTC-USDT", "btc/usdt:usdt")}
        assert len(spellings) == 1, f"{fn.__name__} даёт разные полы для одного символа: {spellings}"


def test_anchor_symbols_get_a_tighter_floor_than_the_rest() -> None:
    """У якорных мажоров пол обязан быть СТРОЖЕ общего, иначе список якорей бессмыслен.

    Это и есть смысл `_ANCHOR_SYMBOLS`: BTC/ETH/XAU/XAG ходят спокойнее, поэтому им
    позволен более узкий номинальный стоп. Если полы сравняются, ветка станет мёртвой.
    """
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    for fn in (long_min_sl_dist_pct, short_min_sl_dist_pct):
        assert fn("BTC/USDT:USDT") < fn("SOMECOINUSDT")
