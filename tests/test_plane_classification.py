"""Гард границы «отказ данных vs штатная деградация» (`view/build.py::plane_is_required`).

Зачем тест, если правило — одна строка. Эта граница разъезжалась ДВАЖДЫ, и оба раза дорого:

1. 2026-07-22 (`7bec80c`) снёс продюсера `data_violations`, и `universe_health` перестал видеть
   блэкаут вообще — строка, собранная на ЗАМОРОЖЕННОМ кадре, считалась HEALTHY целый месяц.
2. При починке 2026-07-26 обратный перекос: если считать отказом КАЖДЫЙ незакрытый план, то на
   здоровой вселенной `failure_frac` = 1.0 → `critical` → `should_self_restart_on_blackout`
   уводит процесс в цикл перезапусков. Замер: `kline.*` в `not_ready` у 0% строк,
   `basis`/`liq`/`global_ls_5m`/`top_ls_*` — у 100%, `oi`/`taker_5m` — у 86%.

Оба раза дефект был не в формуле, а в том, что знание о границе жило КОПИЕЙ у потребителя.
Тест держит инвариант: каждый план, который тик реально запрашивает, классифицирован явно.
Fail-loud живёт ИМЕННО ЗДЕСЬ, а не в рантайме: `plane_is_required` намеренно консервативен и
неизвестное имя отказом не считает — иначе строка вида `"BTC/USDT:USDT: not tracked"` дала бы
ложный блэкаут и самоперезапуск. Забытая классификация обязана валить CI, а не прод.

⚠ Тест НЕ проверяет, верна ли классификация — это решают живые данные (директива пользователя
2026-07-25). Он проверяет только, что решение ПРИНЯТО и что копий знания больше нет.
"""
from __future__ import annotations

import ast
from pathlib import Path

from hunt_core.diagnostics.universe_health import classify_row_health
from hunt_core.view.build import (
    OPTIONAL_PLANES,
    REQUIRED_PLANE_ROOTS,
    plane_is_required,
    requested_planes,
)


def test_every_requested_plane_is_classified() -> None:
    """Каждый запрашиваемый план объявлен ЯВНО — обязательным либо необязательным.

    Это и есть fail-loud этой границы. В рантайме `plane_is_required` намеренно консервативен
    (неизвестное ≠ отказ, иначе ложный блэкаут и цикл перезапусков), поэтому «забыл
    классифицировать» обязано падать здесь, на этапе правки, а не тихо стать необязательным.
    """
    optional_roots = {p.split(".", 1)[0] for p in OPTIONAL_PLANES}
    unclassified = [
        plane
        for plane in requested_planes()
        if plane.split(".", 1)[0] not in optional_roots | REQUIRED_PLANE_ROOTS
    ]
    assert not unclassified, (
        f"Планы без явной классификации: {unclassified}. Добавь в `OPTIONAL_PLANES` "
        "(штатная деградация) либо в `REQUIRED_PLANE_ROOTS` с обоснованием, почему это ОТКАЗ."
    )


def test_required_and_optional_do_not_overlap() -> None:
    """Один план не может быть одновременно отказом и штатной деградацией."""
    overlap = {p.split(".", 1)[0] for p in OPTIONAL_PLANES} & REQUIRED_PLANE_ROOTS
    assert not overlap, f"план объявлен и обязательным, и необязательным: {sorted(overlap)}"


def test_klines_are_the_only_required_planes() -> None:
    """Кадры обязательны, всё остальное из запрашиваемого — нет."""
    required = [p for p in requested_planes() if plane_is_required(p)]
    assert required, "хотя бы один план обязан быть обязательным — иначе блэкаут не детектируется"
    assert all(p.startswith("kline") for p in required), (
        f"необязательный план объявлен отказом: {[p for p in required if not p.startswith('kline')]}"
    )


def test_unknown_plane_is_not_a_blackout() -> None:
    """Неизвестное имя НЕ объявляется отказом — иначе ложный блэкаут и самоперезапуск.

    Полнота классификации держится тестом выше, а не рантайм-вердиктом: неверный выбор здесь
    роняет прод в цикл перезапусков, а там — валит CI.
    """
    assert not plane_is_required("some_new_plane_nobody_classified")
    assert not plane_is_required("gls")  # имя из старых строк; продюсера в дереве нет


def test_universe_health_has_no_private_copy_of_the_boundary() -> None:
    """У детектора блэкаута не должно быть СВОЕГО списка планов.

    Именно копия знания (префиксный фильтр `_FAILING_PLANE_PREFIXES`) была механизмом обоих
    расхождений. Проверяем по исходнику: импорт есть, собственного списка нет.
    """
    src = Path("hunt_core/diagnostics/universe_health.py").read_text(encoding="utf-8")
    assert "from hunt_core.view.build import plane_is_required" in src

    # ⚠ Разбор через AST, а не grep по тексту. Первая редакция этого теста грепала сырой
    # исходник и падала на СОБСТВЕННОМ комментарии, объясняющем, почему копии больше нет.
    # Запрещать надо код, а не упоминание: история правки обязана оставаться читаемой.
    tree = ast.parse(src)
    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    for copy in ("_FAILING_PLANE_PREFIXES", "_DATA_ERROR_RE"):
        assert copy not in assigned, (
            f"вернулась локальная копия границы ({copy}) — источник истины один: `view/build.py`"
        )

    # Имя необязательного плана как СТРОКОВЫЙ ЛИТЕРАЛ в коде — заготовка следующей копии:
    # снесённый `_DATA_ERROR_RE` объявлял отказом `book|ticker|funding|oi`, все четыре
    # необязательные. Докстроки исключаем — они как раз должны это обсуждать.
    docstrings = {
        ast.get_docstring(n)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    literals = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docstrings
    }
    leaked = sorted(lit for lit in literals if lit in OPTIONAL_PLANES)
    assert not leaked, f"имена необязательных планов зашиты в детекторе блэкаута: {leaked}"


def test_native_violation_shapes_classify_as_measured() -> None:
    """Формы строк, реально виденные на живом прогоне 2026-07-26, дают ожидаемый вердикт."""
    assert classify_row_health({"data_violations": ["kline.4h: stale 40336224ms>36000000ms"]}) == "kline.4h.stale"
    assert classify_row_health({"data_violations": ["kline.1m: absent"]}) == "kline.1m.absent"
    # Необязательные — на здоровом прогоне встречались у 86–100% строк; отказом не считаются.
    for benign in ("basis: stale 406098ms>360000ms", "liq: stale 770470ms>60000ms",
                   "bbo: stale 5558ms>5000ms", "top_ls_pos_5m: absent"):
        assert classify_row_health({"data_violations": [benign]}) is None, benign
    # Смешанная строка: обязательный план найден среди необязательных.
    mixed = {"data_violations": ["liq: absent", "basis: absent", "kline.15m: absent"]}
    assert classify_row_health(mixed) == "kline.15m.absent"


def test_symbol_qualified_reason_is_not_mistaken_for_a_plane() -> None:
    """``"BTC/USDT:USDT: not tracked"`` — это символ, а не план с именем ``BTC/USDT``."""
    assert classify_row_health({"data_violations": ["BTC/USDT:USDT: not tracked"]}) is None
