"""`wilder_mean` на дыре в данных: null остаётся null, а не превращается в ноль.

ЗАЧЕМ. До 2026-08-03 `features/shared.py::wilder_mean` начинался с
``.fill_nan(0.0).fill_null(0.0)``. Отсутствующий бар становился **измеренным нулевым**
TR/DM и уходил в экспоненциальную память сглаживания с весом ``(1−1/N)^k`` — то есть
занижал ATR не на одном баре, а на десятках следующих. ATR питает стопы, цели, NATR и
фильтры волатильности; систематически заниженный ATR — это систематически заужённый стоп.
Это нарушение I-6 внутри ядра индикаторов, где его труднее всего заметить.

ЧТО ПРОВЕРЯЕТСЯ (всё на НАСТОЯЩЕЙ функции, живые кадры с CCXT):

1. **Эквивалентность скалярному Уайлдеру** на кадре без дыр — правка не должна сдвинуть
   числа там, где данные целые. Порог 1e-7, как и заявлял проект.
2. **Дыра больше не занижает хвост.** В живой кадр вносится один null; сравниваются
   старое поведение (fill 0.0, воспроизведено здесь явно) и новое. Печатается величина
   занижения в процентах ATR — это и есть «дифф old/new», которого требует ТЗ.
3. **Дыра не пропадает молча:** на самом пропущенном баре выход null.
4. **Неполное окно затравки — отказ**, а не среднее по остатку.

    uv run python scripts/verify_wilder.py [SYMBOL] [TIMEFRAME]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.features.shared import wilder_mean  # noqa: E402

PERIOD = 14


def scalar_wilder(values: list[float | None], *, period: int, seed_offset: int = 0) -> list[float | None]:
    """Независимый эталон: скалярный цикл Уайлдера, без polars.

    Намеренно написан как в учебнике (затравка = SMA, дальше
    ``prev + (x − prev)/N``), чтобы сверка шла с ДРУГОЙ реализацией, а не с
    перефразировкой проверяемой.
    """
    size = len(values)
    seed_end = seed_offset + period
    out: list[float | None] = [None] * size
    if size < seed_end:
        return out
    window = values[seed_offset:seed_end]
    if any(v is None for v in window):
        return out
    prev = sum(float(v) for v in window if v is not None) / period  # noqa: RUF015
    out[seed_end - 1] = prev
    for idx in range(seed_end, size):
        x = values[idx]
        if x is None:
            out[idx] = None
            continue
        prev = prev + (float(x) - prev) / period
        out[idx] = prev
    return out


def old_wilder_fill_zero(series: pl.Series, *, period: int) -> pl.Series:
    """Прежнее поведение, воспроизведённое дословно — для диффа old/new."""
    size = len(series)
    clean = (
        series.replace([float("inf"), float("-inf")], None)
        .fill_nan(0.0)
        .fill_null(0.0)
        .cast(pl.Float64)
    )
    sma = clean.slice(0, period).mean() or 0.0
    ewm_input = pl.concat([pl.Series([sma], dtype=pl.Float64), clean.slice(period, size - period)])
    out = ewm_input.ewm_mean(alpha=1.0 / period, adjust=False)
    return pl.concat([pl.Series([None] * (period - 1), dtype=pl.Float64), out])


async def fetch_frame(symbol: str, timeframe: str, limit: int = 400) -> pl.DataFrame:
    import ccxt.async_support as ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    try:
        await ex.load_markets()
        ohlcv = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    finally:
        await ex.close()
    # I-5: форминг-бар отбрасывается на входе, как и везде в проекте.
    ohlcv = ohlcv[:-1]
    return pl.DataFrame(
        {
            "ts": [int(r[0]) for r in ohlcv],
            "high": [float(r[2]) for r in ohlcv],
            "low": [float(r[3]) for r in ohlcv],
            "close": [float(r[4]) for r in ohlcv],
        }
    )


def true_range_series(df: pl.DataFrame) -> pl.Series:
    return df.select(
        pl.max_horizontal(
            (pl.col("high") - pl.col("low")).abs(),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr")
    ).to_series()


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC/USDT:USDT"
    timeframe = sys.argv[2] if len(sys.argv) > 2 else "15m"
    print(f"живой кадр: {symbol} {timeframe}")
    df = asyncio.run(fetch_frame(symbol, timeframe))
    print(f"баров закрытых: {df.height}")
    tr = true_range_series(df)
    failures: list[str] = []

    # ── 1. Эквивалентность скалярному Уайлдеру на целом кадре ──────────────────────
    got = wilder_mean(tr, period=PERIOD, name="atr", seed_offset=1).to_list()
    want = scalar_wilder(tr.to_list(), period=PERIOD, seed_offset=1)
    deltas = [
        abs(g - w) for g, w in zip(got, want, strict=True) if g is not None and w is not None
    ]
    shape_ok = [(g is None) == (w is None) for g, w in zip(got, want, strict=True)]
    max_delta = max(deltas) if deltas else 0.0
    print(f"\n1. эквивалентность скалярному Уайлдеру: max|Δ| = {max_delta:.3e} по {len(deltas)} барам")
    if max_delta >= 1e-7:
        failures.append(f"расхождение со скалярным Уайлдером {max_delta:.3e} >= 1e-7")
        print("   [FAIL]")
    elif not all(shape_ok):
        failures.append("разошлись позиции null")
        print("   [FAIL] разошлись позиции null")
    else:
        print("   [OK  ]")

    # ── 2. Дыра: старое поведение занижало ВСЕГДА, новое — не смещено ──────────────
    #
    # ⚠ ПОЧЕМУ ЗДЕСЬ РАСПРЕДЕЛЕНИЕ, А НЕ ОДНА ДЫРА. Первая редакция этой проверки
    # пробивала один бар и сравнивала |отклонение| от эталона без дыры. Замер показал
    # СТАРОЕ −3.48% против НОВОГО +3.54% — то есть «новое не лучше», и это был вывод
    # об одном конкретном TR, а не о поведении. Восстановить эталон после дыры нельзя
    # в принципе: пропущенное значение неизвестно. Мерить надо не близость к эталону,
    # а СМЕЩЕНИЕ по многим положениям дыры.
    #
    # TR неотрицателен, поэтому подстановка 0.0 — это утверждение «волатильность была
    # ровно нулевой», то есть минимально возможной. Ошибка такого рода ВСЕГДА одного
    # знака и всегда в опасную для стопа сторону. Перевзвешивание знака не имеет.
    clean_out = wilder_mean(tr, period=PERIOD, name="atr", seed_offset=1).to_list()
    lag = 10  # на сколько баров позже дыры смотрим хвост
    old_err: list[float] = []
    new_err: list[float] = []
    for hole in range(PERIOD + 5, df.height - lag):
        punched = tr.clone().to_list()
        punched[hole] = None
        ps = pl.Series("tr", punched, dtype=pl.Float64)
        ref = clean_out[hole + lag]
        if ref is None or ref == 0:
            continue
        o = old_wilder_fill_zero(ps, period=PERIOD).to_list()[hole + lag]
        n = wilder_mean(ps, period=PERIOD, name="atr", seed_offset=1).to_list()[hole + lag]
        if o is not None:
            old_err.append((o - ref) / ref * 100.0)
        if n is not None:
            new_err.append((n - ref) / ref * 100.0)

    def _stats(errs: list[float]) -> tuple[float, float, float, float]:
        if not errs:
            return (0.0, 0.0, 0.0, 0.0)
        mean = sum(errs) / len(errs)
        mae = sum(abs(e) for e in errs) / len(errs)
        return (mean, mae, min(errs), max(errs))

    o_mean, o_mae, o_lo, o_hi = _stats(old_err)
    n_mean, n_mae, n_lo, n_hi = _stats(new_err)
    neg_share_old = sum(1 for e in old_err if e < 0) / max(1, len(old_err)) * 100.0
    neg_share_new = sum(1 for e in new_err if e < 0) / max(1, len(new_err)) * 100.0

    print(f"\n2. одна дыра, {len(old_err)} положений; ошибка ATR через {lag} баров после дыры")
    print(f"   {'':>16} {'смещение':>10} {'|ошибка|':>10} {'min':>9} {'max':>9} {'доля<0':>8}")
    print(f"   {'СТАРОЕ fill 0.0':>16} {o_mean:>9.3f}% {o_mae:>9.3f}% {o_lo:>8.2f}% {o_hi:>8.2f}% {neg_share_old:>7.1f}%")
    print(f"   {'НОВОЕ':>16} {n_mean:>9.3f}% {n_mae:>9.3f}% {n_lo:>8.2f}% {n_hi:>8.2f}% {neg_share_new:>7.1f}%")
    if neg_share_old < 99.0:
        failures.append(f"старое поведение занижало не всегда ({neg_share_old:.1f}%) — гипотеза о знаке неверна")
        print("   [FAIL] ожидалось, что fill 0.0 занижает ВСЕГДА")
    elif abs(n_mean) >= abs(o_mean):
        failures.append(f"смещение не снято: старое {o_mean:.3f}%, новое {n_mean:.3f}%")
        print("   [FAIL] систематическое смещение осталось")
    else:
        print(
            f"   [OK  ] одностороннее занижение снято: смещение {o_mean:+.3f}% → {n_mean:+.3f}%"
            f" (в {abs(o_mean) / max(abs(n_mean), 1e-9):.1f} раза меньше)"
        )

    # ── 3. На самом пропущенном баре — null, а не число ────────────────────────────
    hole_at = df.height // 2
    punched = tr.clone().to_list()
    punched[hole_at] = None
    punched_s = pl.Series("tr", punched, dtype=pl.Float64)
    new_out = wilder_mean(punched_s, period=PERIOD, name="atr", seed_offset=1).to_list()
    old_out = old_wilder_fill_zero(punched_s, period=PERIOD).to_list()
    print(f"\n3. на пропущенном баре (бар {hole_at}) выход null")
    if new_out[hole_at] is None:
        print(f"   [OK  ] null — СТАРОЕ отдавало {old_out[hole_at]:.6f} как измеренное число")
    else:
        failures.append("дыра не видна на выходе")
        print(f"   [FAIL] {new_out[hole_at]!r}")

    # ── 4. Неполное окно затравки — отказ целиком ──────────────────────────────────
    print("\n4. дыра ВНУТРИ окна затравки → отказ целиком, а не среднее по остатку")
    seeded = tr.clone().to_list()
    seeded[3] = None  # окно затравки при seed_offset=1 это бары 1..14
    seed_out = wilder_mean(
        pl.Series("tr", seeded, dtype=pl.Float64), period=PERIOD, name="atr", seed_offset=1
    ).to_list()
    if all(v is None for v in seed_out):
        print(f"   [OK  ] все {len(seed_out)} значений null")
    else:
        n_val = sum(1 for v in seed_out if v is not None)
        failures.append("неполная затравка дала числа")
        print(f"   [FAIL] {n_val} значений не null")

    print()
    if failures:
        print(f"НАРУШЕНИЙ: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("wilder_mean: все проверки прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
