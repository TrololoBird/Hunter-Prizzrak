"""Проверка: пробуждение по закрытию бара НЕ читает ещё не закрытый бар.

Это главное свойство безопасности правки «полоса эмиссии просыпается по границе бара»
(`runtime/analyst_assembly.py::_next_bar_wake_ts`). Проснуться поздно — потеря лага.
Проснуться РАНО — хуже: полоса прочитает ПРОШЛЫЙ бар, но со свежим штампом, то есть
получит замороженный кадр, которого не видит ни один прибор (`stale-htf-cache-trap`).

Скрипт гоняет НАСТОЯЩУЮ функцию планирования против ЖИВОГО движка:

    для каждой границы бара
        wake = _next_bar_wake_ts(now)      ← та же функция, что в полосе
        спим ровно до wake
        спрашиваем движок: какой у него сейчас новейший ЗАКРЫТЫЙ бар?
        ОЖИДАНИЕ: это ровно тот бар, который только что закрылся

Провал (движок отдаёт предыдущий бар) означает, что запаса `_BAR_SETTLE_S` не хватает.

Сетка берётся 1m, а не 15m, намеренно: путь появления бара
(`engine/ingest.py::_step_ohlcv` — WS-сигнал → REST full-fidelity → merge) от таймфрейма
не зависит, а замеров за то же время получается в 15 раз больше.

Запуск:
    uv run python scripts/verify_bar_close_wake.py --cycles 8
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import structlog

from hunt_core import clock
from hunt_core.engine.api import Engine
from hunt_core.runtime.analyst_assembly import _BAR_SETTLE_S, _next_bar_wake_ts

LOG = structlog.get_logger(__name__)

_GRID_S = 60.0  # 1m — тот же код доставки, больше выборка
_TF = "1m"


async def _run(symbols: list[str], cycles: int) -> int:
    engine = Engine(symbols)
    await engine.start()
    # Часы уже сведены с биржей внутри `Engine.start` — сетка баров живёт в биржевом
    # времени, и проверять её по локальным часам значило бы проверять не то.
    LOG.info("verify_wake_clock", offset_ms=round(clock.offset_ms(), 1), synced=clock.is_synced())
    LOG.info("verify_wake_warmup", seconds=25)
    await asyncio.sleep(25.0)

    ok = 0
    early = 0
    skipped = 0
    margins: list[float] = []
    try:
        for _ in range(cycles):
            now = clock.now_ms() / 1000.0
            wake = _next_bar_wake_ts(now, grid_s=_GRID_S, settle_s=_BAR_SETTLE_S)
            boundary = wake - _BAR_SETTLE_S
            await asyncio.sleep(max(0.0, wake - now))

            # Бар, который ДОЛЖЕН быть новейшим закрытым в момент пробуждения.
            expected_open_ms = int((boundary - _GRID_S) * 1000)
            try:
                frame = engine.snapshot(symbols[0], (f"kline.{_TF}",)).require(f"kline.{_TF}")
            # План не свеж — это НЕ «проснулись рано», а отказ плана, и считается отдельно.
            # (Директивы `noqa: BLE001` здесь нет намеренно: правило в `pyproject.toml`
            # не включено, поэтому такая директива ничего не подавляет — см. Issue #20.)
            except Exception as exc:
                skipped += 1
                LOG.warning("verify_wake_plane_unavailable", err=repr(exc))
                continue
            if not isinstance(frame, list) or not frame:
                skipped += 1
                LOG.warning("verify_wake_empty_frame")
                continue
            newest_open = int(frame[-1][0])
            lag_s = clock.now_ms() / 1000.0 - boundary
            if newest_open == expected_open_ms:
                ok += 1
                margins.append(lag_s)
                LOG.info(
                    "verify_wake_ok",
                    boundary=int(boundary),
                    lag_s=round(lag_s, 2),
                    newest_open=newest_open,
                )
            elif newest_open < expected_open_ms:
                early += 1
                LOG.error(
                    "verify_wake_TOO_EARLY",
                    boundary=int(boundary),
                    expected_open=expected_open_ms,
                    got_open=newest_open,
                    behind_bars=(expected_open_ms - newest_open) // int(_GRID_S * 1000),
                    note="движок ещё не отдал закрытый бар — запаса не хватает",
                )
            else:
                # Кадр УШЁЛ ВПЕРЁД относительно ожидаемого — значит проспали целый бар.
                skipped += 1
                LOG.warning(
                    "verify_wake_overslept",
                    expected_open=expected_open_ms,
                    got_open=newest_open,
                )
    finally:
        await engine.close()

    total = ok + early + skipped
    print(f"\nПРОВЕРКА ПРОБУЖДЕНИЯ ПО ЗАКРЫТИЮ БАРА (сетка {_GRID_S:.0f} с, запас {_BAR_SETTLE_S:.0f} с)")
    print(f"  циклов {total}: вовремя {ok} · РАНО {early} · пропущено/не готово {skipped}")
    if margins:
        print(
            f"  фактический лаг от границы: med {statistics.median(margins):.2f} с · "
            f"min {min(margins):.2f} с · max {max(margins):.2f} с"
        )
        print(f"  запас над наблюдённым минимумом доставки: {min(margins):.2f} с")
    if early:
        print("\n  ❌ ПРОВАЛ: полоса просыпалась РАНЬШЕ появления бара — поднять _BAR_SETTLE_S")
        return 1
    if not ok:
        print("\n  ⚠ ВЫВОДА НЕТ: ни одного удачного цикла (план не поднялся) — не ноль, а нечем мерить")
        return 2
    print("\n  ✅ Ни одного раннего чтения: в каждый момент пробуждения бар уже закрыт и доставлен")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="BTC/USDT:USDT")
    ap.add_argument("--cycles", type=int, default=8)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    started = time.monotonic()
    rc = asyncio.run(_run(symbols, args.cycles))
    print(f"  (прогон {time.monotonic() - started:.0f} с)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
