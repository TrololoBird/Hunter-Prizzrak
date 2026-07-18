"""Тест-инвариант реестра (Шаг 0.6 / F1 / тест-полнота): каждый эмитируемый setup_kind имеет
RU-метку в карточке. Иначе в Telegram падает сырой id (F1: figure_pennant_6touch так и
случилось). CI-тест вместо «prose + review» — новый детектор группы A не проскочит без метки.
"""
from __future__ import annotations

import re
from pathlib import Path

from hunt_core.prizrak.build import AnalystReport
from hunt_core.prizrak.orchestrator import _TIER_SETUP_KIND

_ORCH = (Path(__file__).resolve().parent.parent / "hunt_core" / "prizrak" / "orchestrator.py").read_text()


def _emitted_setup_kinds() -> set[str]:
    """Все setup_kind, реально эмитируемые оркестратором: строковые литералы + тир-дефолты."""
    literals = set(re.findall(r'setup_kind="([a-z_0-9]+)"', _ORCH))
    return literals | set(_TIER_SETUP_KIND.values())


def test_every_emitted_setup_kind_has_ru_label() -> None:
    ru = AnalystReport._SETUP_KIND_RU
    missing = sorted(k for k in _emitted_setup_kinds() if k not in ru)
    assert not missing, f"setup_kind без RU-метки (в TG покажется сырой id): {missing}"


def test_figure_pennant_6touch_is_labelled() -> None:
    """Регрессия F1 — именно этот kind падал в сырой id."""
    assert "figure_pennant_6touch" in AnalystReport._SETUP_KIND_RU
