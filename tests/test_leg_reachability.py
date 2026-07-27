"""Гард: «не проверено» не имеет права выглядеть как «проверено и чисто».

Дефект измерения. `build_trade_frame` не умел выразить, что сделка опирается на цену, по
которой рынок не торговал, — и складывал такие сделки с настоящими.

ЗАМЕР `scripts/verify_trade_legs.py` на живых свечах, 283 записи, обе ноги:

| подвыборка | сделок | чистый R | среднее | бутстрэп 95% |
|---|---|---|---|---|
| обе ноги реальны | 191 | **−14.6** | **−0.076R** | **[−0.276 … +0.124]** |
| нога недостижима | 89 | +278.8 | +3.132R | — |

**Недостижимые несут 105.5% чистого R при доле 32% сделок.** То есть весь плюс результата
держится на сделках, которых рынок не подтверждает, а на подтверждённых знак матожидания
НЕ УСТАНОВЛЕН — интервал накрывает ноль.

⚠ `None` (вердикта нет) и `False` (нога недостижима) — РАЗНЫЕ значения, и их нельзя
схлопывать. Отсутствие проверки это «не знаем», а не «чисто» (I-6).

⚠ Вердикт живёт в ОТДЕЛЬНОМ файле, а не в леджере. В самом леджере уже лежат осиротевшие
`market_confirmed`/`recheck_ts` — 65 строк, у которых нет НИ ОДНОГО читателя во всём дереве:
кто-то посчитал вердикт, записал его в данные и не подключил. Ключ вердикта поэтому объявлен
в `equity.leg_verdict_key` и используется обеими сторонами — разъехавшиеся ключи дали бы
пустое пересечение, и «чистая подвыборка» молча оказалась бы пустой вместо ошибки.
"""
from __future__ import annotations

import datetime as dt

from hunt_core.track.equity import build_trade_frame, leg_verdict_key

_T0 = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


def _row(symbol: str = "AAAUSDT", pnl: float = 4.0) -> dict:
    return {
        "symbol": symbol,
        "direction": "long",
        "entry_lo": 100.0,
        "entry_hi": 100.0,
        "original_stop_loss": 98.0,
        "stop_loss": 98.0,
        "exit_price": 100.0 * (1 + pnl / 100.0),
        "close_reason": "tp2_hit",
        "opened_at": _T0.isoformat(),
        "closed_at": (_T0 + dt.timedelta(hours=1)).isoformat(),
        "duration_min": 60.0,
    }


def test_no_verdict_is_null_not_clean() -> None:
    """Без вердиктов признак None — «не проверено», а не «чисто»."""
    frame = build_trade_frame([_row()])
    assert frame["legs_reachable"][0] is None, (
        "непроверенная сделка выдана за подтверждённую"
    )


def test_verdict_is_carried_into_the_frame() -> None:
    """Вердикт доезжает до кадра — иначе он снова осиротеет, как market_confirmed."""
    row = _row()
    verdicts = {leg_verdict_key(row): {"both_reachable": True}}
    assert build_trade_frame([row], leg_verdicts=verdicts)["legs_reachable"][0] is True

    verdicts = {leg_verdict_key(row): {"both_reachable": False}}
    assert build_trade_frame([row], leg_verdicts=verdicts)["legs_reachable"][0] is False


def test_key_matches_what_the_script_writes() -> None:
    """Ключ вердикта объявлен в одном месте и обеими сторонами читается одинаково.

    Разъехавшиеся ключи дали бы ПУСТОЕ пересечение — отчёт напечатал бы «чистая подвыборка:
    0 сделок» вместо ошибки, и это выглядело бы как результат.
    """
    # `scripts/` не пакет — грузим по пути, иначе гард просто не увидит вторую сторону.
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "_verify_trade_legs", pathlib.Path("scripts/verify_trade_legs.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    row = _row(symbol="btcusdt")
    assert module.leg_key(row) == leg_verdict_key(row)


def test_unrelated_verdict_does_not_leak_onto_another_trade() -> None:
    """Вердикт от чужой сделки не должен подхватываться по частичному совпадению."""
    row = _row(symbol="AAAUSDT")
    other = _row(symbol="BBBUSDT")
    verdicts = {leg_verdict_key(other): {"both_reachable": True}}
    assert build_trade_frame([row], leg_verdicts=verdicts)["legs_reachable"][0] is None
