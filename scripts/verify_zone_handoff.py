"""Проверка гейта R:R в передаче зоны трекеру — на живых зонах.

Гейт добавлен после живого дефекта SOL 2026-07-25 (вотчер завёл сделку с входом шириной 7.26% и
R:R по худшему заливу 1:0.17), но в бою ни разу не срабатывал: ``skipped_rr = 0`` за прогон, потому
что вход в зону — событие редкое. «Не сработал» и «работает» — разные вещи, поэтому здесь гейт
проверяется не ожиданием события, а прогоном НАСТОЯЩЕЙ логики на зонах, которые модуль строит
прямо сейчас по всем переданным символам.

Для каждой актуальной зоны считается то же, что посчитал бы ``_handoff``: стоп за структуру
(стр.33), полоса ТВХ от ПОК (стр.30) и R:R по ХУДШЕМУ заливу — и сверяется с ``cfg.min_rr``.
Отчёт показывает, сколько зон гейт пропустил бы, сколько отклонил, и какова геометрия у худших.

Что это НЕ доказывает: что гейт вызывается на живом пути (это видно только по логу
``zone_watch_handoff`` / ``zone_watch_handoff_skipped_rr``). Здесь проверяется, что при живых
числах он выносит осмысленный вердикт, а не пропускает всё подряд и не режет всё подряд.

Запуск:
    uv run python scripts/verify_zone_handoff.py "BTC/USDT:USDT" "SOL/USDT:USDT" ...
"""
from __future__ import annotations

import asyncio
import sys

import ccxt.async_support as ccxt

from _verify_common import report_skipped, verdict_scope

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.setups import build_symbol_setups
from hunt_core.prizrak.zone_watch import (
    _actionable_zones,
    _entry_band,
    _rr_worst_fill,
    _stop_for,
)

CFG = PrizrakConfig.load()
TFS = ("5m", "15m", "1h", "4h", "1d", "1w")
# Незагруженные пары символ/ТФ — печатаются рядом с вердиктом (см. хвост main).
SKIPPED: list[str] = []


async def main(symbols: list[str]) -> None:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    passed: list[tuple[str, str, float, float]] = []
    blocked: list[tuple[str, str, float | None, float]] = []
    no_geo = 0
    try:
        await ex.load_markets()
        now = ex.milliseconds()
        for sym in symbols:
            raw: dict[str, list[list[float]]] = {}
            for tf in TFS:
                try:
                    o = await ex.fetch_ohlcv(sym, tf, limit=500)
                except Exception as exc:  # noqa: BLE001 — недоступный ТФ не приговор прогону
                    # Пропуск обязан дойти до итога: вердикт «гейт выносит осмысленный
                    # вердикт» на половине данных — утверждение шире сделанного.
                    SKIPPED.append(f"{sym}/{tf}: {exc.__class__.__name__}")
                    continue
                step = ex.parse_timeframe(tf) * 1000
                raw[tf] = [b for b in o if int(b[0]) + step <= now]  # I-5
            if not raw.get("4h"):
                continue
            price = float(raw["4h"][-1][4])
            setups = build_symbol_setups(raw, price=price, cfg=CFG, structure=None)
            for z in _actionable_zones(setups):
                stop = _stop_for(
                    z["lo"], z["hi"], buffer_frac=float(CFG.stop_buffer_pct),
                    direction=z["direction"],
                )
                lo, hi = _entry_band(z)
                tps = list(z.get("targets") or [])
                rr = _rr_worst_fill(
                    direction=z["direction"], entry_lo=lo, entry_hi=hi,
                    stop=stop, tp1=tps[0] if tps else None,
                )
                width = (hi / lo - 1.0) * 100.0 if lo > 0 else 0.0
                label = f"{z['kind']}/{z['direction']}"
                if rr is None:
                    no_geo += 1
                    blocked.append((sym, label, None, width))
                elif rr < float(CFG.min_rr):
                    blocked.append((sym, label, rr, width))
                else:
                    passed.append((sym, label, rr, width))
    finally:
        await ex.close()

    total = len(passed) + len(blocked)
    if not total:
        print("зон нет — проверять нечего")
        return
    print(f"живых зон: {total}  ·  пол R:R = {CFG.min_rr}")
    print(f"  пропущено гейтом : {len(passed):3d} ({len(passed) / total * 100:.0f}%)")
    print(f"  отклонено        : {len(blocked):3d} ({len(blocked) / total * 100:.0f}%)"
          f"  из них без целей/геометрии: {no_geo}")
    if passed:
        w = sorted(passed, key=lambda t: t[2])[:5]
        print("\n  прошедшие с наименьшим R:R (обязаны быть >= пола):")
        for s, lab, rr, width in w:
            print(f"    {s:16s} {lab:18s} R:R {rr:6.2f}  полоса входа {width:5.2f}%")
    rej = [b for b in blocked if b[2] is not None]
    if rej:
        print("\n  отклонённые с наибольшим R:R (обязаны быть < пола):")
        for s, lab, rr, width in sorted(rej, key=lambda t: -(t[2] or 0))[:5]:
            print(f"    {s:16s} {lab:18s} R:R {rr:6.2f}  полоса входа {width:5.2f}%")

    problems = [f"{s} {lab}: R:R {rr} прошёл при поле {CFG.min_rr}"
                for s, lab, rr, _w in passed if rr < float(CFG.min_rr)]
    report_skipped(SKIPPED)
    if problems:
        print(f"\n❌ ГЕЙТ ПРОПУСКАЕТ НИЖЕ ПОЛА: {len(problems)}")
        for p in problems:
            print("   ", p)
    elif not passed:
        print("\n⚠️  гейт отклонил ВСЕ зоны — проверить, не зарезан ли путь целиком")
    else:
        print(f"\n✅ гейт выносит осмысленный вердикт: всё прошедшее выше пола"
              f"{verdict_scope(SKIPPED)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]))
