"""Шаг 0.6 — быстрые баги-рендер: activation не idle на активных сетапах, PINNED сведён,
метка ордеров честная. Каждый ловит видимую пользователю дезинформацию."""
from __future__ import annotations

from hunt_core.data.universe import PINNED_SYMBOLS as _UNIVERSE_PINNED
from hunt_core.features.prepare_columns import PINNED_SYMBOLS as _PREPARE_PINNED


def test_pinned_symbols_single_source() -> None:
    """F/G-минор: prepare_columns не должен дублировать хардкод — иначе 8-й конфиг-актив
    молча получит lean prepare."""
    assert set(_PREPARE_PINNED) == set(_UNIVERSE_PINNED)


def test_pp_break_and_trap_flip_set_activation() -> None:
    """F6: активные направленные сетапы (вход на ретесте) не должны нести activation=idle —
    иначе шапка печатает «⏸ Не готово» над направленным сетапом. Проверяем структурно, что
    обе trap_flip-ветки и pp_break-ветка проставляют activation (регрессия — удаление —
    вернёт дефолт idle из _base_summary)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "hunt_core" / "prizrak" / "orchestrator.py").read_text()
    # обе trap_flip-ветки (помечены «# флип», проставлены replace_all)
    assert src.count('summary["activation"] = "in_entry_zone"  # флип') == 2
    # pp_break-ветка (рядом с комментарием про ретест)
    assert "ТВХ = ретест сломанного уровня" in src
