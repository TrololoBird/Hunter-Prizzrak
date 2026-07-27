"""Калибровка ширины уровневых полос по НУЛЕВОЙ ГИПОТЕЗЕ: p(отскок) на случайном блуждании.

Зачем. В репозитории 167 из 205 окон без обоснования, и половина из них — «полосы»: допуск
склейки пивотов (`accumulation._CLUSTER_TOL = 0.6%`), «цена тестирует уровень»
(`orchestrator._RETEST_TOL = 0.7%`), «одна и та же зона» (`setups._HORIZON_MATCH_TOL_PCT = 1.0%`).
Ни одна не выведена — ставили «разумное значение».

Литература даёт для этого falsifiable-критерий, и он единственный найденный:

    Garzarelli, Cristelli, Zaccaria, Pietronero, *Scientific Reports* 4:4487 (2014)
    Chung & Bellotti, arXiv:2101.07410 (2021)

Уровень моделируется ПОЛОСОЙ полуширины γ. «Отскок» = цена вошла в полосу и вышла той же
стороной; «пробой» = вышла противоположной. Критерий Chung–Bellotti дословно: подход даёт
«p(b) = 0.5 consistently for random walk simulations», и «a higher γ would inflate p(b) while a
lower γ would deflate p(b)». То есть **верная ширина — та, при которой случайное блуждание с
вашим распределением приращений даёт ровно 0.5**. Шире — вы производите отскоки из ничего;
уже — производите пробои.

⚠ Это НЕ синтетическая фикстура вместо живых данных (директива 2026-07-25 запрещает такие).
Приращения берутся из ЖИВЫХ баров конкретного символа и ТФ, а перемешиваются только для того,
чтобы разрушить структуру: получается ряд с той же волатильностью и теми же хвостами, но
заведомо без уровней. Это нулевая гипотеза, а не подмена данных. Без неё измеренная частота
отскоков не значит ничего: Osler (ФРБ Нью-Йорка, 2000) намерил, что СЛУЧАЙНЫЕ уровни
отрабатывают 56.2%, а профессиональные — 60.8%, то есть весь эффект 4.6 п.п.

Запуск:
    uv run python scripts/calibrate_level_band.py
    uv run python scripts/calibrate_level_band.py BTC/USDT:USDT 1h
"""
from __future__ import annotations

import asyncio
import random
import statistics
import sys

import ccxt.pro as ccxtpro

from hunt_core.toolkit.level_band import mean_abs_increment_pct

# Полосы-кандидаты в процентах — покрывают все константы-допуски, которые есть в дереве.
_BANDS_PCT = (0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0)
_SHUFFLES = 40          # независимых перемешиваний на каждый (символ, ТФ)
_BARS = 1000            # длина живого ряда
_SEED = 20260727        # фиксирован: замер обязан воспроизводиться


def _bounce_rate(closes: list[float], band_pct: float) -> tuple[int, int]:
    """(отскоки, всего событий) для уровней, найденных как локальные экстремумы ряда.

    Уровень объявляется по экстремуму ПРОШЛОГО и проверяется только на будущем — иначе
    получается look-ahead. Chung & Bellotti отдельно оговаривают вторую ловушку: пока цена
    ВНУТРИ полосы, обнаружение новых уровней обязано быть заморожено, иначе новый экстремум
    переопределит границу и занизит вероятность пробоя. Здесь это соблюдается: уровень
    фиксируется до входа и не пересматривается до выхода.
    """
    n = len(closes)
    if n < 60:
        return 0, 0
    bounces = total = 0
    i = 20
    while i < n - 1:
        window = closes[max(0, i - 20):i]
        level = min(window)  # поддержка: локальный минимум прошлого
        band = level * band_pct / 100.0
        if not (level - band <= closes[i] <= level + band):
            i += 1
            continue
        # цена вошла в полосу — уровень ЗАМОРОЖЕН до выхода
        j = i + 1
        while j < n:
            if closes[j] > level + band:
                bounces += 1
                total += 1
                break
            if closes[j] < level - band:
                total += 1
                break
            j += 1
        i = max(j, i + 1)
    return bounces, total


async def main() -> None:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    ]
    tfs = [sys.argv[2]] if len(sys.argv) > 2 else ["15m", "1h", "4h"]
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}})
    await ex.load_markets()
    rng = random.Random(_SEED)
    print(f"нулевая гипотеза: {_SHUFFLES} перемешиваний × {_BARS} баров, seed={_SEED}")
    print("верная полоса — та, где p(отскок) на перемешанном ряде ≈ 0.500\n")
    try:
        for sym in symbols:
            for tf in tfs:
                bars = await ex.fetch_ohlcv(sym, tf, limit=_BARS)
                if not bars or len(bars) < 200:
                    continue
                closes = [float(b[4]) for b in bars]
                rets = [closes[k + 1] / closes[k] - 1.0 for k in range(len(closes) - 1)]
                live = {b: _bounce_rate(closes, b) for b in _BANDS_PCT}
                null: dict[float, list[float]] = {b: [] for b in _BANDS_PCT}
                for _ in range(_SHUFFLES):
                    shuffled = rets[:]
                    rng.shuffle(shuffled)
                    px = [closes[0]]
                    for r in shuffled:
                        px.append(px[-1] * (1.0 + r))
                    for b in _BANDS_PCT:
                        hit, tot = _bounce_rate(px, b)
                        if tot >= 20:
                            null[b].append(hit / tot)
                delta = mean_abs_increment_pct(closes)
                if delta is not None:
                    dn: list[float] = []
                    for _ in range(_SHUFFLES):
                        sh = rets[:]
                        rng.shuffle(sh)
                        px = [closes[0]]
                        for r in sh:
                            px.append(px[-1] * (1.0 + r))
                        h, t = _bounce_rate(px, delta)
                        if t >= 20:
                            dn.append(h / t)
                    lh, lt = _bounce_rate(closes, delta)
                    p0 = statistics.median(dn) if dn else float("nan")
                    p1 = lh / lt if lt >= 20 else float("nan")
                    print(f"=== {sym} {tf} ===  δ(τ) = {delta:.3f}%  "
                          f"→ НУЛЬ {p0:.3f}, ЖИВОЙ {p1:.3f}, превышение {(p1 - p0) * 100:+.1f} п.п.")
                else:
                    print(f"=== {sym} {tf} ===")
                print(f"  {'полоса %':>9s} {'p(отскок) НУЛЬ':>15s} {'n':>5s} "
                      f"{'p(отскок) ЖИВОЙ':>16s} {'событий':>8s} {'превышение п.п.':>16s}")
                for b in _BANDS_PCT:
                    ns = null[b]
                    if not ns:
                        continue
                    p0 = statistics.median(ns)
                    hit, tot = live[b]
                    if tot < 20:
                        print(f"  {b:9.2f} {p0:15.3f} {len(ns):5d} {'мало событий':>16s} {tot:8d}")
                        continue
                    p1 = hit / tot
                    mark = "  ← полоса нейтральна" if abs(p0 - 0.5) <= 0.02 else ""
                    print(f"  {b:9.2f} {p0:15.3f} {len(ns):5d} {p1:16.3f} {tot:8d} "
                          f"{(p1 - p0) * 100:+16.1f}{mark}")
                print()
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
