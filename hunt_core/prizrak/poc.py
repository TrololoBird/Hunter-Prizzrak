"""POC / VAH / VAL on a found накопление zone — the centerpiece confirmed twice this
session: independent recomputation matched PrizrakTrade's own visually-marked POC
almost exactly on both ONDO (0.310-0.311 vs his 0.3114) and BTC (60,271 vs his
60,511.9/60,173.3/59,978.7 bracket).

Reuses ``features.volume_profile.volume_profile_levels`` verbatim (the project's own
fixed-range histogram implementation) — no reimplementation of the bucket math.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.features.volume_profile import volume_profile_levels


def _frame_from_ohlcv(ohlcv: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": [float(r[2]) for r in ohlcv],
            "low": [float(r[3]) for r in ohlcv],
            "volume": [float(r[5]) for r in ohlcv],
        }
    )


_MIN_STRUCTURE_BARS = 5
# Сколько баров подряд структура может провести ВНЕ своей полосы, не перестав быть одной
# структурой. Ноль рвал бы накопление на клочки от каждого выноса фитиля.
_STRUCTURE_GAP = 2


def _structure_runs(
    ohlcv: list[list[float]], *, lo: float, hi: float
) -> list[tuple[int, int]]:
    """Непрерывные серии баров, реально торгующих в полосе ``[lo, hi]``.

    Именно так автор рисует бокс — вокруг компактного скопления свечей, а линию тянет вправо.
    Ретест через сто баров принадлежит УРОВНЮ, а не структуре, которая его породила.
    """
    inside = [i for i, b in enumerate(ohlcv) if float(b[3]) <= hi and float(b[2]) >= lo]
    if not inside:
        return []
    out: list[tuple[int, int]] = []
    start = prev = inside[0]
    for i in inside[1:]:
        if i - prev <= _STRUCTURE_GAP + 1:
            prev = i
            continue
        out.append((start, prev))
        start = prev = i
    out.append((start, prev))
    return out


def _structure_bars(
    ohlcv: list[list[float]], zone: dict[str, Any] | None
) -> list[list[float]]:
    """Bars the structure actually occupies, or the full window if it can't be located.

    Два продюсера — и только ОДИН из них отдаёт настоящий span. Стоповый объём размечает его
    честно (``structure_lo_idx``/``structure_hi_idx`` — плотное подокно, stop_volume.find_stop_volume).
    А ``first_touch_idx``/``last_touch_idx`` зоны накопления — НЕ span: ``accumulation._cluster``
    группирует пивоты по ЦЕНЕ, «regardless of when they occurred», так что интервал склеивает
    происхождение структуры со всеми последующими ретестами.

    Измерено на живых BTC 4h (2026-07-25): огибающая касаний давала 236–289 баров из 300 — 79–96%
    окна, — и «ПОК зоны» вырождался в ПОК ВСЕГО ОКНА. Для бокса 65484–65750 он выпадал на 62902
    (на 2600 пунктов ниже самой зоны) и корректно глушился guard'ом I-6 — то есть вход терял якорь
    и падал на кромку. По непрерывной серии тот же бокс даёт 65755, бокс 64473–64919 → 64716 против
    уровня автора 64754 (0.06%), а зона, у которой огибающая и так совпадала со структурой, — 60157
    против его 60173 (0.01%). Механизм был верен, вход в него — нет.

    Серия выбирается по ОБЪЁМУ, а не по длине: «сила уровня определяется ТФ и объемом» (стр.22),
    и профиль строится ради объёма. Длина — только тай-брейк.
    """
    if not zone:
        return ohlcv
    first = zone.get("structure_lo_idx")
    last = zone.get("structure_hi_idx")
    if first is not None and last is not None:
        lo_i, hi_i = int(first), int(last) + 1
        if 0 <= lo_i and hi_i <= len(ohlcv) and hi_i - lo_i >= _MIN_STRUCTURE_BARS:
            return ohlcv[lo_i:hi_i]
        return ohlcv
    lo, hi = zone.get("lo"), zone.get("hi")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and 0 < lo <= hi:
        runs = [r for r in _structure_runs(ohlcv, lo=float(lo), hi=float(hi))
                if r[1] - r[0] + 1 >= _MIN_STRUCTURE_BARS]
        if runs:
            a, b = max(runs, key=lambda r: (sum(float(x[5]) for x in ohlcv[r[0]:r[1] + 1]),
                                            r[1] - r[0]))
            return ohlcv[a:b + 1]
    return ohlcv


def zone_poc(
    ohlcv: list[list[float]],
    *,
    zone: dict[str, Any] | None = None,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """POC/VAH/VAL over the given bars, optionally restricted to a found накопление zone.

    ``ohlcv`` is the tier lookback the zone (from ``accumulation.find_accumulation_zone``)
    was found on; the zone's ``first_touch_idx``/``last_touch_idx`` index into it. The
    profile is fitted to those bars alone, per course methodology (с. 26: "натягивая
    профиль на структуру — важно захватить все свечи структуры"). Profiling the whole
    lookback instead put the POC outside the zone on a large share of candidates, which
    is not a POC of that накопление at all.
    """
    cfg = cfg or PrizrakConfig.load()
    bars = _structure_bars(ohlcv, zone)
    frame = _frame_from_ohlcv(bars)
    poc, vah, val = volume_profile_levels(frame, buckets=cfg.vp_buckets, value_area_pct=cfg.vp_value_area_pct)
    if poc is None:
        return {}
    out: dict[str, Any] = {"poc": round(poc, 8), "vah": round(vah, 8) if vah else None, "val": round(val, 8) if val else None}
    if zone:
        lo, hi = zone.get("lo"), zone.get("hi")
        if lo and hi and hi > lo:
            out["poc_stable"] = _poc_is_stable(frame, poc, lo=float(lo), hi=float(hi), cfg=cfg)
            position = (poc - lo) / (hi - lo)  # 0=at support, 1=at resistance
            # A profile fitted to the structure can still peak just outside the boundary
            # band (the zone's hi/lo are cluster means, not hard extremes), so keep the
            # guard rather than emit a ratio that isn't one.
            if 0.0 <= position <= 1.0:
                out["poc_position_in_zone"] = round(position, 4)
    return out


# Доля ширины зоны, на которую ПОК может гулять при смене разбиения корзин, оставаясь «устойчивым».
# Порог измерен: у одномодальных структур разброс 1.8–5.0% ширины (ETH 1653–1851, SOL 66.9–74.3,
# AVAX 6.14–6.87), у бимодальной LTC 40.78–45.59 — 54.1%. Между этими режимами провал, 15% лежит в нём.
#
# ✔ ПЕРЕПРОВЕРЕНО 2026-07-27 независимой развёрткой (`scripts/verify_poc_origin_guard.py`,
# 6 символов × 3 ТФ, 55 настоящих зон, разброс по числу корзин И по началу сетки). Гистограмма
# разброса имеет ПУСТУЮ корзину ровно на `[15, 20)%` — то есть 15.0 стоит в реальном провале
# между режимами, а не «примерно там»:
#     [0,2)% 3 · [2,5)% 14 · [5,10)% 9 · [10,15)% 7 · **[15,20)% 0** · [20,30)% 4 · [30,50)% 5 …
# Разделение: 33 зоны устойчивы, 22 нет.
#
# Неустойчивость растёт МОНОТОННО с таймфреймом — 15m 19% зон (медиана разброса 6.9%),
# 1h 42% (14.1%), 4h 55% (24.2%). То есть «ПОК на 4h шумит» — правда, и она уже обработана:
# `setups.py` при `poc_stable=False` якорится на КРОМКУ зоны, а не на ПОК. Это согласуется с
# разбором `prizrak_btc_1h_20260725`, где в «корявой» структуре ключевой уровень автора —
# граница с максимумом касаний, а ПОК профиля лежал на 1.7% в стороне.
_POC_STABILITY_MAX_SPREAD = 15.0
# Разбиения для пробы. Одномодальный пик остаётся тем же при любом из них; на двух почти равных
# пиках argmax перескакивает — это и есть наблюдаемый симптом бимодальности.
_POC_STABILITY_BUCKETS = (40, 60, 90, 120)
# ⚠ СДВИГИ НАЧАЛА СЕТКИ: ПРОБОВАЛИ, ЗАМЕРИЛИ, НЕ ДОБАВИЛИ. Не «не дошли руки».
#
# Довод был сильный: перебор одного числа корзин неполон — все разбиения стартуют с минимума
# окна, то есть меняют ШАГ и не меняют НАЧАЛО, а мода гистограммы зависит от обоих. На
# 300-барном окне (`scripts/verify_poc_plateau.py`) один только сдвиг сетки уводил ПОК до
# **11.87%** цены. Возможность сдвига добавлена в `volume_profile_levels(origin_shift=...)`.
#
# Но замер на НАСТОЯЩИХ зонах (`scripts/verify_poc_origin_guard.py`, 6 символов × 3 ТФ, 55 зон,
# профиль натянут на бары зоны, разброс нормирован на ширину зоны — как здесь) дал:
#     неустойчивых только по числу корзин   22 из 55
#     с добавленным перебором начала        22 из 55   → ДОБАВЛЕНО НОЛЬ
# При этом худший разброс по началу — 672.8% ширины зоны, то есть эффект огромен, но целиком
# накрывается перебором корзин: зона, у которой ПОК скачет от начала сетки, скачет и от шага.
#
# Два лишних профиля на зону на каждом тике за ноль обнаружений — это и есть «инертная
# настройка», против которой стоит I-7. Проверка оставлена как была; возможность сдвига
# сохранена для замеров. Возвращать — только с новым замером, показывающим ненулевой прирост.
_POC_STABILITY_ORIGINS = (0.25, 0.5)


def _poc_is_stable(frame: pl.DataFrame, poc: float, *, lo: float, hi: float,
                   cfg: PrizrakConfig) -> bool:
    """Устойчив ли ПОК к смене разбиения — то есть один ли у профиля доминирующий пик.

    Зачем. Вход якорится на ПОК (стр.30), но на БИМОДАЛЬНОЙ зоне это ложная точность: измерено на
    LTC 4h 40.78–45.59 профилем по 4785 пятнадцатиминуткам — два пика несут 12.9% и 12.4% объёма,
    и argmax перескакивает между ними, сдвигая якорь входа на 5.7%. Курс это предвидит прямо:
    «до POC может не дойти» (стр.30), поэтому он и разносит закуп на 2–3 ордера, а не ставит один.

    Мерить сами моды напрямую я пробовал и получил самоопровергающийся детектор (окно ±6 корзин
    при шаге 6 покрыло 52 корзины из 60 → «все зоны четырёхмодальны, по 25% каждая»). Здесь
    меряется НАБЛЮДАЕМЫЙ симптом — перескок ПОК при смене разбиения, — а не гипотеза о форме.
    """
    span = hi - lo
    if span <= 0:
        return True
    seen: list[float] = [poc]
    for buckets in _POC_STABILITY_BUCKETS:
        if buckets == cfg.vp_buckets:
            continue
        p, _vah, _val = volume_profile_levels(
            frame, buckets=buckets, value_area_pct=cfg.vp_value_area_pct
        )
        if p is not None:
            seen.append(float(p))
    if len(seen) < 3:
        return True  # мерить нечем — не объявляем неустойчивость без основания (I-6)
    return (max(seen) - min(seen)) / span * 100.0 <= _POC_STABILITY_MAX_SPREAD


__all__ = ["zone_poc"]
