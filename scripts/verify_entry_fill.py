"""Проверка на живых свечах: торговался ли рынок в зоне входа, когда сигнал открыли.

Зачем. Пересчёт леджера искал недостижимые цены ВЫХОДА и нашёл 14 записей. Но независимая
проверка вскрыла, что дефект на входе крупнее: у BREVUSDT записана зона входа 0.06855 при
рынке 0.08285 в ту же минуту — **вход на 20.9% НИЖЕ рынка**, — а записанная прибыль +19.79%
почти в точности равна этому разрыву. Позиция «открылась» по цене, до которой рынок не
доходил, и разница между выдуманным входом и настоящим рынком сразу превратилась в MFE.

Косвенная улика в самой записи: `extreme_lo` РОВНО равен цене входа (0.06855), то есть
экстремум засеян входом, а не наблюдением. Дальше трейлинг идёт от него, стоп подтягивается
в «прибыль», и выход книжится как `trailing_stop_profit`.

Что меряется. Для каждой закрытой записи поднимаются настоящие 1m-свечи Binance и задаются
два вопроса:

  1. **Где был рынок в момент регистрации** относительно зоны входа. Для лонга вход обязан
     быть НЕ ВЫШЕ рынка (лимитка стоит снизу), но если рынок уже сильно ВЫШЕ зоны — лимитка
     не могла исполниться, её просто перепрыгнули.
  2. **Касался ли рынок зоны** в окне перед регистрацией. Если за час до открытия цена ни
     разу не была в [entry_lo, entry_hi] — исполнения не было ни в какой момент.

⚠ Порог `_GAP_TOL_PCT` — не «разумное значение», а допуск на то, что зона могла быть задета
секундой раньше открытия бара. Считается от медианного размаха 1m-бара самого символа, а не
константой: у неликвида бар шире, и единый процент объявил бы фантомом нормальное исполнение.

Запуск:
    uv run python scripts/verify_entry_fill.py
    uv run python scripts/verify_entry_fill.py --write
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import pathlib

import ccxt.pro as ccxtpro

from hunt_core.track.outcomes import is_polluted

HISTORY = pathlib.Path("data/signal_history.jsonl")
REPORT = pathlib.Path("docs/audit/entry-fill-2026-07-27.md")
_LOOKBACK_MIN = 60
_BAR_MS = 60_000


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


def load() -> list[dict]:
    rows = [json.loads(ln) for ln in HISTORY.open(encoding="utf-8") if ln.strip()]
    return [r for r in rows if not is_polluted(r) and r.get("close_reason")]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="проверить только N записей")
    args = ap.parse_args()

    rows = load()
    if args.limit:
        rows = rows[: args.limit]
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    verdict: collections.Counter[str] = collections.Counter()
    bad: list[dict] = []
    checked = 0
    try:
        for r in rows:
            sym = str(r.get("symbol") or "")
            direction = str(r.get("direction") or "")
            opened = _ts(r.get("opened_at"))
            lo, hi = _num(r.get("entry_lo")), _num(r.get("entry_hi"))
            if not (sym and opened and lo and hi):
                verdict["нет данных"] += 1
                continue
            unified = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym
            if unified not in ex.markets:
                verdict["символ делистнут"] += 1
                continue
            since = int(opened.timestamp() * 1000 // _BAR_MS * _BAR_MS) - _LOOKBACK_MIN * _BAR_MS
            try:
                bars = await ex.fetch_ohlcv(unified, "1m", since=since, limit=_LOOKBACK_MIN + 3)
            except Exception:  # noqa: BLE001 — недоступная история не приговор записи
                verdict["история недоступна"] += 1
                continue
            if len(bars) < 5:
                verdict["мало свечей"] += 1
                continue
            open_ms = int(opened.timestamp() * 1000 // _BAR_MS * _BAR_MS)
            at_open = next((b for b in bars if b[0] == open_ms), None)
            before = [b for b in bars if b[0] <= open_ms]
            if at_open is None or len(before) < 5:
                verdict["мало свечей"] += 1
                continue
            checked += 1
            market = float(at_open[1])  # open первой минуты сделки
            # Допуск от медианного размаха бара самого символа — не константа.
            spans = sorted((float(b[2]) - float(b[3])) / float(b[4]) * 100.0
                           for b in before if float(b[4]) > 0)
            tol = spans[len(spans) // 2] if spans else 0.1
            # Касалась ли цена зоны за час до открытия
            touched = any(float(b[3]) <= hi and float(b[2]) >= lo for b in before)
            if direction == "long":
                gap = (market - hi) / hi * 100.0
            else:
                gap = (lo - market) / lo * 100.0
            if gap > tol and not touched:
                verdict["ВХОД НЕ ИСПОЛНИМ — рынок мимо зоны"] += 1
                bad.append({**r, "_gap": gap, "_market": market, "_tol": tol})
            elif gap > tol:
                verdict["рынок ушёл, но зона касалась ранее"] += 1
            else:
                verdict["исполним"] += 1
    finally:
        await ex.close()

    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    say(f"проверено записей: **{checked}** из {len(rows)}")
    say()
    for k, v in verdict.most_common():
        say(f"* {k}: **{v}**" if "НЕ ИСПОЛНИМ" in k else f"* {k}: {v}")
    say()
    if bad:
        pnl = sum(float(b.get("pnl_pct") or 0) for b in bad)
        near = sum(1 for b in bad
                   if abs(float(b.get("pnl_pct") or 0) - b["_gap"]) < 1.5)
        say(f"Суммарный записанный pnl нефилуемых входов: **{pnl:+.1f}%**")
        say(f"У **{near} из {len(bad)}** записанный pnl совпадает с разрывом вход↔рынок "
            "в пределах 1.5 п.п. — то есть «прибыль» и ЕСТЬ этот разрыв, а не ход цены.")
        say()
        say("| символ | напр. | зона входа | рынок при регистрации | разрыв | pnl | причина |")
        say("|---|---|---|---|---|---|---|")
        for b in sorted(bad, key=lambda x: -x["_gap"])[:20]:
            say(f"| {b['symbol']} | {b['direction']} | {b.get('entry_lo'):g}–{b.get('entry_hi'):g} "
                f"| {b['_market']:g} | **{b['_gap']:+.1f}%** | {float(b.get('pnl_pct') or 0):+.2f}% "
                f"| {b.get('close_reason')} |")

    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Исполнимость входа — 2026-07-27\n\n" + "\n".join(out) + "\n", encoding="utf-8"
        )
        print(f"\nотчёт: {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
