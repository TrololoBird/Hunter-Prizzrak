"""Гейт свежести слома: фильтрует ли он и в ту ли сторону (T4.2).

ЗАЧЕМ. `orchestrator.py::_direction_has_slom` — гейт, РАЗРЕШАЮЩИЙ контр-трендовый вход:
курс требует «для шортов нужен свежий слом структуры на МТФ», и без свежего слома вход
против старшего смещения ветируется (`_htf_gate` → veto). Значит ошибка здесь либо
открывает вход там, где курс запрещает, либо закрывает там, где он разрешён.

Аудит `windows-2026-07-26.md:53` записал «`offset=0` в 280/322 на 1ч и 206/221 на 1д» и
вывел «проверка свежести не фильтрует». Проверяется ОБА утверждения — и факт, и вывод.

⚠ ПОДОЗРЕНИЕ, РАДИ КОТОРОГО ЗАМЕР И ПИШЕТСЯ. Проверка выглядит так:

    (s.get(f"{k}_bar_offset") or 99) <= max_bar_offset

Это falsy-zero цепочка (класс I-6). Продюсер (`pipeline/structure.py:131`) считает
``bos_up_bar_offset = (_n - 1 - idx_hh)``, то есть **0 = сломанный уровень стоит на
ПОСЛЕДНЕМ баре**, самый свежий слом из возможных. А ``0 or 99`` даёт 99, и `99 <= 5`
ложно — то есть самый свежий слом объявляется самым протухшим.

Если так, вывод аудита неверен вдвойне: гейт не «не фильтрует», а фильтрует НАОБОРОТ.

СЧИТАЕТСЯ: распределение offset по каждому виду слома; доля нулей; и вердикт `_fresh`
в трёх вариантах — как сейчас, с корректной проверкой `is None`, и разница между ними.

    uv run python scripts/measure_bos_freshness.py [N_SYMBOLS]
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.prizrak.config import PrizrakConfig  # noqa: E402
from hunt_core.prizrak.pipeline.structure import _detect_structure  # noqa: E402

TFS = ("1h", "4h", "1d")
LIMIT = 400
KEYS_LONG = ("bos_up", "choch_bull")
KEYS_SHORT = ("bos_down", "choch_bear")


def fresh_now(s: dict[str, Any], keys: tuple[str, ...], max_off: int) -> bool:
    """Текущая реализация — дословно из `_direction_has_slom`."""
    return any(s.get(k) and (s.get(f"{k}_bar_offset") or 99) <= max_off for k in keys)


def fresh_fixed(s: dict[str, Any], keys: tuple[str, ...], max_off: int) -> bool:
    """Та же проверка с честным `is None` вместо falsy-zero."""
    for k in keys:
        if not s.get(k):
            continue
        off = s.get(f"{k}_bar_offset")
        if off is not None and int(off) <= max_off:
            return True
    return False


async def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    import ccxt.async_support as ccxt

    cfg = PrizrakConfig()
    max_off = cfg.bos_max_bar_offset
    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "swap"}})

    offsets: dict[str, Counter[int]] = {k: Counter() for k in KEYS_LONG + KEYS_SHORT}
    n_struct = 0
    agree = disagree_now_false = disagree_now_true = 0

    try:
        await ex.load_markets()
        symbols = sorted(
            s
            for s, m in ex.markets.items()
            if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")
        )
        step = max(1, len(symbols) // n_symbols)
        picked = symbols[::step][:n_symbols]
        print(f"вселенная {len(symbols)}, взято {len(picked)}; порог свежести = {max_off}\n")

        for i, sym in enumerate(picked, 1):
            for tf in TFS:
                try:
                    raw = await ex.fetch_ohlcv(sym, timeframe=tf, limit=LIMIT)
                except Exception as exc:  # noqa: BLE001 — молчать нельзя: выборка сжимается
                    print(f"   ! {sym} {tf}: {type(exc).__name__} — ТФ выпал")
                    continue
                if not raw or len(raw) < 80:
                    print(f"   ! {sym} {tf}: баров {len(raw) if raw else 0} — мало, ТФ выпал")
                    continue
                bars = [
                    {"open": float(b[1]), "high": float(b[2]), "low": float(b[3]),
                     "close": float(b[4])}
                    for b in raw[:-1]  # I-5
                ]
                # Скользим окном: одна структура на символ/ТФ дала бы 3 наблюдения,
                # а нужен распределённый замер.
                for end in range(80, len(bars) + 1, 10):
                    s = _detect_structure(
                        bars[:end],
                        lookback_pivot=cfg.structure_lookback_pivot,
                        lookback_hh_ll=cfg.structure_lookback_hh_ll,
                        bos_buffer=cfg.structure_bos_buffer_pct,
                    )
                    if not isinstance(s, dict):
                        continue
                    n_struct += 1
                    for k in KEYS_LONG + KEYS_SHORT:
                        if s.get(k):
                            off = s.get(f"{k}_bar_offset")
                            offsets[k][-1 if off is None else int(off)] += 1
                    for keys in (KEYS_LONG, KEYS_SHORT):
                        a = fresh_now(s, keys, max_off)
                        b = fresh_fixed(s, keys, max_off)
                        if a == b:
                            agree += 1
                        elif b and not a:
                            disagree_now_false += 1
                        else:
                            disagree_now_true += 1
            if i % 10 == 0:
                print(f"  … {i}/{len(picked)}", flush=True)
    finally:
        await ex.close()

    if not n_struct:
        print("НИ ОДНОЙ структуры — замер не состоялся (это не ноль).")
        return 1

    print(f"\n{'='*70}\nструктур посчитано: {n_struct}\n{'='*70}")
    print("Распределение bar_offset у СРАБОТАВШИХ сломов (-1 = offset отсутствует):\n")
    print(f"   {'вид слома':<16}{'всего':>8}{'offset=0':>12}{'доля 0':>9}{'<=порог':>10}{'>порог':>9}")
    total_zero = total_flagged = 0
    for k in KEYS_LONG + KEYS_SHORT:
        c = offsets[k]
        tot = sum(c.values())
        if not tot:
            print(f"   {k:<16}{0:>8}{'—':>12}{'—':>9}{'—':>10}{'—':>9}")
            continue
        zero = c[0]
        within = sum(v for off, v in c.items() if 0 <= off <= max_off)
        beyond = sum(v for off, v in c.items() if off > max_off)
        total_zero += zero
        total_flagged += tot
        print(f"   {k:<16}{tot:>8}{zero:>12}{zero/tot*100:>8.1f}%{within:>10}{beyond:>9}")
    if total_flagged:
        print(f"\n   ИТОГО: сломов {total_flagged}, из них offset=0 — {total_zero} "
              f"({total_zero/total_flagged*100:.1f}%)")

    checks = agree + disagree_now_false + disagree_now_true
    print(f"\nВердикт «свежий слом», {checks} проверок направления:")
    print(f"   совпало (сейчас == честная проверка) : {agree} ({agree/checks*100:.1f}%)")
    print(f"   сейчас FALSE, честная TRUE           : {disagree_now_false}"
          f" ({disagree_now_false/checks*100:.1f}%)  ← свежий слом объявлен протухшим")
    print(f"   сейчас TRUE, честная FALSE           : {disagree_now_true}"
          f" ({disagree_now_true/checks*100:.1f}%)")
    if disagree_now_false:
        print("\n⚠ ГЕЙТ ФИЛЬТРУЕТ НАОБОРОТ: falsy-zero превращает offset=0 (самый свежий")
        print("  слом) в 99 и ветирует контр-трендовый вход, который курс разрешает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
