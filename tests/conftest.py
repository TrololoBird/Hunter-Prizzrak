"""Изоляция боевых путей данных от тестового прогона.

Зачем этот файл появился (замер 2026-07-27). `data/signal_history.jsonl` — леджер, по
которому считается винрейт и калибруются гейты. В нём было 3722 строки, и **3423 из них
оказались тестовыми фикстурами**: символ `X` со входом 100 / стопом 90 / целями 110 и 150,
плюс 247 идентичных копий ETHUSDT со входом 99/100 и выходом 116.5. Эти строки давали
**86% суммарного pnl** (+37205% из +43045%) и 85% выборки винрейта.

Механика утечки. У `tracker.close_signal` параметр `archive` по умолчанию `True`, а
докстрока просила тесты передавать `archive=False`. Прямые вызовы так и делали — но
`close_signal` зовут ИЗНУТРИ ещё 17 мест (`_evaluate_levels`, `_followups`,
`auto_resolve_active_signals`), и там аргумент не передаётся. Любой тест, дёргающий функцию
уровнем выше, писал в боевой файл. Проверено счётчиком строк: один прогон
`tests/test_manipulation_runner.py` дописывал 9 строк.

Почему autouse-фикстура, а не правка каждого теста: правка тестов чинит те, что есть
сегодня, и ничего не говорит о завтрашних. Здесь перенаправлены САМИ ПУТИ, поэтому новый
тест изолирован по умолчанию — помнить об этом не нужно. Второй слой —
`outcomes.py::_refuse_production_write`: он падает, если запись в `data/` всё-таки
началась (например, путь взяли в обход `hunt_core.paths`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Пути-приёмники, в которые тест не имеет права писать. Читаются потребителями поздним
# импортом (`from hunt_core.paths import SIGNAL_HISTORY` внутри функции), поэтому
# monkeypatch атрибута модуля действует на весь вызов.
_REDIRECTED = ("SIGNAL_HISTORY", "SIGNAL_EVENTS", "SIGNAL_STATE")


@pytest.fixture(autouse=True)
def _isolate_production_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Перенаправить боевые файлы-приёмники в tmp на время каждого теста."""
    import hunt_core.paths as paths

    sandbox = tmp_path / "data"
    sandbox.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "DATA", sandbox, raising=False)
    for name in _REDIRECTED:
        real = getattr(paths, name, None)
        if real is None:
            continue
        monkeypatch.setattr(paths, name, sandbox / Path(real).name, raising=False)
