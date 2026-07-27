"""Паритет индикаторов моста с канонической TA-Lib — на ЖИВЫХ данных.

Заменяет удалённый `tests/test_indicator_reference_values.py`. Прежний тест пинил значения
константами (в т.ч. `adx[0] == 0.0` с комментарием «never null» — то есть закреплял
сфабрикованный ноль как контракт) и был зелёным всё время, пока NATR и Aroon отличались
от канона **ровно в 100 раз**. Здесь проверка устроена иначе и по правилу проекта:

* данные — живые, тянутся через публичный CCXT прямо сейчас, а не фикстура;
* эталон — НЕЗАВИСИМАЯ реализация: `polars_talib` вендорит **TA-Lib 0.4.0** (C-код Уайлдера),
  это другой код, а не наш стек и не `polars_ta`, поэтому расхождение что-то значит;
* сравнивается НАСТОЯЩИЙ код моста (`features/polars_ta_bridge.py`), а не локальная арифметика.

⚠⚠ ГЛАВНОЕ ПРО ЭТАЛОН: «канон TA-Lib» — НЕ синоним «верно». У TA-Lib свои АБСОЛЮТНЫЕ эпсилоны
в единицах цены (наследие фондового рынка 1990-х), и на дешёвых перпах они дают не смещение, а
схлоп в крайнее значение. Замер 2026-07-27:

* `ta_STDDEV.c` обнуляет дисперсию при `var < 1e-8` → у монет дешевле ~$0.003 полосы Боллинджера
  схлопываются в среднюю линию на **96.8% баров**. Поэтому `bb_*` считает `polars_ta`, и эталон
  для них — тоже `polars_ta`, а не TA-Lib;
* RSI на 1000SATS отдавал РОВНО 0.0 (предельная перепроданность) на 24 барах при реальном
  приросте. Лечится нормировкой цены степенью двойки — эталон здесь тоже нормированный.

Поэтому две колонки сравниваются НЕ с сырой TA-Lib. Если это забыть и «починить» их обратно —
инструмент станет зелёным ровно тогда, когда данные испорчены.

⚠ Допуски здесь ИЗМЕРЕНЫ, а не выбраны «разумными» (I-7). Основание каждого — в `_TOL` ниже;
у затравочных (`atr`/`natr`/`macd`) допуск ненулевой не по неряшливости, а потому что TA-Lib
и `polars_ta` законно расходятся на прогреве Уайлдера и сходятся дальше — величина замерена
2026-07-27 на 27 живых кадрах. Ужесточать допуск, не перемерив, нельзя: получишь красный
прогон на здоровом коде.

⚠ Символы намеренно берутся по ВСЕМУ диапазону цен. Дефект эпсилона (`polars_ta` делит на
`RMA(|Δ|) + 1e-8`, эпсилон в единицах ЦЕНЫ) виден только на дешёвых монетах: на BTC он
неразличим, на 1000SATS давал −14.6 пункта RSI. Проверка на одних мажорах его пропустит.

Запуск:
    uv run python scripts/verify_talib_parity.py                    # набор по умолчанию
    uv run python scripts/verify_talib_parity.py BTC/USDT:USDT ...  # свои символы
"""
from __future__ import annotations

import asyncio
import math
import sys

import ccxt.async_support as ccxt
import polars as pl
import polars_ta.ta as pta
import polars_talib as tl

from hunt_core.features.polars_ta_bridge import (
    _price_norm,
    adx_from_polars_ta,
    aroon_series,
    atr_series,
    bbands_series,
    cci_from_polars_ta,
    ema_series,
    macd_series,
    mfi_from_polars_ta,
    natr_series,
    obv_series,
    roc_series,
    rsi_series,
    stochastic_series,
    willr_from_polars_ta,
)

# Прогрев: сколько первых баров исключить из сравнения. 150 — не круглое число «на глаз»:
# замер 2026-07-27 показал, что у мажоров последнее расхождение RSI приходится на бар 36-54,
# а самый долгий хвост (Уайлдер ATR/NATR/MACD) укладывается в ~120. 150 берётся с запасом.
WARMUP = 150
LIMIT = 600
TIMEFRAME = "15m"

# По умолчанию — разброс на 10 порядков по цене, иначе эпсилон-класс дефектов невидим.
DEFAULT_SYMBOLS = (
    "BTC/USDT:USDT",       # ~65000
    "ETH/USDT:USDT",       # ~1900
    "XRP/USDT:USDT",       # ~1
    "DOGE/USDT:USDT",      # ~0.07
    "HMSTR/USDT:USDT",     # ~0.00017
    "1000SATS/USDT:USDT",  # ~9.4e-06  ← здесь и только здесь виден эпсилон
)

# индикатор -> (допуск по МОДУЛЮ, допуск ОТНОСИТЕЛЬНЫЙ, чем обоснован)
_TOL: dict[str, tuple[float, float, str]] = {
    "rsi14":    (1e-6, 1e-9, "после ухода с polars_ta эпсилона нет — обязано совпадать"),
    "atr14":    (1e-6, 5e-5, "затравка Уайлдера расходится на прогреве; замер: отн. 2.2e-5"),
    "natr14":   (1e-6, 5e-5, "то же, что atr14 — natr = atr/close*100"),
    "ema20":    (1e-6, 1e-6, "замер: отн. 4e-9, это шум float"),
    "roc10":    (1e-9, 1e-9, "замер: отн. 1.5e-14"),
    "obv":      (1e-6, 1e-9, "замер: совпадение точное (0.0)"),
    "willr14":  (1e-9, 1e-9, "замер: отн. 1.4e-16 — точное совпадение"),
    "stoch_k":  (1e-9, 1e-9, "замер: отн. 1.4e-16 — точное совпадение"),
    "stoch_d":  (1e-9, 1e-9, "замер: отн. 2.0e-15"),
    "cci20":    (1e-6, 1e-9, "замер: отн. 7.9e-12 (tdx CCI совпадает с каноном)"),
    "aroon_up": (1e-6, 1e-9, "после ухода с окна N на N+1 обязано совпадать"),
    "aroon_dn": (1e-6, 1e-9, "то же"),
    "mfi14":    (1e-6, 1e-9, "после ухода с tdx MFI на канонический обязано совпадать"),
    # ⚠ adx14 — НАША собственная реализация Уайлдера (`adx_from_polars_ta`), а НЕ TA-Lib, и
    # это осознанно: она уже была проверена против независимого эталона Уайлдера, её читают
    # живые гейты (`toolkit.adx_thresholds`, полосы 20/25), и менять их входы без замера
    # нельзя. Расхождение с TA-Lib — чистая ЗАТРАВКА рекурсии (TA-Lib сидирует SMA первых
    # N значений, мы — тоже, но DX-серия стартует иначе), переходный процесс затухает как
    # (1−1/14)^k. Замер 2026-07-27 по 27 живым кадрам: max отн. 2.59e-5. Допуск 5e-5 — с
    # запасом к замеру, а не «разумное число».
    "adx14":    (1e-6, 5e-5, "своя реализация Уайлдера; замер расхождения затравки: отн. 2.59e-5"),
    "bb_up":    (1e-6, 1e-9, "сверяется с polars_ta (TA-Lib схлопывает полосы) — обязано совпадать"),
    "bb_mid":   (1e-6, 1e-9, "замер: отн. 7.4e-15"),
    "macd":     (1e-6, 5e-4, "затравка EMA расходится на прогреве; замер: отн. 6.1e-5"),
}

FAIL: list[str] = []


def _finite(v: object) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))


def _compare(name: str, ours: pl.Series, ref: pl.Series, symbol: str) -> None:
    """Сравнить наш ряд с эталонным, исключив прогрев. Пишет в FAIL при превышении допуска."""
    tol_abs, tol_rel, why = _TOL[name]
    a, b = ours.to_list(), ref.to_list()
    pairs = [
        (x, y) for i, (x, y) in enumerate(zip(a, b))
        if i >= WARMUP and _finite(x) and _finite(y)
    ]
    if not pairs:
        # I-6: «сравнить не удалось» — это НЕ «совпало». Молчать здесь нельзя.
        FAIL.append(f"{symbol} {name}: НЕТ сравнимых баров после прогрева ({len(a)} всего)")
        return
    scale = max((abs(y) for _, y in pairs), default=0.0) or 1.0
    worst = max(pairs, key=lambda p: abs(p[0] - p[1]))
    dmax = abs(worst[0] - worst[1])
    if dmax > tol_abs and dmax / scale > tol_rel:
        FAIL.append(
            f"{symbol} {name}: max|Δ|={dmax:.6g} (отн. {dmax / scale:.2e}) "
            f"> допуска ({tol_abs:.0e} / {tol_rel:.0e}); наш={worst[0]:.6g} канон={worst[1]:.6g}"
            f"  [{why}]"
        )
    print(f"    {name:<9} n={len(pairs):>4}  max|Δ|={dmax:>11.6g}  отн.={dmax / scale:>9.2e}")


async def _fetch(ex: ccxt.Exchange, symbol: str) -> pl.DataFrame | None:
    rows = await ex.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
    rows = rows[:-1]  # I-5: формирующийся бар не сравниваем
    if len(rows) < WARMUP + 50:
        return None
    return pl.DataFrame(
        {
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        }
    )


def _check_symbol(symbol: str, df: pl.DataFrame) -> None:
    H, L, C, V = pl.col("high"), pl.col("low"), pl.col("close"), pl.col("volume")
    # ⚠ Нормировка ТОЛЬКО у RSI и ТОЛЬКО степенью двойки — ровно как в мосте. Иначе эталон
    # сам принесёт схлоп, который мы и проверяем. Точный множитель важен: со степенью десяти
    # округление переворачивает сравнения на квантованных ценах (замер: MFI уезжал до 11.5 п.).
    k = _price_norm(df)
    ref = df.select(
        tl.rsi(C * k, 14).alias("rsi14"),
        tl.atr(H, L, C, 14).alias("atr14"),
        tl.natr(H, L, C, 14).alias("natr14"),
        tl.ema(C, 20).alias("ema20"),
        tl.roc(C, 10).alias("roc10"),
        tl.obv(C, V).alias("obv"),
        tl.willr(H, L, C, 14).alias("willr14"),
        tl.cci(H, L, C, 20).alias("cci20"),
        tl.mfi(H, L, C, V, 14).alias("mfi14"),
        tl.adx(H, L, C, 14).alias("adx14"),
        tl.stochf(H, L, C, 14, 3).struct.field("fastk").alias("stoch_k"),
        tl.stochf(H, L, C, 14, 3).struct.field("fastd").alias("stoch_d"),
        tl.aroon(H, L, 14).struct.field("aroonup").alias("aroon_up"),
        tl.aroon(H, L, 14).struct.field("aroondown").alias("aroon_dn"),
        # ⚠ bb_* сверяются с `polars_ta`, а НЕ с TA-Lib: см. шапку — TA-Lib здесь схлопывает
        # полосы на дешёвых монетах, и эталоном служить не может.
        pta.BBANDS(C, 20, 2.0, 2.0).struct.field("upperband").alias("bb_up"),
        pta.BBANDS(C, 20, 2.0, 2.0).struct.field("middleband").alias("bb_mid"),
        tl.macd(C, 12, 26, 9).struct.field("macd").alias("macd"),
    )

    stoch_k, stoch_d = stochastic_series(df, period=14, smooth_d=3)
    a_up, a_dn, _osc = aroon_series(df, period=14)
    bb_up, bb_mid, _bb_low = bbands_series(df, period=20, nbdev=2.0)
    macd_line = macd_series(df)[0]
    adx, _pdi, _mdi = adx_from_polars_ta(df, 14)

    ours: dict[str, pl.Series] = {
        "rsi14": rsi_series(df, 14),
        "atr14": atr_series(df, 14),
        "natr14": natr_series(df, 14),
        "ema20": ema_series(df, 20),
        "roc10": roc_series(df, 10),
        "obv": obv_series(df),
        "willr14": willr_from_polars_ta(df, 14),
        "cci20": cci_from_polars_ta(df, 20),
        "mfi14": mfi_from_polars_ta(df, 14),
        "adx14": adx,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "aroon_up": a_up,
        "aroon_dn": a_dn,
        "bb_up": bb_up,
        "bb_mid": bb_mid,
        "macd": macd_line,
    }
    for name, series in ours.items():
        _compare(name, series, ref[name], symbol)


async def main(symbols: tuple[str, ...]) -> int:
    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
    checked = 0
    try:
        await ex.load_markets()
        for symbol in symbols:
            if symbol not in ex.markets:
                print(f"  {symbol}: НЕТ в листинге — пропуск")
                continue
            df = await _fetch(ex, symbol)
            if df is None:
                print(f"  {symbol}: мало баров — пропуск")
                continue
            print(f"\n  {symbol}  баров={df.height}  close={df['close'][-1]:g}")
            _check_symbol(symbol, df)
            checked += 1
    finally:
        await ex.close()

    print()
    if not checked:
        # Ноль проверенных символов — это ОТКАЗ инструмента, а не «всё хорошо» (I-6).
        print("ОТКАЗ: не проверено ни одного символа — биржа недоступна или листинг изменился")
        return 2
    if FAIL:
        print(f"РАСХОЖДЕНИЙ: {len(FAIL)} (символов проверено: {checked})")
        for line in FAIL:
            print(f"  ✗ {line}")
        return 1
    print(f"ПАРИТЕТ С TA-Lib: OK — {checked} символов, расхождений сверх допуска нет")
    return 0


if __name__ == "__main__":
    args = tuple(sys.argv[1:]) or DEFAULT_SYMBOLS
    raise SystemExit(asyncio.run(main(args)))
