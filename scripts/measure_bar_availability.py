"""Замер: через сколько после ГРАНИЦЫ бара движок реально отдаёт этот бар закрытым.

Нужен, чтобы будильник «по закрытию свечи» не разбудил полосу РАНЬШЕ данных. Проснуться
рано хуже, чем поздно: полоса прочитает прошлый бар, но с новым штампом свежести — то есть
получится замороженный кадр, который не видит ни один прибор (сигнатура `stale-htf-cache-trap`,
трап №4 в `.claude/rules/engine-data-plane.md`).

Путь появления бара (`engine/ingest.py::_step_ohlcv`): WS `watch_ohlcv` отдаёт СИГНАЛ о
закрытии → из ccxt-кэша берутся закрытые бары → на новый бар делается REST
`fetch_klines_full` (WS-бар без taker-объёма не мержится) → `merge_frame`. То есть задержка =
латентность WS + круг REST, и она НЕ зависит от таймфрейма. Поэтому меряем на 1m: тот же
код, 15 замеров за то же время, что один замер на 15m.

Запуск:
    uv run python scripts/measure_bar_availability.py --minutes 12
    uv run python scripts/measure_bar_availability.py --symbols BTC/USDT:USDT --minutes 6
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import structlog

from hunt_core.engine.api import Engine

LOG = structlog.get_logger(__name__)

_POLL_S = 0.25
_TF = "1m"
_STEP_MS = 60_000


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


async def _measure(symbols: list[str], minutes: float) -> int:
    engine = Engine(symbols)
    await engine.start()
    # Прогрев: до первого закрытия WS-хвост может ещё не завестись, и первый бар померил бы
    # не задержку доставки, а время подъёма подписок. Это разные величины.
    LOG.info("bar_availability_warmup", seconds=20)
    await asyncio.sleep(20.0)

    newest_seen: dict[str, int] = {}
    delays: dict[str, list[float]] = {s: [] for s in symbols}
    deadline = time.monotonic() + minutes * 60.0
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_S)
            now_ms = time.time() * 1000.0
            for symbol in symbols:
                try:
                    frame = engine.snapshot(symbol, (f"kline.{_TF}",)).require(f"kline.{_TF}")
                except Exception as exc:  # noqa: BLE001 — план ещё не свеж; называем и идём
                    LOG.debug("bar_availability_plane_not_ready", symbol=symbol, err=repr(exc))
                    continue
                if not isinstance(frame, list) or not frame:
                    continue
                newest_open = int(frame[-1][0])
                prev = newest_seen.get(symbol)
                newest_seen[symbol] = newest_open
                if prev is None or newest_open <= prev:
                    continue
                # Бар с open=T закрылся в T+step; замеряем от ГРАНИЦЫ, а не от открытия.
                boundary_ms = newest_open + _STEP_MS
                delay_s = (now_ms - boundary_ms) / 1000.0
                if delay_s < -1.0:
                    # Кадр отдал бар, который ещё не закрылся, — это нарушение I-5, а не шум.
                    LOG.error(
                        "bar_availability_lookahead",
                        symbol=symbol,
                        open_ms=newest_open,
                        ahead_s=round(-delay_s, 2),
                    )
                    continue
                delays[symbol].append(max(0.0, delay_s))
                LOG.info(
                    "bar_closed_visible",
                    symbol=symbol,
                    open_ms=newest_open,
                    delay_s=round(delay_s, 2),
                )
    finally:
        await engine.close()

    print("\nЗАДЕРЖКА «граница бара → бар виден закрытым в движке» (1m, живой WS+REST)")
    print(f"опрос каждые {_POLL_S} с — разрешение замера {_POLL_S} с\n")
    everything: list[float] = []
    for symbol in symbols:
        vals = delays.get(symbol) or []
        if not vals:
            print(f"  {symbol:<18} НИ ОДНОГО закрытия за окно — замер не состоялся, не ноль")
            continue
        everything.extend(vals)
        print(
            f"  {symbol:<18} n={len(vals):>3}  med {statistics.median(vals):5.2f} с · "
            f"p90 {_pct(vals, 0.90):5.2f} с · max {max(vals):5.2f} с"
        )
    if not everything:
        print("\n  ВЫВОДА НЕТ: ни одного закрытого бара не наблюдалось.")
        return 1
    med, p90, p99, mx = (
        statistics.median(everything),
        _pct(everything, 0.90),
        _pct(everything, 0.99),
        max(everything),
    )
    print(
        f"\n  ИТОГО n={len(everything)}  med {med:.2f} с · p90 {p90:.2f} с · "
        f"p99 {p99:.2f} с · max {mx:.2f} с"
    )
    # Рекомендация — от ИЗМЕРЕННОГО хвоста, а не «разумное значение» (I-7 запрещает второе).
    settle = max(2.0, round(mx + 1.0, 1))
    print(
        f"  ⇒ безопасная задержка будильника после границы: {settle:.1f} с "
        f"(max наблюдённый {mx:.2f} с + 1 с запаса)"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT")
    ap.add_argument("--minutes", type=float, default=12.0)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(_measure(symbols, args.minutes))


if __name__ == "__main__":
    raise SystemExit(main())
