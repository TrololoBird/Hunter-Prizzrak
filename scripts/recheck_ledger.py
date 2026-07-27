"""Пересчёт леджера: какие записанные победы РЫНОК НЕ ПОДТВЕРЖДАЕТ.

Зачем. Трекер закрывал позицию по сдвинутому стопу, сверяя его с минимумом, накопленным ЗА ВСЮ
жизнь сделки — то есть из времени, когда этого стопа ещё не существовало. Выход книжился по
цене стопа, которая для лонга стоит ВЫШЕ рынка, и запись уходила в победы. Дефект исправлен
(`_evaluate_levels.py::_bar_extremes` + `_trailing.py::reset_stop_window`), но история осталась.

Что делает. Для каждой закрытой сделки, чей стоп был сдвинут В ПРИБЫЛЬ (для лонга — выше входа,
для шорта — ниже; исходный стоп по построению стоит с другой стороны, поэтому признак надёжен),
поднимает НАСТОЯЩИЕ 15-минутные свечи за время жизни сделки и спрашивает ровно одно:

    торговался ли рынок по цене выхода, по которой сделка записана?

Лонг закрыт по 104.5, а минимум за всё время жизни — 105.2? Значит по 104.5 не отдали бы:
запись невозможна. Это не оценка «а как было бы», это опровержение факта.

⚠ Чего скрипт НЕ делает и не может. Он НЕ восстанавливает истинный исход: после ложного
закрытия сделку перестали вести, и путь трейлинга неизвестен. Опровергнутая запись помечается
`НЕ ПОДТВЕРЖДЕНА`, а не переписывается новой цифрой — придумать исход было бы той же ошибкой,
только с другой стороны.

Также проверяется, был ли достигнут TP — если цель взята раньше, чем стоп стал достижим,
запись хотя бы качественно верна, даже если цена выхода завышена.

Запуск:
    uv run python scripts/recheck_ledger.py
    uv run python scripts/recheck_ledger.py --write   # записать отчёт в docs/audit/
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import pathlib

import ccxt.pro as ccxtpro

HISTORY = pathlib.Path("data/signal_history.jsonl")
REPORT = pathlib.Path("docs/audit/ledger-recheck-2026-07-27.md")
_STOP_REASONS = {"stop_hit", "stop_loss", "trailing_stop_profit"}
_TF = "15m"


def _parse_ts(value: object) -> dt.datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return dt.datetime.fromtimestamp(value / 1000 if value > 1e11 else value, dt.UTC)
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _entry_mid(row: dict) -> float | None:
    lo, hi = _num(row.get("entry_lo")), _num(row.get("entry_hi"))
    return (lo + hi) / 2 if lo and hi else None


def affected_rows() -> list[dict]:
    """Записи, закрытые по стопу, который был сдвинут В ПРИБЫЛЬ."""
    out: list[dict] = []
    for line in HISTORY.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(r.get("close_reason") or "") not in _STOP_REASONS:
            continue
        entry, stop, d = _entry_mid(r), _num(r.get("stop_loss")), r.get("direction")
        if not entry or not stop:
            continue
        if (d == "long" and stop > entry) or (d == "short" and stop < entry):
            out.append(r)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="записать отчёт в docs/audit/")
    args = ap.parse_args()

    rows = affected_rows()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    verdicts: collections.Counter[str] = collections.Counter()
    lines: list[str] = []
    pnl_refuted = 0.0
    try:
        for r in rows:
            sym, d = str(r.get("symbol") or ""), str(r.get("direction") or "")
            op, cl = _parse_ts(r.get("opened_at")), _parse_ts(r.get("closed_at"))
            exit_px, tp1 = _num(r.get("exit_price")), _num(r.get("tp1"))
            pnl = float(r.get("pnl_pct") or 0.0)
            if not (sym and op and cl and exit_px):
                verdicts["нет данных для проверки"] += 1
                continue
            unified = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym
            if unified not in ex.markets:
                verdicts["символ делистнут"] += 1
                continue
            since = int(op.timestamp() * 1000)
            span_min = max(1.0, (cl - op).total_seconds() / 60.0)
            # ТФ выбирается под длительность сделки. Первый прогон шёл только на 15m и не смог
            # проверить 31 запись из 65 — короткие сделки просто не набирали двух свечей, а
            # «не проверено» неотличимо от «в порядке». Минутки закрывают этот провал.
            tf, tf_min = (_TF, 15.0) if span_min > 240.0 else ("1m", 1.0)
            limit = min(1000, int(span_min / tf_min) + 4)
            try:
                bars = await ex.fetch_ohlcv(unified, tf, since=since, limit=limit)
            except Exception:  # noqa: BLE001 — недоступная история не приговор записи
                verdicts["история недоступна"] += 1
                continue
            bars = [b for b in bars if op.timestamp() * 1000 <= b[0] <= cl.timestamp() * 1000]
            if len(bars) < 2:
                verdicts["мало свечей"] += 1
                continue
            lo_all = min(float(b[3]) for b in bars)
            hi_all = max(float(b[2]) for b in bars)
            reachable = lo_all <= exit_px if d == "long" else hi_all >= exit_px
            tp_hit = bool(tp1 and (hi_all >= tp1 if d == "long" else lo_all <= tp1))
            if reachable:
                verdicts["подтверждена"] += 1
                continue
            verdicts["НЕ ПОДТВЕРЖДЕНА"] += 1
            pnl_refuted += pnl
            gap = (
                (lo_all - exit_px) / exit_px * 100 if d == "long"
                else (exit_px - hi_all) / exit_px * 100
            )
            lines.append(
                f"| {sym} | {d} | {exit_px:g} | {lo_all if d == 'long' else hi_all:g} | "
                f"{gap:+.2f}% | {pnl:+.2f}% | {'да' if tp_hit else 'НЕТ'} |"
            )
    finally:
        await ex.close()

    print(f"проверено записей: {len(rows)}")
    for k, v in verdicts.most_common():
        print(f"  {k:24s} {v}")
    print(f"\nсуммарный pnl опровергнутых записей: {pnl_refuted:+.1f}%")
    if lines:
        print(f"\n{'символ':12s} — цена выхода против крайней достигнутой (первые 10):")
        for line in lines[:10]:
            print("   ", line.strip("| ").replace(" | ", "  "))
    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Пересчёт леджера — 2026-07-27\n\n"
            f"Проверено записей, закрытых по сдвинутому в прибыль стопу: **{len(rows)}**.\n\n"
            + "\n".join(f"* {k}: {v}" for k, v in verdicts.most_common())
            + f"\n\nСуммарный pnl опровергнутых: **{pnl_refuted:+.1f}%**\n\n"
            "| символ | напр. | цена выхода | крайняя достигнутая | зазор | записанный pnl | TP взят |\n"
            "|---|---|---|---|---|---|---|\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        print(f"\nотчёт: {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
