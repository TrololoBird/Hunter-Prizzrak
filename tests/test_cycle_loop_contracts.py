"""Контракты полосы МАНИПУЛЯЦИЙ в главном цикле — на живом коде `_manipulation_scan_loop`.

Зачем. Замер 2026-07-26: `runtime/cycle/_cycle_loop.py` — 518 инструкций, покрытие **8%**.
Это оркестратор всех полос: он решает, какая стратегия вообще получит шанс сработать.
Директива «проверять только на живых данных» его не защищает — это не рыночная геометрия,
а управляющая логика, и её поведение не зависит от того, что сейчас на бирже.

Здесь закреплены два факта, которые уже стоили времени.

**1. `--no-telegram` глушит МАНИПУЛЯЦИИ целиком, а не только отправку.** Флаг задуман как
«не слать», но `deliver_manipulation_setups` делает И ДЕТЕКТ, и вызов спрятан за
`if send_telegram and broadcaster is not None`. Значит документированный smoke-прогон
`watch --once --no-telegram` **не проверяет Pattern A/B вообще**, хотя выглядит как проверка
всего бота. Это ловушка ложной безопасности, а не баг — но она обязана быть громкой:
CLAUDE.md предупреждает о ней прозой, а проза скипается.

**2. Вселенная сканера пересобирается КАЖДЫЙ цикл.** Раньше список захватывался один раз на
старте процесса, и watchlist, который переписывает prescan, до сканера не доходил: монета,
начавшая поджиматься после запуска, оставалась невидимой до рестарта — ровно то, ради чего
prescan и существует. Регрессия сюда не видна ни в логах, ни в тестах геометрии.

Ни одно ожидание здесь не выведено из вывода кода: оба — заявленные контракты.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hunt_core.runtime.cycle import _cycle_loop


class _SpyDeliver:
    """Считает вызовы доставки и запоминает переданную вселенную."""

    def __init__(self) -> None:
        self.calls = 0
        self.symbols_seen: list[list[str]] = []

    async def __call__(
        self, symbols: list[str], feed: Any, broadcaster: Any, *, tracker_state: Any
    ) -> list[dict[str, Any]]:
        self.calls += 1
        self.symbols_seen.append(list(symbols))
        return []


@pytest.fixture
def one_cycle(monkeypatch: pytest.MonkeyPatch) -> _SpyDeliver:
    """Прогоняет РОВНО один оборот цикла с изолированными побочными эффектами."""
    spy = _SpyDeliver()

    ticks = iter([False, True])  # первый проход входит в тело, второй завершает while
    monkeypatch.setattr(_cycle_loop, "should_stop", lambda: next(ticks, True))
    monkeypatch.setattr(_cycle_loop, "is_blacklisted", lambda _s: False)

    import hunt_core.data.lake as lake
    import hunt_core.data.universe as universe
    import hunt_core.deliver.manipulation_delivery as delivery
    import hunt_core.track.tracker as tracker

    monkeypatch.setattr(delivery, "deliver_manipulation_setups", spy)
    monkeypatch.setattr(universe, "PINNED_SYMBOLS", ("BTCUSDT",))
    monkeypatch.setattr(universe, "load_watchlist_symbols", lambda: ["WATCHUSDT"])
    monkeypatch.setattr(tracker, "load_tracker_state", lambda: {})
    monkeypatch.setattr(lake, "buffer_tracker_state", lambda _s: None)
    monkeypatch.setattr(lake, "flush_tracker_state", lambda: None)
    return spy


def _run(**kw: Any) -> None:
    asyncio.run(
        _cycle_loop._manipulation_scan_loop(
            kw.pop("cli_symbols", ["ALTUSDT"]),
            kw.pop("feed", object()),
            kw.pop("broadcaster", object()),
            kw.pop("send_telegram", True),
            interval_s=0,
        )
    )


def test_no_telegram_silences_detection_not_only_delivery(one_cycle: _SpyDeliver) -> None:
    """`send_telegram=False` → детект манипуляций НЕ ЗАПУСКАЕТСЯ ни разу.

    Падение здесь означает, что ловушку починили — тогда правь CLAUDE.md § Commands и
    `docs/ARCHITECTURE.md` §5: оба заявляют, что smoke `--no-telegram` сканер не проверяет.
    """
    _run(send_telegram=False)
    assert one_cycle.calls == 0


def test_missing_broadcaster_silences_detection_too(one_cycle: _SpyDeliver) -> None:
    """Тот же гейт срабатывает и по `broadcaster is None` — вторая половина условия."""
    _run(send_telegram=True, broadcaster=None)
    assert one_cycle.calls == 0


def test_telegram_enabled_runs_detection_once_per_cycle(one_cycle: _SpyDeliver) -> None:
    """С включённой отправкой детект запускается ровно один раз за оборот."""
    _run(send_telegram=True)
    assert one_cycle.calls == 1


def test_universe_is_rebuilt_each_cycle_and_excludes_pinned(one_cycle: _SpyDeliver) -> None:
    """Вселенная = cli + watchlist, БЕЗ пиннутых (они домен призрака), собрана в этом обороте.

    Пиннутые символы обслуживает deep-полоса. Если они протекут сюда, один символ будет
    вести две независимые стратегии одновременно — а трекер ключует `SYMBOL:direction`
    и физически не удержит два разнонаправленных плана.
    """
    _run(send_telegram=True)

    assert one_cycle.symbols_seen, "детект не вызвался — гейт отправки закрыт?"
    universe = one_cycle.symbols_seen[0]
    assert "BTCUSDT" not in universe, "пиннутый символ протёк в полосу манипуляций"
    assert "WATCHUSDT" in universe, "watchlist от prescan до сканера не дошёл"
    assert "ALTUSDT" in universe, "символы из CLI потерялись"


def test_blacklisted_symbols_never_reach_detection(
    one_cycle: _SpyDeliver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Символ в чёрном списке не должен стоить сканеру ни одного REST-запроса.

    До этого гейта забанненный символ каждый цикл тянул 6 ТФ OHLCV + funding, падал на
    debug-уровне и жёг вес — невидимо, потому что ошибка не поднималась выше debug.
    """
    monkeypatch.setattr(_cycle_loop, "is_blacklisted", lambda s: str(s).upper() == "WATCHUSDT")
    _run(send_telegram=True)

    assert one_cycle.symbols_seen
    assert "WATCHUSDT" not in one_cycle.symbols_seen[0]


def test_detection_failure_does_not_kill_the_loop(
    one_cycle: _SpyDeliver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исключение в доставке гасится: полоса переживает цикл, а не уносит процесс.

    Полосы независимы по замыслу — падение сканера не должно останавливать призрака и
    главный тик. `asyncio.run` завершится штатно, если исключение поймано внутри.
    """
    import hunt_core.deliver.manipulation_delivery as delivery

    async def _boom(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(delivery, "deliver_manipulation_setups", _boom)
    _run(send_telegram=True)  # не должно бросить наружу
