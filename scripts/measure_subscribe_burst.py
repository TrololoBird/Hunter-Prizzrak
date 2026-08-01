"""Замер всплеска WS-подписок на старте движка — против лимита Binance 10 сообщений/с.

ЗАЧЕМ. ``engine/params.py::MAX_SUBSCRIBE_PER_S = 5`` — фантомная ручка: на 2026-08-01 в дереве
ровно ОДНО вхождение имени, её собственное объявление. Ноль читателей, то есть значение не
ограничивает ничего, а выглядит настройкой (класс I-7: «окно без замера — магическое число»).

Прежде чем её сносить или подключать, нужен факт: сколько кадров SUBSCRIBE наш старт реально
отправляет и в каком темпе. Лимит Binance — **10 входящих сообщений в секунду на соединение**,
превышение закрывает сокет. Наш старт спавнит по 7 kline + book + trades на символ плюс четыре
общевселенных потока, то есть ~9·N + 4 задач, и каждая из них зовёт ``watch_*``.

Скрипт НЕ доверяет ни коду, ни докам: он перехватывает ``Client.send`` самого ccxt и штампует
каждый исходящий кадр. Считается:
  * сколько кадров SUBSCRIBE ушло, по соединениям (URL);
  * пик в скользящем окне 1 с — величина, которую и ограничивает биржа;
  * сколько ИМЁН потоков в них перечислено (ccxt считает подписку как 1 кадр, отправляя N имён,
    а Binance считает по кадрам — это разные величины, и путать их нельзя).

Запуск (символы — как у бота, через движок, а не мимо него):

    uv run python scripts/measure_subscribe_burst.py BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT

⚠ Пик считается по КАДРАМ, а не по именам: лимит биржи — на сообщения. Число имён печатается
рядом, потому что оно бьётся о ДРУГОЙ лимит (200 потоков на соединение).
"""
from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from ccxt.async_support.base.ws.client import Client  # noqa: E402

from hunt_core.engine.api import Engine  # noqa: E402

# (t_monotonic, url, method, n_names)
SENT: list[tuple[float, str, str, int]] = []
_ORIG_SEND = Client.send


async def _send_spy(self: Any, message: Any) -> Any:
    method = ""
    n_names = 0
    if isinstance(message, dict):
        method = str(message.get("method") or "")
        params = message.get("params")
        n_names = len(params) if isinstance(params, list) else 0
    SENT.append((time.monotonic(), str(getattr(self, "url", "?")), method, n_names))
    return await _ORIG_SEND(self, message)


def _peak_per_second(stamps: list[float]) -> tuple[int, float]:
    """Максимум кадров в скользящем окне 1 с и момент его начала (от t0)."""
    if not stamps:
        return 0, 0.0
    stamps = sorted(stamps)
    best, best_at, j = 0, 0.0, 0
    for i, t in enumerate(stamps):
        while stamps[j] < t - 1.0:
            j += 1
        if i - j + 1 > best:
            best, best_at = i - j + 1, stamps[j]
    return best, best_at


async def main(symbols: list[str], observe_s: float) -> int:
    Client.send = _send_spy  # type: ignore[method-assign]
    eng = Engine(symbols)
    t0 = time.monotonic()
    try:
        await eng.start()
        # Старт возвращается сразу после спавна задач — подписки уходят уже ПОСЛЕ него,
        # поэтому наблюдение обязано продолжаться, иначе замерим пустоту.
        await asyncio.sleep(observe_s)
    finally:
        Client.send = _ORIG_SEND  # type: ignore[method-assign]
        await eng.close()

    subs = [(t - t0, url, m, n) for (t, url, m, n) in SENT if m.upper() == "SUBSCRIBE"]
    other = [(t - t0, url, m, n) for (t, url, m, n) in SENT if m.upper() != "SUBSCRIBE"]

    print(f"символов: {len(symbols)}   наблюдение: {observe_s:.0f} с")
    print(f"исходящих кадров всего: {len(SENT)}   из них SUBSCRIBE: {len(subs)}")
    if not subs:
        print("\nSUBSCRIBE не отправлялось вообще — ccxt поднял потоки через URL-путь,")
        print("а не командой (комбинированный поток /stream?streams=...). Тогда лимит")
        print("10 сообщений/с к старту НЕ ОТНОСИТСЯ, и ручка не нужна по построению.")
    else:
        names = sum(n for _t, _u, _m, n in subs)
        print(f"имён потоков в них: {names}")

        # ⚠ ПИК СЧИТАЕТСЯ НА СОЕДИНЕНИЕ. Лимит Binance — «10 входящих сообщений в секунду»
        # НА КАЖДОЕ WS-соединение, а не на клиента. Первая редакция этого скрипта считала
        # агрегат по всем сокетам и объявила превышение (14 > 10) там, где его нет: те 14
        # кадров ушли по 11 РАЗНЫМ соединениям, по одному-два на каждое. Агрегатное число
        # печатается ниже отдельно и только как справка о нагрузке на event loop.
        by_stamps: dict[str, list[float]] = defaultdict(list)
        by_url: dict[str, list[int]] = defaultdict(list)
        for t, url, _m, n in subs:
            by_stamps[url].append(t)
            by_url[url].append(n)
        worst_url, worst_peak, worst_at = "", 0, 0.0
        for url, ts in by_stamps.items():
            p, at_ = _peak_per_second(ts)
            if p > worst_peak:
                worst_url, worst_peak, worst_at = url, p, at_
        agg_peak, agg_at = _peak_per_second([t for t, _u, _m, _n in subs])
        print(f"ПИК кадров в окне 1 с НА СОЕДИНЕНИЕ: {worst_peak}   (лимит Binance: 10)")
        print(f"  худшее соединение: {worst_url}   начало окна t+{worst_at:.2f} с")
        print(f"вердикт по лимиту: {'ПРЕВЫШЕНИЕ' if worst_peak > 10 else 'в пределах'}")
        print(f"справочно, агрегат по всем сокетам: {agg_peak} кадров/с при t+{agg_at:.2f} с "
              f"— это НЕ лимит биржи, а нагрузка на наш event loop")
        print("\nпо соединениям (лимит 200 потоков на соединение):")
        for url, ns in sorted(by_url.items(), key=lambda kv: -sum(kv[1])):
            print(f"  {len(ns):3d} кадров, {sum(ns):4d} имён  {url[:90]}")
        print("\nхронология первых 25 кадров SUBSCRIBE:")
        for t, url, _m, n in subs[:25]:
            print(f"  t+{t:6.3f}s  имён={n:3d}  {url[-40:]}")
    if other:
        kinds: dict[str, int] = defaultdict(int)
        for _t, _u, m, _n in other:
            kinds[m or "(без method)"] += 1
        print("\nпрочие исходящие кадры:", ", ".join(f"{k}×{v}" for k, v in kinds.items()))
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    syms = args or ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    raise SystemExit(asyncio.run(main(syms, observe_s=25.0)))
