"""Ключи настроек: у каждого есть читатель, и каждое чтение имеет ключ.

ЗАЧЕМ. Это фирменный дефект конфигурации проекта, и он измерен дважды.

Шапка `config.defaults.toml` фиксирует замер 2026-07-26: **63 ключа из 158 (40%)** не
доходили ни до одного читателя — правка TOML была молчаливым no-op. Свип секции
`[tracker]` в тот же день нашёл встречное: **4 ФАНТОМНЫЕ РУЧКИ** — код читал
``tr.get("atr_trail_risk_fraction", …)``, а записать это значение было НЕГДЕ, поэтому
всегда побеждал инлайн-дефолт. И 2026-08-03 нашёлся третий вид: `orphan_ttl_hours`
читался, но следующей строкой перекрывался, то есть управлял только наполовину.

Три разных дефекта, одна причина: между файлом и кодом нет механической сверки.
Держал её `tests/test_config_keys_wired.py`, удалённый вместе с каталогом `tests/`.

ЧТО ПРОВЕРЯЕТСЯ

(A) **Каждый ключ `config.defaults.toml` имеет читателя.** Ключ без читателя — обманка:
    его правят, и ничего не происходит.

(B) **Каждое чтение конфигурации имеет ключ в дефолтах.** Чтение без ключа — фантомная
    ручка: она выглядит настраиваемой, но записать значение негде, и инлайн-дефолт
    побеждает всегда.

⚠ МЕТОДИКА, И ГДЕ ОНА НАМЕРЕННО НЕТОЧНА.

Для (A) поиск читателя ТЕКСТОВЫЙ — имя ключа как строковый литерал либо как атрибут.
Это сознательно ПЕРЕСТРАХОВКА в сторону «читатель есть»: ключ, чьё имя совпало со
случайной строкой, не будет объявлен мёртвым. Ложная тревога здесь дороже пропуска —
гейт, который ругается зря, перестают читать. Именно такой скан и нашёл 63 ключа.

Для (B) разбор AST и ТОЧНЫЙ: ищутся только `.get("ключ")` на переменной, которой в той же
функции присвоен результат известного аксессора param-store (``tracker_thresholds`` и
родня). Прочие `.get` в дереве — это обычные словари, и трогать их нельзя.

⚠ «ЗАПИСАТЬ НЕГДЕ» СЧИТАЕТСЯ ПО ДВУМ ИСТОЧНИКАМ СРАЗУ, И ЭТО НЕ ПЕДАНТИЗМ. Первая
редакция сверяла ключ только с ``UNIVERSAL_DEFAULTS`` и объявила 15 фантомных ручек —
все пятнадцать оказались ЛОЖНОЙ ТРЕВОГОЙ: они лежат в ``config.defaults.toml [tracker]``,
откуда их форвардит ``domain/config.py::load_config_defaults_toml`` в тот же param-store.
Проверено вызовом: ``tracker_thresholds('BTCUSDT')['min_trail_mfe_pct']`` = 2.5 при
отсутствии ключа в ``UNIVERSAL_DEFAULTS``. Гейт, который ругается зря, ровно так и
перестают читать — поэтому источником истины взято объединение обоих.

⚠ Калибровочный JSON в объединение НЕ входит намеренно. ``universal_section()`` подмешивает
его третьим слоем, и гейт, читающий его, давал бы РАЗНЫЙ вердикт на машине с калибровкой и
без — то есть зависел бы от локального состояния. Вопрос здесь «может ли оператор записать
этот ключ», а канонические места для этого два: ``UNIVERSAL_DEFAULTS`` и
``config.defaults.toml``. Калибровка генерируется, а не пишется руками.

⚠ ЧЕГО ЭТА ПРОВЕРКА НЕ ЛОВИТ ПО ПОСТРОЕНИЮ: ключ, который читается И перекрывается ниже
по коду (случай `orphan_ttl_hours`). Формально у него есть и ключ, и читатель. Такое
ловится только чтением кода — см. T2.3.

    uv run python scripts/check_config_keys.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import tomllib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "hunt_core"
DEFAULTS = ROOT / "config.defaults.toml"

#: Аксессоры param-store → секция `UNIVERSAL_DEFAULTS`, из которой они читают.
#: Сверено с `hunt_core/params/store.py` 2026-08-03.
ACCESSOR_SECTION: dict[str, str] = {
    "tracker_thresholds": "tracker",
    "btc_corr_thresholds": "btc",
    "ws_thresholds": "ws",
    "filter_thresholds": "filters",
    "basis_thresholds": "basis",
    "orderflow_thresholds": "orderflow",
    "stats_thresholds": "stats",
    "prep_shadow_thresholds": "prep_shadow",
    "walk_forward_thresholds": "walk_forward",
    "liquidation_thresholds": "liquidation",
}

#: Ключи, которые читаются не по имени, а перечислением секции (`for k, v in section`)
#: или уезжают в модель pydantic целиком. Проверять их именем бессмысленно.
SECTION_CONSUMED_WHOLE: frozenset[str] = frozenset(
    {
        # [pinned.defaults] разбирается целиком в data/universe.py::pinned_symbols
        "symbols",
        "modes",
        "analyst",
        "deep_continuous",
        "hunt_confirm",
    }
)


class ConfigUnreadable(RuntimeError):
    """Файл настроек не разбирается — это отказ гейта, а не «ключей ноль»."""


def toml_leaf_keys(path: pathlib.Path) -> dict[str, str]:
    """``{имя_ключа: секция}`` по всем листьям файла."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # ⚠ Молчать здесь нельзя, но и трейсбеком отвечать не следует: битый TOML —
        # это ОТКАЗ ПРОВЕРКИ, а не её отрицательный результат. Без явного отказа
        # пустой разбор дал бы «ключей 0, нарушений 0» — зелёный гейт на нечитаемом файле.
        raise ConfigUnreadable(f"{path.name} не разбирается: {exc}") from exc
    out: dict[str, str] = {}

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if isinstance(value, dict) and value and all(isinstance(k, str) for k in value):
                # Вложенная таблица — секция. Но inline-таблица вида
                # `modes = { BTCUSDT = "both" }` это ЗНАЧЕНИЕ ключа, а не секция;
                # отличаем по тому, объявлена ли она через [section].
                if prefix and key in SECTION_CONSUMED_WHOLE:
                    out[key] = prefix
                    continue
                walk(value, f"{prefix}.{key}" if prefix else key)
                continue
            out[key] = prefix or "<root>"

    walk(data, "")
    return out


def code_blobs() -> dict[pathlib.Path, str]:
    return {
        p: p.read_text(encoding="utf-8", errors="replace")
        for p in CORE.rglob("*.py")
        if "__pycache__" not in p.parts
    }


def find_readerless(keys: dict[str, str], blobs: dict[pathlib.Path, str]) -> list[tuple[str, str]]:
    """(A) Ключи, чьё имя не встречается в коде ни строкой, ни атрибутом."""
    missing: list[tuple[str, str]] = []
    for key, section in sorted(keys.items()):
        if key in SECTION_CONSUMED_WHOLE:
            continue
        pattern = re.compile(rf"""["']{re.escape(key)}["']|\.{re.escape(key)}\b""")
        if not any(pattern.search(text) for text in blobs.values()):
            missing.append((key, section))
    return missing


def find_keyless_reads(
    universal: dict[str, Any], blobs: dict[pathlib.Path, str]
) -> list[tuple[str, str, str]]:
    """(B) ``accessor().get("key")`` там, где такого ключа в дефолтах нет."""
    out: list[tuple[str, str, str]] = []
    for path, text in blobs.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                continue
            # переменная -> секция, по присваиваниям внутри этой области
            bound: dict[str, str] = {}
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in ACCESSOR_SECTION
                ):
                    bound[node.targets[0].id] = ACCESSOR_SECTION[node.value.func.id]
            if not bound:
                continue
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in bound
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    continue
                section = bound[node.func.value.id]
                key = node.args[0].value
                known = universal.get(section) or {}
                if key not in known:
                    rel = path.relative_to(ROOT)
                    out.append((key, section, f"{rel}:{node.lineno}"))
    return sorted(set(out))


def writable_sections() -> dict[str, dict[str, Any]]:
    """``{секция: {ключ: значение}}`` — куда оператор ФАКТИЧЕСКИ может записать.

    Объединение ``UNIVERSAL_DEFAULTS`` и форварда ``config.defaults.toml``. Оба источника
    читаются из репозитория, поэтому вердикт не зависит от локального состояния машины
    (калибровочный JSON сюда не входит — см. докстроку модуля).
    """
    from hunt_core.domain.config import load_config_defaults_toml
    from hunt_core.params.store import UNIVERSAL_DEFAULTS

    merged: dict[str, dict[str, Any]] = {
        name: dict(body) for name, body in UNIVERSAL_DEFAULTS.items() if isinstance(body, dict)
    }
    for name, body in (load_config_defaults_toml() or {}).items():
        if isinstance(body, dict):
            merged.setdefault(name, {}).update(body)
    return merged


def main() -> int:
    sys.path.insert(0, str(ROOT))

    try:
        keys = toml_leaf_keys(DEFAULTS)
    except ConfigUnreadable as exc:
        print(f"ОТКАЗ ПРОВЕРКИ: {exc}")
        return 2
    blobs = code_blobs()
    writable = writable_sections()

    readerless = find_readerless(keys, blobs)
    keyless = find_keyless_reads(writable, blobs)

    print(f"ключей в config.defaults.toml : {len(keys)}")
    print(f"секций, куда можно записать   : {len(writable)}"
          f" ({sum(len(v) for v in writable.values())} ключей)")
    print(f"файлов кода просмотрено       : {len(blobs)}")

    if readerless:
        print(f"\n(A) КЛЮЧИ БЕЗ ЧИТАТЕЛЯ — {len(readerless)}:")
        for key, section in readerless:
            print(f"    [{section}] {key}")
        print("    Правка такого ключа — молчаливый no-op. Либо подключить, либо удалить.")
    if keyless:
        print(f"\n(B) ЧТЕНИЕ БЕЗ КЛЮЧА В ДЕФОЛТАХ — {len(keyless)}:")
        for key, section, where in keyless:
            print(f"    {where}: {section}.get({key!r}) — записать негде")
        print("    Фантомная ручка: инлайн-дефолт побеждает всегда. Завести ключ либо убрать.")

    if readerless or keyless:
        print(f"\nПРОВАЛ: {len(readerless)} ключей без читателя, {len(keyless)} чтений без ключа")
        return 1
    print("\nOK — каждый ключ имеет читателя, каждое чтение имеет ключ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
