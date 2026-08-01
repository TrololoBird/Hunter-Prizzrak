"""Сколько работы тика ОСМЫСЛЕННО, а сколько — пересчёт одного и того же.

ВОПРОС ВЛАДЕЛЬЦА (2026-08-01): «откуда вообще идея тика в 30 секунд?»

ФАКТ, с которого начинается разбор: `_cli.py` содержит
``watch_parser.add_argument("--interval", type=int, default=30)`` — голый дефолт из первого
коммита, без замера и без обоснования. По инварианту I-7 это магическое число, и стоит оно
в самом нагруженном месте системы.

ПОЧЕМУ ЭТО НЕ ПРОСТО «НЕОБОСНОВАННО». Фичи считаются на ЗАКРЫТЫХ барах (инвариант I-5):
``features/build.py::compute_features`` берёт кадр каждого ТФ и гоняет по нему полный стек
индикаторов. Пока новый бар не закрылся, кадр НЕ МЕНЯЕТСЯ — значит результат тот же самый.
Тик по стенным часам исполняет работу, привязанную к событию «бар закрылся».

ЧТО МЕРИТ ЭТОТ СКРИПТ. Подключается к движку как боевой прогон, каждые ``--interval``
секунд снимает время последнего ЗАКРЫТОГО бара у каждой пары (символ, ТФ) и считает, у
скольких оно изменилось. Отношение «изменилось / всего» — это доля осмысленной работы;
остальное пересчитывается впустую.

⚠ Скрипт НЕ доверяет арифметике «бар 1ч делится на 30 с = 120». Реальный ответ может
отличаться: кадр приходит с задержкой эмиссии, движок пересевает планы при переподключении,
а `1m` иногда обновляется чаще ожидаемого. Меряется наблюдаемое, а не выводимое.

    uv run python scripts/measure_tick_redundancy.py                 # 8 циклов по 30 с
    uv run python scripts/measure_tick_redundancy.py --cycles 20 --interval 30
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.engine.api import Engine  # noqa: E402

_DEFAULT_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    "XAU/USDT:USDT", "XAG/USDT:USDT", "PAXG/USDT:USDT",
]
# Ровно те, что считает `features/build.py::_TF_TO_FIELD`.
_TFS = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
# Номинальная длительность бара — только для печати ожидания рядом с фактом.
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}


def _last_open_ms(frame: Any) -> int | None:
    """Время ОТКРЫТИЯ последнего бара кадра, мс. ``None`` — кадра нет или он пуст.

    ⚠ Кадр движка — это ``list[Bar]``, где ``Bar = list[float]`` вида
    ``[open_ms, open, high, low, close, volume, ...]`` (см. ``engine/state.py``), а НЕ
    Polars-кадр: первая редакция этого скрипта звала ``frame.row(-1)`` и не получила бы
    ничего. Опознавательный признак нового бара — ``open_ms`` последнего элемента; брать
    ``close_ms`` нельзя, он в кадре опционален.
    """
    if not frame:
        return None
    try:
        last = frame[-1]
        return int(last[0])
    except (IndexError, TypeError, ValueError):
        return None


async def main(symbols: list[str], cycles: int, interval: float) -> int:
    eng = Engine(symbols)
    prev: dict[tuple[str, str], int] = {}
    changed: dict[str, int] = defaultdict(int)
    observed: dict[str, int] = defaultdict(int)
    absent: dict[str, int] = defaultdict(int)

    print(f"символов {len(symbols)} × ТФ {len(_TFS)} = {len(symbols) * len(_TFS)} кадров на цикл")
    print(f"циклов {cycles} × интервал {interval:g} с ≈ {cycles * interval / 60:.1f} мин\n")
    await eng.start()
    try:
        for cycle in range(cycles):
            if cycle:
                await asyncio.sleep(interval)
            n_changed = n_total = 0
            for sym in symbols:
                state = eng._ingest.states.get(sym)  # noqa: SLF001 — диагностика по месту
                if state is None:
                    continue
                for tf in _TFS:
                    key = (sym, tf)
                    frame = state.frame_of(f"kline.{tf}")
                    stamp = _last_open_ms(frame)
                    if stamp is None:
                        absent[tf] += 1
                        continue
                    observed[tf] += 1
                    n_total += 1
                    if key in prev and prev[key] != stamp:
                        changed[tf] += 1
                        n_changed += 1
                    prev[key] = stamp
            if cycle:
                pct = (n_changed / n_total * 100.0) if n_total else 0.0
                print(f"  цикл {cycle:3d}: изменилось {n_changed:3d} из {n_total:3d} кадров "
                      f"({pct:5.1f}%)")
    finally:
        await eng.close()

    print("\nпо таймфреймам (за все циклы, кроме первого — он только заполняет базу):")
    print(f"  {'ТФ':5s} {'наблюдений':>11s} {'изменений':>10s} {'доля':>7s}  "
          f"{'ожидание по номиналу':>21s}")
    total_obs = total_chg = 0
    for tf in _TFS:
        obs = max(0, observed[tf] - len([s for s in symbols]))  # первый цикл не считается
        chg = changed[tf]
        total_obs += obs
        total_chg += chg
        share = (chg / obs * 100.0) if obs else 0.0
        expect = (interval / _TF_SECONDS[tf] * 100.0) if _TF_SECONDS.get(tf) else 0.0
        note = f"{expect:5.2f}%" if expect else "—"
        print(f"  {tf:5s} {obs:11d} {chg:10d} {share:6.1f}%  {note:>21s}")
        if absent[tf]:
            print(f"        (кадр отсутствовал {absent[tf]} раз — план не прогрет либо не ведётся)")

    if not total_obs:
        print("\nНАБЛЮДЕНИЙ НЕТ — движок не прогрелся. Это НЕ результат, повторить дольше.")
        return 1

    useful = total_chg / total_obs * 100.0
    print(f"\n{'=' * 72}")
    print(f"ОСМЫСЛЕННАЯ ДОЛЯ РАБОТЫ ТИКА: {total_chg} из {total_obs} пересчётов кадра = "
          f"{useful:.1f}%")
    print(f"ВПУСТУЮ: {100.0 - useful:.1f}%  ({total_obs - total_chg} пересчётов дали тот же результат)")
    print()
    print("Читать так: кадр пересчитывается полным стеком индикаторов независимо от того,")
    print("закрылся ли новый бар. Пока не закрылся — вход тот же, значит и выход тот же.")
    print("Это не «медленный код», а работа, привязанная к событию, но исполняемая по таймеру.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=8)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("symbols", nargs="*")
    args = ap.parse_args()
    t0 = time.monotonic()
    code = asyncio.run(main(args.symbols or _DEFAULT_SYMBOLS, args.cycles, args.interval))
    print(f"\nзамер занял {time.monotonic() - t0:.0f} с")
    raise SystemExit(code)
