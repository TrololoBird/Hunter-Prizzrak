"""Гард: таймаут — третья категория, а не победа.

Сделка здесь устроена как triple-barrier: стоп, цель, время. Первые два барьера РАЗРЕШАЮТ
тезис — цена куда-то дошла. Вертикальный барьер не разрешает ничего: позицию закрыли по рынку,
потому что кончилось отведённое время. В методологии López de Prado это отдельная метка.

Замер по ОЧИЩЕННОМУ леджеру (283 настоящие записи, 2026-07-27): `timeout` — 25 сделок (8.8%),
и 18 из них (72%) уходили в победы по НЕЗАКРЫТОЙ бумажной прибыли. Вся категория
`unresolved` — 91 запись, **32.2% выборки**: треть статистики состояла из позиций, которые
ничего не доказали.

ПОСЛЕ правки: win 91 · loss 100 · unresolved 91. Винрейт по РАЗРЕШЁННЫМ сделкам — **47.6%**
на 191 сделке.

⚠ Первая редакция этого файла называла 3672 записи и винрейт 87.6%. Числа были посчитаны по
файлу, в котором 3423 строки из 3722 оказались ТЕСТОВЫМИ ФИКСТУРАМИ — они попадали в боевой
леджер через `close_signal(archive=True)` из внутренних вызовов (утечка закрыта
`tests/conftest.py` + `outcomes.py::_refuse_production_write`). Фикстуры были победами почти
поголовно: их собственный винрейт 90.9%. Настоящий — 47.6%. Логика категорий от этого не
меняется, но всякий, кто сошлётся на «87.6%», сошлётся на артефакт.

⚠ PnL при этом сохраняется и у неразрешённых: если бы позицию закрыли по рынку в тот момент,
результат был бы именно такой. Неверна была МЕТКА, а не число.
"""
from __future__ import annotations

from hunt_core.track.outcomes import (
    LOSS_REASONS,
    UNRESOLVED_REASONS,
    WIN_REASONS,
    outcome_kind,
)


def test_timeout_is_never_a_win_regardless_of_pnl() -> None:
    """Бумажная прибыль на момент истечения времени победой не является."""
    assert outcome_kind("timeout", pnl_pct=25.0) == "unresolved"
    assert outcome_kind("timeout", pnl_pct=-25.0) == "unresolved"
    assert outcome_kind("timeout", pnl_pct=0.0) == "unresolved"


def test_barrier_outcomes_are_unchanged() -> None:
    """Достижение цели и стопа классифицируется как прежде — правка их не касается."""
    assert outcome_kind("tp2_hit", pnl_pct=8.0) == "win"
    assert outcome_kind("stop_hit", pnl_pct=-4.0) == "loss"
    # Стоп, доведённый до прибыли после частичной фиксации, — законная победа.
    assert outcome_kind("stop_hit", pnl_pct=2.4) == "win"


def test_self_closed_exits_are_unresolved_not_losses() -> None:
    """Выход по смене режима — не проваленный тезис, а непроверенный.

    `bias_flip`, `lifecycle_stale` и `opposite_signal` переехали из `LOSS_REASONS`: считать их
    убытком значит утверждать, что тезис проверен и не сработал, тогда как проверки не было.
    """
    for reason in ("bias_flip", "lifecycle_stale", "opposite_signal"):
        assert outcome_kind(reason, pnl_pct=1.0) == "unresolved", reason
        assert outcome_kind(reason, pnl_pct=-1.0) == "unresolved", reason


def test_categories_do_not_overlap() -> None:
    """Одна причина не может быть одновременно победой, убытком и неразрешённой."""
    assert not (WIN_REASONS & LOSS_REASONS)
    assert not (WIN_REASONS & UNRESOLVED_REASONS)
    assert not (LOSS_REASONS & UNRESOLVED_REASONS)


def test_unresolved_wins_over_pnl() -> None:
    """Порядок проверок важен: категория решается ДО обращения к pnl.

    Иначе неразрешённая сделка с прибылью выше шумового порога снова стала бы победой —
    ровно тот дефект, который правка закрывает.
    """
    for reason in sorted(UNRESOLVED_REASONS):
        assert outcome_kind(reason, pnl_pct=99.0) == "unresolved", reason
