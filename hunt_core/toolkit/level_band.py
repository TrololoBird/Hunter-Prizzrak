"""Ширина уровневой полосы δ(τ), выведенная из данных, а не назначенная константой.

Публикация, из которой взята формула:

    Garzarelli, Cristelli, Zaccaria, Pietronero, *Scientific Reports* 4:4487 (2014), Eq. 1
    Chung & Bellotti, arXiv:2101.07410 (2021)

Уровень моделируется полосой полуширины δ, и Garzarelli задают её **средним абсолютным
приращением цены на том же масштабе выборки**:

    δ(τ) = ⟨ |P(t_{k+1}) − P(t_k)| ⟩

Смысл — «допуск инвесторов»: «The width of the stripe represents the tolerance of the investors
on a given support or resistance: if the price drops below this threshold the investors regard
the support or resistance as broken». Авторы отдельно отмечают δ(τ) ~ τ^α, то есть полоса
ОБЯЗАНА масштабироваться с таймфреймом и фиксированным процентом быть не может.

## Почему это заменило константу

Замер `scripts/calibrate_level_band.py` (4 символа × 3 ТФ, 40 перемешиваний по 1000 баров).
Критерий Chung & Bellotti: верная полоса даёт на СЛУЧАЙНОМ БЛУЖДАНИИ p(отскок) = 0.5; шире —
полоса производит отскоки из ничего, уже — производит пробои.

    полоса                        p(отскок) на случайном ряде
    δ(τ) (эта формула)            0.469 … 0.544, медиана 0.516   ← нейтральна
    0.6% (`_CLUSTER_TOL` было)    0.547 (BTC 1h) … 0.732 (BTC 15m)
    1.0% (`_MATCH_TOL_PCT`)       0.610 (BTC 1h) … 0.818 (BTC 15m)

То есть при полосе 1% случайный ряд «отскакивал» в 82% случаев — любая измеренная на такой
полосе частота отскоков не значила ничего. Живое превышение над нулём на δ-полосе при этом
сохраняется и положительно: +3.7 … +16.4 п.п.

Измеренные δ(τ): 15m 0.099–0.140%, 1h 0.274–0.409%, 4h 0.631–0.906%. Фиксированные 0.6% были
вдвое ШИРЕ нужного на 15m/1h и УЖЕ нужного на 4h — то есть ошибались в обе стороны сразу.
"""
from __future__ import annotations

from collections.abc import Sequence

import polars as pl

# Границы санитарной обрезки. Нужны не «на всякий случай», а против двух конкретных вырождений:
# сверхтонкий ряд (все бары одинаковы → δ=0 → кластеризация склеит всё в один уровень) и
# аварийный выброс (одна свеча на 40% → δ раздувается и зона поглотит весь график).
# Значения — из измеренного диапазона δ с запасом по краям: минимум вдвое ниже самого
# тонкого замеренного (15m BTC 0.099%), максимум вдвое выше самого широкого (4h SOL 0.906%).
MIN_BAND_PCT = 0.05
MAX_BAND_PCT = 2.0


def mean_abs_increment_pct(closes: Sequence[float]) -> float | None:
    """δ(τ) в процентах цены, либо ``None``, если мерить не на чем.

    Fail-loud: меньше ``min_n`` точек — не оценка, а совпадение, и лучше вернуть ``None``,
    чем правдоподобное число (I-6). Вызывающий обязан решить, что делать без замера.

    Args:
        closes: Цены закрытия подряд идущих баров ОДНОГО таймфрейма.

    Returns:
        Среднее абсолютное приращение в процентах, обрезанное в
        ``[MIN_BAND_PCT, MAX_BAND_PCT]``, либо ``None``.
    """
    if len(closes) < 30:
        return None
    # Polars Expression API, а не питон-цикл: правило проекта — считать выражениями, а не
    # руками. `pct_change().abs().mean()` — это в точности δ(τ) в процентах.
    #
    # ⚠ Плагины проверены и НЕ подошли, и это стоит записать, чтобы не проверяли заново:
    # `polars_ds.query_mean_abs_change` даёт то же самое, но в АБСОЛЮТНЫХ единицах цены —
    # для полосы нужны проценты, иначе она не переносится между символами. А `query_mad`,
    # на который тянет имя, считает СРЕДНЕЕ отклонение от СРЕДНЕГО (замер: 26.389 против
    # медианного 3.0 на одном ряду) — точка слома 0%, а не 50%, робастной оценкой не является.
    series = pl.Series("close", [float(c) for c in closes], dtype=pl.Float64)
    delta = (
        pl.DataFrame({"close": series})
        .select(
            (pl.col("close").pct_change().abs().mean() * 100.0).alias("delta")
        )["delta"][0]
    )
    if delta is None or not (delta > 0.0):
        return None
    return min(MAX_BAND_PCT, max(MIN_BAND_PCT, float(delta)))


def level_band_fraction(bars: Sequence[dict[str, float]], *, fallback: float) -> float:
    """δ(τ) как ДОЛЯ (0.006 = 0.6%) по барам, с явным запасным значением.

    Args:
        bars: Бары с ключом ``close``.
        fallback: Что вернуть, если замерить не удалось (обычно прежняя константа —
            так поведение на коротком окне остаётся ровно прежним, а не «немного другим»).

    Returns:
        Полуширина полосы как доля цены.
    """
    closes = [float(b["close"]) for b in bars if b.get("close")]
    delta = mean_abs_increment_pct(closes)
    return fallback if delta is None else delta / 100.0


def level_band_from_ohlcv(ohlcv: Sequence[Sequence[float]], *, fallback: float) -> float:
    """δ(τ) как ДОЛЯ по сырым OHLCV-строкам (``close`` — индекс 4).

    Args:
        ohlcv: Строки ``[ts, open, high, low, close, volume]`` одного таймфрейма.
        fallback: Значение при невозможности замера (обычно прежняя константа — так на
            коротком окне поведение остаётся ровно прежним, а не «немного другим»).

    Returns:
        Полуширина полосы как доля цены.
    """
    closes: list[float] = []
    for row in ohlcv:
        if len(row) > 4:
            closes.append(float(row[4]))
    delta = mean_abs_increment_pct(closes)
    return fallback if delta is None else delta / 100.0


__all__ = [
    "MAX_BAND_PCT",
    "MIN_BAND_PCT",
    "level_band_fraction",
    "level_band_from_ohlcv",
    "mean_abs_increment_pct",
]
