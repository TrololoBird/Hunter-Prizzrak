"""Достижимость ОБЕИХ ног сделки на живых свечах → артефакт для метрик.

Заменяет `verify_entry_fill.py`, который проверял только ВХОД. Вопрос один и тот же для двух
ног, и отвечать на него надо разом: **торговал ли рынок по цене, которая записана**.

* вход — цена обязана побывать в полосе `[entry_lo, entry_hi]` за окно до регистрации;
* выход — рынок обязан достать записанный `exit_price` за время жизни сделки.

Зачем артефакт, а не печать. Аудит показал: **103 сделки из 280 несут 101.1% чистого R**, имея
хотя бы одну ногу, которой рынок не печатал. На подвыборке, где ОБЕ ноги реальны, среднее
уходит в **−0.0278R** с интервалом, накрывающим ноль. Пока вердикт живёт только в отчёте,
метрики его не видят и складывают настоящие сделки с ненастоящими.

⚠ Вердикт кладётся в ОТДЕЛЬНЫЙ файл, а не в леджер. В леджере уже лежат осиротевшие
`market_confirmed`/`recheck_ts` — 65 строк, у которых нет НИ ОДНОГО читателя во всём дереве:
кто-то посчитал вердикт, записал его в данные и не подключил. Повторять это нельзя. Данные —
наблюдения биржи; вердикт — производная, и живёт рядом, а не внутри.

Запуск:
    uv run python scripts/verify_trade_legs.py            # печать + запись артефакта
    uv run python scripts/verify_trade_legs.py --limit 40 # быстрый прогон
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
ARTIFACT = pathlib.Path("data/leg_reachability.json")
_BAR_MS = 60_000
_ENTRY_LOOKBACK_MIN = 60


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


def leg_key(row: dict) -> str:
    """Ключ вердикта: символ + момент открытия. Тот же, что у `outcome_archive_key`."""
    return f"{str(row.get('symbol') or '').upper()}|{row.get('opened_at')}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = [json.loads(ln) for ln in HISTORY.open(encoding="utf-8") if ln.strip()]
    rows = [r for r in rows if not is_polluted(r) and r.get("close_reason")]
    if args.limit:
        rows = rows[: args.limit]

    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    verdicts: dict[str, dict] = {}
    stat: collections.Counter[str] = collections.Counter()
    try:
        for r in rows:
            sym = str(r.get("symbol") or "")
            direction = str(r.get("direction") or "")
            opened, closed = _ts(r.get("opened_at")), _ts(r.get("closed_at"))
            lo, hi = _num(r.get("entry_lo")), _num(r.get("entry_hi"))
            exit_px = _num(r.get("exit_price"))
            unified = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym
            if not (sym and opened and closed and lo and hi) or unified not in ex.markets:
                stat["не проверено"] += 1
                continue
            span_min = max(1.0, (closed - opened).total_seconds() / 60.0)
            tf, tf_min = ("15m", 15.0) if span_min > 600.0 else ("1m", 1.0)
            span_ms = tf_min * 60_000.0
            since = int(
                (opened.timestamp() * 1000 - _ENTRY_LOOKBACK_MIN * _BAR_MS) // span_ms * span_ms
            )
            try:
                bars = await ex.fetch_ohlcv(
                    unified, tf, since=int(since),
                    limit=min(1000, int((span_min + _ENTRY_LOOKBACK_MIN) / tf_min) + 4),
                )
            except Exception:  # noqa: BLE001 — недоступная история не приговор записи
                stat["история недоступна"] += 1
                continue
            if len(bars) < 3:
                stat["мало свечей"] += 1
                continue
            open_ms, close_ms = opened.timestamp() * 1000, closed.timestamp() * 1000
            # Свеча, СОДЕРЖАЩАЯ момент, включается: её метка — время открытия бара, он начался
            # раньше события. Без этого проверка односторонне фабрикует «недостижимо».
            pre = [b for b in bars if b[0] + span_ms > open_ms - _ENTRY_LOOKBACK_MIN * _BAR_MS
                   and b[0] <= open_ms]
            life = [b for b in bars if b[0] + span_ms > open_ms and b[0] <= close_ms]
            entry_ok = any(float(b[3]) <= hi and float(b[2]) >= lo for b in pre) if pre else None
            if exit_px is None or not life:
                exit_ok = None
            elif direction == "long":
                exit_ok = min(float(b[3]) for b in life) <= exit_px
            else:
                exit_ok = max(float(b[2]) for b in life) >= exit_px
            both = (entry_ok is True) and (exit_ok is True)
            verdicts[leg_key(r)] = {
                "entry_reachable": entry_ok,
                "exit_reachable": exit_ok,
                "both_reachable": both,
                "tf": tf,
            }
            stat["обе ноги реальны" if both else "хотя бы одна нога недостижима"] += 1
    finally:
        await ex.close()

    print(f"проверено записей: {len(verdicts)} из {len(rows)}")
    for k, v in stat.most_common():
        print(f"  {k}: {v}")
    ARTIFACT.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nартефакт: {ARTIFACT} ({len(verdicts)} вердиктов)")


if __name__ == "__main__":
    asyncio.run(main())
