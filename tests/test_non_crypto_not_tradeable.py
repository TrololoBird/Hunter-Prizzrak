"""Гард: токенизированный актив наблюдается, но сделкой не становится.

Binance USDⓈ-M листит не только крипту: XAG (серебро), XAU (золото), SPY/QQQ/ORCL/MSTR
(акции), CL (нефть). Обе стратегии построены на крипто-микроструктуре — OI, фандинг,
ликвидации, CVD. У токенизированного товара фандинг печатается «+0.000%», эндпоинт базиса
отвечает `-4104` навсегда, а цену двигает внешний рынок с сессией, которую бот не наблюдает.

ЗАМЕР 2026-07-27: в леджере **54 записи из 283 (19.1%)** — не крипта: 43 EQUITY,
8 COMMODITY, 3 KR_EQUITY. Полоса манипуляций свой фильтр держит (после 2026-07-14 таких
записей нет), а пиннед-путь призрака идёт по `PINNED_SYMBOLS` напрямую, мимо
`gate_symbol_list`, и строит по XAG полную карточку со входом, стопом и целями.

⚠ РАЗЛИЧИЕ, КОТОРОГО В ПРОЕКТЕ НЕ БЫЛО: **наблюдается для контекста** ≠ **допущен к
сделкам**. XAU/XAG/PAXG закреплены в пиннед-наборе НАМЕРЕННО — в трёх местах
(`config.defaults.toml`, `domain/config.py::REQUIRED_PINNED_SYMBOLS`,
`data/universe.py::_CANONICAL_PINNED`), и CLI прямо пишет «anchors BTC ETH XAU XAG». Это
макро-якоря риск-он/риск-офф, и выкидывать их из наблюдения не нужно. Гард отделяет второе
от первого.

Отказ стоит на ОБЩЕМ приёмнике `register_signal_open`, потому что он обслуживает обе полосы
и его нельзя обойти, — иначе тот же гейт пришлось бы городить в каждом продюсере.
"""
from __future__ import annotations

from datetime import UTC, datetime

from hunt_core.market.symbols import (
    is_crypto_symbol,
    register_underlyings_from_markets,
    underlying_type_for,
)
from hunt_core.track import tracker

# Форма записи CCXT, из которой читается класс актива (публичный exchangeInfo).
_MARKETS = [
    {"symbol": "BTC/USDT:USDT", "swap": True, "linear": True, "settle": "USDT",
     "type": "swap", "info": {"underlyingType": "COIN"}},
    {"symbol": "XAG/USDT:USDT", "swap": True, "linear": True, "settle": "USDT",
     "type": "swap", "info": {"underlyingType": "COMMODITY"}},
    {"symbol": "SPY/USDT:USDT", "swap": True, "linear": True, "settle": "USDT",
     "type": "swap", "info": {"underlyingType": "EQUITY"}},
]


def _register(symbol: str) -> dict:
    state: dict = {"signals": {}}
    tracker.register_signal_open(
        state, symbol=symbol, direction="long", price=100.0,
        setup={
            "entry_zone": [99.0, 101.0], "stop_loss": 95.0, "tp1": 110.0,
            "delivery_tier": "triggered", "phase": "test",
        },
        lifecycle={}, now=datetime.now(UTC),
    )
    return state["signals"]


def test_registry_reads_the_asset_class(monkeypatch) -> None:  # noqa: ANN001
    """Класс актива берётся из публичных метаданных биржи, а не угадывается по тикеру."""
    import hunt_core.market.symbols as symbols

    monkeypatch.setattr(symbols, "_UNDERLYINGS", {}, raising=False)
    assert register_underlyings_from_markets(_MARKETS) == 3
    assert underlying_type_for("BTCUSDT") == "COIN"
    assert underlying_type_for("XAGUSDT") == "COMMODITY"
    assert underlying_type_for("SPYUSDT") == "EQUITY"


def test_symbol_key_is_shape_agnostic(monkeypatch) -> None:  # noqa: ANN001
    """`BTC/USDT:USDT` и `BTCUSDT` — один и тот же символ, как в тиковом реестре."""
    import hunt_core.market.symbols as symbols

    monkeypatch.setattr(symbols, "_UNDERLYINGS", {}, raising=False)
    register_underlyings_from_markets(_MARKETS)
    assert underlying_type_for("XAG/USDT:USDT") == underlying_type_for("XAGUSDT")


def test_unknown_symbol_is_not_assumed_non_crypto(monkeypatch) -> None:  # noqa: ANN001
    """Fail-open на неизвестном — осознанно, а не недосмотр.

    Реестр заполняется после `load_markets`; до этого он пуст. Строгий отказ заглушил бы
    вообще всё, включая BTC. Заглушить биржу хуже, чем пропустить одну акцию до прогрева.
    """
    import hunt_core.market.symbols as symbols

    monkeypatch.setattr(symbols, "_UNDERLYINGS", {}, raising=False)
    assert underlying_type_for("ЧТОУГОДНО") is None
    assert is_crypto_symbol("ЧТОУГОДНО") is True


def test_crypto_symbol_still_registers(monkeypatch) -> None:  # noqa: ANN001
    """Гард не имеет права глушить настоящую крипту."""
    import hunt_core.market.symbols as symbols

    monkeypatch.setattr(symbols, "_UNDERLYINGS", {}, raising=False)
    register_underlyings_from_markets(_MARKETS)
    assert len(_register("BTCUSDT")) == 1


def test_tokenized_commodity_does_not_become_a_trade(monkeypatch) -> None:  # noqa: ANN001
    """Серебро остаётся в наблюдении, но отслеживаемой сделкой не становится."""
    import hunt_core.market.symbols as symbols

    monkeypatch.setattr(symbols, "_UNDERLYINGS", {}, raising=False)
    register_underlyings_from_markets(_MARKETS)
    assert len(_register("XAGUSDT")) == 0, "по токенизированному товару заведена сделка"


def test_tokenized_equity_does_not_become_a_trade(monkeypatch) -> None:  # noqa: ANN001
    """Измеренный случай: SPYUSDT — 6 записей в леджере."""
    import hunt_core.market.symbols as symbols

    monkeypatch.setattr(symbols, "_UNDERLYINGS", {}, raising=False)
    register_underlyings_from_markets(_MARKETS)
    assert len(_register("SPYUSDT")) == 0
