"""A/B буфера стопа по таймфреймам — сколько РЕШЕНИЙ меняет каждое значение.

Дефект, ради которого замер. `stop_buffer_pct = 2.0` стоял константой во всех 24 502 записях
отказа и всех 366 карточках живого прогона — одна доля ЦЕНЫ на любой таймфрейм. Но структура,
которую буфер обязан прикрывать, с таймфреймом меняется на порядок:

    ТФ    стоп %   цель %   стоп/цель   карточек эмитировано
    5m     2.256    0.129      17.54          0
    15m    2.285    0.610       3.74         67
    1h     2.402    0.913       2.63         21
    4h     4.227    1.526       2.77         22
    1d     7.708    1.585       4.86          0
    1w     2.624    3.923       0.67        256

На 5m буфер в 17.5 раза больше цели — гейт `min_rr = 2.0` пройти арифметически невозможно, и
тир действительно не выдал ни одной карточки. Курс даёт диапазон, а не точку: «за структуру с
запасом **1-3%**» (стр.33), «**1-5%** … это для торговли лимитными ордерами» (стр.18); разбор
`prizrak_btc_eth_keyzone` фиксирует практику автора — «очень короткий стоп (**~1-1.5%**)».

Замер честный по методу из аудита окон: одни и те же живые бары, меняется ТОЛЬКО константа,
считается число ИЗМЕНИВШИХСЯ решений. Ноль изменений — константа инертна.

Запуск:
    uv run python scripts/ab_stop_buffer.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys

import ccxt.pro as ccxtpro

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.structure import bars_from_ohlcv

_BUFFERS = (0.01, 0.015, 0.02, 0.03)
_SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
            "XAU/USDT:USDT", "PAXG/USDT:USDT")
_TFS = ("5m", "15m", "1h", "4h", "1d")


def _rr_for(zone: dict[str, float], price: float, buffer_pct: float,
            target: float) -> float | None:
    """R:R лонга от нижней кромки зоны со стопом за структуру + буфер."""
    lo = float(zone["lo"])
    if lo <= 0 or price <= 0:
        return None
    stop = lo * (1.0 - buffer_pct)
    risk = price - stop
    if risk <= 0:
        return None
    return (target - price) / risk


async def main() -> None:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else list(_SYMBOLS)
    cfg = PrizrakConfig.load()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}})
    await ex.load_markets()
    min_rr = float(cfg.min_rr)
    print(f"гейт min_rr = {min_rr}; цель = верхняя кромка той же зоны (структурная)\n")
    per_tf: dict[str, dict[float, list[float]]] = {tf: {b: [] for b in _BUFFERS} for tf in _TFS}
    passes: dict[str, dict[float, int]] = {tf: {b: 0 for b in _BUFFERS} for tf in _TFS}
    counts: dict[str, int] = {tf: 0 for tf in _TFS}
    try:
        for sym in symbols:
            for tf in _TFS:
                bars = await ex.fetch_ohlcv(sym, tf, limit=400)
                if not bars or len(bars) < 200:
                    continue
                rows = bars_from_ohlcv(bars)
                zones = find_accumulation_zones(rows, tf=tf, cfg=cfg, max_zones=4)
                price = float(rows[-1]["close"])
                for z in zones:
                    target = float(z["hi"])
                    if target <= price:
                        continue  # цель ниже цены — это не лонг от нижней кромки
                    counts[tf] += 1
                    for b in _BUFFERS:
                        rr = _rr_for(z, price, b, target)
                        if rr is None:
                            continue
                        per_tf[tf][b].append(rr)
                        if rr >= min_rr:
                            passes[tf][b] += 1
    finally:
        await ex.close()

    print(f"{'ТФ':5s} {'зон':>5s} " + " ".join(f"{'RR@' + str(b * 100) + '%':>12s}" for b in _BUFFERS))
    for tf in _TFS:
        if not counts[tf]:
            continue
        cells = []
        for b in _BUFFERS:
            vals = per_tf[tf][b]
            cells.append(f"{statistics.median(vals):12.2f}" if vals else f"{'—':>12s}")
        print(f"{tf:5s} {counts[tf]:5d} " + " ".join(cells))

    print(f"\nСКОЛЬКО ЗОН ПРОХОДИТ ГЕЙТ RR>={min_rr} (это и есть «изменившиеся решения»):")
    print(f"{'ТФ':5s} {'зон':>5s} " + " ".join(f"{str(b * 100) + '%':>10s}" for b in _BUFFERS))
    for tf in _TFS:
        if not counts[tf]:
            continue
        cells = [f"{passes[tf][b]:10d}" for b in _BUFFERS]
        print(f"{tf:5s} {counts[tf]:5d} " + " ".join(cells))

    base = 0.02
    print(f"\nПрирост против нынешних {base * 100}%:")
    for tf in _TFS:
        if not counts[tf]:
            continue
        b0 = passes[tf][base]
        deltas = " ".join(
            f"{str(b * 100) + '%'}: {passes[tf][b] - b0:+d}" for b in _BUFFERS if b != base
        )
        print(f"  {tf:5s} база {b0:3d} из {counts[tf]:3d}   {deltas}")


if __name__ == "__main__":
    asyncio.run(main())
