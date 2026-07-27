"""Гард: тестовый прогон не имеет права дописывать строки в боевой леджер.

Дефект (замер 2026-07-27). `data/signal_history.jsonl` — файл, по которому считается винрейт
и калибруются гейты. В нём было 3722 строки, и **3423 оказались тестовыми фикстурами**:
3175 записей по символу `X` (вход 100, стоп 90, цели 110/150) и 248 копий ETHUSDT (вход
99/100, выход 116.5, ровно 60.0 мин — 247 идентичных строк подряд). Они давали **86% всей
суммы pnl**: +41414% из +43045%.

Механика. У `tracker.close_signal` параметр `archive` по умолчанию `True`, а докстрока просила
тесты передавать `archive=False`. Прямые вызовы так и делали — но `close_signal` зовут ИЗНУТРИ
ещё 17 мест (`auto_resolve_active_signals`, `_evaluate_levels`, `_followups`), и туда аргумент
не передаётся. Любой тест, дёргающий функцию уровнем выше, писал в боевой файл. Проверено
счётчиком строк: один прогон `tests/test_manipulation_runner.py` добавлял 9 строк; за
2026-07-26 накопилось 372 строки, за 07-27 — 198.

Почему договорённости оказалось мало: «тесты передают archive=False» — это обещание, а не
механизм, и оно молча не выполнялось месяц. Здесь проверяются ОБА слоя защиты — изолирующая
фикстура в `tests/conftest.py` и отказ на самом приёмнике.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hunt_core.paths as paths
from hunt_core.track import tracker
from hunt_core.track.outcomes import (
    ProductionWriteUnderTestError,
    append_outcome_record,
)


def _real_history() -> Path:
    """Боевой путь, вычисленный от файла модуля — мимо любых подмен."""
    return Path(paths.__file__).resolve().parents[1] / "data" / "signal_history.jsonl"


def test_sink_refuses_a_write_into_the_real_data_dir() -> None:
    """Прямая запись в боевой каталог из-под pytest обязана падать, а не проходить."""
    with pytest.raises(ProductionWriteUnderTestError):
        append_outcome_record(_real_history(), {"symbol": "GUARD", "opened_at": "x"})


def test_guard_resolves_production_dir_independently_of_monkeypatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Гард обязан брать боевой каталог ОТ ФАЙЛА МОДУЛЯ, а не из `paths.DATA`.

    Первая редакция читала `paths.DATA` — который изолирующая фикстура как раз подменяет на
    tmp — и объявляла боевым сам песочный каталог: 9 честных тестов упали на ровном месте.
    """
    monkeypatch.setattr(paths, "DATA", tmp_path, raising=False)
    append_outcome_record(tmp_path / "signal_history.jsonl", {"symbol": "OK", "opened_at": "1"})
    assert (tmp_path / "signal_history.jsonl").exists()
    with pytest.raises(ProductionWriteUnderTestError):
        append_outcome_record(_real_history(), {"symbol": "GUARD", "opened_at": "2"})


def test_conftest_redirects_the_ledger_away_from_production() -> None:
    """Автофикстура обязана увести приёмник в tmp — иначе гард срабатывал бы постоянно."""
    assert not Path(paths.SIGNAL_HISTORY).resolve().is_relative_to(
        _real_history().parent
    ), "SIGNAL_HISTORY всё ещё указывает в боевой каталог"


def test_default_archive_close_does_not_reach_production() -> None:
    """Закрытие с ДЕФОЛТНЫМ archive=True — ровно тот путь, что натёк 3423 строки."""
    before = _real_history().read_text(encoding="utf-8") if _real_history().exists() else ""
    state: dict = {
        "signals": {
            "XUSDT:long": {
                "symbol": "XUSDT", "direction": "long",
                "entry_lo": 100.0, "entry_hi": 100.0, "stop_loss": 90.0,
                "opened_at": datetime.now(UTC).isoformat(),
                "status": "triggered", "phase": "triggered",
            }
        }
    }
    tracker.close_signal(
        state, symbol="XUSDT", direction="long", reason="stop_hit",
        exit_price=95.0, now=datetime.now(UTC),
    )
    after = _real_history().read_text(encoding="utf-8") if _real_history().exists() else ""
    assert after == before, "боевой леджер изменился во время теста"


def test_production_ledger_carries_no_fixture_geometry() -> None:
    """В боевом файле не должно остаться повторяющейся фикстурной геометрии.

    Признак фикстуры — ПОВТОР ключа (символ, кромки входа, стоп, цели, выход, длительность):
    у настоящей сделки цены приходят с биржи float'ами, а длительность меряется в момент
    закрытия, так что пять одинаковых строк рынок не порождает.
    """
    history = _real_history()
    if not history.exists():
        pytest.skip("боевого леджера нет — проверять нечего")
    seen: dict[tuple, int] = {}
    for line in history.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (
            row.get("symbol"), row.get("entry_lo"), row.get("entry_hi"),
            row.get("stop_loss"), row.get("tp1"), row.get("tp2"),
            row.get("exit_price"), row.get("duration_min"),
        )
        seen[key] = seen.get(key, 0) + 1
    worst = max(seen.values(), default=0)
    assert worst < 5, (
        f"в леджере {worst} буквально одинаковых записей — фикстуры вернулись; "
        "прогони scripts/purge_fixture_rows.py --apply"
    )
