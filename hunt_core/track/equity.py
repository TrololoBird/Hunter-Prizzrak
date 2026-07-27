"""Честный счёт результата: размер позиции, издержки, перекрытия.

Что было не так. Леджер складывал `pnl_pct` — процентные ходы цены — и печатал сумму как
результат. Такая сумма не означает ничего, и ровно по трём причинам.

1. **Размер позиции.** Сделка со стопом 0.23% и сделка со стопом 30% — разные ставки, а
   сложение процентов приравнивает их. Замер по 299 настоящим записям: медиана дистанции
   стопа 4.0%, p10 = 1.0%, максимум 53.9%, и 17 записей ниже 0.3%. Складывать их напрямую —
   всё равно что складывать выигрыш по ставке в рубль с выигрышем по ставке в тысячу.
   Единица, в которой слагаемые сопоставимы, — **R**, результат в долях риска сделки:
   `R = ход_цены_% / дистанция_стопа_%`. Ровно это и даёт фиксированно-долевой сайзинг: если
   каждая сделка рискует одной и той же долей капитала, её вклад в счёт и есть R.

2. **Издержки.** Их не было в счёте вообще. И это не мелкая поправка, потому что комиссия
   берётся с **НОМИНАЛА**, а номинал при фиксированном риске обратно пропорционален стопу:
   `номинал = бюджет_риска / дистанция_стопа`. Значит издержка в единицах R равна
   `издержка_% / дистанция_стопа_%` — **чем теснее стоп, тем дороже сделка в долях риска.**
   При стопе 0.3% круговой оборот 0.07% съедает 0.23R, то есть почти четверть риска, ещё до
   того как цена куда-то пошла. Именно поэтому тесные стопы обязаны считаться отдельно, а не
   усредняться с широкими.

3. **Перекрытия.** Сумма процентов молча утверждает, что каждая сделка получила весь капитал
   и что сделки шли по очереди. Замер: **максимум 32 одновременно открытых позиции**. При
   риске 1% на сделку это 32% капитала под риском разом — не портфель, а рулетка. Реальный
   счёт обязан идти по времени и отказывать сделке, на которую не осталось лимита; отказ —
   это часть результата, а не помеха ему, поэтому число пропущенных печатается.

Что здесь ИЗМЕРЕНО, а что НАЗНАЧЕНО — граница проведена намеренно, её нельзя размывать.
ИЗМЕРЕНО из данных: ход цены, дистанция стопа, длительность, спред (`spread_bps`, есть у 220
из 283), ставка фандинга там, где она ненулевая (22 записи из 224), интервал фандинга (4ч у
126, 8ч у 93). НАЗНАЧЕНО мной как политика: доля риска на сделку, потолок одновременного
риска, плечевой потолок, тариф комиссий (VIP 0 — мейкер 0.02%, тейкер 0.05%). Политика
меняется параметром и обязана проверяться на чувствительность; измеренное — не обязано.

⚠ Кривая капитала — МОДЕЛЬ, а не замер. Она отвечает на вопрос «что дал бы этот поток
сигналов при такой-то политике», а не «сколько заработано». Настоящих исполнений у нас нет:
это аналитика сигналов, а не торговый бот.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl
import structlog
from pydantic import BaseModel, Field

from hunt_core.track.pnl import entry_base, realized_pct
from hunt_core.track.pnl import stop_distance_pct as _stop_distance_pct

LOG = structlog.get_logger(__name__)

# Binance USDⓈ-M VIP 0, сверено 2026-07-27 (binance.com/en/fee/futureFee).
_MAKER_FEE_PCT = 0.02
_TAKER_FEE_PCT = 0.05

# Выходы, исполняемые ПО РЫНКУ, то есть по тейкерскому тарифу. Стоп-маркет и любое закрытие
# «по решению» пересекают спред; лимитная цель — нет.
_TAKER_EXIT_REASONS = frozenset(
    {
        "stop_hit",
        "stop_loss",
        "trailing_stop_profit",
        "timeout",
        "orphan_expired",
        "time_stall",
        "bias_flip",
        "lifecycle_stale",
        "opposite_signal",
        "bounce_invalidate",
        "trend_exhaustion",
        "reclaim_invalidation",
        "support_lost",
    }
)

# Медиана `spread_bps` по 220 записям, где поле измерено (2026-07-27): 2.439 бп. Ставится
# только там, где своего замера нет. Половина спреда на сторону — минимальная цена пересечения
# книги; это ПОЛ издержки проскальзывания, а не её оценка, и занижает результат в пользу
# осторожности.
_FALLBACK_SPREAD_BPS = 2.439

# ⚠ ПОДСТАВНОЙ СТАВКИ ФАНДИНГА БОЛЬШЕ НЕТ — и это отмена моего же прежнего решения.
#
# Здесь стояла медиана ненулевых ставок (0.005% за интервал), которой заменялся ЛЮБОЙ ноль.
# Обоснование было: «отличить настоящий нуль от незаполненного поля нельзя». Проверка это
# опровергла:
#   * `feature_engine._coerce_float(None)` возвращает `None`, а не `0.0` — продюсер нулей
#     не изобретает, значит записанный `0.0` пришёл от движка;
#   * у ВСЕХ 202 нулевых записей заполнены соседние фандинг-поля (`funding_zscore_48h`,
#     `funding_interval_h`, `funding_trend`) — блок собирался целиком;
#   * на живой бирже ровно нулевую ставку имеют 29.6% символов (252 из 851) — нуль штатен.
# Записанный ноль — измерение. Заменять его медианой значило выдумывать число ПОВЕРХ данных.
#
# ⚠ Открытый вопрос, который правка НЕ закрывает: у нас нулей 90% против 29.6% на бирже —
# втрое больше. Это подозрение на дефект продюсера, и мерить его надо отдельно. Подстановка
# медианы как раз маскировала бы его.
_DEFAULT_FUNDING_INTERVAL_H = 8.0

# Ниже этого стоп неотличим от шума и означал бы фантастическое плечо: при 0.01% требуется
# 10000×. Замер: 7 записей ниже 0.1%, минимум 0.0002%. Такие сделки не «маленький риск», а
# отсутствие измеримого риска — они исключаются И ПЕРЕСЧИТЫВАЮТСЯ ОТДЕЛЬНО, а не сайзятся.
_MIN_STOP_DIST_PCT = 0.05


class SizingPolicy(BaseModel):
    """Политика капитала. Это НАЗНАЧЕННЫЕ величины, а не измеренные.

    Attributes:
        risk_per_trade_pct: Доля капитала под риском в одной сделке.
        max_concurrent_risk_pct: Потолок суммарного риска по открытым позициям. Сигнал,
            которому не хватило лимита, пропускается и попадает в счётчик пропусков.
        max_leverage: Потолок плеча. Без него тесный стоп требует номинала, которого биржа
            не даст: при стопе 0.06% фиксированный риск 1% просит 16× — уже у предела.
        start_equity: Стартовый капитал модели; на форму кривой не влияет, только на масштаб.
    """

    risk_per_trade_pct: float = Field(default=1.0, gt=0.0, le=100.0)
    max_concurrent_risk_pct: float = Field(default=6.0, gt=0.0, le=100.0)
    max_leverage: float = Field(default=20.0, gt=0.0)
    start_equity: float = Field(default=10_000.0, gt=0.0)


class CostModel(BaseModel):
    """Тариф и издержки удержания. Комиссии — тариф биржи, остальное — замер или пол."""

    maker_fee_pct: float = Field(default=_MAKER_FEE_PCT, ge=0.0)
    taker_fee_pct: float = Field(default=_TAKER_FEE_PCT, ge=0.0)
    fallback_spread_bps: float = Field(default=_FALLBACK_SPREAD_BPS, ge=0.0)
    # ⚠ `fallback_funding_pct` УДАЛЁН, а не оставлен «на всякий случай». Он подставлял
    # медиану вместо записанного нуля, то есть выдумывал число поверх измерения (I-6).
    # Ручка без читателя — свой класс дефекта: она выглядит настройкой и ничего не меняет.


class TradeAccounting(BaseModel):
    """Одна сделка, посчитанная в номинале и в R."""

    symbol: str
    direction: str
    opened_at: dt.datetime
    closed_at: dt.datetime
    close_reason: str
    gross_pct: float
    stop_dist_pct: float
    cost_pct: float
    net_pct: float
    r_gross: float
    r_cost: float
    r_net: float
    leverage: float
    leverage_capped: bool


class EquityResult(BaseModel):
    """Итог прогона по портфелю."""

    policy: SizingPolicy
    start_equity: float
    final_equity: float
    total_return_pct: float
    # ⚠ ИМЯ НАЗЫВАЕТ РОВНО ТО, ЧТО ИЗМЕРЕНО. Раньше поле звалось `max_drawdown_pct` и
    # читалось как просадка портфеля, хотя `_settle_until` двигает пик и минимум ТОЛЬКО при
    # ЗАКРЫТИИ сделки. Портфель из 32 одновременных позиций может быть глубоко в минусе без
    # единого закрытия, и это число ничего не заметит: проверено — оно побитово одинаково
    # при 13 и при 32 одновременных позициях.
    #
    # Настоящую нереализованную просадку посчитать НЕЧЕМ: в кадре нет ценового пути внутри
    # сделки, только цена выхода. Выдумать её было бы хуже, чем назвать вещи своими именами,
    # поэтому рядом печатается ВТОРАЯ величина — пик одновременно развёрнутого риска. Она
    # ограничивает нереализованную просадку сверху: если бы все открытые позиции разом
    # дошли до стопа, счёт потерял бы ровно её.
    realized_drawdown_pct: float
    max_open_risk_pct: float
    trades_taken: int
    trades_skipped_no_capital: int
    trades_unsizeable: int
    r_net_sum: float
    r_gross_sum: float
    r_cost_sum: float
    fees_paid: float
    max_concurrent: int


def _num(value: Any) -> float | None:
    """Положительное число или None — вход из JSONL не типизирован."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0.0 else None


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)


# ⚠ Геометрия входа и риска НЕ дублируется здесь — она живёт в `track/pnl.py` вместе с
# формулой результата, и `tracker.close_signal` пользуется той же. Две копии базы входа уже
# однажды разошлись (середина зоны против худшей кромки) и впрыснули половину ширины зоны в
# каждый R, включая живой гейт `_cooldowns.net_r`. Имена ре-экспортируются ради вызывающих.
risk_base = entry_base
stop_distance_pct = _stop_distance_pct


def _exit_fee_pct(row: dict[str, Any], costs: CostModel) -> float:
    """Комиссия выхода с учётом частичной фиксации на первой цели.

    Метод банкует часть на TP1 лимитом и ведёт остаток. Это ДВА выхода с разными тарифами,
    поэтому единая ставка здесь была бы враньём в обе стороны сразу.
    """
    reason = str(row.get("close_reason") or "")
    final_fee = costs.taker_fee_pct if reason in _TAKER_EXIT_REASONS else costs.maker_fee_pct
    fixed_pct = float(row.get("partial_fixed_pct") or 0.0)
    if row.get("tp1_hit") and 0.0 < fixed_pct < 100.0:
        frac = fixed_pct / 100.0
        return frac * costs.maker_fee_pct + (1.0 - frac) * final_fee
    return final_fee


def _funding_pct(row: dict[str, Any], costs: CostModel) -> tuple[float, bool]:
    """Стоимость удержания в процентах от номинала и БЫЛА ЛИ ОНА ИЗМЕРЕНА.

    ⚠ ЗАПИСАННЫЙ НОЛЬ — ЭТО ИЗМЕРЕНИЕ, А НЕ ПРОПУСК. Прежняя редакция делала
    `if rate_pct <= 0.0: rate_pct = fallback` и подменяла медианой И отсутствие поля, И
    настоящий нуль — то есть выдумывала число поверх данных (I-6, в моём же коде).

    Почему ноль здесь настоящий. Продюсер нулей не изобретает:
    `feature_engine._coerce_float(None)` возвращает `None`, а не `0.0`, так что записанный
    `0.0` пришёл от движка. Замер по 283 записям: 202 нуля, и у ВСЕХ 202 заполнены соседние
    фандинг-поля (`funding_zscore_48h`, `funding_interval_h`, `funding_trend`) — блок
    фандинга собирался целиком. На живой бирже ровно нулевую ставку имеют 29.6% символов
    (252 из 851), то есть нуль — штатное значение, а не признак поломки.

    ⚠ ОТКРЫТЫЙ ВОПРОС, НЕ ЗАКРЫТЫЙ ЭТОЙ ПРАВКОЙ: у нас нулей 90% (202 из 224 с полем)
    против 29.6% на бирже — втрое больше. Это подозрение на дефект ПРОДЮСЕРА, и его надо
    измерять отдельно. Подменять медианой значило бы замаскировать его: теперь заниженный
    фандинг виден как «издержка ≈ 0», а не растворён в выдуманном числе.

    Знак не учитывается: фандинг бывает и в пользу позиции, но направление ставки за время
    удержания не измерено — записан лишь срез на закрытии. Брать его как доход значило бы
    подарить портфелю то, чего не измеряли, поэтому удержание всегда считается РАСХОДОМ.

    Returns:
        ``(процент, измерено)``. При ``measured=False`` возвращается 0.0, и полная издержка
        сделки становится НИЖНЕЙ ОЦЕНКОЙ — вызывающий обязан это показать, а не выдать за
        точное значение.
    """
    del costs  # ставка больше не подставляется — только измеренная либо никакой
    market = (row.get("features_close") or {}).get("market") or {}
    rate = market.get("funding_rate")
    if rate is None:
        return 0.0, False
    try:
        rate_pct = abs(float(rate)) * 100.0
    except (TypeError, ValueError):
        return 0.0, False
    interval_h = _num(market.get("funding_interval_h")) or _DEFAULT_FUNDING_INTERVAL_H
    duration_min = float(row.get("duration_min") or 0.0)
    intervals = max(0.0, duration_min / (interval_h * 60.0))
    return rate_pct * intervals, True


def _slippage_pct(row: dict[str, Any], costs: CostModel) -> float:
    """Пол проскальзывания: половина спреда на каждую сторону."""
    market = (row.get("features_close") or {}).get("market") or {}
    spread_bps = market.get("spread_bps")
    try:
        bps = float(spread_bps) if spread_bps is not None else costs.fallback_spread_bps
    except (TypeError, ValueError):
        bps = costs.fallback_spread_bps
    if bps <= 0.0:
        bps = costs.fallback_spread_bps
    return bps / 100.0  # полспреда × две стороны = целый спред; бп → проценты


def cost_pct_of_notional(
    row: dict[str, Any], costs: CostModel | None = None
) -> tuple[float, bool]:
    """Полная круговая издержка сделки в процентах от НОМИНАЛА и полнота замера.

    Вход считается мейкерским: метод заходит лимитной лестницей в зону, а не по рынку.

    Returns:
        ``(процент, фандинг_измерен)``. При ``False`` фандинг в сумму не вошёл, и величина
        является НИЖНЕЙ ОЦЕНКОЙ издержки, а не её значением.
    """
    model = costs or CostModel()
    funding, measured = _funding_pct(row, model)
    total = (
        model.maker_fee_pct
        + _exit_fee_pct(row, model)
        + _slippage_pct(row, model)
        + funding
    )
    return total, measured


def leg_verdict_key(row: dict[str, Any]) -> str:
    """Ключ вердикта достижимости — символ + момент открытия.

    Тот же ключ пишет `scripts/verify_trade_legs.py`. Держать его здесь, а не в скрипте,
    обязательно: разъехавшиеся ключи дали бы пустое пересечение, и «чистая подвыборка» тихо
    оказалась бы пустой вместо ошибки.
    """
    return f"{str(row.get('symbol') or '').upper()}|{row.get('opened_at')}"


def build_trade_frame(
    rows: list[dict[str, Any]],
    *,
    costs: CostModel | None = None,
    leg_verdicts: dict[str, dict[str, Any]] | None = None,
) -> pl.DataFrame:
    """Поколоночный расчёт по сделкам: ход, риск, издержки, R.

    Полярная часть — вся арифметика; последовательным остаётся только распределение
    капитала (`simulate_equity`), потому что оно зависит от пути: пропуск одной сделки меняет
    состав открытых позиций дальше и векторизации не поддаётся.

    Returns:
        Кадр по сделкам, пригодным к сайзингу. Непригодные (нет геометрии либо стоп ниже
        `_MIN_STOP_DIST_PCT`) отброшены — их число возвращает `unsizeable_count`.
    """
    model = costs or CostModel()
    prepared: list[dict[str, Any]] = []
    for row in rows:
        opened, closed = _parse_ts(row.get("opened_at")), _parse_ts(row.get("closed_at"))
        dist = stop_distance_pct(row)
        # ⚠ РЕЗУЛЬТАТ ПЕРЕСЧИТЫВАЕТСЯ ИЗ ГЕОМЕТРИИ, ХРАНИМЫЙ `pnl_pct` НЕ ЧИТАЕТСЯ.
        #
        # В колонке `pnl_pct` смешаны ТРИ поколения формулы: пересчёт 283 записей по текущей
        # конвенции даёт +976.2% против хранимых +1575.2% — расхождение 599 п.п. Хуже, что
        # у 157 из 168 невырожденных зон хранимое число писалось от СЕРЕДИНЫ зоны, тогда как
        # `stop_distance_pct` ниже меряет риск от ХУДШЕЙ кромки: числитель и знаменатель R
        # брались от разных точек отсчёта.
        #
        # `realized_pct` — та же и единственная формула, которой считает `tracker.close_signal`
        # (`track/pnl.py`). Держать вторую копию здесь значило бы воспроизвести ровно тот
        # дефект, который правка закрывает.
        realized = realized_pct(row)
        gross = realized[0] if realized is not None else None
        if opened is None or closed is None or dist is None or gross is None:
            continue
        if dist < _MIN_STOP_DIST_PCT:
            continue
        cost, funding_measured = cost_pct_of_notional(row, model)
        verdict = (leg_verdicts or {}).get(leg_verdict_key(row))
        prepared.append(
            {
                "symbol": str(row.get("symbol") or "?"),
                "direction": str(row.get("direction") or "?"),
                "opened_at": opened,
                "closed_at": closed,
                "close_reason": str(row.get("close_reason") or "?"),
                "gross_pct": float(gross),
                "stop_dist_pct": float(dist),
                "cost_pct": cost,
                # Фандинг не измерен → издержка НИЖНЯЯ ОЦЕНКА, не значение. Флаг едет в
                # кадр, чтобы отчёт мог показать покрытие, а не выдать оценку за факт.
                "funding_measured": funding_measured,
                # ⚠ ОПИРАЕТСЯ ЛИ СДЕЛКА НА ЦЕНУ, КОТОРОЙ НЕ БЫЛО.
                #
                # `None` — «не проверено» (вердиктов не передали), и это НЕ «чисто»: смешать
                # два значения значило бы выдать непроверенное за проверенное (I-6).
                #
                # ЗАМЕР `scripts/verify_trade_legs.py` по 283 записям: у **89** хотя бы одна
                # нога недостижима. На подвыборке, где обе ноги реальны, среднее уходит в
                # минус с интервалом, накрывающим ноль, — то есть весь плюс результата
                # держится на сделках, которых рынок не подтверждает.
                "legs_reachable": (
                    None if verdict is None else bool(verdict.get("both_reachable"))
                ),
            }
        )
    if not prepared:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8, "direction": pl.Utf8,
                "opened_at": pl.Datetime(time_zone="UTC"),
                "closed_at": pl.Datetime(time_zone="UTC"),
                "close_reason": pl.Utf8, "gross_pct": pl.Float64,
                "stop_dist_pct": pl.Float64, "cost_pct": pl.Float64,
                "funding_measured": pl.Boolean, "legs_reachable": pl.Boolean,
            }
        )
    return (
        pl.DataFrame(prepared)
        .lazy()
        .with_columns(
            (pl.col("gross_pct") - pl.col("cost_pct")).alias("net_pct"),
            (pl.col("gross_pct") / pl.col("stop_dist_pct")).alias("r_gross"),
            (pl.col("cost_pct") / pl.col("stop_dist_pct")).alias("r_cost"),
        )
        .with_columns((pl.col("r_gross") - pl.col("r_cost")).alias("r_net"))
        .sort("opened_at")
        .collect()
    )


def unsizeable_count(rows: list[dict[str, Any]]) -> int:
    """Сколько записей нельзя оценить: нет геометрии либо стоп ниже порога шума."""
    bad = 0
    for row in rows:
        dist = stop_distance_pct(row)
        if (
            _parse_ts(row.get("opened_at")) is None
            or _parse_ts(row.get("closed_at")) is None
            or row.get("pnl_pct") is None
            or dist is None
            or dist < _MIN_STOP_DIST_PCT
        ):
            bad += 1
    return bad


def simulate_equity(
    rows: list[dict[str, Any]],
    *,
    policy: SizingPolicy | None = None,
    costs: CostModel | None = None,
) -> EquityResult:
    """Прогнать поток сигналов по портфелю с лимитом одновременного риска.

    Ход по времени: сделка размеряется по капиталу НА МОМЕНТ ВХОДА, и если свободного лимита
    риска не хватает — пропускается. Пропуски считаются: без этой цифры результат читался бы
    как «столько дали сигналы», хотя часть из них взять было физически нечем.
    """
    pol = policy or SizingPolicy()
    model = costs or CostModel()
    frame = build_trade_frame(rows, costs=model)
    result_base = EquityResult(
        policy=pol,
        start_equity=pol.start_equity,
        final_equity=pol.start_equity,
        total_return_pct=0.0,
        realized_drawdown_pct=0.0,
        max_open_risk_pct=0.0,
        trades_taken=0,
        trades_skipped_no_capital=0,
        trades_unsizeable=unsizeable_count(rows),
        r_net_sum=0.0,
        r_gross_sum=0.0,
        r_cost_sum=0.0,
        fees_paid=0.0,
        max_concurrent=0,
    )
    if frame.is_empty():
        return result_base

    risk_frac = pol.risk_per_trade_pct / 100.0
    equity = pol.start_equity
    peak = equity
    max_dd = 0.0
    open_positions: list[tuple[dt.datetime, float, float]] = []  # (closed_at, risk_usd, pnl)
    taken = skipped = 0
    max_conc = 0
    max_open_risk = 0.0  # пик одновременно развёрнутого риска, в % капитала
    r_net_sum = r_gross_sum = r_cost_sum = 0.0
    fees_paid = 0.0

    def _settle_until(moment: dt.datetime) -> None:
        """Закрыть все позиции, чей выход раньше `moment`, и обновить капитал."""
        nonlocal equity, peak, max_dd, open_positions
        due = sorted((p for p in open_positions if p[0] <= moment), key=lambda p: p[0])
        for closed_at, _risk, pnl in due:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)
        open_positions = [p for p in open_positions if p[0] > moment]

    for trade in frame.iter_rows(named=True):
        opened = trade["opened_at"]
        _settle_until(opened)
        deployed = sum(p[1] for p in open_positions)
        budget = equity * risk_frac
        headroom = equity * (pol.max_concurrent_risk_pct / 100.0) - deployed
        if budget > headroom or equity <= 0.0:
            skipped += 1
            continue
        dist_frac = trade["stop_dist_pct"] / 100.0
        notional = min(budget / dist_frac, equity * pol.max_leverage)
        risk_usd = notional * dist_frac
        pnl_usd = notional * (trade["net_pct"] / 100.0)
        fees_paid += notional * (trade["cost_pct"] / 100.0)
        scale = risk_usd / budget if budget > 0 else 0.0
        r_net_sum += trade["r_net"] * scale
        r_gross_sum += trade["r_gross"] * scale
        r_cost_sum += trade["r_cost"] * scale
        open_positions.append((trade["closed_at"], risk_usd, pnl_usd))
        max_conc = max(max_conc, len(open_positions))
        # Пик развёрнутого риска — верхняя граница нереализованной просадки. Считается ПОСЛЕ
        # добавления позиции: именно в этот момент экспозиция максимальна.
        if equity > 0:
            open_risk = sum(p[1] for p in open_positions) / equity * 100.0
            max_open_risk = max(max_open_risk, open_risk)
        taken += 1

    _settle_until(dt.datetime.max.replace(tzinfo=dt.UTC))
    return result_base.model_copy(
        update={
            "final_equity": equity,
            "total_return_pct": (equity / pol.start_equity - 1.0) * 100.0,
            "realized_drawdown_pct": max_dd,
            "max_open_risk_pct": max_open_risk,
            "trades_taken": taken,
            "trades_skipped_no_capital": skipped,
            "r_net_sum": r_net_sum,
            "r_gross_sum": r_gross_sum,
            "r_cost_sum": r_cost_sum,
            "fees_paid": fees_paid,
            "max_concurrent": max_conc,
        }
    )


__all__ = [
    "CostModel",
    "EquityResult",
    "SizingPolicy",
    "TradeAccounting",
    "build_trade_frame",
    "cost_pct_of_notional",
    "risk_base",
    "simulate_equity",
    "stop_distance_pct",
    "unsizeable_count",
]
