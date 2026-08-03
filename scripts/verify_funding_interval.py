"""Интервал фандинга: измеряется ли он и доходит ли до расчёта издержки.

ЗАЧЕМ. `track/equity.py` читал ключ `funding_interval_h`, которого **не писала ни одна
строка в дереве** — фантомная ручка. Чтение всегда сваливалось в `_DEFAULT_FUNDING_INTERVAL_H
= 8.0`, а Binance держит часть символов на 4 ч и 1 ч. Занижённое число интервалов удержания
⇒ занижённая издержка фандинга ⇒ завышенный PnL в Outcome Ledger.

Проверяется вся цепочка НА ЖИВЫХ ДАННЫХ, а не только её концы:

1. **Биржа отдаёт интервалы** — `rest.poll_funding_intervals` на настоящем ccxt, с
   распределением по часам (оно и есть доказательство, что дефолт 8 ч был неверен).
2. **Движок их публикует** — `Engine.funding_interval_h(symbol)` после старта.
3. **Расчёт издержки их использует** — `equity._funding_pct` на строке с реальным
   интервалом против той же строки без него; печатается разница.
4. **Неизмеренный интервал = отказ**, а не подставленная восьмёрка.

    uv run python scripts/verify_funding_interval.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.engine import rest  # noqa: E402
from hunt_core.engine.api import Engine  # noqa: E402
from hunt_core.track.equity import _funding_pct  # noqa: E402

PROBE_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "LPT/USDT:USDT", "TRB/USDT:USDT"]
HOLD_MIN = 24 * 60.0  # сутки удержания — на них разница 8ч/4ч видна как ×2
RATE = 0.0001  # 0.01% — типичная ставка


def _row(interval_h: float | None) -> dict:
    market: dict[str, float] = {"funding_rate": RATE}
    if interval_h is not None:
        market["funding_interval_h"] = interval_h
    return {"features_close": {"market": market}, "duration_min": HOLD_MIN}


async def main() -> int:
    failures: list[str] = []

    # ── 1. Биржа ───────────────────────────────────────────────────────────────────
    import ccxt.async_support as ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    try:
        await ex.load_markets()
        intervals = await rest.poll_funding_intervals(ex)
    finally:
        await ex.close()

    hist = Counter(intervals.values())
    print(f"\n1. биржа отдала интервалы по {len(intervals)} символам")
    for hours, n in sorted(hist.items()):
        share = n / max(1, len(intervals)) * 100.0
        print(f"   {hours:g}ч: {n:>4} символов ({share:.1f}%)")
    if not intervals:
        failures.append("биржа не отдала ни одного интервала")
        print("   [FAIL] пусто")
    else:
        non8 = sum(n for h, n in hist.items() if h != 8.0)
        print(
            f"   [OK  ] НЕ 8ч: {non8} символов ({non8 / len(intervals) * 100:.1f}%) — "
            f"ровно та доля вселенной, на которой прежний дефолт был неверен"
        )

    # ── 2. Движок ──────────────────────────────────────────────────────────────────
    print("\n2. движок публикует интервал после старта")
    engine = Engine(PROBE_SYMBOLS)
    await engine.start()
    try:
        # Полоса опроса делает первый проход сразу, но она асинхронная — дать ей дойти.
        for _ in range(30):
            if engine.funding_interval_h(PROBE_SYMBOLS[0]) is not None:
                break
            await asyncio.sleep(1.0)
        measured: dict[str, float | None] = {
            s: engine.funding_interval_h(s) for s in PROBE_SYMBOLS
        }
    finally:
        await engine.close()
    for sym, val in measured.items():
        print(f"   {sym:<20} {'не измерен' if val is None else f'{val:g}ч'}")
    if all(v is None for v in measured.values()):
        failures.append("движок не опубликовал ни одного интервала")
        print("   [FAIL] движок не отдал ничего")
    else:
        print("   [OK  ]")

    # ── 3. Расчёт издержки ─────────────────────────────────────────────────────────
    print(f"\n3. издержка фандинга за {HOLD_MIN / 60:.0f} ч удержания при ставке {RATE * 100:.2f}%")
    print(f"   {'символ':<20} {'интервал':>10} {'издержка %':>12} {'СТАРОЕ (8ч)':>14} {'занижение':>11}")
    old_cost, _ = _funding_pct(_row(8.0), None)  # то, что считалось всегда
    for sym, val in measured.items():
        if val is None:
            continue
        cost, ok = _funding_pct(_row(val), None)
        ratio = cost / old_cost if old_cost else float("nan")
        print(f"   {sym:<20} {val:>9g}ч {cost:>11.4f}% {old_cost:>13.4f}% {ratio:>10.1f}x")
        if not ok:
            failures.append(f"{sym}: интервал есть, но measured=False")

    # ── 4. Неизмеренный интервал — отказ ───────────────────────────────────────────
    print("\n4. интервала нет → отказ, а не подстановка 8ч")
    cost, ok = _funding_pct(_row(None), None)
    if ok is False and cost == 0.0:
        print("   [OK  ] measured=False, издержка не выдумана (сумма станет нижней оценкой)")
    else:
        failures.append(f"неизмеренный интервал дал measured={ok}, cost={cost}")
        print(f"   [FAIL] measured={ok}, cost={cost}")

    print()
    if failures:
        print(f"НАРУШЕНИЙ: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Интервал фандинга: цепочка биржа → движок → расчёт замкнута.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
