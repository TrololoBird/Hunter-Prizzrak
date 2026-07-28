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


# Минимум баров, на которых вообще строится профиль структуры. Диапазон изменения задан НЕ
# вкусом — обе границы уперты в механику, замерено 2026-07-27 на двух независимых наборах
# живых зон (744 и 584 зоны, символы не пересекаются):
#
#  • ПОЛ = 5 — это пол ПРИМИТИВА: `features/volume_profile.py::volume_profile_levels` при
#    height < 5 отдаёт `(None, None, None)`. Проверено живым вызовом: 2/3/4 бара → None,
#    5 баров → ПОК. Ниже физически нечего профилировать.
#  • ПОТОЛОК — СВЯЗЬ с `stop_volume.py::_SUB_WINDOW`, а не потолок сам по себе. Поднять этот
#    порог, не подняв ТО число, значит уронить импорт (AssertionError → orchestrator → весь
#    ПРИЗРАК): T=7/8/10/12 в одиночку падают, T=7+_SUB_WINDOW=7 и T=20+20 импортируются.
#    Поднимать имеет смысл только ОБА — и это уже правка геометрии стопового объёма, которая
#    на живых данных НЕ измерена.
#  • ПОДЪЁМ СТРОГО УХУДШАЕТ задетые зоны, потому что порог не отключает ПОК, а ПОДМЕНЯЕТ его
#    ПОКом ВСЕГО ОКНА (см. `_structure_bars`). При T=10 серию теряют 7–9% зон, и ПОК всего
#    окна попадает внутрь своей зоны у **0%** из них.
#  • При T=5 серии нет у 0.4% зон (3 из 747) — константа работает ПОЛОМ, а не фильтром.
#
# ⚠ Что НЕ является обоснованием и цитировать это НЕЛЬЗЯ: «плато устойчивости наступает к ~40
# барам». Величина нормирована на ширину зоны, а ширина сама растёт с длиной структуры
# (ρ = +0.46 на обеих выборках). В процентах ЦЕНЫ разброс по числу корзин от длины не зависит
# вовсе (ρ = +0.06). Замер, который «показывал» зависимость, мерил знаменатель.
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

    ⚠ ОТКАТ НА ВСЁ ОКНО — не безобидная деградация. Замер 2026-07-27 на двух независимых
    наборах живых зон (744 и 584, символы не пересекаются): ПОК всего окна попадает внутрь
    своей зоны лишь у 12% и 18% зон, медианно мимо на 335–518% ширины зоны (5.4–7.9% ЦЕНЫ).
    Срабатывает он редко (3 зоны из 747 = 0.4%; на отдельном прогоне 84 зон — 0 раз), и
    именно поэтому НЕЛЬЗЯ поднимать ``_MIN_STRUCTURE_BARS``: порог управляет не
    «отвечать/не отвечать», а «какой объект профилировать».

    Кому нужен ЧЕСТНЫЙ ОТКАЗ вместо отката — :func:`_structure_span`.
    """
    span = _structure_span(ohlcv, zone)
    if span is None:
        return ohlcv
    a, b = span
    return ohlcv[a:b + 1]


def _structure_span(
    ohlcv: list[list[float]], zone: dict[str, Any] | None
) -> tuple[int, int] | None:
    """Индексы ``[первый, последний]`` баров структуры, либо ``None`` — локализовать не удалось.

    Тот же выбор, что делает :func:`_structure_bars`; разница ровно в ответе на неудачу, и оба
    ответа нужны — подменять один другим нельзя:

    * ``zone_poc`` исторически откатывается на всё окно, и ниже по течению это уже частично
      обезврежено (``setups.py`` требует «ПОК внутри полосы», а ПОК окна там оказывается в 0%
      случаев). Менять его без отдельного замера — риск вырождения, описанный выше;
    * ``grid.zone_lines`` обязан ОТКАЗАТЬ: его проверка ``len(bars) < _MIN_STRUCTURE_BARS`` на
      400-барном окне тривиально ложна, поэтому при откате сетка плотности считалась бы по ВСЕЙ
      истории — ровно против замера в докстроке самого ``grid.py`` («на широком окне НИ ОДНА
      его линия не появляется»).
    """
    if not zone:
        return None
    first = zone.get("structure_lo_idx")
    last = zone.get("structure_hi_idx")
    if first is not None and last is not None:
        lo_i, hi_i = int(first), int(last) + 1
        if 0 <= lo_i and hi_i <= len(ohlcv) and hi_i - lo_i >= _MIN_STRUCTURE_BARS:
            return lo_i, hi_i - 1
        # Размеченный span невалиден — в ветку lo/hi НЕ падаем (так было и до выделения
        # функции: `_structure_bars` здесь возвращал всё окно немедленно).
        return None
    lo, hi = zone.get("lo"), zone.get("hi")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and 0 < lo <= hi:
        runs = [r for r in _structure_runs(ohlcv, lo=float(lo), hi=float(hi))
                if r[1] - r[0] + 1 >= _MIN_STRUCTURE_BARS]
        if runs:
            return max(runs, key=lambda r: (sum(float(x[5]) for x in ohlcv[r[0]:r[1] + 1]),
                                            r[1] - r[0]))
    return None


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
# ⚠ ПРЕЖНЕЕ «ПЕРЕПРОВЕРЕНО» СНЯТО — оно утверждало замер, который не воспроизводится.
# Говорилось: гистограмма разброса имеет ПУСТУЮ корзину ровно на `[15,20)%`, то есть порог
# стоит в реальном провале между режимами. Перепроверка 2026-07-27 на ДВУХ независимых
# наборах живых зон (181 и 201 зона, символы не пересекаются) даёт там **12 и 11 зон**, а
# `scripts/verify_poc_origin_guard.py`, на который ссылались, печатает сегодня 3, а не 0.
# Распределение тяжелохвостое; чистого провала между режимами на этих данных НЕТ.
#
# Ссылка на тот скрипт как на сертификат тоже снята: он профилирует ОГИБАЮЩУЮ КАСАНИЙ
# (`first_touch_idx … last_touch_idx`, медиана 138–168 баров), а продакшн — `_structure_bars`
# (медиана 23–26). Он мерил не тот объект и давал 45–56% неустойчивых против 20–31% у
# продакшна. Починен тем же коммитом; ссылку вернуть можно только после нового замера.
#
# ЧТО ВОСПРОИЗВЕЛОСЬ ВМЕСТО ЭТОГО и почему порог всё же не двигаем: вердикт устойчив к
# самому порогу. 12.0 и 15.0 дают ИДЕНТИЧНУЮ опубликованную карту; 18.0/20.0 — тоже;
# расхождение начинается только на 25.0. Перетюнивать здесь нечего — это точка, в которой
# карта не меняется, а не оптимум.
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
# ⚠ НУЖНЫ ≥3 РАЗЛИЧНЫХ РАЗБИЕНИЯ, И ЭТО МЕХАНИКА, А НЕ ВКУС. `_poc_is_stable` собирает
# `seen = [канон] + пробы ≠ cfg.vp_buckets` и при `len(seen) < 3` честно возвращает True
# («мерить нечем», I-6). Значит набор из ДВУХ значений выключает гард МОЛЧА — без единого
# признака в выводе. Замер 2026-07-27 на двух независимых наборах: `(40, 60)` → 0 неустойчивых
# из 181 и 0 из 201, а опубликованная карта совпадает с «гард выключен» ключ в ключ.
# Ассерт — та же форма связи констант, что уже принята в `stop_volume.py::_SUB_WINDOW`;
# число 3 не новое магическое, оно взято из существующего правила `len(seen) < 3`.
assert len(set(_POC_STABILITY_BUCKETS)) >= 3, (
    f"_POC_STABILITY_BUCKETS={_POC_STABILITY_BUCKETS}: нужно >= 3 РАЗЛИЧНЫХ разбиения — иначе "
    "после исключения канона (cfg.vp_buckets) остаётся < 2 проб, len(seen) < 3, и "
    "_poc_is_stable всегда возвращает True: гард выключен без единого признака в выводе"
)
# ⚠ СДВИГИ НАЧАЛА СЕТКИ: ПРОБОВАЛИ, ЗАМЕРИЛИ, НЕ ДОБАВИЛИ. Не «не дошли руки».
#
# Довод был сильный: перебор одного числа корзин неполон — все разбиения стартуют с минимума
# окна, то есть меняют ШАГ и не меняют НАЧАЛО, а мода гистограммы зависит от обоих. На
# 300-барном окне (`scripts/verify_poc_plateau.py`) один только сдвиг сетки уводил ПОК до
# **11.87%** цены. Возможность сдвига добавлена в `volume_profile_levels(origin_shift=...)`.
#
# ⚠ ЗАМЕР, НА КОТОРЫЙ ЭТО ОПИРАЛОСЬ, БЫЛ СНЯТ НЕ С ТОГО ОБЪЕКТА — и цифра менялась дважды.
# Прежняя редакция утверждала «ДОБАВЛЕНО НОЛЬ» по `scripts/verify_poc_origin_guard.py`. Но тот
# скрипт натягивал профиль на ОГИБАЮЩУЮ КАСАНИЙ (медиана 138–168 баров), а не на структуру
# (медиана 23–26), то есть мерил другой объект и завышал неустойчивость вдвое (45–56% против
# 20–31% у продакшна). Скрипт починен тем же коммитом.
#
# Замер ИСПРАВЛЕННЫМ скриптом, 2026-07-27, 6 символов × 3 ТФ, 52 зоны:
#     неустойчивых только по числу корзин   8 из 52  (15%)
#     с добавленным перебором начала       12 из 52  (23%)   → ДОБАВЛЯЕТ 4
# Худший разброс по началу — 69.0% ширины зоны.
#
# ⚠ РЕШЕНИЕ ОСТАВЛЕНО ПРЕЖНИМ (не включать), НО ОСНОВАНИЕ ОСЛАБЛО, и это надо знать. +4 на 52
# зонах — это рост числа обнаружений в полтора раза (8 → 12), а не «ноль» и не «шум», как
# говорили обе прежние редакции. Против включения сейчас ровно один довод: два лишних профиля
# на зону на КАЖДОМ тике. Довода «оно ничего не ловит» больше нет.
# Включать — только с замером ОПУБЛИКОВАННОЙ КАРТЫ (меняются ли реальные уровни) и стоимости
# на горячем пути, а не по одному числу обнаружений. Возможность сдвига сохранена в
# `volume_profile_levels(origin_shift=...)`.
#
# ⚠ И отдельно — контрпример к соблазнительному рассуждению «член с бо́льшим разбросом полезнее
# как детектор». По медиане сдвига ПОКа НАЧАЛО СЕТКИ (4.0–4.9% ширины зоны) вдвое крупнее числа
# корзин (2.6–2.8%) и выигрывает попарно 81:12 — а обнаружений добавляет 0–2. Ранг члена по
# величине разброса НЕ равен его ценности как детектора. Замерено дважды, на разных наборах.
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
