"""Замер: гоняется ли граница зоны за ценой, пока цена ВНУТРИ зоны (ловушка Chung–Bellotti).

Chung & Bellotti (arXiv:2101.07410) описывают дефект прямо: процедура обнаружения уровня
обязана ЗАМИРАТЬ, пока цена внутри уровня, — «If the discovery procedure continues operating,
a new minimum (maximum) would create a new lower (upper) boundary … thus erroneously reducing
the probability of penetration». То есть граница уезжает за ценой, пробой становится
невозможным по построению, а «отскок от зоны» — тавтологией.

Для нас это не абстракция: `orchestrator._structural_stop` ставит стоп на `zone["lo"] * (1-буфер)`.
Если `lo` едет вниз вслед за ценой, то стоп едет вместе с ним — а стоп, уходящий против позиции,
стопом не является. Это же инвариант I-5 (никакого lookahead), только применённый к границе.

Метод: строим зону на окне, кончающемся баром T, затем продлеваем окно по одному бару и смотрим,
сдвинулась ли граница — отдельно считая случаи, когда цена в этот момент была ВНУТРИ зоны.

Запуск:
    uv run python scripts/verify_zone_freeze.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys

import ccxt.pro as ccxtpro

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig

_STEPS = 40  # сколько баров продлевать окно


def _to_rows(bars: list[list[float]]) -> list[dict[str, float]]:
    return [
        {"ts": float(b[0]), "open": float(b[1]), "high": float(b[2]),
         "low": float(b[3]), "close": float(b[4]), "volume": float(b[5])}
        for b in bars
    ]


async def main() -> None:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    ]
    tfs = [sys.argv[2]] if len(sys.argv) > 2 else ["15m", "1h", "4h"]
    cfg = PrizrakConfig.load()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}})
    await ex.load_markets()
    print(f"продление окна на {_STEPS} баров; смотрим сдвиг границы зоны\n")
    print(f"{'символ':14s} {'ТФ':4s} {'шагов':>6s} {'внутри зоны':>12s} "
          f"{'сдвигов границы':>16s} {'медиана сдвига %':>17s} {'макс %':>8s}")
    try:
        for sym in symbols:
            for tf in tfs:
                bars = await ex.fetch_ohlcv(sym, tf, limit=500)
                if not bars or len(bars) < 300:
                    continue
                rows = _to_rows(bars)
                base_n = len(rows) - _STEPS
                moves: list[float] = []
                inside_n = moved_inside = steps = 0
                prev: dict[str, float] | None = None
                for k in range(_STEPS):
                    window = rows[: base_n + k]
                    zones = find_accumulation_zones(window, tf=tf, cfg=cfg, max_zones=4)
                    if not zones:
                        prev = None
                        continue
                    z = zones[0]
                    px = window[-1]["close"]
                    inside = z["lo"] <= px <= z["hi"]
                    steps += 1
                    if prev is not None:
                        d_lo = abs(z["lo"] - prev["lo"]) / prev["lo"] * 100 if prev["lo"] else 0.0
                        d_hi = abs(z["hi"] - prev["hi"]) / prev["hi"] * 100 if prev["hi"] else 0.0
                        d = max(d_lo, d_hi)
                        if inside:
                            inside_n += 1
                            if d > 1e-9:
                                moved_inside += 1
                                moves.append(d)
                    prev = {"lo": float(z["lo"]), "hi": float(z["hi"])}
                med = statistics.median(moves) if moves else 0.0
                mx = max(moves) if moves else 0.0
                flag = "  ← ГРАНИЦА ЕДЕТ ЗА ЦЕНОЙ" if moved_inside else ""
                print(f"{sym:14s} {tf:4s} {steps:6d} {inside_n:12d} "
                      f"{moved_inside:16d} {med:17.3f} {mx:8.3f}{flag}")
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
