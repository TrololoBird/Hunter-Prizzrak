"""Грид печатает ТОЛЬКО усиленный мульти-ТФ конфлюенс — поуровневого дампа больше нет.

Раньше здесь фиксировались порядок и дедуп внутри строк «· 1h: сопротивл=…, поддержка=…».
Эти строки удалены намеренно: карточка несла ДВЕ карты уровней сразу — зоны из ``format_post``
(структура + ПОК + цели) и этот дамп, посчитанный другим кодом по другим входам, — и они
расходились в числах на одном экране (живой BTC 2026-07-26: зоны «4h шорт 64473–64919 ·
65484–65750», дамп «4h сопротивл=65780.0»; зоны «1d добор 62232–62316», дамп «1d
поддержка=61297.0»). Читателю предлагалось выбрать самому.

Уникальное в гриде ровно одно — совпадение уровня на НЕСКОЛЬКИХ ТФ, чего карта зон не
показывает, потому что разносит горизонты по отдельным блокам. Курс ценит это прямо («сила
уровня определяется ТФ и объёмом», стр.22). Его и пиним.
"""
from __future__ import annotations

from hunt_core.deliver.confluence_grid import format_grid_telegram


def test_no_per_tf_dump_is_rendered() -> None:
    """Ни строк «· <tf>:», ни заголовка «Карта уровней» — иначе вернулись две карты."""
    grid = [
        {"tf": "1h", "support": 61806.0, "resistance": 63527.6},
        {"tf": "1h", "resistance": 63362.4},
        {"tf": "4h", "support": 60500.0},
    ]
    out = format_grid_telegram(grid, price=62077.6)
    assert "Карта уровней" not in out
    assert not any(ln.startswith("· ") for ln in out.splitlines()), out
    assert "63527.6" not in out and "63362.4" not in out, f"дамп уровней вернулся: {out}"


def test_deeper_and_zone_rows_are_not_rendered() -> None:
    """«глубже»/«выше»/«зона N» тоже уходят: их содержимое несут зоны и спот-лестница поста.

    Именно строка «зона 2» приходила безымянной и с сопротивлением задом наперёд
    (67255.4–66924.1) — она не должна существовать отдельно от карты зон.
    """
    grid = [
        {"tf": "глубже", "support": [61520.0, 61297.0, 59800.0]},
        {"tf": "зона 2", "support": "59800.0–58030.0", "_skip_generic": True},
    ]
    out = format_grid_telegram(grid, price=62000.0)
    assert out == "", f"списочные строки не должны печататься: {out}"


def test_multi_tf_level_surfaced_as_confluence() -> None:
    """Уровень, совпавший на двух ТФ, — единственное, что грид обязан напечатать."""
    grid = [
        {"tf": "1w", "support": 57758.6},
        {"tf": "1d", "support": 57758.6},
        {"tf": "4h", "support": 61806.0},  # одиночный ТФ → не конфлюенс
    ]
    conf = format_grid_telegram(grid, price=62000.0)
    assert "мульти-ТФ конфлюенс" in conf
    assert "57758.6" in conf and "1d+1w" in conf
    assert "61806.0" not in conf, "одиночный уровень не должен попадать в конфлюенс"
    # дистанция до цены остаётся частью строки — без неё число нечитаемо
    assert "%" in conf or "у цены" in conf, conf


def test_confluence_prints_the_shared_level_once() -> None:
    grid = [
        {"tf": "1h", "support": 62505.1, "resistance": 64356.7},
        {"tf": "4h", "support": 62505.1, "resistance": 65589.7},
    ]
    out = format_grid_telegram(grid, price=63926.6)
    assert out.count("62505.1") == 1, f"общий уровень напечатан не один раз: {out}"
    assert "1h+4h" in out


def test_empty_when_no_shared_levels() -> None:
    """Без совпадений на нескольких ТФ гриду сказать нечего — пусто, а не заголовок."""
    grid = [{"tf": "1h", "support": 61806.0, "resistance": 63000.0}]
    assert format_grid_telegram(grid, price=62000.0) == ""


def test_empty_grid_stays_empty() -> None:
    assert format_grid_telegram([], price=63000.0) == ""
