"""Независимая проверка геометрии карты зон на ЖИВЫХ данных.

Инструмент вместо синтетического теста. На активно рефакторящемся проекте тест фиксирует
поведение, а не корректность (`CLAUDE.md`: source-of-truth ставит реальные данные выше кода и
тестов), поэтому проверка устроена иначе: скрипт НЕ доверяет модулю — сам тянет сырые OHLCV
через CCXT, сам строит объёмный профиль и сверяет выводы модуля со своим анализом тех же данных.

Что проверяется:
* структура — порядок кромок, ПОК и вход внутри зоны, стороны относительно цены;
* цели — монотонность и правильная сторона ОТ СВОЕЙ ЗОНЫ (не от текущей цены: зона может лежать
  глубоко под рынком, и её цель законно окажется ниже текущих — на этом я сам сначала ошибся);
* коридор — арифметика ширины и что кромки действительно охватывают цену;
* заземление — каждая кромка обязана быть реально торгованной ценой в окне своего ТФ;
* ПОК — сверка с независимо посчитанным профилем.

Расхождение ПОК НЕ означает дефект автоматически: модуль натягивает профиль на бары СТРУКТУРЫ
(`poc._structure_bars`, курс с.26), а не на всё окно, и на бимодальной зоне выбор пика висит на
разбиении корзин — измерено на LTC 4h 40.78–45.59, где два пика несут 12.9% и 12.4% объёма и
якорь сдвигается на 5.7%. Смотреть как на сигнал к ручному разбору, а не как на приговор.

Запуск:
    uv run python scripts/verify_zone_geometry.py "BTC/USDT:USDT" "ETH/USDT:USDT" ...
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import ccxt.async_support as ccxt

from hunt_core.engine.params import OHLCV_LIMIT

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.toolkit.ohlcv import ccxt_ohlcv_to_frame
from hunt_core.engine.spot import SpotEngine
from hunt_core.prizrak.setups import build_symbol_setups
from hunt_core.runtime.native_producers import spot_weekly_ladder_native

CFG = PrizrakConfig.load()
TFS = ("5m", "15m", "1h", "4h", "1d", "1w")
FAIL: list[str] = []
WARN: list[str] = []
# Незагруженные пары символ/ТФ — печатаются рядом с вердиктом (см. хвост main).
SKIPPED: list[str] = []


def bad(sym: str, msg: str) -> None:
    FAIL.append(f"{sym}: {msg}")


def warn(sym: str, msg: str) -> None:
    """Не нарушение инварианта, а расхождение, которое верификатор ДОКАЗАННО не может решить сам.

    Свой ПОК считается по опубликованной полосе, а модуль строит профиль по ИСХОДНОМУ боксу и лишь
    потом сужает его до value area — исходные кромки наружу не отдаются. То есть два числа считаются
    по разным наборам баров by design, и записывать разницу в ❌ значит топить настоящие дефекты в
    шуме (18 «нарушений», из которых 17 были этим). Смотреть глазами, не считать приговором.
    """
    WARN.append(f"{sym}: {msg}")


def _zones(hz: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = hz.get(key)
    if isinstance(raw, list):
        return [z for z in raw if isinstance(z, dict)]
    return [raw] if isinstance(raw, dict) else []


def my_poc(bars: list[list[float]], lo: float, hi: float, bins: int = 60) -> float | None:
    """Свой объёмный профиль в полосе [lo,hi]: цена корзины с максимальным объёмом."""
    if hi <= lo:
        return None
    w = (hi - lo) / bins
    buckets = [0.0] * bins
    for b in bars:
        h, low, v = float(b[2]), float(b[3]), float(b[5])
        if h < lo or low > hi or h <= low:
            continue
        a = max(lo, low)
        z = min(hi, h)
        if z <= a:
            continue
        i0 = min(bins - 1, max(0, int((a - lo) / w)))
        i1 = min(bins - 1, max(0, int((z - lo) / w)))
        share = v * ((z - a) / (h - low)) / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            buckets[i] += share
    if not any(buckets):
        return None
    top = max(range(bins), key=lambda i: buckets[i])
    return lo + (top + 0.5) * w


def my_structure(bars: list[list[float]], lo: float, hi: float, *, gap: int = 2,
                 min_bars: int = 5) -> list[list[float]]:
    """Свои бары структуры: самая объёмная НЕПРЕРЫВНАЯ серия свечей, торгующих в [lo,hi].

    Сверять модульный ПОК с профилем ВСЕГО окна значило бы сравнивать разные величины — и до
    правки `poc._structure_bars` они «сходились» именно потому, что модуль тоже считал по окну
    (19% зон с ПОК, 2 расхождения). Реализация здесь СВОЯ, а не импорт из модуля: смысл
    верификатора в том, чтобы прийти к тому же числу другим кодом.
    """
    inside = [i for i, b in enumerate(bars) if float(b[3]) <= hi and float(b[2]) >= lo]
    if not inside:
        return bars
    runs: list[tuple[int, int]] = []
    s = p = inside[0]
    for i in inside[1:]:
        if i - p <= gap + 1:
            p = i
            continue
        runs.append((s, p))
        s = p = i
    runs.append((s, p))
    runs = [r for r in runs if r[1] - r[0] + 1 >= min_bars]
    if not runs:
        return bars
    a, b = max(runs, key=lambda r: (sum(float(x[5]) for x in bars[r[0]:r[1] + 1]), r[1] - r[0]))
    return bars[a:b + 1]


def check(sym: str, setups: dict[str, Any], price: float, raw: dict[str, list[list[float]]]) -> dict[str, int]:
    stat = {"zones": 0, "checked": 0, "poc": 0}
    horizons = setups.get("horizons") or {}
    for hname, hz in horizons.items():
        if not isinstance(hz, dict):
            continue
        tf = hz.get("tf")
        bars = raw.get(str(tf)) or []
        for key in ("perezakup", "dobor", "short"):
            for z in _zones(hz, key):
                stat["zones"] += 1
                lo, hi = float(z["lo"]), float(z["hi"])
                # --- структура ---
                # lo == hi ЗАКОННО: это линейный уровень — потолок/пол straddle-бокса, чей
                # граничный кластер собран на одной цене (ext_hi == hi). Встречается часто — у 7
                # из 17 проверенных символов, — и все потребители его держат: poc.py защищён
                # `hi > lo`, форматтер печатает одну цену, zone_watch принимает. Ловим только lo > hi.
                if lo > hi:
                    bad(sym, f"{hname}/{key}: lo>hi ({lo}>{hi})")
                poc = z.get("poc")
                # Доля зон, у которых ПОК ЕСТЬ, — метрика якорения. Без неё «ПОК считается»
                # неотличимо от «ПОК всегда None»: измерено на BTC 4h, где ПОК выпадал у ВСЕХ
                # зон разом, а инвариантных нарушений при этом не было ни одного (poc.py
                # _structure_bars получал огибающую касаний вместо структуры).
                stat["poc"] += int(poc is not None)
                if poc is not None and not (lo <= float(poc) <= hi):
                    bad(sym, f"{hname}/{key}: ПОК {poc} вне [{lo},{hi}]")
                ent = z.get("entry")
                if ent is not None and not (lo <= float(ent) <= hi):
                    bad(sym, f"{hname}/{key}: вход {ent} вне [{lo},{hi}]")
                # --- сторона относительно цены ---
                if key in ("perezakup", "dobor") and lo > price:
                    bad(sym, f"{hname}/{key}: лонг-зона ВЫШЕ цены (lo={lo} > {price})")
                if key == "short" and hi < price:
                    bad(sym, f"{hname}/{key}: шорт-зона НИЖЕ цены (hi={hi} < {price})")
                # --- заземление: кромки реально торговались ---  # noqa: E501
                if bars:
                    stat["checked"] += 1
                    lows = [float(b[3]) for b in bars]
                    highs = [float(b[2]) for b in bars]
                    if not (min(lows) <= lo <= max(highs)):
                        bad(sym, f"{hname}/{key}: lo={lo} вне торгованного диапазона {tf}")
                    if not (min(lows) <= hi <= max(highs)):
                        bad(sym, f"{hname}/{key}: hi={hi} вне торгованного диапазона {tf}")
                    # свой ПОК — сверка с модульным
                    if poc is not None:
                        mine = my_poc(my_structure(bars, lo, hi), lo, hi)
                        if mine is not None:
                            span = hi - lo
                            if span > 0 and abs(mine - float(poc)) / span > 0.34:
                                warn(sym, f"{hname}/{key}: ПОК {float(poc):.8g} vs мой {mine:.8g} "
                                         f"(расхождение {abs(mine-float(poc))/span*100:.0f}% ширины)")
        # --- цели ---
        lt = [float(x) for x in (hz.get("long_targets") or [])]
        if lt != sorted(lt):
            bad(sym, f"{hname}: long_targets не возрастают: {lt}")
        # Цель лонга меряется от ВЕРХА своей зоны, а не от текущей цены: зона может лежать глубоко
        # под рынком, и её цель законно окажется ниже текущих. Равенство верху зоны тоже законно —
        # это «первая частичная фиксация на верхе базы» (вход берётся на ПОК внутри зоны, ниже).
        lz = _zones(hz, "perezakup") + _zones(hz, "dobor")
        if lt and lz:
            top = max(float(z["hi"]) for z in lz)
            off = [t for t in lt if t < top]
            if off:
                bad(sym, f"{hname}: long_target не выше верха лонг-зоны {top}: {off}")
        st = [float(x) for x in (hz.get("short_targets") or [])]
        if st != sorted(st, reverse=True):
            bad(sym, f"{hname}: short_targets не убывают: {st}")
        sz = _zones(hz, "short")
        if st and sz:
            bot = min(float(z["lo"]) for z in sz)
            off = [t for t in st if t > bot]
            if off:
                bad(sym, f"{hname}: short_target не ниже низа шорт-зоны {bot}: {off}")
    # --- коридор ---
    hr = setups.get("headroom")
    if isinstance(hr, dict):
        up, dn, w = hr.get("up_price"), hr.get("down_price"), hr.get("width_pct")
        if up is not None and float(up) <= price:
            bad(sym, f"коридор: up_price {up} НЕ выше цены {price}")
        if dn is not None and float(dn) >= price:
            bad(sym, f"коридор: down_price {dn} НЕ ниже цены {price}")
        if w is not None and up is not None and dn is not None:
            exp = (float(up) / float(dn) - 1.0) * 100.0
            if abs(exp - float(w)) > 0.02:
                bad(sym, f"коридор: width {w} != расчётной {exp:.2f}")
    return stat


async def main(symbols: list[str]) -> None:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    spot_eng = SpotEngine([])
    await spot_eng._ex.load_markets()
    tot = {"zones": 0, "checked": 0, "poc": 0}
    try:
        await ex.load_markets()
        now = ex.milliseconds()
        for sym in symbols:
            raw: dict[str, list[list[float]]] = {}
            for tf in TFS:
                try:
                    # Продовая глубина, а не 500: движок обрезает кадр ровно до OHLCV_LIMIT,
                    # и у одной и той же правки на 500 и на 1000 бывает РАЗНЫЙ ЗНАК (замер
                    # 2026-07-27, см. `setups._horizon_zones`). Верификатор, меряющий на
                    # кадрах мельче боевых, сертифицирует не тот объект.
                    o = await ex.fetch_ohlcv(sym, tf, limit=OHLCV_LIMIT)
                except Exception as exc:  # noqa: BLE001 — недоступный ТФ не приговор прогону
                    # Пропуск обязан дойти до итога: «нарушений инвариантов не найдено»
                    # на неполном покрытии — утверждение шире сделанного.
                    SKIPPED.append(f"{sym}/{tf}: {exc.__class__.__name__}")
                    continue
                step = ex.parse_timeframe(tf) * 1000
                closed = [b for b in o if int(b[0]) + step <= now]  # I-5: только закрытые
                if len(closed) < len(o):
                    pass  # форминг-бар отброшен нами; модуль обязан делать то же
                raw[tf] = closed
            if "4h" not in raw or not raw["4h"]:
                print(f"{sym:16s} НЕТ ДАННЫХ")
                continue
            price = float(raw["4h"][-1][4])
            s = build_symbol_setups(raw, price=price, cfg=CFG, structure=None)
            st = check(sym, s, price, raw)
            tot["zones"] += st["zones"]
            tot["checked"] += st["checked"]
            tot["poc"] += st["poc"]
            # Источник макро-лестницы — проверяется здесь, а не тестом: Binance листит золото и
            # серебро как СВОИ токенизированные перпы без спот-пары, поэтому XAU обязан
            # резолвиться на PAXG (то же золото, 309 недель против 33), а XAG — падать на бары
            # собственного контракта. Синтетика это подтвердить не может: вопрос ровно в том,
            # какие рынки биржа листит СЕЙЧАС.
            # contract_weekly передаётся ИМЕННО как в проде (native_assembly.py): без него
            # символ без спот-пары (XAG) выглядит потерявшим горизонт, хотя фолбэк на бары
            # собственного контракта работает. Верификатор обязан зеркалить прод, иначе ловит
            # себя, а не модуль — на этом я тут уже ошибся.
            w1 = ccxt_ohlcv_to_frame(raw.get("1w") or [], "1w", exchange=ex)
            lad = await spot_weekly_ladder_native(
                sym, price=price, spot=spot_eng, contract_weekly=w1
            )
            if lad is None:
                bad(sym, "макро-лестница отсутствует — символ теряет спот-горизонт целиком")
                src = "—"
            else:
                src = str(lad.get("source") or "?")
                if not (lad.get("below") or lad.get("above")):
                    bad(sym, f"лестница пуста при источнике {src}")

            hzs = ",".join(sorted((s.get("horizons") or {}).keys())) or "—"
            hr = s.get("headroom") or {}
            wid = f"{hr.get('width_pct')}%" if hr.get("width_pct") is not None else "—"
            print(f"{sym:16s} price={price:<12.8g} горизонты=[{hzs:24s}] зон={st['zones']:2d} коридор={wid:6s} лестница={src}")
    finally:
        await ex.close()
        await spot_eng.close()
    print(f"\nвсего зон: {tot['zones']}, из них заземлено на свечи: {tot['checked']}")
    if tot["zones"]:
        print(f"ЯКОРЬ ПОК: {tot['poc']}/{tot['zones']} зон = "
              f"{tot['poc'] / tot['zones'] * 100:.0f}%  (остальные входят по кромке)")
    if SKIPPED:
        print(f"\nПОКРЫТИЕ НЕПОЛНОЕ — не загружено пар символ/ТФ: {len(SKIPPED)}")
        for s in SKIPPED[:20]:
            print("   ", s)
        if len(SKIPPED) > 20:
            print(f"    … и ещё {len(SKIPPED) - 20}")
    if WARN:
        print(f"\n⚠️  К РУЧНОМУ РАЗБОРУ (не нарушения): {len(WARN)}")
        for w in WARN:
            print("   ", w)
    if FAIL:
        print(f"\n❌ НАРУШЕНИЙ: {len(FAIL)}")
        for f in FAIL:
            print("   ", f)
    elif SKIPPED:
        print("\nнарушений не найдено НА ЗАГРУЖЕННОЙ ЧАСТИ — см. пропуски выше")
    else:
        print("\n✅ нарушений инвариантов не найдено")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
