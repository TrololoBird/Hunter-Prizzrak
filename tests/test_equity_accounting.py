"""Гард: сумма результата считается в долях риска, с издержками и с лимитом перекрытий.

Дефект. Леджер складывал `pnl_pct` — проценты хода цены — и печатал сумму как результат.
Три причины, по которым такая сумма не значит ничего, и все три измерены на 283 настоящих
записях (2026-07-27):

1. РАЗМЕР ПОЗИЦИИ. Медиана дистанции стопа 2.42%, разброс от 0.0002% до 53.9%. Сложение
   приравнивало ставку в рубль к ставке в тысячу.
2. ИЗДЕРЖКИ. Их не было вовсе. Комиссия берётся с НОМИНАЛА, а номинал обратно пропорционален
   стопу, поэтому издержка в R равна `издержка_% / стоп_%`: у сделок со стопом <1% это 0.218R
   против 0.019R у сделок со стопом ≥6% — разница в ОДИННАДЦАТЬ раз.
3. ПЕРЕКРЫТИЯ. Одновременно открытых бывало до 32. Сумма молча считала, что каждая сделка
   получила весь капитал и что сделки шли по очереди.
"""
from __future__ import annotations

import datetime as dt

from hunt_core.track.equity import (
    CostModel,
    SizingPolicy,
    build_trade_frame,
    cost_pct_of_notional,
    simulate_equity,
    stop_distance_pct,
)

_T0 = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


def _row(
    *,
    direction: str = "long",
    entry: float = 100.0,
    stop: float = 98.0,
    pnl: float = 4.0,
    reason: str = "tp2_hit",
    opened: dt.datetime | None = None,
    hours: float = 1.0,
    symbol: str = "AAAUSDT",
) -> dict:
    start = opened or _T0
    return {
        "symbol": symbol,
        "direction": direction,
        "entry_lo": entry,
        "entry_hi": entry,
        "original_stop_loss": stop,
        "stop_loss": stop,
        "pnl_pct": pnl,
        "close_reason": reason,
        "opened_at": start.isoformat(),
        "closed_at": (start + dt.timedelta(hours=hours)).isoformat(),
        "duration_min": hours * 60.0,
    }


def test_risk_unit_is_the_original_stop_not_the_trailed_one() -> None:
    """Размер позиции решается на входе; подтянутый позже стоп риска не меняет.

    Считать риск по сдвинутому стопу значило бы задним числом объявить сделку крупнее, чем
    она была, — и тем сильнее, чем удачнее она шла.
    """
    row = _row(stop=98.0)
    row["stop_loss"] = 99.9  # трейл подтянул стоп почти в безубыток
    assert abs(stop_distance_pct(row) - 2.0) < 1e-9


def test_short_measures_risk_from_the_lower_edge() -> None:
    """У шорта худшая кромка — нижняя, та же база, что и у PnL."""
    row = _row(direction="short", entry=100.0, stop=104.0)
    row["entry_lo"], row["entry_hi"] = 100.0, 102.0
    assert abs(stop_distance_pct(row) - 4.0) < 1e-9


def test_cost_in_risk_units_grows_as_the_stop_tightens() -> None:
    """Главный вывод всей правки: издержка в R обратно пропорциональна ширине стопа.

    Один и тот же круговой оборот съедает у тесной сделки во много раз больший кусок риска.
    """
    wide = build_trade_frame([_row(stop=90.0, pnl=4.0)])
    tight = build_trade_frame([_row(stop=99.7, pnl=4.0)])
    assert tight["r_cost"][0] > wide["r_cost"][0] * 20, (
        "издержка перестала масштабироваться со стопом — потеряна суть пересчёта"
    )


def test_stop_market_exit_costs_more_than_a_limit_target() -> None:
    """Выход по рынку платит тейкера, лимитная цель — мейкера."""
    stopped = cost_pct_of_notional(_row(reason="stop_hit"))
    target = cost_pct_of_notional(_row(reason="tp2_hit"))
    assert stopped > target


def test_partial_fix_splits_the_exit_fee() -> None:
    """Частичная фиксация на TP1 — ДВА выхода с разными тарифами, а не один."""
    plain = _row(reason="stop_hit")
    partial = _row(reason="stop_hit") | {"tp1_hit": True, "partial_fixed_pct": 80.0}
    assert cost_pct_of_notional(partial) < cost_pct_of_notional(plain)


def test_holding_longer_costs_funding() -> None:
    """Удержание не бесплатно: фандинг платится за каждый интервал."""
    short_hold = cost_pct_of_notional(_row(hours=1.0))
    long_hold = cost_pct_of_notional(_row(hours=72.0))
    assert long_hold > short_hold


def test_unsizeable_stop_is_excluded_not_sized() -> None:
    """Стоп 0.001% — не «маленький риск», а отсутствие измеримого риска.

    Пропустить такую сделку в сайзинг значило бы запросить плечо в сто тысяч и получить
    фантастический R из ничего.
    """
    frame = build_trade_frame([_row(stop=99.999, pnl=1.0)])
    assert frame.is_empty()


def test_concurrent_risk_cap_skips_trades_it_cannot_fund() -> None:
    """Перекрытия: сделка, на которую не осталось лимита, ПРОПУСКАЕТСЯ и считается.

    Без этого сумма молча утверждала бы, что капитала хватило на все 32 одновременные позиции.
    """
    rows = [
        _row(opened=_T0, hours=100.0, symbol=f"S{i}USDT", pnl=1.0)
        for i in range(20)
    ]
    res = simulate_equity(
        rows,
        policy=SizingPolicy(risk_per_trade_pct=1.0, max_concurrent_risk_pct=5.0),
    )
    assert res.trades_taken == 5, f"взято {res.trades_taken}, а лимит допускает ровно 5"
    assert res.trades_skipped_no_capital == 15
    assert res.max_concurrent == 5


def test_sequential_trades_do_not_exhaust_the_cap() -> None:
    """Тот же лимит не мешает сделкам, идущим ПО ОЧЕРЕДИ — иначе он резал бы вслепую."""
    rows = [
        _row(opened=_T0 + dt.timedelta(hours=2 * i), hours=1.0, symbol=f"S{i}USDT")
        for i in range(20)
    ]
    res = simulate_equity(
        rows,
        policy=SizingPolicy(risk_per_trade_pct=1.0, max_concurrent_risk_pct=5.0),
    )
    assert res.trades_taken == 20
    assert res.trades_skipped_no_capital == 0
    assert res.max_concurrent == 1


def test_leverage_cap_binds_and_lowers_effective_risk() -> None:
    """Плечевой потолок: биржа не даст номинал, которого требует тесный стоп."""
    tight = _row(stop=99.5, pnl=0.0, hours=0.1)  # стоп 0.5% → фикс. риск 1% просит 2×
    capped = simulate_equity([tight], policy=SizingPolicy(max_leverage=1.0))
    assert capped.trades_taken == 1
    assert abs(capped.r_net_sum) < abs(
        simulate_equity([tight], policy=SizingPolicy(max_leverage=20.0)).r_net_sum
    )


def test_losses_reduce_equity_and_show_up_as_drawdown() -> None:
    """Просадка обязана считаться — иначе кривая читается как безрисковая."""
    rows = [
        _row(opened=_T0 + dt.timedelta(hours=2 * i), hours=1.0, pnl=-2.0,
             reason="stop_hit", symbol=f"S{i}USDT")
        for i in range(5)
    ]
    res = simulate_equity(rows)
    assert res.final_equity < res.start_equity
    assert res.max_drawdown_pct > 0.0


def test_costs_never_flatter_the_result() -> None:
    """Издержки только уменьшают результат — знак перепутать нельзя."""
    rows = [_row(opened=_T0 + dt.timedelta(hours=2 * i), symbol=f"S{i}USDT") for i in range(5)]
    res = simulate_equity(rows, costs=CostModel())
    assert res.r_net_sum < res.r_gross_sum
    assert res.fees_paid > 0.0
