"""Замер: ловит ли `_poc_is_stable` неустойчивость по НАЧАЛУ сетки — и на скольких зонах.

Проверка `poc._poc_is_stable` до 2026-07-27 перебирала только ЧИСЛО корзин: все разбиения
стартуют с минимума окна, то есть меняют шаг и не меняют начало. Мода гистограммы зависит от
обоих. Замер `verify_poc_plateau.py` показал, что при неизменном числе корзин один только сдвиг
сетки уводил ПОК до 11.87% цены.

Здесь считается, что даёт добавленный перебор по началу НА НАСТОЯЩИХ зонах (профиль натянут на
бары зоны, разброс нормирован на ширину зоны — как в самой проверке), а не на 300-барном окне.

Запуск:
    uv run python scripts/verify_poc_origin_guard.py
"""
from __future__ import annotations

import asyncio
import sys

import ccxt.pro as ccxtpro
import polars as pl

from hunt_core.features.volume_profile import volume_profile_levels
from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _POC_STABILITY_BUCKETS, _POC_STABILITY_ORIGINS
from hunt_core.prizrak.structure import bars_from_ohlcv

_TFS = ("15m", "1h", "4h")


def _frame(rows: list[dict[str, float]], lo_i: int, hi_i: int) -> pl.DataFrame:
    seg = rows[max(0, lo_i):hi_i + 1]
    return pl.DataFrame({
        "high": [r["high"] for r in seg],
        "low": [r["low"] for r in seg],
        "volume": [r["volume"] for r in seg],
    })


async def main() -> None:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
        "XAU/USDT:USDT", "PAXG/USDT:USDT",
    ]
    cfg = PrizrakConfig.load()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}})
    await ex.load_markets()
    zones_n = only_buckets = with_origins = 0
    spreads: list[float] = []
    by_tf: dict[str, list[float]] = {}
    worst_origin = 0.0
    try:
        for sym in symbols:
            for tf in _TFS:
                bars = await ex.fetch_ohlcv(sym, tf, limit=400)
                if not bars or len(bars) < 200:
                    continue
                rows = bars_from_ohlcv(bars)
                for z in find_accumulation_zones(rows, tf=tf, cfg=cfg, max_zones=4):
                    fr = _frame(rows, int(z["first_touch_idx"]), int(z["last_touch_idx"]))
                    if fr.height < 5:
                        continue
                    span = float(z["hi"]) - float(z["lo"])
                    if span <= 0:
                        continue
                    base, _v, _a = volume_profile_levels(
                        fr, buckets=cfg.vp_buckets, value_area_pct=cfg.vp_value_area_pct
                    )
                    if base is None:
                        continue
                    zones_n += 1
                    by_buckets = [base]
                    for b in _POC_STABILITY_BUCKETS:
                        if b == cfg.vp_buckets:
                            continue
                        p, _x, _y = volume_profile_levels(
                            fr, buckets=b, value_area_pct=cfg.vp_value_area_pct
                        )
                        if p is not None:
                            by_buckets.append(float(p))
                    by_origin = [base]
                    for s in _POC_STABILITY_ORIGINS:
                        p, _x, _y = volume_profile_levels(
                            fr, buckets=cfg.vp_buckets,
                            value_area_pct=cfg.vp_value_area_pct, origin_shift=s,
                        )
                        if p is not None:
                            by_origin.append(float(p))
                    spread_b = (max(by_buckets) - min(by_buckets)) / span * 100.0
                    spread_o = (max(by_origin) - min(by_origin)) / span * 100.0
                    worst_origin = max(worst_origin, spread_o)
                    spreads.append(max(spread_b, spread_o))
                    if spread_b > 15.0:
                        only_buckets += 1
                    if max(spread_b, spread_o) > 15.0:
                        with_origins += 1
                    by_tf.setdefault(tf, []).append(max(spread_b, spread_o))
    finally:
        await ex.close()
    spreads.sort()
    print(f"зон измерено: {zones_n}")
    if by_tf:
        print("\nПО ТАЙМФРЕЙМАМ (неустойчив = разброс > 15% ширины зоны):")
        for tf in _TFS:
            vals = by_tf.get(tf) or []
            if not vals:
                continue
            bad = sum(1 for v in vals if v > 15.0)
            import statistics as _s
            print(f"    {tf:4s} зон {len(vals):3d}  неустойчивых {bad:3d} "
                  f"({100 * bad / len(vals):4.0f}%)  медиана разброса {_s.median(vals):6.1f}%")
    if spreads:
        import statistics as _st
        print("\nРАСПРЕДЕЛЕНИЕ разброса ПОК (% ширины зоны, максимум по корзинам И началу):")
        for q in (0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
            print(f"    p{int(q*100):3d} = {spreads[min(int(len(spreads)*q), len(spreads)-1)]:8.1f}%")
        print(f"    медиана {_st.median(spreads):.1f}%")
        print("\n  ГИСТОГРАММА (ищем провал между режимами — там и должен стоять порог):")
        edges=[0,2,5,10,15,20,30,50,100,200,10**9]
        for a,b in zip(edges, edges[1:]):
            n=sum(1 for x in spreads if a<=x<b)
            bar="#"*n
            hi_lbl = "∞" if b>10**8 else str(b)
            print(f"    [{a:>4}, {hi_lbl:>4})%  {n:3d}  {bar}")
    print(f"  объявлено неустойчивыми ТОЛЬКО по числу корзин: {only_buckets}")
    print(f"  объявлено неустойчивыми с добавленным перебором начала: {with_origins}")
    print(f"  ДОБАВЛЕНО перебором начала: {with_origins - only_buckets}")
    print(f"  худший разброс по началу сетки: {worst_origin:.1f}% ширины зоны")


if __name__ == "__main__":
    asyncio.run(main())
