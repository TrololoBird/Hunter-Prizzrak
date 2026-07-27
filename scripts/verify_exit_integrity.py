"""Проверка на живых свечах: не был ли стоп пробит РАНЬШЕ записанного выхода.

Зачем. Пересчёт в R показал, что 33% валового результата дают выходы категории
`unresolved` — прежде всего `orphan_expired` (+127.7R на 43 сделках, в среднем +2.97R).
А `orphan_expired` закрывается так (`_evaluate_levels.py`): позицию не сверяли
`orphan_ttl_h` часов, это заметили и закрыли ПО ТЕКУЩЕЙ ЦЕНЕ. То есть цена выхода берётся в
момент обнаружения — и запись молча утверждает, что за всё время без присмотра стоп не
задели. Никто этого не проверял.

Метод. Для каждой сделки поднимаются настоящие свечи Binance между открытием и закрытием и
задаётся один вопрос: **касалась ли цена ПЕРВОНАЧАЛЬНОГО стопа до записанного выхода?**

Почему именно первоначального — это и делает вывод строгим. Стоп двигают только в сторону
прибыли (безубыток, трейл), значит первоначальный — самый ДАЛЁКИЙ из всех, что когда-либо
стояли. Если цена дошла до него, она прошла и через любой более поздний. Обратное неверно,
поэтому пробой первоначального стопа — достаточное условие выхода при ЛЮБОЙ модели ведения
позиции, а не только при той, что была включена.

Что скрипт НЕ делает: не переписывает историю и не выдумывает «настоящий» результат. Он
помечает запись как ОПРОВЕРГНУТУЮ и считает, во сколько R обходится разница, если
опровергнутым сделкам присвоить −1R (выход по стопу) вместо записанной прибыли.

Запуск:
    uv run python scripts/verify_exit_integrity.py
    uv run python scripts/verify_exit_integrity.py --write
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import pathlib

import ccxt.pro as ccxtpro

from hunt_core.track.equity import stop_distance_pct
from hunt_core.track.outcomes import UNRESOLVED_REASONS, is_polluted

HISTORY = pathlib.Path("data/signal_history.jsonl")
REPORT = pathlib.Path("docs/audit/exit-integrity-2026-07-27.md")


def _ts(value: object) -> dt.datetime | None:
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)


def _num(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def candidates() -> list[dict]:
    """Записи с прибылью, закрытые НЕ по достижению цели или стопа."""
    rows = [json.loads(ln) for ln in HISTORY.open(encoding="utf-8") if ln.strip()]
    out = []
    for r in rows:
        if is_polluted(r):
            continue
        if str(r.get("close_reason")) not in UNRESOLVED_REASONS:
            continue
        if float(r.get("pnl_pct") or 0.0) <= 0.0:
            continue
        out.append(r)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = candidates()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    verdict: collections.Counter[str] = collections.Counter()
    lines: list[str] = []
    r_recorded = r_if_stopped = 0.0
    try:
        for r in rows:
            sym = str(r.get("symbol") or "")
            direction = str(r.get("direction") or "")
            opened, closed = _ts(r.get("opened_at")), _ts(r.get("closed_at"))
            stop = _num(r.get("original_stop_loss")) or _num(r.get("stop_loss"))
            dist = stop_distance_pct(r)
            pnl = float(r.get("pnl_pct") or 0.0)
            if not (sym and opened and closed and stop and dist):
                verdict["нет данных"] += 1
                continue
            unified = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym
            if unified not in ex.markets:
                verdict["символ делистнут"] += 1
                continue
            span_min = max(1.0, (closed - opened).total_seconds() / 60.0)
            tf, tf_min = ("15m", 15.0) if span_min > 240.0 else ("1m", 1.0)
            try:
                bars = await ex.fetch_ohlcv(
                    unified, tf, since=int(opened.timestamp() * 1000),
                    limit=min(1000, int(span_min / tf_min) + 4),
                )
            except Exception:  # noqa: BLE001 — недоступная история не приговор записи
                verdict["история недоступна"] += 1
                continue
            lo_ms, hi_ms = opened.timestamp() * 1000, closed.timestamp() * 1000
            bars = [b for b in bars if lo_ms <= b[0] <= hi_ms]
            if len(bars) < 2:
                verdict["мало свечей"] += 1
                continue
            breached = any(
                (float(b[3]) <= stop) if direction == "long" else (float(b[2]) >= stop)
                for b in bars
            )
            r_recorded += pnl / dist
            if not breached:
                verdict["подтверждена"] += 1
                r_if_stopped += pnl / dist
                continue
            verdict["ОПРОВЕРГНУТА — стоп был задет"] += 1
            r_if_stopped += -1.0
            lines.append(
                f"| {sym} | {direction} | {r.get('close_reason')} | {stop:g} | "
                f"{pnl:+.1f}% | {pnl / dist:+.1f}R | {span_min / 60:.1f}ч |"
            )
    finally:
        await ex.close()

    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    say(f"проверено прибыльных неразрешённых выходов: {len(rows)}")
    for k, v in verdict.most_common():
        say(f"  {k:32s} {v}")
    say()
    say(f"записанный вклад этих сделок:      {r_recorded:+.1f}R")
    say(f"если опровергнутым присвоить −1R:  {r_if_stopped:+.1f}R")
    say(f"разница:                           {r_if_stopped - r_recorded:+.1f}R")
    if lines:
        say()
        say("| символ | напр. | причина | стоп | записанный pnl | в R | под присмотром |")
        say("|---|---|---|---|---|---|---|")
        out.extend(lines)
        for line in lines[:12]:
            print("   ", line)

    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Целостность выходов: был ли стоп задет раньше — 2026-07-27\n\n"
            + "\n".join(out) + "\n",
            encoding="utf-8",
        )
        print(f"\nотчёт: {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
