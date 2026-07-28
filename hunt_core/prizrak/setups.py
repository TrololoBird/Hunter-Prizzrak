"""Мульти-горизонт PRIZRAK-сетапы — «4 сетапа на актив» в стиле реальных постов автора
(локальный / недельный / снайпер / спот), КАЖДАЯ зона ПОК-якорена.

Слой ДЕРИВАЦИИ для формата постов (``format_post.py``). Заменяет одно-ТФ отображение
``compute_interest_zones`` там, где нужна карта зон в грамматике автора. Переиспользует ту же
машинерию накоплений/ПОК — ничего в движке эмиссии не меняет:

* ``find_accumulation_zones`` (accumulation.py) — боксы 4+ касаний;
* ``zone_poc`` (poc.py) — ПОК/VAH/VAL, натянутый на бары структуры (стр.26);
* ``_split_below_above`` (orchestrator) — декомпозиция straddle-бокса на грани;
* ``_level_already_worked`` / ``detect_level_saw`` — курс стр.25/31/28.

Что нового против ``compute_interest_zones``:

1. **ПОК на КАЖДОЙ зоне** — фикс замеренного бага (BCH: лонг-вход был ВЕРХ бокса 218 вместо ПОК
   внизу 196). Лонг-якорь = ПОК (стр.30 «надёжнее всего брать от уровня ПОК»), не край у цены.
2. **Мульти-горизонт** — локальный (4h) и недельный (1d/1w) считаются ОБА, не fallback-один-ТФ.
   Спот-горизонт добавляет формат-слой из ``native.spot_ladder`` (полная 10-лет история, ATL).
3. **Грамматика зон**: 🟢 перезакуп (сильнейшая ПОК-зона) · 🟡 добор (ближние опорные) · 🔴 шорт.
4. **«По факту»** — контр-трендовые/отработанные зоны помечаются ``by_fact=True`` (не дропаются, как
   в авто-сигнале — автор именно так и торгует «по факту слома»).
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.grid import zone_lines
from hunt_core.prizrak.orchestrator import (
    _INTEREST_ZONE_MAX_WIDTH_PCT,
    _level_already_worked,
    _split_below_above,
    _tf_lookback_map,
)
from hunt_core.prizrak.poc import zone_poc
from hunt_core.prizrak.structure import bars_from_ohlcv
from hunt_core.prizrak.traps import detect_level_saw

# Horizon → ordered TF preference (first with usable zones wins per horizon). Local = the near
# 4h/1h map the trader trades intraday-to-swing; weekly = the 1d/1w levels his «шорт от недельного»
# / «глобальный» setups anchor to. Spot is merged separately from the full-history spot ladder.
# Разбор ASTR (2026-07-25) показал, что автор работает на ТРЁХ горизонтах сразу и называет их
# явно: «уровень поддержки 4ч ТФ — 0.005059», «ближайший уровень сопротивления 0.005170» и
# «лонг от уровня поддержки 1ч ТФ». Ключевое — на кадре 27 он ПЕРЕКЛЮЧАЕТСЯ на 15-минутный
# график и размечает 0.005177/0.005165 именно там. Без внутридневного горизонта этот уровень
# физически не выражался: на 4ч его нет, на 1ч он размазан в зону 0.005093–0.005282 шириной
# 3.71%, а на 15м это 0.005150–0.005186 — 0.70% и 56 касаний, почти точно его кромки.
#
# 1ч — СВОЙ горизонт, а не запасной для 4ч. Раньше он стоял фолбэком в паре ("4h","1h"), то есть на
# любом ликвидном символе не смотрелся никогда. Между тем автор публикует разметку именно на нём
# (график BTCUSDT.P 1ч от 2026-07-25) и в тексте разделяет слои прямо: «нет ЧАСОВЫХ/4ч диверов»,
# «нет ЧАСОВЫХ разворотных структур». Измерено на том же BTC: его линия 65 609,1 — это ПОК часовой
# зоны 65462.9–65658.0, равный 65 610,5 (расхождение 0.002%), а 64 754,0 — ПОК зоны 64500.3–64961.0
# (64 678,8). На 4ч этих зон нет вовсе, и уровни подбирались кромками соседних боксов и строками
# спот-лестницы. Тот же класс дефекта, что вскрыл разбор ASTR: отсутствующий горизонт неотличим от
# пустого, пока не с чем сверить.
_HORIZONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intraday", ("15m", "5m")),
    ("hourly", ("1h",)),
    ("local", ("4h",)),
    ("weekly", ("1d", "1w")),
)
_LADDER_MAX = 3

# Level-map lookback per TF. The tier CANDIDATE lookback (meso=60 → floor 120 ≈ 20 days on 4h) is far
# too short for a LEVEL MAP: it clips deep-but-recent support out of the window, so nothing below price
# surfaces. The case that produced these numbers (BCH snapshot, 2026-07-22 — a dated observation, not a
# standing fact: price has since returned to that band): the author's headline 🟢190–205 ПОК196 base had
# last traded ~127×4h bars back, JUST outside a 120-bar window, so a win=120 map showed only shorts above
# price while win≈250 surfaced the 196 база cleanly. The general rule is what matters — a level map must
# outlive the swing that left the level, and the author explicitly works from a fuller chart («проверь,
# полный ли график»). These windows cover the current swing structure's whole accumulation without
# reaching into a prior regime; ``raw[-lookback:]`` is a no-op when fewer bars exist.
_SETUP_LOOKBACK: dict[str, int] = {"5m": 500, "15m": 400, "1h": 360, "4h": 300, "1d": 365, "1w": 260}


def _formed_at(z: dict[str, Any]) -> int | None:
    """Индекс бара, на котором структура зоны ДОСТРОИЛАСЬ (``last_touch_idx``), или None.

    Всё, что происходило ДО него, — это касания, которые зону и построили; «уровень уже
    отработал» может относиться только к тому, что было ПОСЛЕ.
    """
    v = z.get("last_touch_idx")
    return int(v) if isinstance(v, (int, float)) else None


def _course_flags(
    bars: list[dict[str, float]], *, level: float, side: str, since: int | None = None
) -> dict[str, Any]:
    """Курс стр.25/31/28 verdict at ``level``: reacted-off (worked) / «пила» (saw) / limit_ok.

    ``since`` — индекс, с которого считаются отработки (см. :func:`_formed_at`). Без него счёт
    шёл по ВСЕМУ окну, и критерий противоречил сам себе: ``find_accumulation_zones`` требует
    ``accumulation_min_touches`` касаний, то есть зоной объект становится ИМЕННО по реакциям, —
    а затем ``worked >= 1`` его за эти же реакции дисквалифицировал. Замер 2026-07-27 (топ-30
    символов по обороту, живой CCXT) показал вырождение, растущее с длиной окна:

    | ТФ  | сопротивления | поддержки |
    |-----|---------------|-----------|
    | 15m | 60.7%         | 60.0%     |
    | 1h  | 78.8%         | 88.6%     |
    | 4h  | **97.0%**     | **100%**  |
    | 1d  | **97.4%**     | **100%**  |

    На 4ч окно = 300 баров ≈ 50 дней, там отреагировал хоть раз ЛЮБОЙ уровень. Метка, стоящая
    на всём, не отделяет ничего (ровно то, чем начинается докстрока :func:`_tag_by_fact`).
    ``saw`` передаётся ВСЁ окно, но это ни на что не влияет и «по всему окну» она НЕ считается:
    ``traps.detect_level_saw`` смотрит только последние ``traps._SAW_WINDOW_BARS`` (=12) баров,
    поэтому ``since`` ей не нужен. Прежняя редакция этой строки утверждала обратное — исправлено
    2026-07-27. По существу член всё равно ничего не решает, и это подтверждено ТРЕМЯ
    независимыми замерами: 2 срабатывания из 106, 1 из 138, 3 из 338.
    """
    scan = bars[since:] if since is not None and 0 <= since < len(bars) else bars
    worked = _level_already_worked(scan, level=level, direction=side)
    saw = detect_level_saw(bars, level=level)
    return {"worked": int(worked), "saw": bool(saw), "limit_ok": worked < 1 and not saw}


def _num(x: Any) -> float | None:
    """Numeric-or-None narrowing for the VRVP outputs (poc/val/vah can be None with no profile)."""
    return float(x) if isinstance(x, (int, float)) else None


def _fact_reason(
    flags: dict[str, Any], *, side: str, bias: str, is_perezakup: bool
) -> tuple[bool, str]:
    """«По факту» verdict + reason for a zone. Two regimes:

    * **Перезакуп** (author's PRIMARY ПОК re-buy): a prior reaction is EXPECTED — you re-buy a level
      that held — and does NOT disqualify the limit. Here the video refines PDF стр.31 (practicum:
      он лимитно перезакупает ПОК даже после реакции). So flag it «по факту» ONLY when it is counter
      to the HTF bias — «шорты держу, по ключевым зонам добираю лонги … по факту» (BTC/ETH обзор).
    * **Добор / шорт rung** (a fresh set-and-forget limit): стр.31/28 hold — a worked level is «только
      по слому», a sawn one is waited out; counter-trend is «по факту слома». All → flagged, not dropped.
    """
    counter = (side == "long" and bias == "short") or (side == "short" and bias == "long")
    if is_perezakup:
        return (counter, "против тренда" if counter else "")
    if flags.get("saw"):
        return (True, "пила")
    if int(flags.get("worked") or 0) >= 1:
        return (True, "отработан")
    return (counter, "против тренда" if counter else "")


def _zone_view(
    raw_window: list[list[float]],
    bars: list[dict[str, float]],
    z: dict[str, Any],
    *,
    side: str,
    cfg: PrizrakConfig,
    bias: str,
) -> dict[str, Any]:
    """A добор/шорт rung: **value area** of the box + its ПОК + course flags + by_fact.

    Порядок у автора — ЗОНА → индикатор НА ней → УРОВЕНЬ: структурный бокс это ВХОД профиля, а
    торгуемый уровень — его ВЫХОД (курс стр.24 и кейс ZEN стр.31 разделяют их прямо: «зона и POC —
    два раздельных объекта отработки»). Перезакуп это уже делал (:func:`_perezakup_view` сужает
    базу до [VAL, VAH]), а добор и шорт публиковали СЫРЫЕ кромки бокса, выбрасывая vah/val, —
    то есть вход индикатора вместо выхода.

    Сужение применяется ТОЛЬКО когда value area реально пересекает бокс: декомпозированная
    straddle-полоса несёт индексы родительской структуры, поэтому её профиль считается по чужим
    барам и может лежать целиком вне полосы — тогда остаётся бокс, а не подставная область (I-6).
    ПОК показывается лишь внутри итоговой полосы. Вход — ПОК, иначе кромка (long=hi, short=lo)."""
    lo, hi = float(z["lo"]), float(z["hi"])
    info = zone_poc(raw_window, zone=z, cfg=cfg)
    poc, val, vah = _num(info.get("poc")), _num(info.get("val")), _num(info.get("vah"))
    if val is not None and vah is not None and val < vah and not (vah < lo or val > hi):
        n_lo, n_hi = max(lo, val), min(hi, vah)
        if n_lo < n_hi:
            lo, hi = n_lo, n_hi
    # Неустойчивый ПОК (бимодальный профиль) не годится в якорь — это ложная точность; падаем
    # на кромку, как при отсутствии ПОК. Флаг считает poc._poc_is_stable по перескоку при смене
    # разбиения: измерено на LTC 40.78–45.59, где якорь гулял на 5.7%.
    stable = bool(info.get("poc_stable", True))
    poc_in = poc is not None and lo <= poc <= hi and stable
    # Ордерная сетка внутри полосы (grid.zone_lines). Ключевая линия — та, которую автор называет
    # «ключевым уровнем» — вытесняет ПОК в роли якоря входа: замерено на его же боксе 17–18.07,
    # где ПОК профиля лежит на 1.8% выше названной им ключевой. ПОК остаётся якорем только когда
    # сетки нет (узкая полоса, мало баров структуры).
    lines = zone_lines(raw_window, zone=z, band=(lo, hi), cfg=cfg)
    key_px = next((float(ln["price"]) for ln in lines if ln.get("key")), None)
    edge = key_px if key_px is not None else (
        poc if (poc_in and poc is not None) else (hi if side == "long" else lo)
    )
    flags = _course_flags(bars, level=edge, side=side, since=_formed_at(z))
    by_fact, reason = _fact_reason(flags, side=side, bias=bias, is_perezakup=False)
    return {
        "lo": round(lo, 8), "hi": round(hi, 8),
        "poc": (round(poc, 8) if (poc_in and poc is not None) else None),
        "poc_unstable": bool(poc is not None and lo <= poc <= hi and not stable),
        "touches": int(z.get("touches") or 0), "entry": round(edge, 8),
        "lines": lines,
        "by_fact": by_fact, "fact_reason": reason, **flags,
    }


def _perezakup_view(
    raw_window: list[list[float]],
    bars: list[dict[str, float]],
    base: dict[str, Any],
    *,
    cfg: PrizrakConfig,
    bias: str,
    price: float,
) -> dict[str, Any] | None:
    """🟢 Перезакуп = the **value area [VAL, VAH] of the dominant volume base**, anchored at ПОК.

    This is the author's «перезакуп лонга … здесь ПОК крупной структуры» (стр.30): re-buy the
    volume core of the whole base, NOT a tight near-price box. Measured fix — BCH gave the box top
    218; the base's value area is 190–205 with ПОК 196, exactly the author's zone. Falls back to the
    box range when VRVP has no profile (crypto-spot etc.).

    A re-buy band lives BELOW price, so both edges are clipped there: the base's BOX sits below
    price, but the structure bars it spans may wick above, which drags VAL/VAH/ПОК up with them.
    Returns ``None`` when the whole value area sits above price — that base has no re-buy band left,
    and inventing one by re-ordering the edges would advertise a "buy zone" you can only enter by
    buying INTO resistance (measured on ARPA: VAL 0.008686 > price 0.008320 rendered as a
    «перезакуп 0.008320–0.008686», i.e. a long entry above spot). ПОК is likewise reported only when
    it lands inside the final band — never a foreign ПОК (I-6), same guard as :func:`_zone_view`."""
    lo_box, hi_box = float(base["lo"]), float(base["hi"])
    info = zone_poc(raw_window, zone=base, cfg=cfg)
    poc, val, vah = _num(info.get("poc")), _num(info.get("val")), _num(info.get("vah"))
    hi = min(vah if vah is not None else hi_box, price)
    lo = min(val if val is not None else lo_box, hi)
    if not lo < hi:
        return None
    stable = bool(info.get("poc_stable", True))
    # На перезакупе ПОК ОСТАЁТСЯ якорем — это его первичный «перезакуп ПОК крупной структуры»
    # (стр.30), и на крупной базе с одной модой ПОК и есть узел. Сетка тут показывает, как база
    # дробится на ордера, но не переопределяет вход; переопределяет она его только у добора/шорта,
    # где полоса «корявая» и выраженной моды нет.
    lines = zone_lines(raw_window, zone=base, band=(lo, hi), cfg=cfg)
    # Широкая полоса БЕЗ надёжного якоря — это не зона входа, а диапазон. Перезакуп намеренно не
    # проходит гейт ширины (``_tight``), потому что якорь ему даёт ПОК крупной базы (стр.30) — но
    # когда ПОК бимодален, якоря нет вовсе, и публиковать нечего. Замер на живом ETH 2026-07-26:
    # дневная полоса 3361.81–3765.99 шириной 12.02% с «ПОК неустойчив», лесенка ордеров растянута
    # на 8.71% — рядом с пятнадцатиминутной зоной шириной 0.267% это объекты разного рода, и лимит
    # по ней поставить некуда.
    if (hi / lo - 1.0) * 100.0 > _INTEREST_ZONE_MAX_WIDTH_PCT and not (
        poc is not None and lo <= poc <= hi and stable
    ):
        return None
    anchor = poc if (poc is not None and lo <= poc <= hi and stable) else hi
    flags = _course_flags(bars, level=anchor, side="long", since=_formed_at(base))
    by_fact, reason = _fact_reason(flags, side="long", bias=bias, is_perezakup=True)
    return {
        "lo": round(lo, 8), "hi": round(hi, 8),
        "poc": (round(poc, 8) if (poc is not None and lo <= poc <= hi and stable) else None),
        "poc_unstable": bool(poc is not None and lo <= poc <= hi and not stable),
        "touches": int(base.get("touches") or 0), "entry": round(anchor, 8),
        "lines": lines,
        "by_fact": by_fact, "fact_reason": reason, **flags,
    }


def _rank_rungs(
    src: list[dict[str, Any]], *, price: float, side: str, n: int = _LADDER_MAX
) -> list[dict[str, Any]]:
    """Какие ``n`` ступеней публиковать: БЛИЖНЯЯ + сильнейшие по объёму, на выходе — по цене.

    ⚠ Раньше здесь стояла чистая сортировка ПО БЛИЗОСТИ (``sorted(..., key=lambda z: z["lo"])``
    у шорта, ``key=z["hi"], reverse=True`` у добора) с срезом ``[:_LADDER_MAX]``. Она игнорирует
    силу уровня — ровно то, что курс стр.22 называет определяющим («сила уровня определяется ТФ и
    объёмом») и что этот же модуль уже цитирует в :func:`_dedupe_horizons`. Замер на трёх реальных
    постах 2026-07-27 показал, чем это стоило: зона, которую автор публикует как ОСНОВНОЙ сетап,
    оказывалась за срезом — ARB 4-я и 5-я из 5, KAS 7-я из 8, BTC (Pavel M, шорт 66 610–67 130,
    найден на 1д с перекрытием 92%) — вне тройки. Сортировка по объёму поднимала её во 2–3.

    Но и чистый объём неверен: ближний уровень автор называет ВСЕГДА — это решение на ближайшие
    часы. В одном обзоре Pavel M держит и ближние (🟡 1844–1858, 1883), и глубокую ключевую
    (🟢 1771–1804, «стоят лимитки»). Поэтому ближняя ступень занимает место безусловно, а
    остальные ``n-1`` разыгрываются по силе.

    Сила = ``касания × объём``. A/B по четырём размеченным постам (2 автора, 19 зон, 2026-07-27):
    объём 10/19 · касания 11/19 · **касания×объём 11/19**. ⚠ Разница — ОДНА зона (KAS 0.03106:
    10 касаний, но наименьший объём в наборе), то есть замер выбор НЕ решает. Ключ взят по
    согласованности: ровно им ``grid.zone_lines`` выбирает КЛЮЧЕВУЮ линию, и там он проверен
    против уровня, который автор назвал ключевым вслух. Два места одного модуля, меряющие «силу»
    разными формулами, — это будущее расхождение карты с сеткой внутри неё.
    """
    if not src:
        return []
    edge = "hi" if side == "long" else "lo"
    near = min(src, key=lambda z: abs(float(z[edge]) - price))
    rest = sorted(
        (z for z in src if z is not near),
        key=lambda z: -(float(z.get("touches") or 0) * float(z.get("zone_volume") or 0.0)),
    )
    return sorted([near, *rest[: max(0, n - 1)]], key=lambda z: float(z["lo"]))


def _tight(side: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Limit-band width gate (стр.31 «вход по факту касания» must be a tight band, not a 12% box)."""
    narrow = [z for z in side if float(z.get("width_pct") or 0) <= _INTEREST_ZONE_MAX_WIDTH_PCT]
    return narrow or (sorted(side, key=lambda z: float(z.get("width_pct") or 0))[:1] if side else [])


def _horizon_zones(
    raw: list[list[float]], *, price: float, cfg: PrizrakConfig, use_tf: str, bias: str
) -> dict[str, Any] | None:
    """🟢 перезакуп (value area of the volume base) · 🟡 добор (tight support above it) · 🔴 шорт."""
    lookback = max(_SETUP_LOOKBACK.get(use_tf, 200), _tf_lookback_map(cfg).get(use_tf, 120))
    window = raw[-lookback:]
    bars = bars_from_ohlcv(window)
    if not bars:
        return None
    # Бюджет боксов-кандидатов. Это НЕ «сколько печатать»: на выходе всё равно ``_LADDER_MAX``
    # ступеней на сторону плюс один перезакуп, — а «из чего выбирать». Поэтому подъём 8 → 12
    # измеряется как БЕСПЛАТНЫЙ, и именно это его и оправдывает.
    #
    # ⚠ ОБОСНОВАНИЕ ПЕРЕПИСАНО 2026-07-27, ПРЕЖНЕЕ БЫЛО НЕГОДНЫМ. Оно ссылалось на «превышение
    # над случайным +50.1 → +50.5 п.п.», а аудит того же дня показал, что эта величина
    # неизмерительна: нулевая модель брала поле из НАШЕЙ ЖЕ карты, и пустая полоса на −86% от
    # спота, не ловящая ни одного из 123 уровней автора, покупала +12.3 п.п. — в 30 раз больше
    # заявленного здесь эффекта. Прибор починен (`score_vs_razbor._control_recall`), правка
    # перемерена с нуля.
    #
    # Замер (I-7) на ПОЧИНЕННОМ приборе, 15 кейсов двух авторов, кадры продовой глубины
    # (`OHLCV_LIMIT`), НЕНАСЫЩЕННЫЕ допуски (при `--tol 1.0` recall упирается в потолок и
    # «не изменился» ничего не значит):
    #
    #   max_zones=8  → 84/123 (--tol 0.5) · 72/123 (--tol 0.3) · зон 976
    #   max_zones=12 → 85/123 (--tol 0.5) · 73/123 (--tol 0.3) · зон 976
    #
    # +1 уровень на ОБОИХ строгих допусках при нулевом росте числа печатаемых зон. Эффект
    # маленький и честно маленький; ценность в том, что он бесплатен. Ловится при этом
    # «база-в-базе» — ПОК BCH 196, тот самый случай, который докстрока ниже называет нерешённым.
    #
    # ⚠ 12, а не 16: 16 даёт ТЕ ЖЕ 108/123 при тех же 976 зонах, то есть разницы не создаёт, —
    # берётся меньшее изменение. И ⚠ мерить это можно ТОЛЬКО на продовой глубине кадров: на
    # прежних 500 та же правка меряется как −3.0 п.п. и +15 зон. Один и тот же код, разный знак.
    zones = find_accumulation_zones(bars, tf=use_tf, cfg=cfg, max_zones=12)
    if not zones:
        return None

    out: dict[str, Any] = {"tf": use_tf}
    # 🟢 ПЕРЕЗАКУП — value area of the strongest-VOLUME base whose box sits BELOW price (a genuine
    # re-buy support, not the current range). NOT tight-filtered: that base carries the ПОК the
    # author re-buys (стр.30). ⚠️ Precision residual: find_accumulation_zones can MERGE a deeper
    # sub-base into one box, so the ПОК lands on recent volume, not the sub-base (BCH: our ~217 vs
    # the author's June-sub-base 196). Reaching the sub-base ПОК needs «база-в-базе» detection (A4).
    below_boxes = [z for z in zones if float(z["hi"]) < price]
    base = max(below_boxes, key=lambda z: float(z.get("zone_volume") or 0)) if below_boxes else None
    if base is not None:
        # None ⇒ that base's value area sits entirely above price: no re-buy band. Дальше полоса
        # перезакупа читается из ``out["perezakup"]``, а не через отдельную переменную-копию: её
        # верх больше НЕ служит полом для доборов (см. ниже), и хранить его отдельно значило бы
        # держать наготове ровно тот ключ, который и создавал дефект.
        pk = _perezakup_view(window, bars, base, cfg=cfg, bias=bias, price=price)
        if pk is not None:
            out["perezakup"] = pk
    # 🟡 ДОБОР — tight support boxes below price: и БЛИЖНИЕ (над перезакупом), и ГЛУБОКИЕ (под ним).
    #
    # Раньше стоял гейт «hi > vah» — добор обязан лежать выше верха value area перезакупа. На
    # широком перезакупе это выбрасывало ВСЁ, что глубже, и стоило измеримо: в разборе BTC
    # 2026-07-25 автор называет своей ЕДИНСТВЕННОЙ четырёхчасовой зоной интереса полосу
    # 58 539,7–60 507,2, а у нас её не было ни на одном горизонте — перезакуп 61 806–63 982
    # накрыл собой пол и отрезал всё под ним. Он же держит зоны на РАЗНОЙ глубине одновременно
    # («по ключевым зонам буду добирать лонги», курс стр.30 — крупная база дробится на ордера).
    # Исключается только пересечение с самим перезакупом, чтобы не печатать его дважды.
    below_long, _ = _split_below_above(zones, price=price, decompose_short=False)
    pk_band = (float(out["perezakup"]["lo"]), float(out["perezakup"]["hi"])) \
        if isinstance(out.get("perezakup"), dict) else None

    def _clashes_with_perezakup(z: dict[str, Any]) -> bool:
        if pk_band is None:
            return False
        return float(z["lo"]) <= pk_band[1] and float(z["hi"]) >= pk_band[0]

    dobor_src = [
        z for z in _tight(below_long)
        if float(z["hi"]) < price and z is not base and not _clashes_with_perezakup(z)
    ]
    if dobor_src:
        out["dobor"] = [
            _zone_view(window, bars, z, side="long", cfg=cfg, bias=bias)
            for z in _rank_rungs(dobor_src, price=price, side="long")
        ]
    # 🔴 ШОРТ — сопротивления (tight above-boxes + straddle ceilings): ближнее + сильнейшие.
    _, above = _split_below_above(zones, price=price, decompose_short=True)
    short_src = _rank_rungs(_tight(above), price=price, side="short")
    if short_src:
        out["short"] = [
            _zone_view(window, bars, z, side="short", cfg=cfg, bias=bias) for z in short_src
        ]
    if "perezakup" not in out and "dobor" not in out and "short" not in out:
        return None
    # Сырые структурные уровни горизонта — сюда, а цели считаются ГЛОБАЛЬНО (:func:`_global_targets`)
    # после того, как собраны все горизонты. Считать их внутри своего горизонта было прямой ошибкой:
    # на живом ETH 2026-07-26 часовой перезакуп 3987.63–4015.59 получил «цель 4035.09», которая
    # лежит ВНУТРИ пятнадцатиминутного добора 4030.49–4041.24 — карточка одновременно велела
    # покупать 4030–4041 и фиксировать прибыль на 4035. И одна и та же стена сверху печаталась
    # тремя числами (1ч 4121.74 · 1д 4128.09 · 15м 4130.18, разброс 0.205%), потому что каждый
    # горизонт видел её со своим разрешением и не знал о соседях.
    out["_levels"] = sorted(
        {float(z[k]) for z in zones for k in ("lo", "hi") if float(z.get(k) or 0) > 0}
    )
    return out


# Насколько близко должны стоять якоря двух зон, чтобы это была ОДНА зона, увиденная на двух ТФ.
# Тот же порог, по которому вотчер узнаёт свою зону между тиками (``zone_watch._MATCH_TOL_PCT``):
# разные пороги здесь и там означали бы, что карта считает зоны одной, а состояние — двумя.
_HORIZON_MATCH_TOL_PCT = 1.0


def _anchor(z: dict[str, Any], *, side: str) -> float:
    """Торгуемая цена зоны: ключевая линия → ПОК → кромка. Тот же порядок, что и в ``entry``."""
    a = z.get("entry")
    if isinstance(a, (int, float)) and float(a) > 0:
        return float(a)
    p = z.get("poc")
    if isinstance(p, (int, float)) and float(p) > 0:
        return float(p)
    return float(z["hi"] if side == "long" else z["lo"])


def _same_zone(z: dict[str, Any], k: dict[str, Any], *, side: str) -> bool:
    """Один ли это уровень, увиденный на двух ТФ, — или два разных.

    Три условия, и третье добавлено 2026-07-27 по замеру. Близости якорей и пересечения полос
    НЕДОСТАТОЧНО: ``_HORIZON_MATCH_TOL_PCT`` — константа 1.0%, а ширина полос гуляет от 0.29%
    до 2.4%, поэтому фиксированный допуск объявляет «одной зоной» объекты, разнесённые вдвое
    больше собственного разрешения победителя.

    Живой случай (BTC, 2026-07-27, обзор Pavel M): дневная полоса 65 481–67 090 с якорем
    **66 600,43** — это названный автором уровень 66 610 с точностью **0.02%** — схлопывалась в
    часовую 66 084–66 278 (якоря расходятся на 0.497% < 1.0%, полосы пересекаются). Узкая
    побеждала по ширине, но якорь 66 600 в неё НЕ ВХОДИТ: публикуемый вход уезжал на 0.5% вниз,
    а подтверждённый дневкой уровень исчезал из карты вовсе.

    Поэтому: полоса-победитель обязана НАКРЫВАТЬ чужой якорь. Не накрывает — это два уровня,
    и схлопывать их значит выдумывать цену, которой ни один из них не считал входом (I-6).
    """
    az, ak = _anchor(z, side=side), _anchor(k, side=side)
    if az <= 0 or ak <= 0:
        return False
    if abs(az / ak - 1.0) * 100.0 > _HORIZON_MATCH_TOL_PCT:
        return False
    if float(z["lo"]) > float(k["hi"]) or float(z["hi"]) < float(k["lo"]):
        return False
    z_w, k_w = float(z["hi"]) - float(z["lo"]), float(k["hi"]) - float(k["lo"])
    narrow, foreign = (z, ak) if z_w < k_w else (k, az)
    return float(narrow["lo"]) <= foreign <= float(narrow["hi"])


def _dedupe_horizons(horizons: dict[str, Any]) -> None:
    """Одна и та же зона, найденная на нескольких ТФ, публикуется ОДИН раз — и тем сильнее.

    Горизонты считаются независимыми прогонами ``find_accumulation_zones`` по независимым окнам,
    поэтому уровень, живущий на 15м, 1ч и 4ч — то есть САМЫЙ сильный, — печатался тремя зонами с
    чуть разными кромками и читался как три РАЗНЫЕ возможности вместо одного подтверждённого
    уровня. Курс оценивает совпадение прямо: «сила уровня определяется ТФ и объёмом» (стр.22).

    Побеждает БОЛЕЕ УЗКАЯ полоса — по ней и ставится лимит, и стоп по ней короче (то же правило,
    что в ``zone_watch._dedupe``). ТФ вытесненных копий не теряются: они складываются в
    ``confirm_tf``, и это единственное, что грид-дамп умел показывать, а карта зон — нет.

    Мутирует ``horizons`` на месте: у карты один источник истины, и расхождение между тем, что
    видит карточка, и тем, что видит вотчер, — ровно тот дефект, который чинил коммит f05cc1e.
    """
    kept: list[tuple[str, dict[str, Any]]] = []  # (kind, zone)
    for hname, _tfs in _HORIZONS:
        hz = horizons.get(hname)
        if not isinstance(hz, dict):
            continue
        tf = str(hz.get("tf") or "")
        for key, side in (("perezakup", "long"), ("dobor", "long"), ("short", "short")):
            raw = hz.get(key)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            survivors: list[dict[str, Any]] = []
            for z in zs:
                if not isinstance(z, dict):
                    continue
                width = float(z["hi"]) - float(z["lo"])
                # Критерий совпадения целиком в :func:`_same_zone` — там же и замер, почему
                # близости якорей с пересечением полос НЕДОСТАТОЧНО. Здесь остаётся только
                # ограничение по ВИДУ: перезакуп это объёмная база, добор — полка внутри неё, и
                # запись содержимого добора в ячейку перезакупа печатала бы «🟢 перезакуп» над
                # чужими числами.
                twin = next(
                    (k for kind, k in kept if kind == key and _same_zone(z, k, side=side)),
                    None,
                )
                if twin is None:
                    z["confirm_tf"] = [tf] if tf else []
                    survivors.append(z)
                    kept.append((key, z))
                    continue
                # Дубль: узкая полоса вытесняет широкую, но ТФ обеих сохраняются.
                seen = list(twin.get("confirm_tf") or [])
                if tf and tf not in seen:
                    seen.append(tf)
                if width < (float(twin["hi"]) - float(twin["lo"])):
                    z["confirm_tf"] = seen
                    twin.clear()
                    twin.update(z)
                else:
                    twin["confirm_tf"] = seen
            if raw is not None:
                hz[key] = survivors if isinstance(raw, list) else (survivors[0] if survivors else None)
                if hz[key] is None or hz[key] == []:
                    hz.pop(key, None)


def _tag_by_fact(horizons: dict[str, Any]) -> dict[str, int]:
    """Сосчитать зоны «по факту». НЕ удалять их — вернуть тираж по причинам.

    ⚠ ПРЕЖНЯЯ РЕДАКЦИЯ УДАЛЯЛА, И ЭТО БЫЛО НЕВЕРНО — вопреки п.4 докстроки модуля («помечаются
    ``by_fact=True`` **не дропаются** … автор именно так и торгует "по факту слома"»). Код
    противоречил собственной задекларированной конструкции; исправлено 2026-07-27 по трём
    измеренным свидетельствам:

    1. **Замер вырождения.** На 4ч метка стояла на 97% сопротивлений и 100% поддержек, на 1д —
       97.4% / 100% (топ-30 символов). Удаление по такому признаку — это удаление всего.
       Причина критерия исправлена отдельно (``since`` в :func:`_course_flags`).
    2. **Автор оставляет такую зону на карте.** Pavel M, обзор BTC/ETH 2026-07-27: «🟡 1987-2014-2040
       — только по факту, нет хороших опорных уровней, а основной уровень уже протестирован и
       **отработан**». Тот же диагноз тем же словом — и зона публикуется с ярлыком, а не стирается.
    3. **Отработанный уровень остаётся торгуемым как НОВАЯ сделка.** PrizrakTrade, #KAS
       (его график подписан 28.07 — это UTC+3, у нас 27.07): «Текущая шортовая позиция уже забрала частичный тейк ✅ · Шорт выше, как
       отдельный новый трейд от уровня 4ч ТФ, по-прежнему остаётся актуальным».

    Дефект, ради которого удаление вводилось, — карточка печатала «🎯 План: ТВХ ★64141.3» в
    полосу, которую строкой выше сама же дисквалифицировала, — настоящий, но лечится он НЕ здесь:
    зона обязана остаться на карте с ярлыком, а из АВТОМАТИЧЕСКИХ путей входа быть исключена.
    Это сделано у потребителей, каждый на своём слое:

    * ``format_post._plan_zone`` — «по факту» не может стать ТВХ плана;
    * ``zone_watch._zone_entry`` — «по факту» не попадает в поток алертов и в передачу трекеру,
      то есть множество РЕАЛЬНО торгуемого ботом не изменилось этой правкой ни на одну зону.

    Курс так и читается: стр.31 запрещает ЛИМИТ на отработанном уровне («вход только по слому»),
    а не упоминание уровня. Возвращается тираж, чтобы карточка могла сказать «N зон только по
    факту» — «зон нет» и «зоны есть, но все по факту» это разные сообщения (I-6).
    """
    tagged: dict[str, int] = {}
    for hname in list(horizons):
        hz = horizons.get(hname)
        if not isinstance(hz, dict):
            continue
        for key in ("perezakup", "dobor", "short"):
            raw = hz.get(key)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zs:
                if isinstance(z, dict) and z.get("by_fact"):
                    reason = str(z.get("fact_reason") or "по факту")
                    tagged[reason] = tagged.get(reason, 0) + 1
        # Горизонт без единой зоны — это не горизонт; цели без своих ступеней тоже бессмысленны.
        if not any(hz.get(k) for k in ("perezakup", "dobor")):
            hz.pop("long_targets", None)
        if not hz.get("short"):
            hz.pop("short_targets", None)
        if not any(hz.get(k) for k in ("perezakup", "dobor", "short")):
            horizons.pop(hname, None)
    return tagged


# Насколько близко две цели должны стоять, чтобы считаться ОДНОЙ стеной. Замер на живом ETH
# 2026-07-26: 4121.74 / 4128.09 / 4130.18 — разброс 0.205%, это одно сопротивление с трёх
# разрешений. Из кластера берётся БЛИЖНЯЯ цена: до неё сделка дойдёт первой.
_TARGET_MERGE_PCT = 0.5


def _published_bands(horizons: dict[str, Any], side: str) -> list[tuple[float, float]]:
    """Опубликованные полосы одной стороны по ВСЕМ горизонтам."""
    keys = ("perezakup", "dobor") if side == "long" else ("short",)
    out: list[tuple[float, float]] = []
    for hz in horizons.values():
        if not isinstance(hz, dict):
            continue
        for key in keys:
            raw = hz.get(key)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zs:
                if isinstance(z, dict):
                    out.append((float(z["lo"]), float(z["hi"])))
    return out


def _merge_close(levels: list[float]) -> list[float]:
    """Схлопнуть уровни, стоящие ближе ``_TARGET_MERGE_PCT``, в один — ближний к началу списка."""
    out: list[float] = []
    for lv in levels:
        if all(abs(lv / k - 1.0) * 100.0 > _TARGET_MERGE_PCT for k in out):
            out.append(lv)
    return out


def _global_targets(horizons: dict[str, Any]) -> dict[str, list[float]]:
    """Цели — ОБЩИЕ для всей карты, а не свои у каждого горизонта.

    Направление у карты одно, значит и встречная стена одна. Цель обязана лежать ВНЕ всех
    опубликованных полос своей стороны: уровень внутри чужой зоны закупа — это чья-то ТВХ, а не
    место фиксации прибыли (замерено на живом ETH 2026-07-26, см. ``_horizon_zones``).
    """
    levels = sorted({lv for hz in horizons.values() if isinstance(hz, dict)
                     for lv in (hz.get("_levels") or [])})
    out: dict[str, list[float]] = {}

    longs = _published_bands(horizons, "long")
    if longs and levels:
        floor_up = max(hi for _lo, hi in longs)
        ups = [lv for lv in levels
               if lv > floor_up and not any(lo <= lv <= hi for lo, hi in longs)]
        merged = _merge_close(ups)[:3]
        if merged:
            out["long_targets"] = merged

    shorts = _published_bands(horizons, "short")
    if shorts and levels:
        floor_dn = min(lo for lo, _hi in shorts)
        downs = [lv for lv in reversed(levels)
                 if 0 < lv < floor_dn and not any(lo <= lv <= hi for lo, hi in shorts)]
        merged = _merge_close(downs)[:3]
        if merged:
            out["short_targets"] = merged
    return out


def _headroom(horizons: dict[str, Any], *, price: float) -> dict[str, Any] | None:
    """Ход до БЛИЖАЙШЕГО встречного уровня по всем горизонтам, вверх и вниз.

    Разбор ASTR (2026-07-25) вскрыл, что «есть уровень» и «есть сделка» — разные вопросы. Уровень
    0.005059 у автора отличный (204 касания на 700 барах, самый нагруженный в серии), и он всё
    равно отказался: «процент движения между уровнями слишком небольшой». Арифметика его отказа:
    от цены до встречного сопротивления 2.33%, а от его зоны закупа до того же уровня 7.27% —
    в 3.1 раза больше при том же типе стопа (за структуру 1–3%, PDF стр. 33).

    Карточка при этом показывала R:R 8.2, потому что мерила до ДАЛЬНЕЙ цели и не видела стену в
    137 касаний на +1.31%. Здесь считается честное расстояние до первого препятствия; решение
    ((«тесно») принимает форматтер, а гейтом эмиссии это сознательно НЕ становится — 2.33%/7.27%
    пока одно наблюдение, и порог по одной точке — ровно тот класс ошибки, от которого защищает
    ``docs/HUNTER_TARGET_SPEC.md`` §1.

    Меряется КОРИДОР, а не одна сторона: он формулирует это как «от этого уровня до этого уровня»,
    то есть его интересует ширина между ближайшей поддержкой и ближайшим сопротивлением, а не
    расстояние до одного из них. Односторонняя цифра к тому же вырождается: когда цена стоит у
    кромки зоны, «до встречного уровня 0.04%» технически верно и совершенно бесполезно.

    Returns:
        ``{"up_price", "down_price", "width_pct"}`` — ``width_pct`` только когда найдены ОБЕ
        стороны (иначе коридора нет и ширину считать не из чего); ``None``, если встречных
        уровней нет вообще (не 0.0 — I-6).
    """
    if price <= 0:
        return None
    ups: list[float] = []
    downs: list[float] = []
    for hz in horizons.values():
        if not isinstance(hz, dict):
            continue
        for key in ("perezakup", "dobor", "short"):
            raw = hz.get(key)
            zones = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zones:
                if not isinstance(z, dict):
                    continue
                for edge in ("lo", "hi"):
                    try:
                        lv = float(z[edge])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if lv > price:
                        ups.append(lv)
                    elif lv < price:
                        downs.append(lv)
    out: dict[str, Any] = {}
    if ups:
        out["up_price"] = min(ups)
    if downs:
        out["down_price"] = max(downs)
    if "up_price" in out and "down_price" in out and out["down_price"] > 0:
        out["width_pct"] = round((out["up_price"] / out["down_price"] - 1.0) * 100.0, 2)
    return out or None


def build_symbol_setups(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    price: float,
    cfg: PrizrakConfig,
    structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Мульти-горизонт карта зон (intraday + local + weekly), ПОК-якорена. Спот мёржится в форматтере.

    Returns ``{"horizons": {name: {tf, perezakup?, dobor?, short?, long_targets?, short_targets?}},
    "price": price, "headroom": {...}}`` — empty ``horizons`` when no usable zone on any TF. Never
    fabricates: a horizon is absent when its TFs have no qualifying accumulation box (I-6).
    """
    if price <= 0:
        return {"horizons": {}, "price": price}
    htf = (structure or {}).get("htf_bias") if isinstance(structure, dict) else None
    bias = str(htf.get("bias") or "").lower() if isinstance(htf, dict) else ""
    horizons: dict[str, Any] = {}
    for name, tfs in _HORIZONS:
        for use_tf in tfs:
            raw = ohlcv_by_tf.get(use_tf)
            if not raw:
                continue
            hz = _horizon_zones(raw, price=price, cfg=cfg, use_tf=use_tf, bias=bias)
            if hz is not None:
                horizons[name] = hz
                break  # first TF with usable zones wins for this horizon
    _dedupe_horizons(horizons)
    tagged = _tag_by_fact(horizons)
    targets = _global_targets(horizons)
    for hz in horizons.values():  # служебный список уровней наружу не отдаём
        if isinstance(hz, dict):
            hz.pop("_levels", None)
    out: dict[str, Any] = {"horizons": horizons, "price": float(price), "bias": bias, **targets}
    # ⚠ Ключ ПЕРЕИМЕНОВАН вместе со сменой смысла: зоны больше не удаляются, а помечаются, и
    # оставить имя ``dropped_by_fact`` значило бы соврать именем (I-6, класс «name-lies»).
    if tagged:
        out["by_fact_tagged"] = tagged
    headroom = _headroom(horizons, price=float(price))
    if headroom is not None:
        out["headroom"] = headroom
    return out


__all__ = ["build_symbol_setups"]
