"""Ордерная сетка ВНУТРИ зоны: 2–4 линии структуры и одна «ключевая».

Зачем. Автор не торгует полосу — он дробит её на ордера и один уровень называет ключевым. В
разборе BTC 1ч от 2026-07-25 из ОДНОГО пятнадцатиминутного бокса 17–18 июля он выводит четыре
линии (63 395,4 / 63 190,8 / 62 837,3 / 62 590,1) и на 09:38 говорит: «ключевой уровень всей этой
корявой ликвидности проходит на отметках 62 837». Курс требует того же прямо (стр.30: крупная база
делится на 2–3 ордера). Модуль до сих пор схлопывал структуру в одну полосу + один скаляр входа.

Как. Плотность касаний по барам САМОЙ структуры, локальные максимумы, ключевая = максимум
``касания × объём``. Замерено на живых 15м Binance против его собственной разметки:

| его бокс | его линии                                   | пикер                        | расхождение          |
|----------|---------------------------------------------|------------------------------|----------------------|
| 17–18.07 | 63 395,4 / 63 190,8 / **62 837,3** / 62 590,1 | 63 455,5 / 63 134,8 / 62 901,6 | +0.09 / −0.09 / +0.10% |
| 22–23.07 | **65 923,8** / 65 609,1                      | 65 912,1 / 65 616,4          | −0.02 / +0.01%       |
| 19–20.07 | 64 754,0                                    | 64 833,9 / **64 669,7**      | +0.12 / −0.13%       |

Жирным — линия с максимумом ``касания × объём``; на боксе 17–18.07 это ровно та, которую он назвал
ключевой вслух.

Два ограничения, тоже замеренные, и оба легко нарушить:

* **Только бары структуры.** На широком окне (16–26.07 вместо самого бокса) те же пики уезжают в
  64–66k и НИ ОДНА его линия не появляется. Поэтому вход тот же, что у профиля зоны, —
  :func:`poc._structure_bars` (это чинил коммит 91fef50).
* **Не якорить на ПОК.** ПОК объёмного профиля ТОГО ЖЕ бокса = 63 950, на 1.8% выше его ключевой,
  устойчиво при 30/60/120 корзинах. ПОК верен для крупной БАЗЫ (стр.30), но внутри «корявой»
  структуры без выраженной моды ключевой уровень — это граница с наибольшим числом касаний, и
  подмена одного другим сместила бы вход на 1.8%.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _MIN_STRUCTURE_BARS, _structure_bars

# Минимальный зазор между соседними линиями. У него внутри бокса 17–18.07 шаги 0.32% / 0.56% /
# 0.39%, в боксе 22–23.07 — 0.48%. Ниже 0.25% две «линии» описывают одно и то же скопление и
# читаются как две возможности вместо одной.
_MIN_SEP_PCT = 0.25
# Сколько линий публиковать. Курс говорит «2-3 ордера» (стр.30), у него их вышло четыре — предел
# берётся по наблюдению, а не по тексту.
_MAX_LINES = 4
# Полоса, в которой считаются касания и объём КОНКРЕТНОЙ линии. Та же, на которой замерялись его 13
# уровней (таблица выше): ±0.08% — примерно толщина линии на его графике.
_LINE_BAND_PCT = 0.08
# Разрешение гистограммы. Корзина не тоньше 0.02% цены (иначе меряется шум тика) и не грубее
# 1/120 ширины зоны (иначе внутри узкой полосы пиков не разглядеть).
_MIN_BUCKET_PCT = 0.02
_MAX_BUCKETS = 120


def _density(
    bars: list[list[float]], *, lo: float, hi: float, buckets: int
) -> tuple[list[int], list[float]]:
    """Касания и объём по корзинам полосы ``[lo, hi]``.

    Касание — перекрытие диапазона бара с корзиной (то же определение, по которому меряются его
    опубликованные уровни). Объём распределяется по СОБСТВЕННОМУ диапазону бара, как в объёмном
    профиле, поэтому бар, задевший полосу краем, отдаёт ей лишь свою долю, а не весь объём.
    """
    step = (hi - lo) / buckets
    touches = [0] * buckets
    volume = [0.0] * buckets
    for b in bars:
        bl, bh, bv = float(b[3]), float(b[2]), float(b[5])
        if bh < lo or bl > hi:
            continue
        i0 = max(0, int((max(bl, lo) - lo) / step))
        i1 = min(buckets - 1, int((min(bh, hi) - lo) / step))
        if i1 < i0:
            continue
        span = bh - bl
        for i in range(i0, i1 + 1):
            touches[i] += 1
            if span > 0:
                c_lo, c_hi = lo + i * step, lo + (i + 1) * step
                overlap = min(bh, c_hi) - max(bl, c_lo)
                if overlap > 0:
                    volume[i] += bv * (overlap / span)
            else:
                volume[i] += bv
    return touches, volume


def _peaks(touches: list[int], volume: list[float], *, lo: float, step: float) -> list[float]:
    """Цены локальных максимумов плотности, сильнейшие первыми, разрежённые ``_MIN_SEP_PCT``."""
    n = len(touches)
    cands: list[tuple[int, float, float]] = []
    for i in range(n):
        left = touches[i - 1] if i > 0 else -1
        right = touches[i + 1] if i < n - 1 else -1
        if touches[i] > 0 and touches[i] >= left and touches[i] >= right:
            cands.append((touches[i], volume[i], lo + (i + 0.5) * step))
    cands.sort(key=lambda t: (-t[0], -t[1]))
    picked: list[float] = []
    for _t, _v, price in cands:
        if all(abs(price / q - 1.0) * 100.0 > _MIN_SEP_PCT for q in picked):
            picked.append(price)
        if len(picked) >= _MAX_LINES:
            break
    return picked


def _measure(bars: list[list[float]], price: float) -> tuple[int, float]:
    """Касания и объём ОДНОЙ линии — пересчёт по барам, а не сумма корзин.

    Корзина отвечает на «где пик», линия — на «сколько его касались»; складывать корзины значило бы
    считать один бар столько раз, сколько корзин он накрыл.
    """
    band_lo, band_hi = price * (1 - _LINE_BAND_PCT / 100.0), price * (1 + _LINE_BAND_PCT / 100.0)
    n, vol = 0, 0.0
    for b in bars:
        if float(b[3]) <= band_hi and float(b[2]) >= band_lo:
            n += 1
            vol += float(b[5])
    return n, vol


def zone_lines(
    ohlcv: list[list[float]],
    *,
    zone: dict[str, Any] | None,
    band: tuple[float, float] | None = None,
    cfg: PrizrakConfig | None = None,
) -> list[dict[str, Any]]:
    """Ордерные линии внутри зоны, по возрастанию цены; ключевая помечена ``key``.

    Args:
        ohlcv: окно, на котором зона найдена (то же, что уходит в :func:`poc.zone_poc`).
        zone: зона накопления — по ней локализуются бары структуры.
        band: ОПУБЛИКОВАННЫЕ границы полосы, если они уже сужены до value area. Линии обязаны лежать
            внутри того, что увидит читатель; ``None`` — берутся ``zone["lo"]``/``zone["hi"]``.
        cfg: конфиг призрака (сейчас не влияет на геометрию, принимается для единообразия вызова).

    Returns:
        ``[{"price", "touches", "volume", "key"}, …]`` или **пустой список**, когда сетку строить
        не из чего: мало баров структуры, вырожденная полоса, ни одного касания. Пустой список —
        честный ответ «линий нет», а не «линия одна» (I-6).
    """
    _ = cfg
    if not ohlcv:
        return []
    if band is not None:
        lo, hi = float(band[0]), float(band[1])
    elif zone and isinstance(zone.get("lo"), (int, float)) and isinstance(zone.get("hi"), (int, float)):
        lo, hi = float(zone["lo"]), float(zone["hi"])
    else:
        return []
    if not 0 < lo < hi:
        return []
    bars = _structure_bars(ohlcv, zone)
    if len(bars) < _MIN_STRUCTURE_BARS:
        return []

    width_pct = (hi / lo - 1.0) * 100.0
    buckets = max(4, min(_MAX_BUCKETS, int(width_pct / _MIN_BUCKET_PCT)))
    touches, volume = _density(bars, lo=lo, hi=hi, buckets=buckets)
    if not any(touches):
        return []
    step = (hi - lo) / buckets
    prices = _peaks(touches, volume, lo=lo, step=step)
    if not prices:
        return []

    # Кромка полосы — тоже уровень: его нижняя линия 62 590,1 это пол бокса 17–18.07, а не пик
    # плотности (на 15м у неё всего 3 касания). Добавляется, только если рядом нет уже выбранной
    # линии и место в лимите осталось — иначе кромка вытеснила бы настоящий узел.
    for edge in (lo, hi):
        if len(prices) >= _MAX_LINES:
            break
        if all(abs(edge / q - 1.0) * 100.0 > _MIN_SEP_PCT for q in prices):
            prices.append(edge)

    out: list[dict[str, Any]] = []
    for price in sorted(prices):
        t, v = _measure(bars, price)
        out.append({"price": round(price, 8), "touches": t, "volume": round(v, 4), "key": False})
    if len(out) < 2:
        return []  # одна линия — это не сетка, полоса уже несёт свой якорь
    key = max(out, key=lambda d: float(d["touches"]) * float(d["volume"]))
    if float(key["touches"]) > 0:
        key["key"] = True
    return out


__all__ = ["zone_lines"]
