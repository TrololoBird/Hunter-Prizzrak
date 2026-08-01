"""Сверка ускоренного детектора пивотов с эталонным — на ЖИВЫХ данных.

ЗАЧЕМ. Профиль живого тика 2026-08-01: `_swing_detect_python` — **8.9 с из 35.4 с** всего
расчёта фич, 315 вызовов, и внутри **4.27 млн** вычислений генератора в `_finite`. Это
чистый Python, то есть он держит GIL и блокирует event loop даже из рабочего потока.

Оптимизация детектора пивотов — правка ПОВЫШЕННОГО риска: рядом живёт I-5 (никакого
заглядывания вперёд), и «ускорил, вроде работает» здесь недопустимо.

⚠ ЭТОТ СКРИПТ УЖЕ ОДИН РАЗ ЗАРУБИЛ ОПТИМИЗАЦИЮ, и в этом его ценность. Первой попыткой
была векторизация выражениями Polars: вывод совпал побитово на всех 75 сочетаниях, но
время вышло **0.98 с у эталона против 1.50 с, то есть ×0.65** — на кадрах в ~1000 баров
накладные расходы выражений больше самого цикла. Замер отверг правку, которая выглядела
очевидно правильной. Прошла вторая: та же питоновская петля, но проверка конечности окна
считается префиксными суммами вместо построения списков — ×2.5 при том же выводе.

Скрипт не сравнивает реализацию с собой: эталоном служит ИСХОДНЫЙ питоновский путь
(`_swing_points_reference`), намеренно оставленный в дереве. Проверяется:

  1. побитовое совпадение обеих масок на каждом баре, по всем символам и ТФ;
  2. отдельно — что НИ ОДНА реализация не ставит пивот на последний бар (I-5);
  3. время — чтобы «оптимизация» не оказалась замедлением (см. выше, случалось).

    uv run python scripts/verify_swing_pivots_equivalence.py
    uv run python scripts/verify_swing_pivots_equivalence.py BTC/USDT:USDT ETH/USDT:USDT
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ccxt.async_support as ccxt  # noqa: E402
import polars as pl  # noqa: E402

from _verify_common import report_skipped  # noqa: E402
from hunt_core.features.pivots import _swing_points, _swing_points_reference  # noqa: E402

_TFS = ("5m", "15m", "1h", "4h", "1d")
# Те же n, что реально ходят в бою: 2 (дивергенции), 3 (prepare), 5 (фигуры).
_LOOKBACKS = (2, 3, 5)
_DEFAULT = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "XAU/USDT:USDT", "DOGE/USDT:USDT",
]

FAIL: list[str] = []
SKIPPED: list[str] = []


def _compare(tag: str, a: pl.Series, b: pl.Series) -> int:
    """Вернуть число расхождений; при ненулевом — записать в FAIL с индексами."""
    if a.len() != b.len():
        FAIL.append(f"{tag}: разная длина {a.len()} vs {b.len()}")
        return max(a.len(), b.len())
    diff = (a != b).sum() or 0
    if diff:
        idx = [i for i, (x, y) in enumerate(zip(a.to_list(), b.to_list(), strict=True)) if x != y]
        FAIL.append(f"{tag}: расхождений {diff}, первые индексы {idx[:10]} из {a.len()} баров")
    return int(diff)


async def main(symbols: list[str]) -> int:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    checked_bars = 0
    combos = 0
    t_ref = t_new = 0.0
    try:
        await ex.load_markets()
        now = ex.milliseconds()
        for sym in symbols:
            for tf in _TFS:
                try:
                    raw = await ex.fetch_ohlcv(sym, tf, limit=1000)
                except Exception as exc:  # noqa: BLE001 — недоступный ТФ не приговор прогону
                    SKIPPED.append(f"{sym}/{tf}: {exc.__class__.__name__}")
                    continue
                step = ex.parse_timeframe(tf) * 1000
                closed = [b for b in raw if int(b[0]) + step <= now]  # I-5: только закрытые
                if len(closed) < 60:
                    SKIPPED.append(f"{sym}/{tf}: закрытых баров {len(closed)} < 60")
                    continue
                frame = pl.DataFrame(
                    {
                        "high": [float(b[2]) for b in closed],
                        "low": [float(b[3]) for b in closed],
                    }
                )
                for n in _LOOKBACKS:
                    combos += 1
                    checked_bars += frame.height

                    t0 = time.perf_counter()
                    ref_h, ref_l = _swing_points_reference(frame, n=n)
                    t_ref += time.perf_counter() - t0

                    t0 = time.perf_counter()
                    new_h, new_l = _swing_points(frame, n=n)
                    t_new += time.perf_counter() - t0

                    _compare(f"{sym}/{tf}/n={n} swing_high", ref_h, new_h)
                    _compare(f"{sym}/{tf}/n={n} swing_low", ref_l, new_l)

                    # I-5: последний бар не может быть подтверждённым пивотом ни у кого.
                    for who, s in (("эталон", ref_h), ("новый", new_h)):
                        if s.len() and bool(s[-1]):
                            FAIL.append(f"{sym}/{tf}/n={n}: {who} поставил swing_high на ПОСЛЕДНИЙ бар")
                    for who, s in (("эталон", ref_l), ("новый", new_l)):
                        if s.len() and bool(s[-1]):
                            FAIL.append(f"{sym}/{tf}/n={n}: {who} поставил swing_low на ПОСЛЕДНИЙ бар")

                print(f"  {sym:16s} {tf:4s} баров={frame.height:5d}  "
                      f"пивотов(n=3): эталон={int(_swing_points_reference(frame, n=3)[0].sum() or 0)}")
    finally:
        await ex.close()

    print(f"\nсочетаний символ/ТФ/n: {combos}   баров сверено: {checked_bars}")
    if combos:
        speedup = (t_ref / t_new) if t_new > 0 else float("inf")
        print(f"время: эталон {t_ref:.3f} с  ·  быстрый {t_new:.3f} с  ·  "
              f"ускорение ×{speedup:.1f}")
        if speedup < 1.0:
            FAIL.append(f"быстрый путь МЕДЛЕННЕЕ эталона — это регрессия, а не оптимизация (×{speedup:.2f})")
    report_skipped(SKIPPED)
    if not combos:
        print("\nСВЕРЯТЬ НЕЧЕГО — ни одного сочетания не загрузилось. Это НЕ успех.")
        return 1
    if FAIL:
        print(f"\nРАСХОЖДЕНИЙ: {len(FAIL)}")
        for f in FAIL[:20]:
            print("   ", f)
        return 1
    print("\nВЫВОД СОВПАДАЕТ С ЭТАЛОНОМ ПОБИТОВО на всех сочетаниях; "
          "последний бар пивотом не объявлен ни разу (I-5)")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    raise SystemExit(asyncio.run(main(args or _DEFAULT)))
