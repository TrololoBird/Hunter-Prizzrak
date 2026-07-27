"""Каждая секция `config.defaults.toml` обязана ДОХОДИТЬ до читателя — проверяется вызовом.

Зачем. Файл озаглавлен «canonical source of truth», но замер 2026-07-26 показал, что **63 из 158
ключей (40%) не влияли ни на что**: правка была молчаливым no-op. Загрузчик
(`domain/config.py::load_config_defaults_toml`) форвардил ровно три секции из шестнадцати, а
остальные читались либо своим путём, либо никак.

Почему прежних защит не хватило:

* **комментарий в файле не гарантия.** Семь секций несли пометку `DOC-ONLY`, и она была
  приблизительной в ОБЕ стороны: `[watch.prescan]` объявлен мёртвым вместе со всем `[watch]`, а
  его ключи упоминаются в Python — но значения всё равно не доходят, потому что `[watch]` не
  форвардится и побеждают инлайн-дефолты `prescan_thresholds`;
* **грепа по имени тоже не хватает.** Имя ключа в Python ≠ достижимость значения. Ровно так
  `[levels.adaptive]` считался живым: `levels_thresholds()` его читала, но саму функцию звал
  только `adaptive_level_params`, снесённый как неисполнявшийся. Секция и функция удалены.

Поэтому тест не грепает, а **вызывает настоящего читателя и сверяет значение**. Реестр ниже —
единственное место, где записано, КАК секция доходит до кода; секция без записи в реестре роняет
тест, поэтому новая инертная секция не появится незаметно.

Задуманные, но не подключённые настройки живут в `docs/config-intended.md`. Подключение любой из
них меняет поведение эмиссии и требует замера, а не просто провода.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Callable

import pytest

from hunt_core.domain.config import _DEFAULTS_PATH, _load_toml

REPO = pathlib.Path(__file__).resolve().parents[1]


def _hunter() -> Any:
    from hunt_core.domain.config import load_config_defaults_toml

    return load_config_defaults_toml().get("hunter", {}).get("watchlist_limit")


def _tracker() -> Any:
    from hunt_core.domain.config import load_config_defaults_toml

    return load_config_defaults_toml().get("tracker", {}).get("min_trail_mfe_pct")


def _maps() -> Any:
    from hunt_core.maps.config import load_maps_config

    return load_maps_config().vp_buckets


def _analyst() -> Any:
    from hunt_core.prizrak.engines.config import load_analyst_config

    return getattr(load_analyst_config(), "signal_queue_top_n", None)


def _pinned() -> Any:
    from hunt_core.data.universe import PINNED_SYMBOLS

    return len(PINNED_SYMBOLS)


def _confirm_short() -> Any:
    from hunt_core.domain.config import load_config_defaults_toml

    return load_config_defaults_toml().get("gates", {}).get("confirm_min_score")


# section → (как достаётся значение, путь до эталона в TOML, как привести эталон к виду читателя)
READERS: dict[str, tuple[Callable[[], Any], tuple[str, ...], Callable[[Any], Any]]] = {
    "hunter": (_hunter, ("hunter", "watchlist_limit"), lambda v: v),
    "tracker": (_tracker, ("tracker", "min_trail_mfe_pct"), lambda v: v),
    "maps": (_maps, ("maps", "vp_buckets"), lambda v: v),
    "analyst": (_analyst, ("analyst", "signal_queue_top_n"), lambda v: v),
    # `[pinned.defaults].symbols` доходит списком — сверяем длину, а не порядок.
    "pinned": (_pinned, ("pinned", "defaults", "symbols"), len),
    # `[confirm.short]` форвардится в секцию `gates` С ПЕРЕИМЕНОВАНИЕМ ключа
    # (`min_score` → `confirm_min_score`); потребитель — `runtime/stats_report.py`.
    "confirm": (_confirm_short, ("confirm", "short", "min_score"), lambda v: v),
}


def _dig(raw: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = raw
    for part in path:
        assert isinstance(cur, dict), f"путь {path} обрывается на {part}"
        cur = cur[part]
    return cur


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    return _load_toml(_DEFAULTS_PATH)


@pytest.mark.parametrize("section", sorted(READERS))
def test_section_value_reaches_its_reader(section: str, raw: dict[str, Any]) -> None:
    """Значение из TOML доходит до читателя — проверяется вызовом, а не грепом.

    Падение означает, что правка этой секции стала молчаливым no-op: файл выглядит настройкой,
    а поведение задают инлайн-дефолты. Это тот же дефект, что закрывали 2026-07-26.
    """
    reader, path, norm = READERS[section]
    expected = norm(_dig(raw, path))
    actual = reader()
    assert actual == expected, (
        f"[{section}] {'.'.join(path)} = {expected!r}, но читатель вернул {actual!r} — "
        f"значение из TOML до кода НЕ доходит"
    )


def test_no_section_without_a_registered_reader(raw: dict[str, Any]) -> None:
    """В живом файле нет секций, для которых не записано, кто их читает.

    Новая секция обязана прийти вместе с записью в `READERS` — то есть автор должен показать
    читателя. Иначе она немедленно становится тем, чем были снесённые семь: настройкой на вид.
    """
    unknown = sorted(set(raw) - set(READERS))
    assert not unknown, (
        f"секции без зарегистрированного читателя: {unknown}. Либо проведите их до кода и "
        f"добавьте в READERS, либо перенесите в docs/config-intended.md"
    )


def test_intended_config_doc_exists_and_is_not_loaded() -> None:
    """Вынесенные настройки хранятся отдельно и НЕ подмешиваются в живой конфиг.

    Если файл намерений когда-нибудь начнут загружать, инертные ключи вернутся в оборот молча —
    поэтому проверяется и его наличие, и то, что загрузчик о нём не знает.
    """
    doc = REPO / "docs" / "config-intended.md"
    assert doc.is_file(), "docs/config-intended.md пропал — вынесенные секции потеряны"
    body = doc.read_text(encoding="utf-8")
    for section in ("[watch]", "[fusion]", "[delivery]", "[intra_bar]", "[scoring]"):
        assert section in body, f"{section} должен быть сохранён в docs/config-intended.md"

    loader = (REPO / "hunt_core" / "domain" / "config.py").read_text(encoding="utf-8")
    assert "config-intended" not in loader, "файл намерений не должен читаться загрузчиком"


def test_removed_dead_sections_did_not_come_back(raw: dict[str, Any]) -> None:
    """Семь инертных секций не должны вернуться в живой файл.

    Их вернёт первый, кто захочет «настроить» порог и найдёт его в документации намерений.
    Возврат допустим ТОЛЬКО вместе с проводом до читателя и записью в `READERS` — тогда
    `test_no_section_without_a_registered_reader` пропустит, а этот тест надо обновить осознанно.
    """
    inert = {"watch", "levels", "market_regime", "delivery", "scoring", "intra_bar", "fusion"}
    back = sorted(inert & set(raw))
    assert not back, (
        f"инертные секции вернулись в config.defaults.toml: {back} — "
        f"без провода до читателя они снова будут молчаливым no-op"
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Ключевой уровень: секция может доходить до читателя ЦЕЛИКОМ и всё равно нести мёртвые ключи.
#
# `[tracker]` форвардится словарём (`domain/config.py`), поэтому проверка секции его пропускала,
# а свип 2026-07-26 нашёл внутри неё дефект С ОБЕИХ сторон сразу:
#   • ФАНТОМНАЯ РУЧКА — код читает `tr.get("atr_trail_risk_fraction", …)`, но записать значение
#     негде: ни в TOML, ни в `UNIVERSAL_DEFAULTS`, ни в `data/hunt_calibration.json` (файла нет).
#     Всегда побеждал инлайн-дефолт: настройка на вид, no-op на деле. Таких было 4.
#   • КЛЮЧ БЕЗ ЧИТАТЕЛЯ — `dump_active_min_trail_mfe_pct` / `min_trail_age_minutes` лежали среди
#     работающих ключей секции; `git log -S` не находит читателя НИ В ОДНОМ коммите.
# Обе стороны одинаково незаметны: ни тест, ни покрытие, ни vulture их не видят.
#
# Скан привязан к ПЕРЕМЕННОЙ, а не к каталогу. Первая редакция сканировала `track/**` на `tr.get(`
# и немедленно дала ложное срабатывание: `tp1_partial_fix_pct_normal/_hot` читает
# `params/store.py::tp1_partial_fix_pct` — то есть читатель живой, но лежит в другом каталоге и
# зовётся из `track/` уже готовой функцией. Ловить по имени файла нельзя; ловим по цепочке
# «переменная получена из `tracker_thresholds(...)` → с неё берут ключ».
_ASSIGN_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*=\s*tracker_thresholds\(", re.MULTILINE)
_INLINE_RE = re.compile(r'tracker_thresholds\([^()]*\)\.get\(\s*"([a-z0-9_]+)"')


def _tracker_keys_read_by_code() -> set[str]:
    found: set[str] = set()
    for path in (REPO / "hunt_core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "tracker_thresholds" not in text:
            continue
        found |= set(_INLINE_RE.findall(text))
        for var in set(_ASSIGN_RE.findall(text)):
            found |= set(
                re.findall(rf'\b{re.escape(var)}\.get\(\s*"([a-z0-9_]+)"', text)
            )
    return found


def test_every_tracker_knob_the_code_reads_can_actually_be_set() -> None:
    """Нет ФАНТОМНЫХ РУЧЕК: всё, что читает `track/`, реально резолвится загрузчиком."""
    from hunt_core.params.store import tracker_thresholds

    resolvable = set(tracker_thresholds("BTCUSDT"))
    phantom = sorted(_tracker_keys_read_by_code() - resolvable)
    assert not phantom, (
        f"фантомные ручки трекера (код читает, записать негде): {phantom}. "
        f"Опубликуйте их в config.defaults.toml [tracker] по текущему инлайн-дефолту "
        f"(тождество по поведению) либо уберите чтение."
    )


def test_no_tracker_key_without_a_reader(raw: dict[str, Any]) -> None:
    """Зеркало: нет ключей `[tracker]`, которых не читает ни одна строка `track/`."""
    declared = set(raw.get("tracker") or {})
    unread = sorted(declared - _tracker_keys_read_by_code())
    assert not unread, (
        f"ключи [tracker] без читателя: {unread}. Такой ключ выглядит гейтом, не будучи им — "
        f"проведите его до кода либо перенесите в docs/config-intended.md"
    )
