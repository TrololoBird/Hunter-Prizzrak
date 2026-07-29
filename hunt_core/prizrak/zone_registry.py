"""Реестр зон ПРИЗРАКА: зона живёт как ОБЪЕКТ, а не пересчитывается заново каждый тик.

Зачем отдельный модуль, если состояние зон уже было. Было — два флага
(``approached_at`` / ``entered_at``) в ``zone_watch.py``, и они физически не могли накапливать
историю, потому что состояние РАЗРУШАЛОСЬ тремя способами сразу:

* ``book[sym] = fresh`` перезаписывал список каждый тик — зоны, выпавшей из карты на один тик,
  больше не существовало;
* ``book.pop(sym, None)`` стирал символ целиком, если карта на миг оказалась пустой;
* при уходе цены дальше ``_RESET_PCT`` оба флага обнулялись — то есть факт «здесь уже касались»
  стирался ровно тогда, когда он становился историей.

Замер на живом состоянии 2026-07-28: у ETHUSDT в ``zone_watch`` лежало **2 зоны**, тогда как
карточка того же символа печатала 12. Разница не случайна: ``_actionable_zones`` берёт только
горизонты ``hourly/local/weekly`` и выбрасывает «по факту». Для АЛЕРТОВ это правильно (15м-зоны
живут минуты и дали бы поток вместо сетапов), но для ОТСЛЕЖИВАНИЯ — нет: подтверждать условие
входа нечем, если 10 из 12 показанных зон нигде не фиксируются.

**Отсюда разделение, на котором держится модуль: отслеживаем ВСЕ зоны карты, алертим — только
по торгуемым.** Реестр ничего не шлёт; он копит наблюдения, на которые ссылается карточка.

Пороги здесь выведены из ГЕОМЕТРИИ САМОЙ ЗОНЫ и уже существующего конфига, а не выбраны
«разумными» (I-7: окно без замера — магическое число):

* реакция, засчитывающая касание, меряется в ШИРИНАХ ЗОНЫ (``hi - lo``) — величина
  самомасштабируется, поэтому один порог одинаково честен для зоны в 0.2% и в 5%;
* пробой считается по ``stop_buffer_pct`` — тому же буферу, за который модуль и так ставит стоп,
  так что «зона пробита» и «стоп бы сработал» не могут разойтись;
* срок жизни без подтверждения считается в БАРАХ своего горизонта, а не в минутах.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import structlog

LOG = structlog.get_logger(__name__)

# Тот же допуск, по которому карта узнаёт свою же зону между тиками (`zone_watch._MATCH_TOL_PCT`).
# Читается из того же env намеренно: разъехавшись, два модуля считали бы РАЗНЫЕ зоны одной и той же,
# и история копилась бы не туда. Это и есть механизм, которым состояние «уже алертили» уходило не к
# той зоне до появления реестра.
MATCH_TOL_PCT = float(os.getenv("HUNT_ZONE_MATCH_TOL_PCT", "1.0") or 1.0)


# Сколько подтверждённых реакций переводит зону в `confirmed`. Единица — потому что ОДНА
# наблюдённая реакция уже отличает зону, по которой рынок действительно ходит, от зоны,
# нарисованной профилем и ни разу не проверенной. Второе касание повышает `touch_count`,
# и карточка показывает его отдельно — но ждать его, чтобы назвать зону живой, значило бы
# молчать про уровень ровно тогда, когда он впервые сработал.
CONFIRM_REACTIONS = int(os.getenv("HUNT_ZONE_CONFIRM_REACTIONS", "1") or 1)

# Сколько баров своего горизонта зона живёт, ни разу не появившись в карте, прежде чем истечёт.
# Не «минуты»: у недельной зоны и у часовой разный масштаб, и общий срок в минутах означал бы
# «недельные зоны не истекают никогда, часовые — мгновенно».
EXPIRE_BARS = int(os.getenv("HUNT_ZONE_EXPIRE_BARS", "8") or 8)

# Длительность бара горизонта, в секундах. Ключи — те же имена, что производит `setups.py`
# (`intraday`/`hourly`/`local`/`weekly`); у горизонта из нескольких ТФ берётся СТАРШИЙ, потому что
# именно он задаёт масштаб зоны.
HORIZON_BAR_S: dict[str, int] = {
    "intraday": 900,  # 15m
    "hourly": 3600,  # 1h
    "local": 14400,  # 4h
    "weekly": 86400,  # 1d
}

# Сколько переходов держит очередь символа. Десять — это два глубоких цикла по пять зон; больше
# карточка всё равно не перечислит, а очередь не журнал: историю держит сам реестр.
_EVENT_QUEUE_MAX = 10

# Горизонты, которые ПОКАЗЫВАЕТ карточка. Реестр наблюдает шире (все ТФ — это его работа как
# истории), но повод для отправки обязан приходить только от видимых зон, иначе бот шлёт
# сообщение о том, чего в сообщении нет. Список держится в согласии с
# `format_post._nearest_zone_lines` и `zone_watch._ALERT_HORIZONS`.
_CARD_HORIZONS = ("hourly", "local", "weekly")

_STATUS_FORMING = "forming"
_STATUS_CONFIRMED = "confirmed"
_STATUS_WORKED = "worked"
_STATUS_BROKEN = "broken"
_STATUS_EXPIRED = "expired"


def zone_id(z: dict[str, Any], *, horizon: str) -> str:
    """Устойчивый идентификатор зоны.

    Якорь округляется до корзины шириной ``MATCH_TOL_PCT``, поэтому дрожание карты между тиками
    (та самая причина, по которой ключ по координатам плодил бы «новую» зону каждые 60 с) не
    порождает новую запись, а попадает в существующую.

    Args:
        z: Нормализованная зона (форма ``zone_watch._mk_zone``).
        horizon: Имя горизонта карты.

    Returns:
        Строковый ключ, стабильный между тиками.
    """
    anchor = float(z.get("anchor") or 0.0)
    # Логарифмическая корзина: шаг корзины ПРОПОРЦИОНАЛЕН цене, поэтому один допуск в процентах
    # одинаково работает и на BTC за 63 000, и на монете за 0.00003.
    bucket = 0
    if anchor > 0:
        import math

        bucket = int(math.floor(math.log(anchor) / math.log(1.0 + MATCH_TOL_PCT / 100.0)))
    return f"{horizon}:{z.get('direction')}:{z.get('kind')}:{bucket}"


def _match_existing(book: dict[str, Any], z: dict[str, Any], *, horizon: str) -> str | None:
    """Найти id уже известной записи для этой зоны (``None`` — зона новая).

    Совпадением считается тот же горизонт, то же направление, тот же вид и якорь в пределах
    ``MATCH_TOL_PCT``. Направление и вид в ключе обязательны: без них встречные зоны с близкими
    якорями слились бы в одну запись — та самая ошибка, из-за которой ``zone_watch._dedupe``
    сравнивает и то, и другое.
    """
    try:
        anchor = float(z.get("anchor") or 0.0)
    except (TypeError, ValueError):
        return None
    if anchor <= 0:
        return None
    for zid, rec in book.items():
        # ⚠ ГОРИЗОНТ В МАТЧЕ НЕ УЧАСТВУЕТ — и это исправление прежней редакции этого файла.
        # Там горизонт входил в сравнение, «чтобы часовая и четырёхчасовая зоны не склеились».
        # Но проект решил ровно обратное, и `zone_watch._dedupe` говорит это прямым текстом:
        # «Одна и та же зона, увиденная на двух ТФ, — это одна зона». Путь алертов их схлопывает,
        # а реестр — нет, и границы разъехались: замер на живом SOL 2026-07-28 дал **14 пар
        # дублей** на 17 записей, среди них побайтово одинаковые полосы (`добор 73.3200–73.3400`
        # дважды, `шорт 76.2750–76.4900` дважды). Каждый дубль — лишнее событие `created`, то есть
        # лишний повод для карточки об уровне, который уже отслеживается.
        if rec.get("direction") != z.get("direction") or rec.get("kind") != z.get("kind"):
            continue
        try:
            a = float(rec.get("anchor") or 0.0)
        except (TypeError, ValueError):
            continue
        if a > 0 and abs(anchor / a - 1.0) * 100.0 <= MATCH_TOL_PCT:
            return zid
    return None


def _width(z: dict[str, Any]) -> float:
    """Ширина полосы зоны в цене (``0.0`` у вырожденной)."""
    try:
        return max(0.0, float(z["hi"]) - float(z["lo"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _dist_pct(price: float, lo: float, hi: float) -> float:
    """Расстояние цены до полосы в процентах (``0.0`` — внутри полосы)."""
    if lo <= price <= hi:
        return 0.0
    edge = lo if price < lo else hi
    return abs(price / edge - 1.0) * 100.0 if edge else 0.0


def _favorable_side(z: dict[str, Any], price: float) -> float:
    """Насколько цена ушла от зоны В СТОРОНУ СДЕЛКИ, в ширинах зоны.

    Лонговая зона отрабатывает вверх, шортовая — вниз. Отрицательное значение означает, что цена
    ушла ПРОТИВ зоны, то есть в сторону её пробоя.
    """
    w = _width(z)
    if w <= 0:
        return 0.0
    if z.get("direction") == "long":
        return (price - float(z["hi"])) / w
    return (float(z["lo"]) - price) / w


def _progress_to_target(z: dict[str, Any], price: float) -> float | None:
    """Какую долю пути до ПЕРВОЙ цели прошла цена от кромки зоны (``None`` — цели нет).

    Единица курсовая: он меряет отработку уровня достижением цели («ваша цель — шорт уровень
    4ч тф; цена забирает цель», стр.24), а не абстрактным отскоком. ``1.0`` означает, что цель
    взята — по стр.25 такой уровень отработан и подлежит снятию.
    """
    tgts = [float(t) for t in (z.get("targets") or []) if isinstance(t, (int, float))]
    if not tgts:
        return None
    try:
        lo, hi = float(z["lo"]), float(z["hi"])
    except (KeyError, TypeError, ValueError):
        return None
    if z.get("direction") == "long":
        edge, tgt = hi, tgts[0]
        span = tgt - edge
        return (price - edge) / span if span > 0 else None
    edge, tgt = lo, tgts[0]
    span = edge - tgt
    return (edge - price) / span if span > 0 else None


def _adverse_pct(z: dict[str, Any], price: float) -> float:
    """Насколько цена зашла ЗА зону против сделки, в процентах от её кромки."""
    try:
        if z.get("direction") == "long":
            lo = float(z["lo"])
            return max(0.0, (lo - price) / lo * 100.0) if lo else 0.0
        hi = float(z["hi"])
        return max(0.0, (price - hi) / hi * 100.0) if hi else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


def _new_record(z: dict[str, Any], *, horizon: str, now: datetime) -> dict[str, Any]:
    """Создать запись реестра для впервые увиденной зоны."""
    return {
        "zone_id": zone_id(z, horizon=horizon),
        "horizon": horizon,
        "kind": z.get("kind"),
        "direction": z.get("direction"),
        "lo": float(z["lo"]),
        "hi": float(z["hi"]),
        "anchor": float(z.get("anchor") or 0.0),
        "poc": z.get("poc"),
        "lines": list(z.get("lines") or []),
        "by_fact": bool(z.get("by_fact")),
        "targets": list(z.get("targets") or []),
        "first_seen_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "seen_ticks": 1,
        "touches": [],
        "touch_count": 0,
        "reaction_count": 0,
        "best_progress_to_target": None,
        "worst_adverse_pct": None,
        "status": _STATUS_FORMING,
        "in_zone": False,
    }


def _refresh_geometry(rec: dict[str, Any], z: dict[str, Any], *, now: datetime) -> None:
    """Обновить полосу зоны, СОХРАНИВ историю.

    Карта пересчитывается каждый тик и полоса слегка гуляет (у перезакупа — измеренно: медиана
    9.1% ширины по низу, см. ``format_post._perezakup_line``). Раньше это означало новую запись;
    здесь — обновление координат при той же истории касаний.
    """
    # Полоса обновляется, только если новая УЖЕ прежней, — то же правило, что в
    # `zone_watch._dedupe`: «Побеждает БОЛЕЕ УЗКАЯ полоса: она точнее как лимит и даёт меньший
    # стоп». Без него запись, matched через горизонты, флип-флопила бы между часовой и
    # четырёхчасовой версией одного уровня, и вместе с полосой дёргались бы стоп и R:R.
    new_w = float(z["hi"]) - float(z["lo"])
    old_w = float(rec.get("hi") or 0.0) - float(rec.get("lo") or 0.0)
    if old_w <= 0 or new_w <= old_w:
        rec["lo"] = float(z["lo"])
        rec["hi"] = float(z["hi"])
        rec["anchor"] = float(z.get("anchor") or rec.get("anchor") or 0.0)
        rec["poc"] = z.get("poc")
        rec["lines"] = list(z.get("lines") or [])
    rec["targets"] = list(z.get("targets") or [])
    rec["by_fact"] = bool(z.get("by_fact"))
    rec["last_seen_at"] = now.isoformat()
    rec["seen_ticks"] = int(rec.get("seen_ticks") or 0) + 1


def _observe_price(
    rec: dict[str, Any], *, price: float, now: datetime, stop_buffer_pct: float
) -> str | None:
    """Занести наблюдение цены в запись: касание, реакция, пробой.

    Здесь и происходит «подтверждение условий входа»: касание само по себе ничего не доказывает —
    доказывает РЕАКЦИЯ на него. Поэтому касание фиксируется при входе цены в полосу, а зачитывается
    только когда цена ушла от зоны на ``REACTION_WIDTHS`` её ширин в сторону сделки.
    """
    inside = _dist_pct(price, rec["lo"], rec["hi"]) == 0.0
    was_inside = bool(rec.get("in_zone"))

    if inside and not was_inside:
        rec["touches"].append({"at": now.isoformat(), "price": price, "reacted": False})
        rec["touch_count"] = int(rec.get("touch_count") or 0) + 1
    rec["in_zone"] = inside

    # ⚠ Реакция существует ТОЛЬКО после касания. Без этого условия цена, которая рядом с зоной
    # никогда и не была, писала бы себе «лучшая реакция 3.0 ширины»: замер на живых зонах ETHUSDT
    # 2026-07-28 — на первом же тике, при нуле касаний, поле показывало 3.0. Ложного подтверждения
    # это не давало (зачитывать было нечего), но число было сфабрикованным, а карточка обосновывает
    # им вход. I-6: отсутствующее наблюдение обязано остаться `None`, а не превратиться в результат.
    # ⚠ РЕАКЦИЯ МЕРИТСЯ ДОСТИЖЕНИЕМ ЦЕЛИ, А НЕ ШИРИНАМИ ЗОНЫ.
    #
    # Прежний порог (`REACTION_WIDTHS`, одна ширина полосы) был МОЕЙ выдумкой: курс нигде не
    # квантифицирует «хорошую реакцию» числом. Он говорит только «увидели хорошую реакцию →
    # уровень отработан → удаляем» (стр.25) и отдельно называет, куда идёт цена от взятого
    # уровня: «ваша цель — шорт уровень 4ч тф; цена забирает цель» (стр.24).
    #
    # На узкой полосе моя мера вырождалась: у живого PAXG `intraday short 4088–4096` ширина
    # 0.196%, значит «одна ширина» — это 0.2%, то есть шум, и `confirmed` зарабатывался
    # движением, которое ничего не подтверждает. Отскок там показал 8.7 ширины — число,
    # выглядящее внушительно и не значащее ничего.
    #
    # Ширина зоны остаётся справочной величиной (её печатает карточка), но статусом больше не
    # управляет: сохраняем пройденную долю ПУТИ ДО ЦЕЛИ, а это уже курсовая единица.
    fav_frac = _progress_to_target(rec, price) if rec["touches"] else None
    if fav_frac is not None and fav_frac > 0:
        best = rec.get("best_progress_to_target")
        if best is None or fav_frac > float(best):
            rec["best_progress_to_target"] = round(fav_frac, 3)
        # Отработка засчитывается ПОСЛЕДНЕМУ незачтённому ретесту, и только по ДОСТИЖЕНИЮ ЦЕЛИ
        # (`fav_frac >= 1.0`). Курс не знает промежуточного «подтверждена»: уровень либо ещё
        # работает, либо отработал и снимается (стр.25). Всё, что между касанием и целью, — это
        # открытая сделка, а не свойство уровня.
        if fav_frac >= 1.0:
            for t in reversed(rec["touches"]):
                if not t.get("reacted"):
                    t["reacted"] = True
                    t["progress_to_target"] = round(fav_frac, 3)
                    rec["reaction_count"] = int(rec.get("reaction_count") or 0) + 1
                    break

    adv = _adverse_pct(rec, price)
    if adv > 0:
        worst = rec.get("worst_adverse_pct")
        if worst is None or adv > float(worst):
            rec["worst_adverse_pct"] = round(adv, 3)

    return _advance_status(rec, price=price, stop_buffer_pct=stop_buffer_pct)


def _advance_status(rec: dict[str, Any], *, price: float, stop_buffer_pct: float) -> str | None:
    """Перевести зону по жизненному циклу. Терминальные статусы не откатываются.

    Returns:
        Новый статус, ЕСЛИ он изменился этим вызовом, иначе ``None``. Возврат перехода — то, ради
        чего существует событийный путь: знание о смене статуса живёт ЗДЕСЬ, и вычислять его
        заново сравнением отпечатков карточки (как делал ``arbiter.wait_card_fingerprint``) значит
        держать копию знания, которая неминуемо разъедется с оригиналом. Она и разъезжалась —
        дважды за 2026-07-28: на неустойчивости корзины якоря и на чувствительности к дрожанию.
    """
    before = rec.get("status")
    _advance_status_inner(rec, price=price, stop_buffer_pct=stop_buffer_pct)
    after = rec.get("status")
    return str(after) if after != before else None


def _advance_status_inner(rec: dict[str, Any], *, price: float, stop_buffer_pct: float) -> None:
    """Собственно переход (см. :func:`_advance_status`)."""
    if rec.get("status") in (_STATUS_BROKEN, _STATUS_EXPIRED, _STATUS_WORKED):
        return

    # Пробой: цена ушла за зону против сделки дальше, чем стоял бы стоп. Порог — тот же буфер,
    # которым модуль и так отбивает стоп от кромки, поэтому «зона пробита» и «стоп бы сработал»
    # не могут разъехаться.
    if (rec.get("worst_adverse_pct") or 0.0) >= stop_buffer_pct * 100.0:
        rec["status"] = _STATUS_BROKEN
        return

    # Отработала: цена дошла до первой цели ПОСЛЕ того, как зона была подтверждена реакцией.
    tgts = [float(t) for t in (rec.get("targets") or []) if isinstance(t, (int, float))]
    if tgts and int(rec.get("reaction_count") or 0) > 0:
        t1 = tgts[0]
        reached = price >= t1 if rec.get("direction") == "long" else price <= t1
        if reached:
            rec["status"] = _STATUS_WORKED
            return

    if int(rec.get("reaction_count") or 0) >= CONFIRM_REACTIONS:
        rec["status"] = _STATUS_CONFIRMED


def _flip_broken(book: dict[str, Any], rec: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    """Пробитый уровень не умирает — он МЕНЯЕТ СТОРОНУ и снова торгуется.

    Курс стр.24, вариант 3 отработки пробитого уровня, дословно:
      «цена пробила уровень, пролетела мимо, выбила стоп и ушла выше. Спустя время — вернулась
       за тестом данного уровня **с обратной стороны — теперь для нас это уровень поддержки
       (в лонг) для новой позиции**»
    и стр.42: «Пробой сильных уровней цена подтверждает с обратной стороны».

    До этого пробой у нас был терминальным: статус ``broken`` и всё. Половина методологии ловушек
    (стр.42–51, десять страниц) сводилась к одному вето «не входить», хотя курс описывает пробой
    как ситуацию, ПОРОЖДАЮЩУЮ вход — просто зеркальный.

    Зеркальная запись наследует ПОЛОСУ (тот же ценовой диапазон — это та же структура, просто
    теперь работающая с другой стороны) и получает ЧИСТУЮ историю: касаний с новой стороны ещё
    не было, поэтому по правилу стр.25 она свежая и под лимитку годится. ПОК не наследуется:
    он считался по объёму внутри структуры до пробоя, а после смены роли якорь надо брать заново
    из профиля — фабриковать его здесь значило бы выдать старое число за новое (I-6).

    Returns:
        Созданную зеркальную запись либо ``None``, если она уже есть.
    """
    opposite = "short" if rec.get("direction") == "long" else "long"
    flipped_id = f"{rec.get('horizon')}:{opposite}:флип:{str(rec.get('zone_id','')).rsplit(':', 1)[-1]}"
    if flipped_id in book:
        return None
    flipped: dict[str, Any] = {
        "zone_id": flipped_id,
        "horizon": rec.get("horizon"),
        "kind": "флип",
        "direction": opposite,
        "lo": float(rec["lo"]),
        "hi": float(rec["hi"]),
        "anchor": float(rec.get("anchor") or 0.0),
        "poc": None,
        "lines": [],
        "by_fact": False,
        "targets": [],
        "first_seen_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "seen_ticks": 1,
        "touches": [],
        "touch_count": 0,
        "reaction_count": 0,
        "best_progress_to_target": None,
        "worst_adverse_pct": None,
        "status": _STATUS_FORMING,
        "in_zone": False,
        "flipped_from": rec.get("zone_id"),
    }
    book[flipped_id] = flipped
    return flipped


def _expire_absent(book: dict[str, Any], *, seen_ids: set[str], now: datetime) -> None:
    """Истечь зоны, которых карта не показывала дольше ``EXPIRE_BARS`` баров их горизонта.

    ⚠ Отсутствие зоны в карте — НЕ основание её забыть немедленно, и это главное отличие от
    прежнего поведения: карта дрожит, и зона регулярно пропадает на тик-другой. Немедленное
    удаление ровно этим и стирало историю.
    """
    for zid, rec in list(book.items()):
        if zid in seen_ids:
            continue
        if rec.get("status") in (_STATUS_EXPIRED, _STATUS_BROKEN, _STATUS_WORKED):
            continue
        bar_s = HORIZON_BAR_S.get(str(rec.get("horizon")), 3600)
        try:
            last = datetime.fromisoformat(str(rec.get("last_seen_at")))
        except (TypeError, ValueError):
            continue
        if now - last > timedelta(seconds=bar_s * EXPIRE_BARS):
            rec["status"] = _STATUS_EXPIRED


def update_registry(
    state: dict[str, Any],
    *,
    symbol: str,
    zones_by_horizon: dict[str, list[dict[str, Any]]],
    price: float,
    now: datetime,
    stop_buffer_pct: float,
) -> dict[str, dict[str, Any]]:
    """Обновить реестр зон символа наблюдением этого тика.

    Args:
        state: Общее состояние трекера (реестр живёт под ``state["zone_registry"][SYMBOL]``
            и персистится вместе с ним через ``track.tracker.save_tracker_state``).
        symbol: Компактный тикер (``BTCUSDT``).
        zones_by_horizon: Зоны карты по горизонтам — ВСЕ, а не только торгуемые.
        price: Текущая цена.
        now: Отметка тика.
        stop_buffer_pct: Буфер стопа ДОЛЕЙ единицы (0.02 = 2%), как в ``PrizrakConfig``.

    Returns:
        Реестр символа: ``{zone_id: record}``. Тот же объект, что лежит в ``state``.
    """
    root = state.setdefault("zone_registry", {})
    book: dict[str, Any] = root.setdefault(symbol, {})

    seen: set[str] = set()
    for horizon, zones in (zones_by_horizon or {}).items():
        for z in zones or []:
            if not isinstance(z, dict):
                continue
            try:
                if float(z["hi"]) < float(z["lo"]):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            # ⚠ Сопоставляем с СУЩЕСТВУЮЩЕЙ записью по близости якоря, и только не найдя — заводим
            # новую по корзине. Ключ-корзина сам по себе НЕУСТОЙЧИВ на границе: якорь у края
            # корзины перескакивает в соседнюю от любого шевеления. Замер 2026-07-28 на реальных
            # зонах ETH: сдвиг полос на 0.3% перевёл `добор:759` в `добор:758` — отпечаток карточки
            # менялся на ровном месте, И в реестре заводилась ВТОРАЯ запись на ту же зону, растаскивая
            # её историю надвое. Матч по близости этим не страдает: запись, однажды созданная,
            # поглощает все последующие якоря в пределах допуска и сохраняет свой id.
            zid = _match_existing(book, z, horizon=horizon) or zone_id(z, horizon=horizon)
            seen.add(zid)
            rec = book.get(zid)
            if rec is None:
                rec = _new_record(z, horizon=horizon, now=now)
                rec["zone_id"] = zid
                book[zid] = rec
                _push_event(state, symbol, zid=zid, kind="created", rec=rec, now=now)
            else:
                _refresh_geometry(rec, z, now=now)
            if price > 0:
                moved = _observe_price(rec, price=price, now=now, stop_buffer_pct=stop_buffer_pct)
                if moved:
                    _push_event(state, symbol, zid=zid, kind=moved, rec=rec, now=now)
                    # ⚠ Пробой РОЖДАЕТ зеркальный уровень, а не заканчивает жизнь структуры
                    # (курс стр.24/42 — см. :func:`_flip_broken`). Ловится именно ПЕРЕХОД
                    # (`moved`), а не статус: по статусу флип пересоздавался бы каждый тик,
                    # пока пробитая запись ещё в книге.
                    if moved == _STATUS_BROKEN:
                        flipped = _flip_broken(book, rec, now=now)
                        if flipped is not None:
                            _push_event(
                                state, symbol, zid=flipped["zone_id"],
                                kind="created", rec=flipped, now=now,
                            )
                            LOG.info(
                                "zone_flipped_after_break", symbol=symbol,
                                broken=zid, flipped=flipped["zone_id"],
                                direction=flipped["direction"],
                                band=f"{flipped['lo']:.8g}-{flipped['hi']:.8g}",
                            )

    _expire_absent(book, seen_ids=seen, now=now)
    return book


def _push_event(
    state: dict[str, Any], symbol: str, *, zid: str, kind: str, rec: dict[str, Any], now: datetime
) -> None:
    """Положить переход в очередь символа.

    Очередь, а не флаг: реестр обновляется каждый тик (~30 с), а карточка собирается раз в 300 с,
    поэтому между двумя карточками умещается до десяти тиков. Флаг «что-то менялось» потерял бы всё,
    кроме последнего события, и карточка не смогла бы назвать ПОВОД — а повод и есть то, чего ей
    не хватало: не «вот карта, снова», а «зона 4017–4033 подтвердилась вторым касанием».
    """
    # ⚠ ПОВОД ТОЛЬКО ОТ ТЕХ ГОРИЗОНТОВ, КОТОРЫЕ КАРТОЧКА ПОКАЗЫВАЕТ. Реестр наблюдательный и
    # собирает ВСЕ горизонты, включая `intraday`, — это правильно для истории. Но карточка их не
    # печатает (`format_post._nearest_zone_lines` берёт только hourly/local/weekly, потому что
    # курс велит работать с уровнем на ТОМ ТФ, где видна его структура, стр.23).
    #
    # Без этого фильтра событие от невидимой зоны отправляло карточку, в которой этой зоны нет.
    # Наблюдено на живом PAXG 2026-07-28: `intraday short 4088–4096` перешёл в `confirmed` и
    # вызвал отправку — читатель получил сообщение про уровень, которого в сообщении не было.
    #
    # Рассогласование внесено сегодня же, двумя правками в разное время: событийную ленту я делал
    # до ограничения карточки по горизонтам и потом не свёл их обратно.
    if str(rec.get("horizon")) not in _CARD_HORIZONS:
        return
    q = state.setdefault("zone_events", {}).setdefault(symbol, [])
    q.append({
        "at": now.isoformat(),
        "zone_id": zid,
        "event": kind,
        "kind": rec.get("kind"),
        "direction": rec.get("direction"),
        "lo": rec.get("lo"),
        "hi": rec.get("hi"),
        "touch_count": rec.get("touch_count"),
    })
    # Очередь ограничена: она — повод для ближайшей карточки, а не журнал. Историю держит сам
    # реестр, и дублировать её здесь значило бы растить файл состояния без читателя.
    if len(q) > _EVENT_QUEUE_MAX:
        del q[:-_EVENT_QUEUE_MAX]


def drain_events(state: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """Забрать и очистить накопленные переходы символа.

    Вызывающий обязан быть ОДИН (сборка карточки): второй потребитель получил бы пустой список и
    решил, что событий не было. Это ровно то, чем плох флаг вместо очереди.
    """
    q = (state.get("zone_events") or {}).get(symbol) or []
    if q:
        state["zone_events"][symbol] = []
    return list(q)


def peek_events(state: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """Посмотреть накопленные переходы, НЕ очищая очередь (для диагностики и рендера)."""
    return list((state.get("zone_events") or {}).get(symbol) or [])


def _find_announced(book: list[dict[str, Any]], z: dict[str, Any], *, event: str) -> dict[str, Any] | None:
    """Найти отметку объявления для этой зоны — ПО БЛИЗОСТИ якоря, а не по корзине.

    ⚠ Корзинование здесь уже пробовалось и провалилось в тот же час: якорь у края корзины
    перескакивает в соседнюю от любого шевеления, и отметка терялась при дрожании полосы на 0.3%
    (проверено сразу после написания). Это ПОВТОР ошибки, найденной и исправленной сегодня же в
    ``_match_existing`` — там корзина ``zone_id`` дала дубли записей на живом SOL.

    Сопоставление по близости этим не страдает: однажды поставленная отметка поглощает все
    последующие якоря в пределах ``MATCH_TOL_PCT``.
    """
    try:
        anchor = float(z.get("anchor") or 0.0)
    except (TypeError, ValueError):
        return None
    if anchor <= 0:
        return None
    for m in book:
        if m.get("event") != event or m.get("direction") != z.get("direction"):
            continue
        if m.get("kind") != z.get("kind"):
            continue
        try:
            a = float(m.get("anchor") or 0.0)
        except (TypeError, ValueError):
            continue
        if a > 0 and abs(anchor / a - 1.0) * 100.0 <= MATCH_TOL_PCT:
            return m
    return None


def was_announced(state: dict[str, Any], symbol: str, z: dict[str, Any], *, horizon: str, event: str) -> bool:
    """Объявляли ли уже эту зону этим событием.

    ⚠ Это НЕ дубликат одноразовых флагов ``zone_watch``: те живут в эфемерной памяти зоны и
    СБРАСЫВАЮТСЯ, как только цена отошла дальше ``_RESET_PCT``. Замер по отправленным сообщениям
    2026-07-28: подход к одной и той же полосе XAG ``55.8067–56.4600`` ушёл в канал **4 раза**
    (18:26, 18:52, 19:42, 19:58), к SOL ``74.2000–74.2300`` — тоже 4 раза; цена в зону так и не
    вошла. Механика: полоса ``_APPROACH_PCT`` (1.5%) и порог перевзвода ``_RESET_PCT`` (3.0%)
    лежат близко, и цена, качающаяся между ними, перевзводит флаг каждый круг.

    Здесь отметка живёт в РЕЕСТРЕ, который колебания цены не сбрасывают: зона, однажды
    объявленная, молчит до конца своей жизни — то есть пока не отработает или не будет пробита.
    """
    # Отметка живёт в СОБСТВЕННОЙ книге объявлений, а не в записи реестра: реестр и путь алертов
    # выводят зоны независимо, и промах поиска раньше означал «не объявляли» (см. `_announce_key`).
    book = (state.get("zone_announced") or {}).get(symbol) or []
    return _find_announced(book, z, event=event) is not None


def mark_announced(state: dict[str, Any], symbol: str, z: dict[str, Any], *, horizon: str, event: str, now: datetime) -> None:
    """Отметить, что зона объявлена этим событием (см. :func:`was_announced`).

    Пишется и в книгу объявлений (источник истины для замка), и — если запись реестра нашлась —
    в неё саму: там отметка видна вместе с историей зоны и полезна при разборе.
    """
    book = state.setdefault("zone_announced", {}).setdefault(symbol, [])
    if _find_announced(book, z, event=event) is None:
        book.append({
            "event": event, "direction": z.get("direction"), "kind": z.get("kind"),
            "anchor": float(z.get("anchor") or 0.0), "at": now.isoformat(),
        })
    rec = registry_for(state, symbol).get(zone_id(z, horizon=horizon))
    if rec is not None:
        rec[f"announced_{event}"] = now.isoformat()


def horizon_of(state: dict[str, Any], symbol: str, z: dict[str, Any]) -> str | None:
    """Горизонт, под которым зона лежит в реестре (``None``, если её там нет).

    Нужен вызывающему, у которого на руках плоский список зон без имени горизонта, а ключ реестра
    горизонт включает — иначе часовая и четырёхчасовая зоны с близкими якорями смешались бы.
    """
    for hname in HORIZON_BAR_S:
        if zone_id(z, horizon=hname) in registry_for(state, symbol):
            return hname
    return None


def registry_for(state: dict[str, Any], symbol: str) -> dict[str, dict[str, Any]]:
    """Прочитать реестр символа (пустой словарь, если зон ещё не видели)."""
    root = state.get("zone_registry") or {}
    book = root.get(symbol) or {}
    return book if isinstance(book, dict) else {}


def card_fingerprint(state: dict[str, Any], symbol: str) -> str:
    """Отпечаток «что читателю нового» — по СОСТАВУ и СТАТУСАМ зон, не по их координатам.

    Прежний ``arbiter.wait_card_fingerprint`` считался по сырой карте: якоря всех зон, огрублённые
    до 0.5%. Огрубление там есть, но полоса всё равно гуляет — у перезакупа измеренно медиана 9.1%
    ширины зоны, — и любой выход за корзину читался как «карта изменилась», после чего часовой
    таймер разрешал новую отправку. Итог по логу 2026-07-28: **142 карточки `wait`**, то есть
    примерно по одной на символ в час, при том что сделки не было ни одной, а цена до старших
    зон даже не доходила.

    Владелец сформулировал правило иначе и точнее: зона, однажды определённая, фиксируется и
    повторно не объявляется, пока не отработает. Отсюда отпечаток из ``zone_id`` (устойчив к
    дрожанию по построению — логарифмическая корзина ``MATCH_TOL_PCT``) и ``status``. Он меняется
    ровно в двух случаях: появилась НОВАЯ зона или существующая сменила статус
    (подтвердилась/отработала/пробита). Дрожание полосы его не двигает.
    """
    book = registry_for(state, symbol)
    parts = sorted(
        f"{zid}={rec.get('status')}"
        for zid, rec in book.items()
        if rec.get("status") != _STATUS_EXPIRED
    )
    return "|".join(parts)


def live_zones(state: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """Зоны символа, по которым ещё имеет смысл работать, ближние — первыми.

    Исключены терминальные статусы: отработавшая и пробитая зона больше не уровень входа, а
    история. Порядок — по расстоянию от якоря, чтобы карточке не пришлось сортировать самой.
    """
    out = [
        rec
        for rec in registry_for(state, symbol).values()
        if rec.get("status") not in (_STATUS_EXPIRED, _STATUS_BROKEN, _STATUS_WORKED)
    ]
    out.sort(key=lambda r: float(r.get("anchor") or 0.0))
    return out


__all__ = [
    "update_registry",
    "registry_for",
    "live_zones",
    "card_fingerprint",
    "drain_events",
    "peek_events",
    "was_announced",
    "mark_announced",
    "horizon_of",
    "zone_id",
    "HORIZON_BAR_S",
]
