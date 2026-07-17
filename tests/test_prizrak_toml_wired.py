"""[deep.prizrak] TOML секция реально доходит до полей PrizrakConfig (проводка жива).

Зеркало tests/test_hunter_toml_wired.py. Секции в config.defaults.toml сейчас НЕТ — все поля
на Pydantic-дефолтах — но проводка (domain/config.py:359-361 форвардит raw["deep"]["prizrak"],
PrizrakConfig.load валидирует его) обязана работать, иначе будущая секция молча не подействует.
Пин против регрессии проводки: пустая секция = дефолты; заданное значение доходит до поля.
"""
from __future__ import annotations

import hunt_core.prizrak.config as pconf
from hunt_core.prizrak.config import PrizrakConfig


def test_empty_section_yields_pydantic_defaults() -> None:
    PrizrakConfig.reset()
    cfg = PrizrakConfig.load()
    assert cfg.min_rr == 2.0  # дефолт модели, секции нет
    assert cfg.accumulation_min_touches == 4
    PrizrakConfig.reset()


def test_toml_value_reaches_the_field(monkeypatch) -> None:
    """Заданный в [deep.prizrak] ключ переопределяет дефолт — проводка жива."""
    monkeypatch.setattr(
        pconf, "load_config_defaults_toml",
        lambda: {"deep": {"prizrak": {"min_rr": 3.5, "accumulation_min_touches": 6}}},
    )
    PrizrakConfig.reset()
    cfg = PrizrakConfig.load()
    assert cfg.min_rr == 3.5, "min_rr из TOML не дошёл — проводка сломана, секция doc-only"
    assert cfg.accumulation_min_touches == 6
    PrizrakConfig.reset()


def test_nested_tier_section_forwards(monkeypatch) -> None:
    """Вложенная [deep.prizrak.meso] тоже доходит (tier — вложенная модель)."""
    monkeypatch.setattr(
        pconf, "load_config_defaults_toml",
        lambda: {"deep": {"prizrak": {"meso": {"timeframes": ["1h", "4h"], "lookback_bars": 120}}}},
    )
    PrizrakConfig.reset()
    cfg = PrizrakConfig.load()
    assert cfg.meso.lookback_bars == 120, "вложенная секция tier не дошла"
    PrizrakConfig.reset()


def test_invalid_timeframe_fails_loud_at_load(monkeypatch) -> None:
    """Literal-ограничение: мусорный ТФ в TOML → ValidationError на загрузке, не молчком."""
    import pytest
    from pydantic import ValidationError

    monkeypatch.setattr(
        pconf, "load_config_defaults_toml",
        lambda: {"deep": {"prizrak": {"meso": {"timeframes": ["30m"], "lookback_bars": 60}}}},
    )
    PrizrakConfig.reset()
    with pytest.raises(ValidationError):
        PrizrakConfig.load()
    PrizrakConfig.reset()
