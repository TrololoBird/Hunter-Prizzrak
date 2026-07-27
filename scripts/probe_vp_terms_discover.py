"""Разведка перед замером 3.2: сколько 1m/5m истории реально нужно на структурные окна зон.

Ничего не меряет — только печатает, на какие интервалы времени натянуты профили зон,
чтобы решить, влезает ли 1m-источник в разумный бюджет запросов.

Запуск: uv run python scripts/probe_vp_terms_discover.py
"""
from __future__ import annotations

import asyncio

import ccxt.pro as ccxtpro

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _structure_bars
from hunt_core.prizrak.setups import bars_from_ohlcv

_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT",
]
_TFS = ("15m", "1h", "4h")
_TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


async def main() -> None:
    cfg = PrizrakConfig()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    try:
        for sym in _SYMBOLS:
            for tf in _TFS:
                bars = await ex.fetch_ohlcv(sym, tf, limit=500)
                shaped = bars_from_ohlcv(bars)
                zones = find_accumulation_zones(shaped, tf=tf, cfg=cfg, max_zones=8)
                now = bars[-1][0]
                spans = []
                for z in zones:
                    if not isinstance(z, dict) or not z.get("lo") or not z.get("hi"):
                        continue
                    st = _structure_bars(bars, z)
                    if len(st) < 5:
                        continue
                    t0, t1 = st[0][0], st[-1][0] + _TF_MS[tf]
                    spans.append((len(st), (now - t0) / 86_400_000, (t1 - t0) / 60_000))
                if not spans:
                    print(f"{sym:18s} {tf:3s} zones={len(zones)} — нет структур")
                    continue
                oldest = max(s[1] for s in spans)
                max_1m = max(s[2] for s in spans)
                print(f"{sym:18s} {tf:3s} zones={len(zones):2d} структур={len(spans):2d} "
                      f"баров={[s[0] for s in spans]} "
                      f"самая старая={oldest:.1f}д назад  макс_окно={max_1m:.0f} 1m-баров")
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
