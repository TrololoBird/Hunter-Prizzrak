"""Замер: есть ли у ПОКа ПЛАТО по ширине корзины и по началу сетки.

ПОК — это мода биннированного распределения, а у моды гистограммы положение зависит и от
ширины бина, и от его НАЧАЛА. Ни один вендор (Sierra Chart, TradingView, CQG, thinkorswim)
начало сетки не документирует, а опубликованного правила выбора ширины не существует вовсе —
все героически используют кратное тику.

Отсюда единственная честная проверка: развернуть оба параметра на замороженном живом снимке.
Устойчивая конфигурация даёт ПЛАТО — область, где ПОК не двигается. Если плато нет, уровень
не настроен, а просто шумит, и «настраивать» его число бессмысленно (I-7).

Дополнительно печатается отношение медианного размаха бара к ширине корзины. Sierra Chart
прямо пишет, что при записях крупнее тика объём раскладывается РАВНОМЕРНО по диапазону
записи; значит, когда бар шире корзины, профиль перестаёт разрешать структуру и превращается
в сглаживающее ядро. Порог ~2 — граница, за которой аппроксимация доминирует.

Запуск:
    uv run python scripts/verify_poc_plateau.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys

import ccxt.pro as ccxtpro

_BUCKETS = (20, 30, 40, 60, 90, 120, 180, 240)
_ORIGIN_SHIFTS = (0.0, 0.2, 0.4, 0.6, 0.8)  # доли ширины корзины


def _poc(bars: list[list[float]], buckets: int, shift_frac: float) -> float | None:
    """ПОК простой гистограммы «объём по цене закрытия», сетка сдвинута на долю корзины."""
    if not bars:
        return None
    hi = max(b[2] for b in bars)
    lo = min(b[3] for b in bars)
    if hi <= lo:
        return None
    width = (hi - lo) / buckets
    origin = lo - width * shift_frac
    acc: dict[int, float] = {}
    for b in bars:
        idx = int((float(b[4]) - origin) / width)
        acc[idx] = acc.get(idx, 0.0) + float(b[5])
    if not acc:
        return None
    best = max(acc.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return origin + (best + 0.5) * width


async def main() -> None:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    ]
    tfs = [sys.argv[2]] if len(sys.argv) > 2 else ["15m", "1h", "4h"]
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}})
    await ex.load_markets()
    try:
        for sym in symbols:
            for tf in tfs:
                bars = await ex.fetch_ohlcv(sym, tf, limit=300)
                if not bars or len(bars) < 100:
                    continue
                hi = max(b[2] for b in bars)
                lo = min(b[3] for b in bars)
                mid = (hi + lo) / 2
                bar_rng = statistics.median([(b[2] - b[3]) / mid * 100 for b in bars])
                print(f"=== {sym} {tf} ===  размах бара {bar_rng:.3f}% от цены")
                print(f"  {'корзин':>7s} {'ширина %':>9s} {'бар/корзина':>12s} "
                      f"{'разброс ПОК по сдвигу %':>24s} {'ПОК':>12s}")
                pocs_by_buckets: dict[int, float] = {}
                for nb in _BUCKETS:
                    width_pct = (hi - lo) / nb / mid * 100
                    vals = [p for s in _ORIGIN_SHIFTS if (p := _poc(bars, nb, s)) is not None]
                    if not vals:
                        continue
                    spread = (max(vals) - min(vals)) / mid * 100
                    pocs_by_buckets[nb] = statistics.median(vals)
                    ratio = bar_rng / width_pct if width_pct else 0.0
                    mark = "  ← бар шире корзины" if ratio > 2 else ""
                    print(f"  {nb:7d} {width_pct:9.3f} {ratio:12.2f} {spread:24.3f} "
                          f"{statistics.median(vals):12.6g}{mark}")
                if len(pocs_by_buckets) >= 3:
                    vs = list(pocs_by_buckets.values())
                    total = (max(vs) - min(vs)) / mid * 100
                    verdict = "ПЛАТО ЕСТЬ" if total <= 1.0 else "ПЛАТО НЕТ — ПОК шумит"
                    print(f"  разброс ПОК по ВСЕМ разрешениям: {total:.3f}%  → {verdict}")
                print()
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
