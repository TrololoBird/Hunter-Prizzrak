"""Насколько 1ч вообще влияет на НАПРАВЛЕНИЕ: замер ДО правки приоритета ТФ (T4.1).

ЗАЧЕМ ИМЕННО ДО. Курс PrizrakTrade требует приоритета старшего ТФ («ПРИОРИТЕТ СТАРШИЙ
ТАЙМФРЕЙМ», «чем старше ТФ, тем выше винрейт») и использует 1ч как ТФ ОТРАБОТКИ, а не
источник директивного смещения. В коде 1ч подмешан в bias весом 0.10
(`prizrak/config.py::htf_1h_weight`). Прежде чем это менять, надо знать цену вопроса:

* если 4ч и 1ч расходятся редко — вес 0.10 декоративен, правка дешёвая и почти
  ничего не сдвинет;
* если часто — вес делал работу, непропорциональную своему размеру, и объём проверки
  должен вырасти соответственно.

Без этого числа нельзя отличить исправление от регрессии: на малой выборке любой исход
задним числом читается как подтверждение.

ЧТО СЧИТАЕТСЯ (всё на ЖИВЫХ кадрах, через НАСТОЯЩИЙ `orchestrator::_htf_bias`):

1. доля символов, где голоса 4ч и 1ч ПРОТИВОПОЛОЖНЫ (bull vs bear);
2. доля, где 1ч — ЕДИНСТВЕННЫЙ прогретый ТФ. Это худший случай текущей схемы:
   нормировка идёт на ДОСТУПНЫЙ вес, поэтому одинокий бычий 1ч даёт norm = +1.0 —
   максимальную уверенность с 10% доказательств (это признано в комментарии самого кода);
3. **сколько символов МЕНЯЮТ итоговый bias**, если вес 1ч обнулить. Это и есть
   old/new-дифф правки, снятый заранее.

    uv run python scripts/measure_tf_priority_conflict.py [N_SYMBOLS]
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.prizrak.config import PrizrakConfig  # noqa: E402
from hunt_core.prizrak.orchestrator import _htf_bias  # noqa: E402

TFS = ("1w", "1d", "4h", "1h")
#: Баров на ТФ: структуре нужен запас на свинги, но не история за годы.
LIMIT = 320


async def fetch_symbol(ex: Any, symbol: str) -> dict[str, list[list[float]]] | None:
    """Кадры по всем ТФ. Пропуск ЛЮБОГО ТФ объявляется вслух — он уменьшает выборку.

    ⚠ Первая редакция глотала отказ (`except Exception: continue`), и это поймал
    pre-commit проекта (ruff S112) — на мне же. Гейт прав по существу, а не формально:
    молча пропущенный символ уменьшает n, по которому потом считаются проценты, и
    делает замер тем самым «числом без охвата», против которого написан I-6.
    """
    out: dict[str, list[list[float]]] = {}
    for tf in TFS:
        try:
            bars = await ex.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
        except Exception as exc:  # noqa: BLE001 — венью может не отдать ТФ; молчать нельзя
            print(f"   ! {symbol} {tf}: кадр не получен ({type(exc).__name__}) — ТФ выпал")
            continue
        if not bars or len(bars) < 30:
            print(f"   ! {symbol} {tf}: баров {len(bars) if bars else 0} < 30 — ТФ выпал")
            continue
        out[tf] = [list(b) for b in bars[:-1]]  # I-5: форминг-бар отбрасывается
    return out or None


async def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    import ccxt.async_support as ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    cfg = PrizrakConfig()

    # Вес 1ч обнулён — ровно та правка, цену которой меряем.
    cfg_no_1h = PrizrakConfig(htf_1h_weight=0.0)

    rows: list[dict[str, Any]] = []
    try:
        await ex.load_markets()
        symbols = [
            s
            for s, m in ex.markets.items()
            if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")
        ]
        symbols.sort()
        # Детерминированная выборка по всему алфавиту вселенной, а не первые N подряд:
        # первые N — это в основном монеты на «1000…», выборка была бы смещённой.
        step = max(1, len(symbols) // n_symbols)
        picked = symbols[::step][:n_symbols]
        print(f"вселенная USDT-перпов: {len(symbols)}, взято {len(picked)} с шагом {step}\n")

        for i, sym in enumerate(picked, 1):
            frames = await fetch_symbol(ex, sym)
            if not frames:
                continue
            old = _htf_bias({}, cfg=cfg, ohlcv_by_tf=frames)
            new = _htf_bias({}, cfg=cfg_no_1h, ohlcv_by_tf=frames)
            votes = old.get("votes") or {}
            rows.append(
                {
                    "symbol": sym,
                    "warm": sorted(votes),
                    "v4h": votes.get("4h", "—"),
                    "v1h": votes.get("1h", "—"),
                    "old_bias": old.get("bias"),
                    "new_bias": new.get("bias"),
                    "old_score": old.get("score"),
                    "new_score": new.get("score"),
                    "old_cov": old.get("weight_available"),
                    "regime": old.get("regime"),
                }
            )
            if i % 10 == 0:
                print(f"  … {i}/{len(picked)}", flush=True)
    finally:
        await ex.close()

    if not rows:
        print("НИ ОДНОГО символа не собрано — замер не состоялся (это не ноль).")
        return 1

    n = len(rows)
    opposed = [r for r in rows if {r["v4h"], r["v1h"]} == {"bull", "bear"}]
    only_1h = [r for r in rows if r["warm"] == ["1h"]]
    flipped = [r for r in rows if r["old_bias"] != r["new_bias"]]

    print(f"\n{'='*74}\nСИМВОЛОВ ИЗМЕРЕНО: {n}\n{'='*74}")
    print(f"1. 4ч и 1ч ПРОТИВОПОЛОЖНЫ      : {len(opposed):>3} ({len(opposed)/n*100:.1f}%)")
    print(f"2. 1ч — ЕДИНСТВЕННЫЙ прогретый : {len(only_1h):>3} ({len(only_1h)/n*100:.1f}%)")
    print(f"3. bias МЕНЯЕТСЯ без веса 1ч   : {len(flipped):>3} ({len(flipped)/n*100:.1f}%)")

    print("\nраспределение bias:")
    print(f"   было : {dict(Counter(r['old_bias'] for r in rows))}")
    print(f"   стало: {dict(Counter(r['new_bias'] for r in rows))}")
    print("\nрежимы (accumulation/distribution нейтрализуют и без правки):")
    print(f"   {dict(Counter(str(r['regime']) for r in rows))}")

    if flipped:
        print(f"\nсимволы, сменившие сторону ({len(flipped)}):")
        print(f"   {'символ':<20}{'4ч':<8}{'1ч':<8}{'было':<10}{'стало':<10}{'score было→стало'}")
        for r in flipped:
            print(
                f"   {r['symbol']:<20}{r['v4h']:<8}{r['v1h']:<8}"
                f"{str(r['old_bias']):<10}{str(r['new_bias']):<10}"
                f"{r['old_score']} → {r['new_score']}"
            )

    if only_1h:
        print(f"\n⚠ символы, где направление задаёт ОДИН 1ч ({len(only_1h)}):")
        for r in only_1h:
            print(f"   {r['symbol']:<20} bias={r['old_bias']} score={r['old_score']} cov={r['old_cov']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
