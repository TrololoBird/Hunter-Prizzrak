"""Единая точка расчёта индикаторов: КАНОН — TA-Lib, `polars_ta` — только там, где канона нет.

⚠ ГЛАВНОЕ, ЧТО НАДО ЗНАТЬ ПРЕЖДЕ ПРАВКИ. `polars_ta` (wukan1986) — это **переосмысление**
TA-Lib, а не обёртка над ней, и расходится с каноном НАМЕРЕННО. Пока мост стоял на ней одной,
здесь копился слой ручных поправок, каждая из которых компенсировала одно такое расхождение —
и каждая была найдена дефектом на живых данных, а не проектированием:

* **шкала** — у `RSI`/`MFI`/`RSV`/`STOCHF`/`WILLR`/`TRIX`/`PPO`/`AROON` в исходнике `polars_ta`
  строка `# * 100` ЗАКОММЕНТИРОВАНА, то есть возвращается доля [0,1], а не проценты;
* **окно Aroon** — `polars_ta` сканирует N баров, TA-Lib N+1;
* **эпсилон в единицах ЦЕНЫ** — `RSI = RMA(gain)/(RMA(|Δ|) + 1e-8)`.

Замер 2026-07-27 на 27 живых 15m кадрах (цены 9.4e-06 … 64992), эталон — вендоренная
**TA-Lib 0.4.0** в `polars_talib` (другой код, не наш стек):

| поле | было против канона |
|---|---|
| `aroon_up14`/`aroon_down14` | расхождение на 20–22% баров, **инверсия до 100 пунктов** |
| `rsi14` | до **28.3 пункта** на дешёвых монетах (1000SATS), смещение всегда ВНИЗ |
| `mfi14` | 22.6% баров, до 16.7 пункта (брался вариант TDX, а не канон) |
| `bb_upper`/`bb_lower` | 5.6% относительных на 100% баров (расходилось только СКО) |
| `trix14`, `ppo12_26` | ровно **в 100 раз меньше** канона |

Поэтому правило теперь такое: **если у индикатора есть каноническое имя TA-Lib — берём его из
`polars_talib` (`tl.*`).** `polars_ta` остаётся ровно там, где аналога в TA-Lib НЕТ: `tdx`
(KDJ/PSY/BIAS/EMV/DPO/BOLL/MTM) и `wq` (`ts_rank`/`ts_delta`/`ts_corr`).

⚠ **Не «мигрировать всё подряд».** Проверено тем же замером: `adx_from_polars_ta` (собственная
реализация Уайлдера), `cci20` (вариант TDX), `ema20`, `roc10`, `obv`, `stoch`, `willr`, `atr`,
`natr`, `macd` уже совпадали с каноном (отн. ≤ 2.6e-5, расхождение только на прогреве). Замена
ради единообразия там ничего не чинит, а трогает работающее.

Проверка паритета — `scripts/verify_talib_parity.py`, на ЖИВЫХ данных.
Разбор: `docs/audit/talib-parity-2026-07-27.md`.
"""
from __future__ import annotations



import math
from collections.abc import Callable

import polars as pl
import polars_ta.ta as plta
import polars_ta.tdx as ptdx
import polars_ta.wq as wq
import polars_talib as tl
import structlog

from hunt_core.errors import DEFENSIVE_EXC
from .shared import clean_non_finite, materialize_series, wilder_mean

LOG = structlog.get_logger("hunt_core.features.polars_ta_bridge")
_BRIDGE_SKIPS: set[str] = set()

BROKEN_PLTA_FUNCTIONS: frozenset[str] = frozenset({"SMA", "WMA", "KAMA", "LINEARREG"})


def polars_ta_available() -> bool:
    return True


def _log_skip(name: str, exc: BaseException) -> None:
    if name not in _BRIDGE_SKIPS:
        _BRIDGE_SKIPS.add(name)
        LOG.info("polars_ta bridge skip", indicator=name, error=str(exc))


def _select_series(df: pl.DataFrame, expr: pl.Expr | pl.Series, *, name: str) -> pl.Series:
    return materialize_series(expr, df=df, name=name)


def _percent_scale(series: pl.Series, *, name: str) -> pl.Series:
    """Доля [0,1] бэкенда `polars_ta` → канонические проценты [0,100]. КОНСТАНТОЙ.

    ⚠ ЗДЕСЬ БЫЛ LOOKAHEAD, и это не теория. Прежняя редакция (`_normalize_percent_scale`)
    решала, умножать ли на 100, ГЛЯДЯ НА САМ РЯД: `if max <= 1.5 and min >= -0.01`. То есть
    масштаб строки зависел от значений ДРУГИХ баров, включая будущие относительно неё —
    добавление одного бара могло изменить прошлую строку в 100 раз (I-5). Ровно этот диагноз
    уже был поставлен в `willr_from_polars_ta` и там исправлен, но в шести других местах
    (`rsi14`, `mfi14`, `kdj_k14`, `kdj_d14`, `stoch_k14`, `stoch_d14`, плюс `rsv14`/`psy12`)
    эвристика осталась жить.

    Почему это не «сработало бы всё равно правильно»: условие ложно ещё и в обратную сторону.
    Осциллятор, УЖЕ отдающий 0…100, но прижатый к нулю (предельная перепроданность — как раз
    тот случай, ради которого его и читают), проходит `max <= 1.5` и получает лишние ×100 —
    то есть значение переворачивается в предельную ПЕРЕкупленность. Эвристика ошибалась бы
    именно на экстремуме, который должна была различать.

    Диапазон бэкенда известен из его исходника — гадать по данным незачем.
    """
    return (series.cast(pl.Float64, strict=False) * 100.0).rename(name)


def _null_where(series: pl.Series, df: pl.DataFrame, *, degenerate: pl.Expr) -> pl.Series:
    """Заменить значение на ``null`` там, где окно ВЫРОЖДЕНО (I-6).

    ⚠ САМА TA-Lib ЗДЕСЬ ВНУТРЕННЕ ПРОТИВОРЕЧИВА, и это не наша выдумка. Замер 2026-07-27 на
    абсолютно плоском окне (H == L == C, 20 баров) даёт ОДНОВРЕМЕННО на ОДНОМ баре:
    ``rsi = 100`` (предельная ПЕРЕкупленность), ``stoch_k = 0`` (предельная ПЕРЕПРОДАННОСТЬ),
    ``willr = 0`` (предельная ПЕРЕкупленность), ``mfi = 0`` (предельная перепроданность).
    Все четыре — это `0/0`, разрешённый в разные стороны разными функциями библиотеки.

    Канон здесь брать НЕЛЬЗЯ: I-6 требует, чтобы отсутствие данных выглядело отсутствием, а не
    крайним значением шкалы. Отклонение от TA-Lib осознанное и ровно одно — вырожденный вход.

    ⚠ Насколько это часто: замер по 60 живым символам × 285 окон — плоских по H/L **0 из
    17100**, плоских по close 1 (0.006%). То есть на ликвидном перпе случай не встречается,
    и гард стоит здесь НЕ ради него, а ради ЗАМЕРШЕГО КАДРА: застрявший фид — самый дорогой
    класс инцидентов в этом проекте (память `stale-htf-cache-trap`), и «кадр замер» плюс
    «предельная перепроданность» — худшее из возможных сочетаний.
    """
    if df.is_empty():
        return series
    return (
        df.lazy()
        .select(
            pl.when(degenerate)
            .then(None)
            .otherwise(pl.lit(series))
            .cast(pl.Float64)
            .alias(series.name)
        )
        .collect()[series.name]
    )


def _flat_hl(period: int) -> pl.Expr:
    """Окно, где весь диапазон схлопнут: ``max(high) == min(low)`` → у стохастика 0/0."""
    n = int(period)
    return pl.col("high").rolling_max(n, min_samples=n) <= pl.col("low").rolling_min(n, min_samples=n)


def _price_norm(df: pl.DataFrame) -> float:
    """Множитель, поднимающий цену к порядку ~100, чтобы АБСОЛЮТНЫЕ эпсилоны TA-Lib не срабатывали.

    ⚠ У TA-Lib СВОЙ эпсилон в единицах цены, и он ХУЖЕ, чем у `polars_ta`. `ta_utility.h`
    сравнивает величины с фиксированным `TA_EPSILON` (~1e-8) БЕЗ поправки на масштаб
    инструмента — допущение «акция стоит десятки долларов», зашитое в 1990-х. На дешёвом
    перпе это не смещение, а СХЛОП В КРАЙНЕЕ ЗНАЧЕНИЕ: замер 2026-07-27 на 1000SATS (цена
    9.4e-06) — 24 бара, где `tl.rsi` отдаёт РОВНО 0.0 (предельная перепроданность) при
    реальном приросте `gain14 = 6e-8 > 0`. Истинное значение того бара — **47.0**, то есть
    нейтраль. Для сравнения `polars_ta` на том же баре даёт 23.1 (своё смещение вниз).
    Гейт «RSI < 30 = перепроданность» срабатывал бы у ОБЕИХ библиотек там, где не должен.

    Почему это лечится умножением: RSI, MFI, стохастик, WILLR, Aroon, NATR, TRIX, PPO, ROC —
    ТОЧНО масштабно-инвариантны (и числитель, и знаменатель линейны по цене), поэтому
    `f(k·close) ≡ f(close)` в точной арифметике. Умножение не меняет ответ, а лишь уводит
    величины от абсолютного порога.

    ⚠ И ЭТО НЕ ТОТ LOOKAHEAD, ЧТО БЫЛ В `_normalize_percent_scale`, хотя выглядит похоже —
    множитель тоже берётся из данных. Разница принципиальная и проверяемая: там масштаб
    ВЫБИРАЛСЯ по данным и ПОПАДАЛ в результат (×100 или ×1 — ответ зависел от того, какие
    бары попали в окно). Здесь результат от множителя НЕ ЗАВИСИТ вовсе: замер на 1000SATS
    дал побитово одинаковый RSI при k = 1e2, 1e4, 1e6, 1e8. Будущий бар может сдвинуть k на
    порядок — на уже посчитанную строку это не влияет.

    ⚠ МНОЖИТЕЛЬ — СТЕПЕНЬ ДВОЙКИ, И ЭТО НЕ КОСМЕТИКА. Степень двойки представима в double
    точно, поэтому `price * k` не вносит округления и ВСЕ сравнения внутри индикатора
    сохраняются побитово. Со степенью десяти это неверно, и цена ошибки измерена: при
    нормировке ×10 у MFI на квантованных ценах переворачивались сравнения `TYP > prev(TYP)`
    (у SPELL 62 бара с точно неизменной типичной ценой, у 1000000MOG — 32), и значение
    уезжало **до 11.5 пункта**. То есть «нормировка» сама стала бы источником дефекта.

    Как отличили одно от другого: масштабирование степенями двойки на тех же кадрах даёт
    РОВНО НОЛЬ расхождений у MFI при любом k (2^10…2^40) — значит эпсилон там не активен
    вовсе, а всё расхождение было артефактом неточного пересчёта. У RSI при том же точном
    масштабировании остаются ровно те же 24 бара 1000SATS — вот это настоящий эпсилон.
    Отсюда и правило: нормируем ТОЛЬКО RSI, и только степенью двойки.
    """
    if df.is_empty() or "close" not in df.columns:
        return 1.0
    col = df["close"].cast(pl.Float64, strict=False).drop_nulls().drop_nans()
    if col.is_empty():
        return 1.0
    med = col.median()
    if med is None:
        return 1.0
    med_f = float(med)  # type: ignore[arg-type]
    if not (med_f > 0.0) or not math.isfinite(med_f):
        return 1.0
    # ceil до степени двойки: доводим медиану цены минимум до ~128.
    exp = math.ceil(math.log2(128.0 / med_f))
    return float(2.0 ** max(0, exp))


def _clean(series: pl.Series, *, fill: float) -> pl.Series:
    return clean_non_finite(series, fill=fill)


def _series_from_expr(
    df: pl.DataFrame,
    expr: pl.Expr,
    *,
    name: str,
    fill: float | None = None,
    percent: bool = False,
    clip: tuple[float, float] | None = None,
) -> pl.Series:
    raw = _select_series(df, expr, name=name)
    if percent:
        raw = _percent_scale(raw, name=name)
    if fill is not None:
        raw = _clean(raw, fill=fill)
    if clip is not None:
        raw = raw.clip(clip[0], clip[1])
    return raw


def _try_scalar_expr(
    df: pl.DataFrame,
    *,
    name: str,
    builder: Callable[[], pl.Expr],
    fill: float = 0.0,
    percent: bool = False,
    clip: tuple[float, float] | None = None,
    skip_prefix: str = "plta",
) -> pl.Series | None:
    try:
        return _series_from_expr(
            df, builder(), name=name, fill=fill, percent=percent, clip=clip
        ).alias(name)
    except DEFENSIVE_EXC as exc:
        _log_skip(f"{skip_prefix}_{name}", exc)
        return None


def _struct_field_series(
    df: pl.DataFrame,
    struct_expr: pl.Expr,
    field: str,
    *,
    name: str,
) -> pl.Series:
    result = df.select(struct_expr)
    sc = result.get_column(result.columns[0])
    return _select_series(df, sc.struct.field(field), name=name)


def _struct_tuple(
    df: pl.DataFrame,
    struct_expr: pl.Expr,
    *fields: tuple[str, str],
) -> tuple[pl.Series, ...]:
    return tuple(
        _struct_field_series(df, struct_expr, src, name=dest) for src, dest in fields
    )


def _ohlc() -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    return pl.col("high"), pl.col("low"), pl.col("close")


def adx_from_polars_ta(
    df: pl.DataFrame,
    period: int = 14,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Canonical Wilder (1978) ADX + DI, pure Polars, SMA-seeded RMA smoothing.

    Replaced the ``ptdx.ADX``/``PLUS_DI``/``MINUS_DI`` backend (2026-07 numeric
    audit): that is the TDX variant — DI from 14-bar rolling *sums* and ADX as a
    6-bar simple MA of DX — which diverged from Wilder ADX by up to ~40 points
    (rel. ~190%) on real 1h data, while every consumer threshold
    (``toolkit.adx_thresholds`` 20/25 regime bands) assumes Wilder semantics.
    Verified against an independent numpy Wilder reference (rtol < 1e-9 at tail).
    """
    n = max(1, int(period))
    base = (
        df.select(
            pl.col("high").cast(pl.Float64, strict=False).alias("h"),
            pl.col("low").cast(pl.Float64, strict=False).alias("l"),
            pl.col("close").cast(pl.Float64, strict=False).alias("c"),
        )
        .with_columns(
            (pl.col("h") - pl.col("h").shift(1)).alias("up"),
            (pl.col("l").shift(1) - pl.col("l")).alias("dn"),
        )
        .with_columns(
            pl.when((pl.col("up") > pl.col("dn")) & (pl.col("up") > 0.0))
            .then(pl.col("up"))
            .otherwise(0.0)
            .alias("plus_dm"),
            pl.when((pl.col("dn") > pl.col("up")) & (pl.col("dn") > 0.0))
            .then(pl.col("dn"))
            .otherwise(0.0)
            .alias("minus_dm"),
            pl.max_horizontal(
                (pl.col("h") - pl.col("l")).abs(),
                (pl.col("h") - pl.col("c").shift(1)).abs(),
                (pl.col("l") - pl.col("c").shift(1)).abs(),
            ).alias("tr"),
        )
    )
    # seed_offset=1: Wilder smoothing starts at the first bar with a prior close.
    atr = wilder_mean(base["tr"], period=n, name="atr", seed_offset=1)
    plus_sm = wilder_mean(base["plus_dm"], period=n, name="plus_sm", seed_offset=1)
    minus_sm = wilder_mean(base["minus_dm"], period=n, name="minus_sm", seed_offset=1)
    di = pl.DataFrame({"atr": atr, "plus_sm": plus_sm, "minus_sm": minus_sm}).with_columns(
        pl.when(pl.col("atr") > 0.0)
        .then(100.0 * pl.col("plus_sm") / pl.col("atr"))
        .otherwise(None)
        .alias("pdi"),
        pl.when(pl.col("atr") > 0.0)
        .then(100.0 * pl.col("minus_sm") / pl.col("atr"))
        .otherwise(None)
        .alias("mdi"),
    ).with_columns(
        pl.when((pl.col("pdi") + pl.col("mdi")) > 0.0)
        .then(100.0 * (pl.col("pdi") - pl.col("mdi")).abs() / (pl.col("pdi") + pl.col("mdi")))
        .otherwise(0.0)
        .alias("dx")
    )
    # DX is defined from index n (first DI); the ADX seed averages DX[n .. 2n-1].
    adx_raw = wilder_mean(di["dx"], period=n, name="adx_raw", seed_offset=n)
    adx = _clean(adx_raw, fill=0.0).clip(0.0, 100.0).rename(f"adx{period}")
    plus_di = _clean(di["pdi"], fill=0.0).clip(0.0, 100.0).rename(f"plus_di{period}")
    minus_di = _clean(di["mdi"], fill=0.0).clip(0.0, 100.0).rename(f"minus_di{period}")
    return adx, plus_di, minus_di


def cci_from_polars_ta(df: pl.DataFrame, period: int = 20) -> pl.Series:
    high, low, close = _ohlc()
    return _series_from_expr(
        df, ptdx.CCI(high, low, close, N=int(period)), name=f"cci{period}", fill=0.0
    )


def mfi_from_polars_ta(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Money Flow Index, канонический (TA-Lib).

    ⚠ БЫЛ ВАРИАНТ TDX, А НЕ КАНОН. `ptdx.MFI` реализует формулу Tongdaxin и вдобавок несёт
    тот же эпсилон в единицах цены×объёма. Замер 2026-07-27 против TA-Lib на живых кадрах:
    расхождение на **22.6% установившихся баров**, максимум **16.7 пункта** (1MBABYDOGE).
    Пороги перекупленности/перепроданности (20/80) читают именно каноническую шкалу, поэтому
    расхождение в 16 пунктов — это переход через порог, а не косметика.
    """
    high, low, close = _ohlc()
    # ⚠ Порядок аргументов у TA-Lib — (high, low, close, volume); у `ptdx.MFI` он был
    # (close, high, low, volume). Перепутать здесь — тихо получить правдоподобный мусор.
    # ⚠ НОРМИРОВКИ ЦЕНЫ ЗДЕСЬ НЕТ — И ЭТО ПРОВЕРЕНО, А НЕ ЗАБЫТО. Сначала она тут стояла:
    # тест инвариантности показал, что 8.6% баров меняют значение при умножении цены на 1e6,
    # и это выглядело как эпсилон хуже, чем в RSI. Проверка точным масштабированием
    # (степенями двойки, без округления) дала **0 расхождений при любом k на всех кадрах** —
    # то есть эпсилон у MFI не активен вовсе, а те 8.6% были артефактом моего же неточного
    # теста: ×1e6 округляет цену и переворачивает сравнения `TYP > prev(TYP)` на барах с
    # неизменной типичной ценой. Добавленная «нормировка» уводила MFI до 11.5 пункта.
    # Мораль: сначала докажи, что дефект есть, точным экспериментом — потом чини.
    raw = _series_from_expr(
        df,
        tl.mfi(high, low, close, pl.col("volume"), timeperiod=int(period)),
        name=f"mfi{period}",
        fill=50.0,
        clip=(0.0, 100.0),
    )
    # Вырождение MFI — неподвижная ТИПИЧНАЯ цена: ни одного бара вверх и ни одного вниз,
    # то есть оба денежных потока равны нулю и индекс это `0/0`. TA-Lib разрешает его в 0.0
    # (предельная перепроданность) — по I-6 это должно быть «нет данных». См. `_null_where`.
    n = int(period)
    typ = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
    return _null_where(
        raw, df, degenerate=typ.diff().abs().rolling_sum(n, min_samples=n) <= 0.0
    )


def willr_from_polars_ta(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Williams %R в каноническом диапазоне −100…0.

    ⚠ ИСТОРИЯ, КОТОРУЮ НЕ НАДО ПОВТОРЯТЬ. `polars_ta.WILLR` возвращает `1 - RSV`, то есть
    ПОЛОЖИТЕЛЬНЫЕ 0…1 вместо канонических −100…0. Первая редакция моста масштабировала ряд,
    только если он целиком ≤ 0, а затем всё равно резала `clip(-100, 0)` — положительный ряд
    обнулялся ЦЕЛИКОМ: замер показал ровно ОДНО значение 0.0 на 400 барах при сыром диапазоне
    0.00…0.96, любой гейт по полю был константой. Вторая редакция развернула диапазон явно
    (`* -100`), и замер 2026-07-27 подтвердил побитовое совпадение с TA-Lib (отн. 1.4e-16).

    Теперь берётся канон напрямую — тот же результат без ручного разворота, то есть без
    места, где можно ошибиться знаком при следующей правке.
    """
    high, low, close = _ohlc()
    raw = _series_from_expr(
        df,
        tl.willr(high, low, close, timeperiod=int(period)),
        name=f"willr{period}",
        fill=-50.0,
        clip=(-100.0, 0.0),
    )
    # ⚠ Тот же вырожденный вход, что у стохастика, и он тот же `0/0` — но TA-Lib разрешает
    # его здесь в 0.0, что на шкале −100…0 означает предельную ПЕРЕКУПЛЕННОСТЬ, тогда как
    # у стохастика тот же бар получает 0.0 = предельную ПЕРЕПРОДАННОСТЬ. Два поля из одного
    # окна давали противоположные крайности; теперь оба честно говорят «данных нет».
    return _null_where(raw, df, degenerate=_flat_hl(period))


_EXTENDED_BUILDERS: list[tuple[str, str, Callable[[], pl.Expr], float, bool, tuple[float, float] | None]] = [
    ("talib", "mom10", lambda: tl.mom(pl.col("close"), timeperiod=10), 0.0, False, None),
    # ⚠ TRIX и PPO ОТДАВАЛИСЬ РОВНО В 100 РАЗ МЕНЬШЕ КАНОНА. У обоих в `polars_ta` снят
    # множитель (`# * 100`), а здесь стоял `percent=False` — то есть доля уезжала в кадр
    # под именем процента. Замер 2026-07-27 против TA-Lib: среднее отношение наш/канон
    # = 0.01 РОВНО, на всех 27 живых кадрах. Это тот же класс, что был у NATR и Aroon.
    ("talib", "trix14", lambda: tl.trix(pl.col("close"), timeperiod=14), 0.0, False, None),
    ("talib", "ppo12_26", lambda: tl.ppo(pl.col("close"), fastperiod=12, slowperiod=26), 0.0, False, None),
    # RSV = FASTK быстрого стохастика; отдельной функции в TA-Lib нет, берём поле STOCHF.
    ("talib", "rsv14", lambda: tl.stochf(*_ohlc(), fastk_period=14, fastd_period=3).struct.field("fastk"), 0.0, False, (0.0, 100.0)),
    ("talib", "rocp10", lambda: tl.rocp(pl.col("close"), timeperiod=10), 0.0, False, None),
    ("talib", "rocr10", lambda: tl.rocr(pl.col("close"), timeperiod=10), 0.0, False, None),
    ("talib", "ad_line", lambda: tl.ad(*_ohlc(), pl.col("volume")), 0.0, False, None),
    (
        "talib",
        "adosc_3_10",
        lambda: tl.adosc(*_ohlc(), pl.col("volume"), fastperiod=3, slowperiod=10),
        0.0,
        False,
        None,
    ),
    # RMA (сглаживание Уайлдера) отдельной функции в TA-Lib не имеет — остаётся на polars_ta.
    ("plta", "rma14", lambda: plta.RMA(pl.col("close"), timeperiod=14), 0.0, False, None),
    ("talib", "trange14", lambda: tl.trange(*_ohlc()), 0.0, False, None),
    ("tdx", "mtm12", lambda: ptdx.MTM(pl.col("close"), N=12), 0.0, False, None),
    # PSY остаётся на TDX (канона в TA-Lib нет) и отдаёт ДОЛЮ → масштаб константой.
    ("tdx", "psy12", lambda: ptdx.PSY(pl.col("close"), N=12), 50.0, True, (0.0, 100.0)),
    ("tdx", "dpo20", lambda: ptdx.DPO(pl.col("close"), N=20), 0.0, False, None),
    ("tdx", "bias6", lambda: ptdx.BIAS(pl.col("close"), N=6), 0.0, False, None),
    ("tdx", "emv14", lambda: ptdx.EMV(pl.col("high"), pl.col("low"), pl.col("volume"), N=14), 0.0, False, None),
    ("tdx", "tdx_boll_mid", lambda: ptdx.BOLL_M(pl.col("close"), M=20, N=2), 0.0, False, None),
    ("tdx", "tdx_boll_upper", lambda: ptdx.BOLL(pl.col("close"), M=20, N=2), 0.0, False, None),
]


def polars_ta_extended_exprs(df: pl.DataFrame) -> list[pl.Series]:
    """Extra polars_ta.ta / tdx columns for pinned deep analysis."""
    out: list[pl.Series] = []
    for prefix, name, builder, fill, percent, clip in _EXTENDED_BUILDERS:
        expr = _try_scalar_expr(
            df,
            name=name,
            builder=builder,
            fill=fill,
            percent=percent,
            clip=clip,
            skip_prefix=prefix,
        )
        if expr is not None:
            out.append(expr)

    kdj_exprs = _kdj_exprs(df)
    if kdj_exprs:
        out.extend(kdj_exprs)
    return out


def _kdj_exprs(df: pl.DataFrame) -> list[pl.Series]:
    if df.is_empty():
        return []
    try:
        high, low, close = _ohlc()
        result = df.select(ptdx.KDJ(high, low, close, N=9, M1=3, M2=3))
        sc = result.get_column(result.columns[0])
        k_raw = materialize_series(sc.struct.field("K"), df=df, name="kdj_k")
        d_raw = materialize_series(sc.struct.field("D"), df=df, name="kdj_d")
        # KDJ остаётся на TDX: канонического аналога в TA-Lib нет. Бэкенд отдаёт долю
        # (`RSV` со снятым `* 100`), поэтому масштаб — КОНСТАНТОЙ, а не по данным.
        k = _percent_scale(_clean(k_raw, fill=50.0), name="kdj_k14")
        d = _percent_scale(_clean(d_raw, fill=50.0), name="kdj_d14")
        try:
            j_raw = materialize_series(sc.struct.field("J"), df=df, name="kdj_j")
        except (KeyError, ValueError, TypeError):
            j_raw = materialize_series(3.0 * k_raw - 2.0 * d_raw, df=df, name="kdj_j")
        j = _clean(j_raw, fill=50.0)
        return [
            k.clip(0.0, 100.0).alias("kdj_k14"),
            d.clip(0.0, 100.0).alias("kdj_d14"),
            j.alias("kdj_j14"),
        ]
    except DEFENSIVE_EXC as exc:
        _log_skip("kdj_tdx", exc)
        return []


def polars_wq_exprs(df: pl.DataFrame) -> list[pl.Series]:
    """WorldQuant-style context features for pinned deep analysis."""
    close = pl.col("close")
    volume = pl.col("volume")
    out: list[pl.Series] = []
    for skip, name, builder, fill, clip in (
        ("wq_ts_rank", "wq_ts_rank_close20", lambda: wq.ts_rank(close, 20), 0.5, (0.0, 1.0)),
        ("wq_ts_corr", "wq_ts_corr_close_vol20", lambda: wq.ts_corr(close, volume, 20), 0.0, (-1.0, 1.0)),
    ):
        try:
            raw = _select_series(df, builder(), name=name)
            out.append(_clean(raw, fill=fill).clip(clip[0], clip[1]).alias(name))
        except DEFENSIVE_EXC as exc:
            _log_skip(skip, exc)
    if "rsi14" in df.columns:
        try:
            delta = _select_series(df, wq.ts_delta(pl.col("rsi14"), 5), name="wq_ts_delta_rsi5")
            out.append(_clean(delta, fill=0.0).alias("wq_ts_delta_rsi5"))
        except DEFENSIVE_EXC as exc:
            _log_skip("wq_ts_delta_rsi", exc)
    return out


def ema_series(df: pl.DataFrame, period: int) -> pl.Series:
    return _series_from_expr(
        df, tl.ema(pl.col("close"), timeperiod=int(period)), name=f"ema{period}"
    )


def rsi_series(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """RSI по Уайлдеру; на НЕПОДВИЖНОМ окне возвращает ``null``, а не 0.

    ⚠ ГАРД НА ВЫРОЖДЕНИЕ. При нулевом среднем изменении RSI не определён — это 0/0, — а
    `polars_ta` отдаёт на таком ряде **0.0** (замер 2026-07-27: сорок одинаковых закрытий →
    сырое значение 0.0 у бэкенда, не у нашей обёртки). Ноль читается любым потребителем с
    порогом перепроданности как «предельная перепроданность», то есть неподвижная цена
    выглядит сильнейшим сигналом на покупку. Ровно тот класс, против которого стоит I-6:
    отсутствие данных обязано выглядеть отсутствием, а не крайним значением шкалы.

    Реалистичность: идеально плоское 14-баровое окно на ликвидном перпе редкость, но на
    неликвиде и — что важнее — на ЗАМЕРШЕМ кадре оно штатно. Замерший кадр здесь самый
    дорогой класс инцидентов, и «замер + предельная перепроданность» — худшее сочетание.

    ⚠ ЭПСИЛОН — ИМЕННО ИЗ-ЗА НЕГО СМЕНЁН БЭКЕНД. Прежняя редакция этой докстроки объявляла
    эпсилон `polars_ta` косметическим («99.99999894 вместо ровно 100»), и на BTC это правда.
    Но он стоит в ЕДИНИЦАХ ЦЕНЫ — `RMA(gain) / (RMA(|Δ|) + 1e-8)`, — то есть его вес растёт
    по мере удешевления монеты, а смещение ВСЕГДА направлено вниз. Замер 2026-07-27 против
    TA-Lib на живых 15m кадрах: у 1000SATS (цена 9.4e-06) среднее смещение **−14.57 пункта**
    на 399 установившихся барах, максимум 28.3; у мажоров после прогрева расхождения нет
    вовсе. По живой вселенной задето 11 символов из 675 (≥0.5 пункта), 6 — от пункта и выше.
    Косметическим это выглядело ровно потому, что мерили на BTC.

    Теперь считает TA-Lib (`tl.rsi`) — деления с эпсилоном там нет. Вырожденный случай она
    разрешает в 100.0, что нам НЕ подходит, поэтому гард ниже остаётся и переопределяет её.
    """
    # ⚠ `percent` больше НЕ НУЖЕН: TA-Lib отдаёт канонические 0…100 сама. `clip` оставлен —
    # он страхует не шкалу, а float-шум на самой кромке диапазона.
    # ⚠ Нормировка цены ОБЯЗАТЕЛЬНА: у TA-Lib свой абсолютный эпсилон, из-за которого на
    # дешёвых монетах RSI схлопывается в ровный 0.0. Подробности и замер — в `_price_norm`.
    rsi = _series_from_expr(
        df,
        tl.rsi(pl.col("close") * _price_norm(df), timeperiod=int(period)),
        name=f"rsi{period}",
        clip=(0.0, 100.0),
    )
    if df.is_empty() or "close" not in df.columns:
        return rsi
    # ⚠ Гард ВЫРАЖЕНИЕМ, а не питоновским проходом по спискам. Прежняя редакция (моя же)
    # делала `zip(rsi.to_list(), moved.to_list())` — это выводило обе серии в питон-объекты и
    # собирало результат поэлементно, в горячем пути расчёта фич. Здесь то же самое считает
    # Polars: `rolling_sum` по модулю приращений и `when/then/otherwise`.
    return (
        df.lazy()
        .select(
            pl.when(
                pl.col("close").diff().abs()
                .rolling_sum(int(period), min_samples=int(period)) <= 0.0
            )
            .then(None)
            .otherwise(pl.lit(rsi))
            .cast(pl.Float64)
            .alias(rsi.name)
        )
        .collect()[rsi.name]
    )


def atr_series(df: pl.DataFrame, period: int = 14) -> pl.Series:
    high, low, close = _ohlc()
    return _series_from_expr(
        df, tl.atr(high, low, close, timeperiod=int(period)), name=f"atr{period}"
    )


def natr_series(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """NATR = **100 × ATR / close**, то есть ПРОЦЕНТ — в этом весь смысл «normalized».

    ⚠ Возвращалась ДОЛЯ: `plta.NATR` — это буквально `ATR(...) / close`, и его собственная
    докстрока предупреждает, что «talib.ATR multiples another 100». Множителя не было, и
    имя обещало процент, а функция отдавала долю — ровно 100×. Замер на живом BTC 15m:
    0.00285249 против канонических 0.285249.

    Ручной `* 100` снят: TA-Lib нормирует сама. Замер 2026-07-27 подтвердил, что прежняя
    поправка была верной (отн. расхождение 2.2e-5, только на прогреве Уайлдера) — но
    множитель в вызове это место, где легко ошибиться при следующей правке, а канон его
    не требует.
    """
    high, low, close = _ohlc()
    return _series_from_expr(
        df,
        tl.natr(high, low, close, timeperiod=int(period)),
        name=f"natr{period}",
    )


def roc_series(df: pl.DataFrame, period: int = 10) -> pl.Series:
    return _series_from_expr(
        df, tl.roc(pl.col("close"), timeperiod=int(period)), name=f"roc{period}"
    )


def macd_series(
    df: pl.DataFrame,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pl.Series, ...]:
    struct_expr = tl.macd(
        pl.col("close"),
        fastperiod=int(fast),
        slowperiod=int(slow),
        signalperiod=int(signal),
    )
    return _struct_tuple(
        df,
        struct_expr,
        ("macd", "macd_line"),
        ("macdsignal", "macd_signal"),
        ("macdhist", "macd_hist"),
    )


def stochastic_series(
    df: pl.DataFrame,
    *,
    period: int = 14,
    smooth_d: int = 3,
) -> tuple[pl.Series, pl.Series]:
    """БЫСТРЫЙ стохастик (``STOCHF``): сырой %K и его %D-сглаживание.

    Сглаживания %K здесь НЕТ, и это осознанно названо. Раньше сигнатура принимала ``smooth_k``
    и молча его выбрасывала — вызывающий просил медленный стохастик и получал быстрый, не узнав
    об этом. Тот же класс дефекта, что и ложное имя ``buffer_pct``: параметр, который выглядит
    настройкой, но ею не является.

    Параметр удалён, а не реализован, намеренно: ``stoch_k14`` питает не только отображение, но и
    детектор скрытых дивергенций (``snapshot._hidden_stoch_divergence``), так что сглаживание
    сдвинуло бы его входы. Бэктеста для этого нет — менять поведение вслепую нельзя; если медленный
    стохастик понадобится, это отдельная ИЗМЕРЕННАЯ правка, а не побочный эффект уборки.

    ⚠ Обоснование «в polars_ta нет STOCH» БОЛЬШЕ НЕ ДЕЙСТВУЕТ: у TA-Lib медленный ``stoch``
    есть (`tl.stoch`). Сохранено именно ``stochf`` — потому что смена быстрого на медленный
    поменяла бы входы детектора дивергенций, а это отдельное измеряемое решение, а не побочный
    эффект смены бэкенда. Прежняя формулировка причины оставлена бы враньём — поэтому заменена.
    """
    struct_expr = tl.stochf(
        *_ohlc(),
        fastk_period=int(period),
        fastd_period=int(smooth_d),
    )
    k_raw, d_raw = _struct_tuple(
        df,
        struct_expr,
        ("fastk", "stoch_k14"),
        ("fastd", "stoch_d14"),
    )
    # clip to [0,100]: float-шум на самой кромке диапазона иначе роняет проверку
    # диапазона stoch_k14/stoch_d14 и отбраковывает символ целиком — а кромка это как раз
    # глубокая перепроданность/перекупленность, то есть капитуляция и истощение.
    k = _clean(k_raw, fill=50.0).clip(0.0, 100.0)
    d = _clean(d_raw, fill=50.0).clip(0.0, 100.0)
    # ⚠ Гард ПОСЛЕ `_clean`, а не до: на вырожденном окне TA-Lib отдаёт КОНЕЧНЫЙ 0.0, а не
    # NaN, поэтому `_clean` его не тронет — занулять надо явно. См. `_null_where`.
    flat = _flat_hl(period)
    return _null_where(k, df, degenerate=flat), _null_where(d, df, degenerate=flat)


def bbands_series(
    df: pl.DataFrame,
    *,
    period: int = 20,
    nbdev: float = 2.0,
) -> tuple[pl.Series, ...]:
    """Полосы Боллинджера. ⚠ ЕДИНСТВЕННОЕ МЕСТО, ГДЕ КАНОН TA-Lib БРАТЬ НЕЛЬЗЯ.

    Здесь `polars_ta` ПРАВИЛЬНЕЕ TA-Lib, и это измерено, а не выведено из принципов.

    TA-Lib считает дисперсию потоковой формулой `E[x²] − E[x]²` (`ta_VAR.c::TA_INT_VAR`), а
    затем `ta_STDDEV.c` обнуляет её по АБСОЛЮТНОМУ порогу: `var < 1e-8` → `std := 0.0`. Порог
    задан в единицах **цены²**, то есть в него зашито допущение «инструмент стоит десятки
    долларов» — наследие фондового рынка 1990-х. Для крипты это фатально: у монеты дешевле
    ~$0.003 двадцатибаровая дисперсия ВСЕГДА ниже порога, и полосы схлопываются в среднюю
    линию.

    Замер 2026-07-27, 27 живых 15m кадров, доля баров с `bb_upper == bb_mid` при переводе на
    TA-Lib: 1000SATS/DOGS/NEIRO/SPELL/TOSHI/HMSTR/1MBABYDOGE/HOT — **96.8%**, 1000PEPE 96.2%,
    DOGE 6.0%, мажоры 0%. То есть у ~5% вселенной (p5 цены = 0.0023) полосы Боллинджера стали
    бы мёртвой константой, а «сжатие полос» — вечно истинным.

    `polars_ta` этой ловушки не имеет: `ta/statistic.py::STDDEV` → `wq/time_series.py::
    ts_std_dev` → `Expr.rolling_std(ddof=0)`, то есть численно устойчивый расчёт Polars без
    абсолютных порогов. Конвенция СКО у обоих ОДНА (генеральное, ddof=0) — проверено: 15360
    из 15360 баров совпадают с `rolling_std(20, ddof=0)` и 0 из 15360 с ddof=1. Прежняя
    редакция этой докстроки объясняла расхождение через ddof — это было НЕВЕРНО.

    ⚠ Мораль шире Боллинджера: «канон TA-Lib» не значит «верно всегда». Обе библиотеки несут
    абсолютные эпсилоны в единицах цены (у `polars_ta` — `TA_EPSILON` в RSI, из-за которого
    RSI и переехал на TA-Lib), и обе ломаются на дешёвых монетах — просто в разных местах.
    Проверять надо ПОФУНКЦИОННО и на кадрах, где цена мала.
    """
    struct_expr = plta.BBANDS(
        pl.col("close"), timeperiod=int(period), nbdevup=float(nbdev), nbdevdn=float(nbdev)
    )
    return _struct_tuple(
        df,
        struct_expr,
        ("upperband", "bb_upper"),
        ("middleband", "bb_mid"),
        ("lowerband", "bb_lower"),
    )


def aroon_series(df: pl.DataFrame, *, period: int = 14) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Aroon Up/Down в канонической шкале **[0, 100]**, осциллятор в [-100, +100].

    ⚠ ЗДЕСЬ БЫЛО ДВА РАЗНЫХ ДЕФЕКТА, И ВТОРОЙ ПЕРЕЖИЛ ПРАВКУ ПЕРВОГО.

    1. **Шкала.** Бэкенд отдавал ДОЛЮ (`(n − barsSince)/n` без множителя) — ровно 100× меньше
       канона: на живом BTC 15m up ∈ [0.0714, 1.0000] вместо [7.14, 100.00]. Исправлено
       ручным `* 100`.
    2. **Окно.** `polars_ta` считает `1 - ts_arg_max(high, N)/N`, сканируя **N** баров, тогда
       как TA-Lib сканирует **N+1** (сегодняшний плюс N предыдущих). Ручной `* 100` этого не
       лечил и не мог. Замер 2026-07-27 против TA-Lib на 27 живых кадрах: расхождение на
       **20–22% установившихся баров у КАЖДОГО символа**, p90 |Δ| = 7.14 — это ровно 100/14,
       один шаг периода, подпись off-by-one, — а максимум **100.0**, то есть полная инверсия,
       когда экстремум приходится на границу окна.

    Оба закрыты переходом на канон. Потребителей с порогом по этим колонкам сегодня нет
    (только реестр `prepare_columns` и `data/completeness`), но значения пишутся в feature
    lake — то есть неверные числа копились бы как основа будущей калибровки.
    """
    up, down = _struct_tuple(
        df,
        tl.aroon(pl.col("high"), pl.col("low"), timeperiod=int(period)),
        ("aroonup", f"aroon_up{period}"),
        ("aroondown", f"aroon_down{period}"),
    )
    osc = materialize_series(up - down, df=df, name=f"aroon_osc{period}")
    return up, down, osc


def obv_series(df: pl.DataFrame) -> pl.Series:
    return _series_from_expr(df, tl.obv(pl.col("close"), pl.col("volume")), name="obv")


__all__ = [
    "BROKEN_PLTA_FUNCTIONS",
    "adx_from_polars_ta",
    "aroon_series",
    "atr_series",
    "bbands_series",
    "cci_from_polars_ta",
    "ema_series",
    "macd_series",
    "mfi_from_polars_ta",
    "natr_series",
    "obv_series",
    "polars_ta_available",
    "polars_ta_extended_exprs",
    "polars_wq_exprs",
    "roc_series",
    "rsi_series",
    "stochastic_series",
    "willr_from_polars_ta",
]
