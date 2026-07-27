"""Гейт для допуска «цена ТЕСТИРУЕТ уровень» (`orchestrator._RETEST_TOL`).

Такого гейта не было, и поэтому подстановку δ(τ) сюда нельзя было ни принять, ни отвергнуть —
только поверить. Здесь он строится.

Что меряется. `_RETEST_TOL` решает ровно один вопрос: «находится ли цена НА уровне». Это тот же
вопрос, на который отвечает полоса из литературы, и у него есть опубликованный критерий
качества (Garzarelli 2014, Chung & Bellotti 2021): верная полуширина даёт на СЛУЧАЙНОМ
БЛУЖДАНИИ p(отскок) = 0.5, а на живом ряде — превышение над этим нулём.

Поэтому гейт двухчастный, и обе части обязательны:

* **нейтральность** — насколько p(отскок) нулевого ряда отклоняется от 0.5. Чем ближе, тем
  честнее полоса: при 0.8 «отскок» перестаёт что-либо означать;
* **превышение** — p(живой) − p(нуль) в пунктах. Это и есть сигнал. Полоса, у которой нуль
  идеален, а превышение нулевое, бесполезна ровно так же, как раздутая.

Сравниваются нынешняя константа 0.7% и δ(τ) на одних и тех же живых барах.

Запуск:
    uv run python scripts/ab_retest_tol.py
"""
from __future__ import annotations

import asyncio
import random
import statistics
import sys

import ccxt.pro as ccxtpro

from hunt_core.prizrak.orchestrator import _RETEST_TOL
from hunt_core.toolkit.level_band import mean_abs_increment_pct

_SHUFFLES = 40
_BARS = 1000
_SEED = 20260727
_TFS = ("15m", "1h", "4h")


def _bounce_rate(closes: list[float], band_pct: float) -> tuple[int, int]:
    """(отскоки, событий). Уровень фиксируется ДО входа и не пересматривается до выхода (I-5)."""
    n = len(closes)
    if n < 60:
        return 0, 0
    bounces = total = 0
    i = 20
    while i < n - 1:
        level = min(closes[max(0, i - 20):i])
        band = level * band_pct / 100.0
        if not (level - band <= closes[i] <= level + band):
            i += 1
            continue
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


def _score(closes: list[float], rets: list[float], band: float,
           rng: random.Random) -> tuple[float, float] | None:
    """(нейтральность нуля |p0−0.5|, превышение живого над нулём в п.п.)."""
    null: list[float] = []
    for _ in range(_SHUFFLES):
        sh = rets[:]
        rng.shuffle(sh)
        px = [closes[0]]
        for r in sh:
            px.append(px[-1] * (1.0 + r))
        h, t = _bounce_rate(px, band)
        if t >= 20:
            null.append(h / t)
    lh, lt = _bounce_rate(closes, band)
    if not null or lt < 20:
        return None
    p0 = statistics.median(null)
    return abs(p0 - 0.5), (lh / lt - p0) * 100.0


async def main() -> None:
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    ]
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}})
    await ex.load_markets()
    rng = random.Random(_SEED)
    const_pct = _RETEST_TOL * 100.0
    dev_c: list[float] = []
    exc_c: list[float] = []
    dev_d: list[float] = []
    exc_d: list[float] = []
    print(f"константа {const_pct:.2f}%  против  δ(τ) из тех же баров; seed={_SEED}\n")
    print(f"{'символ':14s} {'ТФ':4s} {'δ(τ) %':>8s} "
          f"{'|нуль−0.5| конст':>17s} {'δ':>7s} {'превыш. конст':>14s} {'δ':>8s}")
    try:
        for sym in symbols:
            for tf in _TFS:
                bars = await ex.fetch_ohlcv(sym, tf, limit=_BARS)
                if not bars or len(bars) < 200:
                    continue
                closes = [float(b[4]) for b in bars]
                rets = [closes[k + 1] / closes[k] - 1.0 for k in range(len(closes) - 1)]
                delta = mean_abs_increment_pct(closes)
                if delta is None:
                    continue
                sc = _score(closes, rets, const_pct, rng)
                sd = _score(closes, rets, delta, rng)
                if sc is None or sd is None:
                    continue
                dev_c.append(sc[0])
                exc_c.append(sc[1])
                dev_d.append(sd[0])
                exc_d.append(sd[1])
                print(f"{sym:14s} {tf:4s} {delta:8.3f} {sc[0]:17.3f} {sd[0]:7.3f} "
                      f"{sc[1]:+14.1f} {sd[1]:+8.1f}")
    finally:
        await ex.close()
    if not dev_c:
        return
    print(f"\n{'':24s} {'константа':>12s} {'δ(τ)':>10s}")
    print(f"{'медиана |нуль−0.5|':24s} {statistics.median(dev_c):12.3f} "
          f"{statistics.median(dev_d):10.3f}   ← меньше лучше")
    print(f"{'медиана превышения п.п.':24s} {statistics.median(exc_c):12.1f} "
          f"{statistics.median(exc_d):10.1f}   ← больше лучше")
    better_null = statistics.median(dev_d) < statistics.median(dev_c)
    keeps_signal = statistics.median(exc_d) >= statistics.median(exc_c) - 1.0
    print(f"\nВЕРДИКТ: нуль честнее — {'да' if better_null else 'НЕТ'}; "
          f"сигнал не потерян — {'да' if keeps_signal else 'НЕТ'}")


if __name__ == "__main__":
    asyncio.run(main())
