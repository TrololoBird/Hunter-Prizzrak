"""Гард: метрики по общему леджеру обязаны быть подписаны полосой.

Дефект чтения, а не кода — и он случился со мной. `track/` обслуживает ОБЕ стратегии
одинаково и пишет их исходы в ОДИН файл (`data/signal_history.jsonl`). `stats_report` и
`scripts/ledger_metrics.py` считали по нему сплошняком, без различения полос.

ЗАМЕР 2026-07-27: из 283 закрытых записей **283 — полоса манипуляций** (`setup_phase` ∈
{manipulation, pre, smc_dump, ignition}), записей призрака — **НОЛЬ**. То есть винрейт 47.6%,
чистый +437R, концентрация топ-3 = 44% и дефект неисполнимого входа (64 записи, +1589.9%) —
всё это описывает ЧУЖОЙ модуль. Прочитав их как общий результат, я пошёл править файл
модуля, который в текущей работе не участвует.

Смешивать полосы нельзя не из аккуратности, а по существу: у них раздельные стоп, ТФ,
фильтры, гейты и источник истины (CLAUDE.md; `.claude/rules/prizrak.md` и
`manipulations.md`). Среднее по двум стратегиям не описывает ни одну из них.
"""
from __future__ import annotations

from hunt_core.track.outcomes import lane_of, split_by_lane


def _row(phase: str | None) -> dict:
    return {"setup_phase": phase, "close_reason": "tp1_hit", "pnl_pct": 1.0}


def test_manipulation_phases_are_attributed() -> None:
    """Значения, которыми полоса манипуляций метит свои сделки."""
    for phase in ("manipulation", "pre", "smc_dump", "ignition"):
        assert lane_of(_row(phase)) == "manipulations", phase


def test_prizrak_zone_handoff_is_attributed() -> None:
    """`zone_watch` метит сделки как ``zone_<вид>`` — это призрак."""
    for kind in ("perezakup", "dobor", "short"):
        assert lane_of(_row(f"zone_{kind}")) == "prizrak", kind


def test_unrecognised_producer_is_not_guessed() -> None:
    """Неопознанная запись — ``unknown``, а не «наверное, эта полоса».

    Приписать сделку полосе по догадке значило бы выдумать принадлежность (I-6). Ярлык
    ``unknown`` виден в отчёте и требует решения, вместо того чтобы тихо испортить среднее.
    """
    assert lane_of(_row(None)) == "unknown"
    assert lane_of(_row("")) == "unknown"
    assert lane_of(_row("something_new")) == "unknown"
    assert lane_of({}) == "unknown"


def test_split_keeps_every_row_exactly_once() -> None:
    """Разложение по полосам не имеет права терять или дублировать сделки."""
    rows = [_row("manipulation"), _row("zone_dobor"), _row("smc_dump"), _row("mystery")]
    out = split_by_lane(rows)
    assert sum(len(v) for v in out.values()) == len(rows)
    assert set(out) == {"manipulations", "prizrak", "unknown"}
    assert len(out["manipulations"]) == 2


def test_real_ledger_is_single_lane_and_it_is_not_prizrak() -> None:
    """Фиксация факта: боевой леджер сегодня — целиком манипуляции.

    Тест намеренно НЕ требует, чтобы так было всегда. Он требует, чтобы принадлежность была
    ИЗВЕСТНА: если появятся записи призрака — тест продолжит проходить, а если появится
    запись без опознанного продюсера, он упадёт и потребует расширить `lane_of`.
    """
    import json
    from pathlib import Path

    from hunt_core.track.outcomes import is_polluted

    history = Path("data/signal_history.jsonl")
    if not history.exists():
        return
    rows = [
        r for r in (json.loads(ln) for ln in history.read_text(encoding="utf-8").splitlines() if ln.strip())
        if not is_polluted(r) and r.get("close_reason")
    ]
    if not rows:
        return
    lanes = split_by_lane(rows)
    assert not lanes.get("unknown"), (
        f"{len(lanes.get('unknown', []))} записей без опознанной полосы — "
        "появился новый продюсер, `lane_of` надо расширить, а не считать их вместе"
    )
