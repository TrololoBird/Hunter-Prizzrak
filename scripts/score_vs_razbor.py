"""Счёт соответствия карты зон опубликованным разборам автора.

Зачем. Расхождения с его разборами до сих пор находились ПОШТУЧНО и случайно: разбор ASTR вскрыл
отсутствие внутридневного горизонта, и тот же дефект задним числом объяснил промах по SAND из
разбора десятью днями раньше. «До этого всё работало» означало «этот слой никто не задел» —
отсутствующий горизонт неотличим от пустого. Это структурная слепота, и без счёта она повторится.

Что делает. Берёт эталон — его опубликованные уровни, сверенные по свечам в `*.razbor.md`, —
отматывает OHLCV на дату разбора (только ЗАКРЫТЫЕ бары, I-5: никакого загляда вперёд), гоняет
`build_symbol_setups` и считает три величины:

* **recall** — какую долю его уровней модуль воспроизводит (уровень засчитан, если попадает внутрь
  какой-то зоны или в пределах ``--tol`` от её кромки/ПОК);
* **промахи** — какие именно уровни потеряны, с указанием ближайшей нашей зоны;
* **избыток** — сколько зон мы печатаем сверх его разметки (он публикует 2–4, модуль 6–11).

Зачем именно так. Recall и избыток тянут в разные стороны: расширить окна и допуски → recall
растёт, но карточка превращается в шум. Одна цифра тут врала бы; поэтому их две.

Что это НЕ доказывает: совпадение с разбором — не доходность. Автор сам отказывается от сделок на
отличных уровнях («процент движения между уровнями слишком небольшой», ASTR 2026-07-25), так что
высокий recall означает «мы видим то же, что он», а не «мы заработаем».

Запуск:
    uv run python scripts/score_vs_razbor.py                 # весь эталон
    uv run python scripts/score_vs_razbor.py --only ASTR      # один символ
    uv run python scripts/score_vs_razbor.py --tol 0.5        # строже допуск
"""
from __future__ import annotations

import argparse
import random
import asyncio
from datetime import UTC, datetime
from typing import Any

import ccxt.async_support as ccxt

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.engine.spot import SpotEngine
from hunt_core.prizrak.orchestrator import _INTEREST_ZONE_MAX_WIDTH_PCT
from hunt_core.prizrak.setups import build_symbol_setups
from hunt_core.runtime.native_producers import spot_weekly_ladder_native

CFG = PrizrakConfig.load()
SIGNS: list[tuple[str, float]] = []
QUALITY: dict[str, int] = {}
TFS = ("5m", "15m", "1h", "4h", "1d", "1w")

# Эталон: уровни, снятые С ГРАФИКОВ автора и сверенные по свечам в соответствующем .razbor.md.
# Это не «примерно оттуда» — каждое число прочитано с его разметки (кромка бокса, подпись оси,
# линия с 🔫/💰) и проверено на реальных OHLCV. Источник указан, чтобы правку можно было оспорить.
BASELINE: list[dict[str, Any]] = [
    {
        "symbol": "ASTR/USDT:USDT", "date": "2026-07-25", "src": "prizrak_astr_razbor",
        "levels": [
            (0.005059, "поддержка 4ч"), (0.005165, "сопротивление"), (0.005177, "сопротивление"),
            # ⚠ Центр BUY исправлен 0.004826 → 0.004836. Опечатка оцифровки (переставлены
            # цифры): и таблица уровней в самом `prizrak_astr_razbor.razbor.md`, и подпись оси
            # на его графике ASTR 1ч от 2026-07-25 19:10 UTC+3 дают 0.004836. Прежнее число
            # взято из устной реплики на 2:32, где он назвал 0.004826.
            (0.004855, "верх BUY"), (0.004836, "центр BUY"), (0.004797, "низ BUY"),
        ],
    },
    {
        "symbol": "UNI/USDT:USDT", "date": "2026-07-25", "src": "prizrak_alts_10_overview",
        "levels": [
            (2.940, "верх добора"), (2.787, "низ добора"),
            (2.515, "верх глубокой ✔"), (2.350, "низ глубокой ✔"), (3.929, "сопротивление"),
        ],
    },
    {
        "symbol": "SAND/USDT:USDT", "date": "2026-07-25", "src": "prizrak_alts_10_overview",
        "levels": [(0.0460, "верх зелёной"), (0.0440, "низ зелёной"), (0.0320, "верх глубокой"),
                   (0.0294, "низ глубокой")],
    },
    {
        "symbol": "ANKR/USDT:USDT", "date": "2026-07-25", "src": "prizrak_alts_10_overview",
        "levels": [(0.00348, "верх жёлтой"), (0.00300, "низ жёлтой"), (0.00250, "верх зелёной"),
                   (0.00222, "низ зелёной"), (0.00462, "сопротивление"), (0.00507, "сопротивление")],
    },
    {
        "symbol": "ARPA/USDT:USDT", "date": "2026-07-25", "src": "prizrak_alts_10_overview",
        "levels": [(0.00797, "верх базы"), (0.00787, "низ базы"), (0.00716, "верх зелёной"),
                   (0.00640, "низ зелёной"), (0.01050, "отработанное сопр.")],
    },
    # ── разборы прошлых дат: данные отматываются на день публикации (I-5) ──────────────────
    {
        "symbol": "BCH/USDT:USDT", "date": "2026-07-22", "src": "prizrak_bch_praktikum",
        "levels": [(205.0, "верх лонг-зоны"), (196.0, "ПОК базы"), (190.0, "низ лонг-зоны"),
                   (245.0, "верх шорт-зоны"), (236.0, "низ шорт-зоны"),
                   (225.0, "верх mid"), (215.0, "низ mid")],
    },
    {
        "symbol": "BTC/USDT:USDT", "date": "2026-07-22", "src": "prizrak_btc_eth_keyzone",
        "levels": [(62850.0, "ПОК / перезакуп"), (66850.0, "шорт-зона")],
    },
    # ── ВТОРОЙ АВТОР КАНАЛА: Overcarder ───────────────────────────────────────────────────
    #
    # У канала «Призрак» ДВА автора, и разметка Overcarder'а — такой же эталон. В корпусе его
    # не было вообще: `rg -i overcarder research/` не давал ни одного совпадения. Его стиль
    # отличается — цветные полосы (зелёная = закуп, жёлтая = промежуточная, красная = продажа)
    # вместо боксов с вытянутыми вправо линиями, — но объект тот же: уровень с подписью на оси.
    #
    # ⚠ Кейс `prizrak_btc_eth_keyzone` (BTC 2026-07-22, два уровня 62850 и 66850) снят С ЭТОГО
    # ЖЕ графика: оба числа на нём есть. То есть прежний recall 2/2 = 100% измерен по эталону,
    # урезанному до 2 уровней из 14 — цифра была верной и при этом ничего не значила.
    #
    # ⚠ Числа сняты С ПОДПИСЕЙ ОСИ на скриншотах (BTC 4ч и ETH 4ч, 2026-07-22 13:40/13:42 UTC),
    # а не из транскрипта. Подпись однозначна, но это ЧТЕНИЕ КАРТИНКИ: если какое-то значение
    # окажется неверным, оспаривать надо именно эту строку. Границы цветных полос НЕ
    # оцифрованы — на глаз они шире подписи, и выдумывать их точность нельзя (I-6).
    {
        "symbol": "BTC/USDT:USDT", "date": "2026-07-22", "src": "overcarder_btc_4h_chart",
        "levels": [
            (68450.0, "красная верх"), (67860.0, "красная"), (67130.0, "красная"),
            (66850.0, "красная низ"), (66310.0, "жёлтая"), (65500.0, "серая"),
            (64750.0, "жёлтая верх"), (64400.0, "жёлтая низ"), (63140.0, "жёлтая верх ✔"),
            (62850.0, "жёлтая низ ✔"), (62010.0, "зелёная верх ↑"), (61550.0, "серая"),
            (60800.0, "зелёная"), (59920.0, "зелёная низ"),
        ],
    },
    {
        "symbol": "ETH/USDT:USDT", "date": "2026-07-22", "src": "overcarder_eth_4h_chart",
        "levels": [
            (2060.0, "красная верх"), (2014.0, "красная"), (1987.0, "красная ↓"),
            (1964.5, "жёлтая"), (1924.0, "розовая низ"), (1899.0, "жёлтая"),
            (1868.5, "жёлтая"), (1817.0, "жёлтая"), (1804.0, "зелёная верх ↑"),
            (1785.0, "зелёная"), (1771.0, "зелёная ↑"), (1741.0, "жёлтая"),
            (1700.0, "зелёная низ"),
        ],
    },
    {
        # График BTCUSDT.P 1ч от 2026-07-25 09:54 UTC+3 (цена на нём 63 959,9): 13 подписей оси,
        # снятых с его разметки. Он даёт их КЛАСТЕРАМИ — каждый синий бокс несёт 2–4 линии, ровно
        # как курс велит дробить крупную базу на ордера (стр.30). Именно этот случай вскрыл, что
        # 1ч не был самостоятельным горизонтом: 6 из 13 совпали в 0.00% только после его добавления.
        "symbol": "BTC/USDT:USDT", "date": "2026-07-25", "src": "prizrak_btc_chart_1h",
        "levels": [
            (68590.2, "верхняя линия"), (65923.8, "бокс 22-23.07"), (65609.1, "бокс 23.07"),
            (64754.0, "бокс 19-20.07"), (63395.4, "бокс 17-18 верх"), (63190.8, "бокс 17-18"),
            (62837.3, "бокс 17-18"), (62590.1, "бокс 17-18 низ"), (62024.7, "BUY-зона"),
            (60507.2, "бокс 2-3.07"), (60173.3, "бокс 2-3.07"), (59976.6, "бокс 2-3.07"),
            (58539.7, "низ бокса 2-3.07"),
        ],
    },
    {
        "symbol": "ETH/USDT:USDT", "date": "2026-07-12", "src": "prizrak_eth",
        "levels": [(1750.0, "верх добора"), (1740.76, "уровень"), (1700.13, "BUY-зона"),
                   (1700.0, "низ добора")],
    },
    {
        "symbol": "POL/USDT:USDT", "date": "2026-07-22", "src": "prizrak_pol_matic",
        "levels": [(0.1050, "верх шорт-зоны"), (0.1020, "низ шорт-зоны"),
                   (0.0880, "локальное сопр."), (0.0800, "TP лонга"), (0.0780, "TP лонга"),
                   (0.0754, "верх лонг-зоны"), (0.0705, "добор"), (0.0670, "floor зоны")],
    },
    {
        "symbol": "AEVO/USDT:USDT", "date": "2026-07-02", "src": "prizrak_aevo",
        "levels": [(0.01908, "пробитое 4ч сопр."), (0.01859, "верхний добор 1ч"),
                   (0.01809, "ключевой добор 1ч"), (0.01720, "верх BUY"), (0.01700, "низ BUY")],
    },
]


def _zone_edges(setups: dict[str, Any], ladder: dict[str, Any] | None = None
                ) -> list[tuple[float, float, float | None, str]]:
    """Все уровни карты как ``(lo, hi, poc, метка)``.

    Считаются ОБА источника, потому что карточка печатает оба: ближние зоны горизонтов и
    глубокую спот-лестницу («Снайпер · спот-история», «Спот · накопление»). Забыть лестницу —
    значит занизить recall ровно на тех дальних уровнях, ради которых она и существует
    (у CFX в обзоре 2026-07-25 ВСЕ пять зон закупа жили только там).
    """
    out: list[tuple[float, float, float | None, str]] = []
    for side in ("below", "above"):
        for lv in ((ladder or {}).get(side) or []):
            try:
                px = float(lv["price"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append((px, px, None, f"ladder/{side}"))   # уровень-точка
    for key in ("atl", "ath"):
        v = (ladder or {}).get(key)
        if isinstance(v, (int, float)) and float(v) > 0:
            out.append((float(v), float(v), None, f"ladder/{key}"))
    for hname, hz in (setups.get("horizons") or {}).items():
        if not isinstance(hz, dict):
            continue
        for key in ("perezakup", "dobor", "short"):
            raw = hz.get(key)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zs:
                if not isinstance(z, dict):
                    continue
                try:
                    lo, hi = float(z["lo"]), float(z["hi"])
                except (KeyError, TypeError, ValueError):
                    continue
                poc = z.get("poc")
                out.append((lo, hi, float(poc) if isinstance(poc, (int, float)) else None,
                            f"{hname}/{key} {hz.get('tf')}"))
                # Ордерные линии внутри зоны — точечные уровни, ровно та форма, в которой автор и
                # публикует разметку. Считаются как отдельные кандидаты, иначе попадание в линию
                # неотличимо от «накрыт полосой», а разница между ними и есть локализация.
                for ln in (z.get("lines") or []):
                    if not isinstance(ln, dict):
                        continue
                    px = ln.get("price")
                    if not isinstance(px, (int, float)) or float(px) <= 0:
                        continue
                    tag = "линия★" if ln.get("key") else "линия"
                    out.append((float(px), float(px), None, f"{hname}/{key} {hz.get('tf')} {tag}"))
    return out


def _match(level: float, zones: list[tuple[float, float, float | None, str]], tol_pct: float
           ) -> tuple[bool, str, float, float, str]:
    """Уровень засчитан, если он ВНУТРИ зоны либо в ``tol_pct`` от кромки/ПОК.

    Возвращает ещё и КАЧЕСТВО попадания, без которого одна цифра recall врёт: уровень,
    накрытый 8%-полосой, формально «попал», но лимитку по нему не поставить — это «где-то
    здесь», а не локализация. Измерено на BCH: его ПОК базы 196 засчитывался как попадание
    полосой 190–205, и сужение зон до реальной структуры «сломало» его — хотя сломалась
    ровно фиктивная точность. Порог узости взят не с потолка: ``_INTEREST_ZONE_MAX_WIDTH_PCT``,
    то есть тот же, по которому сам модуль решает, можно ли ставить лимит.
    """
    best: tuple[bool, str, float, float, str] = (False, "—", float("inf"), 0.0, "—")
    hold: tuple[float, str] | None = None  # (ширина%, метка) самой УЗКОЙ накрывающей зоны
    for lo, hi, poc, tag in zones:
        if lo <= level <= hi:
            w = (hi / lo - 1.0) * 100.0 if hi > lo > 0 else 0.0
            if hold is None or w < hold[0]:
                hold = (w, tag)
        cands = [lo, hi] + ([poc] if poc is not None else [])
        for c in cands:
            if c <= 0:
                continue
            d = abs(level / c - 1.0) * 100.0
            if d < best[2]:
                # знак: >0 значит НАША кромка ВЫШЕ его уровня, <0 — ниже
                kind = "линия★" if "линия★" in tag else ("линия" if "линия" in tag else "кромка/ПОК")
                best = (d <= tol_pct, tag, d, (c / level - 1.0) * 100.0, kind)
    if best[0] and best[2] <= tol_pct:
        return best
    if hold is not None:
        kind = "узкая" if hold[0] <= _INTEREST_ZONE_MAX_WIDTH_PCT else "ШИРОКАЯ"
        return True, hold[1], 0.0, 0.0, kind
    return best


_CONTROL_DRAWS = 200  # наборов случайных карт на кейс
_CONTROL_SEED = 20260727  # фиксирован: замер обязан воспроизводиться

# Доля попаданий случайной карты по каждому кейсу — заполняется в `main`.
CONTROL: list[float] = []


def _control_recall(
    zones: list[tuple[float, float, float | None, str]],
    levels: list[tuple[float, str]],
    tol: float,
    price: float,
) -> float:
    """Средняя доля попаданий СЛУЧАЙНОЙ карты той же геометрии — нулевая гипотеза.

    Сохраняются число зон и ширина каждой; центры разбрасываются равномерно по тому же
    ценовому диапазону, что покрывает настоящая карта. Так контроль отвечает на вопрос
    «сколько мы поймали бы, ставя зоны наугад той же общей площадью», а не «сколько поймала
    бы пустая карта».
    """
    bands = [(lo, hi) for lo, hi, _p, t in zones if "линия" not in t and hi > lo > 0]
    if not bands or not levels:
        return 0.0
    span_lo = min(lo for lo, _ in bands)
    span_hi = max(hi for _, hi in bands)
    if span_hi <= span_lo:
        return 0.0
    rng = random.Random(_CONTROL_SEED + int(price * 1000) % 100_000)
    total = 0.0
    for _ in range(_CONTROL_DRAWS):
        fake: list[tuple[float, float, float | None, str]] = []
        for lo, hi in bands:
            width = hi - lo
            centre = rng.uniform(span_lo + width / 2, span_hi - width / 2)
            fake.append((centre - width / 2, centre + width / 2, None, "контроль"))
        hit = sum(1 for lvl, _n in levels if _match(float(lvl), fake, tol)[0])
        total += hit / len(levels)
    return total / _CONTROL_DRAWS


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tol", type=float, default=1.0, help="допуск совпадения, %% (по умолчанию 1.0)")
    ap.add_argument("--only", default="", help="фильтр по символу, напр. ASTR")
    args = ap.parse_args()

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    spot_eng = SpotEngine([])
    await spot_eng._ex.load_markets()
    tot_hit = tot_lvl = tot_zones = 0
    widths: list[float] = []
    try:
        await ex.load_markets()
        for case in BASELINE:
            sym = str(case["symbol"])
            if args.only and args.only.upper() not in sym.upper():
                continue
            # I-5: отсекаем всё, что закрылось ПОСЛЕ даты разбора — модуль не должен видеть будущее
            cutoff = int(datetime.fromisoformat(str(case["date"])).replace(tzinfo=UTC).timestamp() * 1000)
            cutoff += 24 * 3600 * 1000  # конец дня публикации
            raw: dict[str, list[list[float]]] = {}
            for tf in TFS:
                try:
                    o = await ex.fetch_ohlcv(sym, tf, limit=500)
                except Exception:
                    continue
                step = ex.parse_timeframe(tf) * 1000
                raw[tf] = [b for b in o if int(b[0]) + step <= cutoff]
            if not raw.get("4h"):
                print(f"{sym}: нет данных")
                continue
            price = float(raw["4h"][-1][4])
            setups = build_symbol_setups(raw, price=price, cfg=CFG, structure=None)
            ladder = await spot_weekly_ladder_native(sym, price=price, spot=spot_eng)
            zones = _zone_edges(setups, ladder)
            # Считаем ПОЛОСЫ, а не всех кандидатов: линии сетки — это разметка ВНУТРИ уже
            # посчитанной зоны, и складывать их с зонами значило бы объявить ростом избытка ровно
            # ту детализацию, ради которой сетка и делалась.
            bands = [z for z in zones if "линия" not in z[3]]
            tot_zones += len(bands)
            # Ширина зоны — вторая метрика, без которой recall врёт. Сужение зоны при СОХРАНЁННОМ
            # покрытии его уровня recall не видит вообще, а это и есть выигрыш: точнее вход, честнее
            # RR. Измерено на правке value area — 35% боксов сузились на медианные 31%, recall 54→54.
            widths.extend((hi / lo - 1.0) * 100.0 for lo, hi, _p, _t in bands if hi > lo > 0)

            # ⚠ КОНТРОЛЬ СЛУЧАЙНЫМИ УРОВНЯМИ. Без него recall не значит НИЧЕГО.
            # Osler (ФРБ Нью-Йорка, 2000, EPR 6(2)) прогнала 10 000 наборов СЛУЧАЙНЫХ уровней
            # в день против 28 месяцев минутных котировок: случайные уровни отрабатывали
            # **56.2%**, опубликованные профессиональные — **60.8%**. Весь эффект — 4.6 п.п.
            # То есть «наши уровни совпадают с его на 60%» — это ровно случайный базлайн.
            # Значение имеет только ПРЕВЫШЕНИЕ над контролем на тех же данных.
            #
            # Контроль строится из НАШИХ зон с сохранением их числа и ширины, но со случайно
            # переставленными центрами внутри того же ценового диапазона: структура разрушена,
            # «сколько площади мы покрываем» — сохранено. Иначе широкая карта набирает recall
            # просто размером.
            ctrl_hits = _control_recall(zones, case["levels"], args.tol, price)
            CONTROL.append(ctrl_hits)

            hits = 0
            misses: list[str] = []
            for lvl, name in case["levels"]:
                ok, tag, dev, signed, kind = _match(float(lvl), zones, args.tol)
                if ok:
                    hits += 1
                    QUALITY[kind] = QUALITY.get(kind, 0) + 1
                else:
                    misses.append(f"{name} {lvl:g} (ближайшая {tag}, наша кромка {signed:+.1f}%)")
                    SIGNS.append((name, signed))
            tot_hit += hits
            tot_lvl += len(case["levels"])
            pct = hits / len(case["levels"]) * 100.0
            print(f"\n{sym:18s} [{case['src']}]  цена {price:g}")
            print(f"  recall {hits}/{len(case['levels'])} = {pct:.0f}%   зон: {len(bands)}  ·  линий сетки: {len(zones) - len(bands)}")
            for m in misses:
                print(f"    ✗ {m}")
    finally:
        await ex.close()
        await spot_eng.close()
    if tot_lvl:
        real = tot_hit / tot_lvl * 100.0
        if CONTROL:
            ctrl = sum(CONTROL) / len(CONTROL) * 100.0
            print(f"\n{'=' * 62}")
            print(f"RECALL {real:.1f}%   СЛУЧАЙНЫЙ КОНТРОЛЬ {ctrl:.1f}%   "
                  f"ПРЕВЫШЕНИЕ {real - ctrl:+.1f} п.п.")
            print("Значение имеет только превышение. Для ориентира: Osler (2000) намерила у "
                  "профессиональных\nуровней превышение над случайными 4.6 п.п. "
                  f"(60.8% против 56.2%). Контроль: {_CONTROL_DRAWS} наборов/кейс.")
            print("=" * 62)
        up = [s for _, s in SIGNS if s > 0]
        dn = [s for _, s in SIGNS if s < 0]
        print(f"\nЗНАК смещения промахов: выше его уровня {len(up)}, ниже {len(dn)}")
        if SIGNS:
            print(f"  среднее смещение: {sum(s for _, s in SIGNS)/len(SIGNS):+.2f}%")
        if widths:
            widths.sort()
            print(f"\nШИРИНА ЗОН: медиана {widths[len(widths) // 2]:.2f}%"
                  f"  ·  p90 {widths[int(len(widths) * 0.9)]:.2f}%  ·  измерено {len(widths)}")
        if QUALITY:
            q = "  ·  ".join(f"{k} {v}" for k, v in sorted(QUALITY.items(), key=lambda kv: -kv[1]))
            wide = QUALITY.get("ШИРОКАЯ", 0)
            print(f"\nКАЧЕСТВО ПОПАДАНИЙ: {q}"
                  f"   (ШИРОКАЯ = накрыт полосой >{_INTEREST_ZONE_MAX_WIDTH_PCT:g}%, лимитку не поставить)")
            print(f"  локализовано точно: {tot_hit - wide}/{tot_lvl} = {(tot_hit - wide) / tot_lvl * 100:.0f}%")
        print(f"\n{'=' * 60}\nИТОГО recall {tot_hit}/{tot_lvl} = {tot_hit / tot_lvl * 100:.0f}%"
              f"   ·   зон напечатано: {tot_zones} (он публикует 2–4 на символ)")


if __name__ == "__main__":
    asyncio.run(main())
