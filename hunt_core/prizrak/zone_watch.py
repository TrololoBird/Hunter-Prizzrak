"""Zone watcher — turns the PRIZRAK zone MAP into watched limit setups with approach/entry alerts.

The deep card's zone map (``prizrak/setups.py``) is a DISPLAY product, and the tracker keys a signal
by ``SYMBOL:direction`` (``tracker._key``) — so it structurally cannot hold перезакуп AND добор as two
separate longs. This module is the layer in between:

* remembers the **actionable zones** per symbol — перезакуп + добор + ближний шорт of the часовой and
  локальный horizons, the ones the author actually places limits on (снайпер/спот are context, not
  live limits; 15м lives minutes);
* computes each zone's own plan — стоп **за структуру с запасом** (стр.33, ``cfg.stop_buffer_pct``) and
  the horizon's цели — which the display map does not carry;
* alerts **once** when price APPROACHES and **once** when it ENTERS;
* on entry, hands the trade to the normal tracker lifecycle (:func:`register_signal_open` →
  armed/triggered → SL/TP follow-ups), unless a gated emitted signal already owns that direction.

**Anti-spam is the core design concern** — this sends to a live chat, so every alert must correspond to
a transition we actually OBSERVED:

* the map is recomputed every tick and zone edges JITTER, so a coordinate-keyed identity would mint a
  "new" zone every tick and re-alert forever — zones are matched to remembered ones by **anchor
  proximity** (``_MATCH_TOL_PCT``);
* each alert is one-shot and only re-arms after price has left by ``_RESET_PCT``;
* on **cold start** (no memory for the symbol) nothing is announced at all: price may have been resting
  in that zone for days, so the state is seeded silently and alerts begin from the next tick. Measured
  live before this guard: a restart fired a 9-message burst across 7 pinned symbols in 15 seconds;
* a zone the map stops producing is dropped, so the symbol re-seeds silently rather than re-announcing
  a level price is already sitting on (a missed alert is cheaper than a false one).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from hunt_core.prizrak.setups import _LADDER_MAX
from hunt_core.track.tracker import HuntFollowUp

if TYPE_CHECKING:
    from hunt_core.prizrak.config import PrizrakConfig
    from hunt_core.runtime.native_assembly import NativeAnalystView

LOG = structlog.get_logger("hunt.track.zone_watch")

# Alert when price comes within this % of a zone edge (from outside).
_APPROACH_PCT = float(os.getenv("HUNT_ZONE_APPROACH_PCT", "1.5") or 1.5)
# Past this % away the approach/entry flags re-arm (price genuinely left the area).
_RESET_PCT = float(os.getenv("HUNT_ZONE_RESET_PCT", "3.0") or 3.0)
# Two zones within this % of each other (same kind+side) are THE SAME zone across ticks — absorbs the
# per-tick jitter of a recomputed map. Too tight ⇒ duplicate alerts; too loose ⇒ a genuinely new zone
# inherits the old one's "already alerted" state.
_MATCH_TOL_PCT = float(os.getenv("HUNT_ZONE_MATCH_TOL_PCT", "1.0") or 1.0)
# Потолок зон на символ. Поднят с 5 до 9, потому что горизонтов теперь три, а не два:
# при пяти слотах часовой горизонт (перезакуп + 3 добора + 3 шорта) выбирал их все, и
# четырёхчасовые с недельными зонами молча отбрасывались после дедупа. Усечение логируется —
# «зона не алертила» и «зоны не было» обязаны различаться.
_MAX_ZONES = int(os.getenv("HUNT_ZONE_MAX_PER_SYMBOL", "9") or 9)
_ENABLED = str(os.getenv("HUNT_ZONE_WATCH", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _compact(symbol: str) -> str:
    return str(symbol or "").split(":", 1)[0].replace("/", "").upper()


def _dist_pct(price: float, lo: float, hi: float) -> float:
    """``0.0`` when price is INSIDE the zone, else the % distance to the nearer edge."""
    if lo <= price <= hi:
        return 0.0
    edge = hi if price > hi else lo
    if edge <= 0:
        return float("inf")
    return abs(price / edge - 1.0) * 100.0


def _mk_zone(z: dict[str, Any], *, kind: str, direction: str, targets: list[Any]) -> dict[str, Any] | None:
    """Normalize one map zone into a watchable record (``None`` when its geometry is unusable)."""
    try:
        lo, hi = float(z["lo"]), float(z["hi"])
    except (KeyError, TypeError, ValueError):
        return None
    if lo <= 0 or hi <= 0 or hi < lo:
        return None
    raw_anchor = z.get("entry")
    anchor = float(raw_anchor) if isinstance(raw_anchor, (int, float)) else (hi if direction == "long" else lo)
    tgts = [float(t) for t in targets if isinstance(t, (int, float))][:3]
    poc = z.get("poc")
    # Ордерные линии зоны — то, что читатель видит в карточке. Якорь (``entry``) у добора/шорта уже
    # и есть КЛЮЧЕВАЯ линия (``setups._zone_view``), так что алерт и карта говорят об одной цене;
    # остальные линии несутся, чтобы алерт мог показать всю лесенку, а не одну её ступень.
    lines = [
        round(float(ln["price"]), 8)
        for ln in (z.get("lines") or [])
        if isinstance(ln, dict) and isinstance(ln.get("price"), (int, float))
    ]
    return {
        "kind": kind,
        "direction": direction,
        "lo": lo,
        "hi": hi,
        "anchor": anchor,
        "poc": float(poc) if isinstance(poc, (int, float)) else None,
        "lines": lines,
        "by_fact": bool(z.get("by_fact")),
        "targets": tgts,
    }


# Горизонты, по которым автор реально сидит лимитами. Часовой идёт ПЕРВЫМ: его разметка публикуется
# на 1ч, и часовая зона всегда уже четырёхчасовой, а значит и стоп по ней короче. Снайпер/спот
# исключены намеренно — это дальний контекст (десятки процентов), а не живые лимиты; внутридневной
# 15м тоже: его зоны живут минуты и дали бы поток алертов вместо сетапов.
#
# «weekly» добавлен по разбору BTC 1ч от 2026-07-25: его ЕДИНСТВЕННАЯ названная зона интереса на
# старшем ТФ — полоса 58 539,7–60 507,2, и по ней у него стоят живые лимитки неделями («у меня там
# даже лимиточки ещё стояли»). Карточка её печатала, а алерта по ней не могло прийти в принципе —
# карта и поток алертов выглядели одним продуктом, будучи разными множествами.
_ALERT_HORIZONS = ("hourly", "local", "weekly")


def _dedupe(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна и та же зона, увиденная на двух ТФ, — это одна зона.

    Совпадением считается тот же порог, по которому карта узнаёт себя между тиками
    (``_MATCH_TOL_PCT``): иначе часовой и четырёхчасовой добор с почти равными якорями стали бы
    неразличимы для ``_find_stored`` и растащили бы состояние друг друга — «уже алертили» уходило
    бы не к той зоне. Побеждает БОЛЕЕ УЗКАЯ полоса: она точнее как лимит и даёт меньший стоп.
    """
    kept: list[dict[str, Any]] = []
    for z in zones:
        dup = None
        for k in kept:
            if k["direction"] == z["direction"] and k["kind"] == z["kind"] and k["anchor"] > 0 \
                    and abs(z["anchor"] / k["anchor"] - 1.0) * 100.0 <= _MATCH_TOL_PCT:
                dup = k
                break
        if dup is None:
            kept.append(z)
        elif (z["hi"] - z["lo"]) < (dup["hi"] - dup["lo"]):
            kept[kept.index(dup)] = z
    return kept


def _actionable_zones(setups: dict[str, Any]) -> list[dict[str, Any]]:
    """Live-limit zones of the ACTIONABLE horizons: 🟢 перезакуп · 🟡 добор · 🔴 ближний шорт.

    ⚠ «По факту» СЮДА НЕ ПОПАДАЕТ — и это не косметика, а граница между картой и торговлей.
    До 2026-07-27 такие зоны удалялись из карты целиком (``setups._drop_by_fact``), поэтому до
    вотчера физически не доходили. Теперь они на карте остаются — потому что оба автора их
    публикуют с ярлыком, а не стирают, — но смысл ярлыка ровно в том, что вход по ним НЕ лимитный:
    стр.31 «вход только по слому», у Pavel M — «только по факту … либо входить по алерту уже
    после теста зоны». Резервировать под них направление в трекере (``_handoff`` ключует
    ``SYMBOL:direction``) значило бы занять место настоящего лимитного сетапа сигналом, который
    сам себя объявил неторгуемым лимитом.

    ⚠ Инвариант здесь ровно один: **ни одна зона «по факту» не попадает в авто-вход**. Он
    проверен на живых данных (BTC/ETH 2026-07-27: 0 из 4 и 0 из 5). Соблазнительно было бы
    написать «торгуемое множество не изменилось» — ЭТО НЕВЕРНО и замер это показал: смежные
    правки того же коммита (``setups._rank_rungs``, ``setups._same_zone``) публикуют больше
    ЧИСТЫХ зон, и авто-вход вырос с 2 до 4–5 на символ. Меняется не фильтр «по факту», а то,
    какие зоны вообще доживают до карты.
    """
    horizons = (setups.get("horizons") or {}) if isinstance(setups, dict) else {}
    out: list[dict[str, Any]] = []
    for hname in _ALERT_HORIZONS:
        _hz = horizons.get(hname)
        hz = _hz if isinstance(_hz, dict) else {}
        if not hz:
            continue
        _lt = hz.get("long_targets")
        long_t = _lt if isinstance(_lt, list) else []
        _st = hz.get("short_targets")
        short_t = _st if isinstance(_st, list) else []

        pk = hz.get("perezakup")
        if isinstance(pk, dict) and not pk.get("by_fact"):
            rec = _mk_zone(pk, kind="перезакуп", direction="long", targets=long_t)
            if rec is not None:
                out.append(rec)
        # Срез [:2] при сортировке ближними-вперёд выбрасывал САМЫЙ ГЛУБОКИЙ добор — ровно тот,
        # ради появления которого снимался гейт `hi > vah` (setups.py), и ровно тот, на котором
        # автор сидит лимитом дольше всего. Предел теперь общий с картой: сколько ступеней
        # напечатано, столько и наблюдается.
        for z in (hz.get("dobor") or [])[:_LADDER_MAX]:
            if isinstance(z, dict) and not z.get("by_fact"):
                rec = _mk_zone(z, kind="добор", direction="long", targets=long_t)
                if rec is not None:
                    out.append(rec)
        for z in (hz.get("short") or [])[:_LADDER_MAX]:
            if isinstance(z, dict) and not z.get("by_fact"):
                rec = _mk_zone(z, kind="шорт", direction="short", targets=short_t)
                if rec is not None:
                    out.append(rec)
    kept = _dedupe(out)
    if len(kept) > _MAX_ZONES:
        LOG.info("zone_watch_truncated", kept=_MAX_ZONES, dropped=len(kept) - _MAX_ZONES,
                 dropped_kinds=[z["kind"] for z in kept[_MAX_ZONES:]])
    return kept[:_MAX_ZONES]


def _stop_for(lo: float, hi: float, *, buffer_frac: float, direction: str) -> float:
    """Стоп за структуру с запасом (курс стр.33) — behind the zone's far edge, not inside it.

    ``buffer_frac`` — ДОЛЯ, не проценты: 0.02 == 2%. Так во всём модуле (``cfg.stop_buffer_pct``
    тоже хранит долю вопреки своему имени), поэтому параметр назван честно — прежнее имя
    ``buffer_pct`` было ложью и работающей ловушкой: подстановка «2.0», прочитанной как «2%»,
    молча даёт лонговый стоп ``lo * (1 - 2.0)`` — ОТРИЦАТЕЛЬНУЮ цену, а шортовый — втрое выше
    уровня. Конфиг от этого защищён границами поля (``ge=0.01, le=0.05``), вызов из кода — не был.
    Ассерт ниже закрывает и его: масштаб процентов сюда физически не пройдёт.
    """
    if not 0.0 < buffer_frac < 0.5:
        raise ValueError(f"buffer_frac must be a fraction in (0, 0.5), got {buffer_frac!r}")
    return lo * (1.0 - buffer_frac) if direction == "long" else hi * (1.0 + buffer_frac)


def _find_stored(stored: list[Any], z: dict[str, Any]) -> dict[str, Any]:
    """Remembered state for this zone, matched by anchor PROXIMITY (map jitter), else ``{}``."""
    for s in stored:
        if not isinstance(s, dict):
            continue
        if s.get("direction") != z["direction"] or s.get("kind") != z["kind"]:
            continue
        a = s.get("anchor")
        if not isinstance(a, (int, float)) or a <= 0:
            continue
        if abs(z["anchor"] / float(a) - 1.0) * 100.0 <= _MATCH_TOL_PCT:
            return s
    return {}


def _followup(
    event: str, sym: str, z: dict[str, Any], *, price: float, stop: float, dist: float, now: datetime
) -> HuntFollowUp:
    # Key is unique PER OCCURRENCE (minute-stamped): the one-shot flags below are the real dedup, so a
    # stable key would let the tracker's cooldown wrongly suppress a genuine re-approach days later.
    key = f"{event}:{sym}:{z['direction']}:{z['kind']}:{z['anchor']:.6g}:{now:%Y%m%d%H%M}"
    return HuntFollowUp(
        event=event,  # type: ignore[arg-type]  # added to SignalEvent
        symbol=sym,
        direction=z["direction"],
        message_key=key,
        detail=z["kind"],
        price=price,
        payload={
            "zone_lo": z["lo"],
            "zone_hi": z["hi"],
            "zone_kind": z["kind"],
            "poc": z.get("poc"),
            # Ордерные линии зоны и R:R по ХУДШЕМУ заливу. Без RR сообщения для зоны, которую бот
            # ведёт, и для зоны, отвергнутой по RR, были неразличимы — читатель не мог понять,
            # считает ли модуль сетап торгуемым. Линии печатаются, чтобы алерт показывал ту же
            # лесенку, что и карточка, а не одну её ступень.
            "lines": z.get("lines") or [],
            "rr": _rr_worst_fill(
                direction=z["direction"], entry_lo=_entry_band(z)[0], entry_hi=_entry_band(z)[1],
                stop=stop, tp1=(list(z.get("targets") or []) or [None])[0],
            ),
            "by_fact": z.get("by_fact"),
            "stop_loss": stop,
            "targets": z.get("targets") or [],
            "dist_pct": dist,
            "announced": True,
        },
    )


def _entry_band(z: dict[str, Any]) -> tuple[float, float]:
    """ТВХ вокруг ПОК, а не вся зона (стр.30: «надёжнее от POC»; 2–3 ордера: на зону + на POC).

    Полоса берётся от якоря (ПОК, когда он внутри зоны) до ближайшей к рынку кромки — то есть та
    часть зоны, которую ордера реально накрывают. Без ПОК остаётся зона целиком: выдумывать якорь
    там, где профиля нет, значило бы фабриковать вход (I-6).
    """
    lo, hi = float(z["lo"]), float(z["hi"])
    anchor = z.get("poc")
    if not isinstance(anchor, (int, float)) or not lo <= float(anchor) <= hi:
        return lo, hi
    a = float(anchor)
    return (a, hi) if z["direction"] == "long" else (lo, a)


def _rr_worst_fill(
    *, direction: str, entry_lo: float, entry_hi: float, stop: float, tp1: float | None
) -> float | None:
    """R:R по ХУДШЕМУ заливу в полосе (long → hi, short → lo) — широкая полоса не льстит отношению.

    Тот же расчёт, что ``orchestrator._rr_conservative`` применяет к эмитируемым сигналам; здесь он
    нужен ровно затем же — вотчер заводит РЕАЛЬНЫЕ сделки и обязан жить по той же дисциплине.
    ``None`` при неполной геометрии — не 0.0 и не «сойдёт» (I-6).
    """
    try:
        lo, hi, sl = float(entry_lo), float(entry_hi), float(stop)
        tp = float(tp1) if tp1 is not None else 0.0
    except (TypeError, ValueError):
        return None
    if min(lo, hi, sl, tp) <= 0:
        return None
    edge = hi if direction == "long" else lo
    risk = (edge - sl) if direction == "long" else (sl - edge)
    reward = (tp - edge) if direction == "long" else (edge - tp)
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 2)


def _handoff(
    state: dict[str, Any], sym: str, z: dict[str, Any], *, price: float, stop: float,
    now: datetime, cfg: PrizrakConfig,
) -> str:
    """Price entered the zone → register it as a real tracked trade so SL/TP follow-ups take over.

    Returns:
        Исход передачи, который УХОДИТ В СООБЩЕНИЕ: ``"tracked"`` · ``"occupied"`` ·
        ``"no_target"`` · ``"rr_below_floor"`` · ``"failed"``. Раньше функция возвращала ``None``
        и все отказы были видны только в логе. Замер живого канала 2026-07-27: из трёх событий
        «🎯 ЦЕНА В ЗОНЕ» передач состоялось **ноль** (дважды направление занято, один раз цели
        не было вовсе), но все три сообщения одинаково звали «вход по факту касания» — и ни одно
        не сказало, что дальше не будет ни SL/TP, ни сообщения о закрытии. Читатель ждал
        сопровождения, которого код не собирался давать.


    Never clobbers an already-open signal for that direction: a gated emitted setup is the
    higher-confidence object, and ``register_signal_open`` would overwrite it under the same key.

    ДВЕ дисциплины курса, которых здесь раньше не было вообще (измерено на живом SOL 2026-07-25):

    * **ТВХ якорится на ПОК, а не на всю полосу** (стр.30: «надёжнее от POC»). Регистрация входа
      как ``[lo, hi]`` при зоне шириной 7.26% давала стоп в 2.01% от НИЗА и 8.63% от ВЕРХА —
      сделка сходилась только при заливе по самому дну.
    * **RR считается по ХУДШЕМУ заливу** и сверяется с полом ``cfg.min_rr``. У того же SOL:
      от низа полосы RR 1:4.38, а от верха 1:0.17 при требовании курса 1:3. Путь эмиссии эту
      дисциплину соблюдает (``orchestrator._rr_conservative`` + RR-floor), а вотчер её обходил и
      заводил РЕАЛЬНЫЕ отслеживаемые сделки с заведомо нерабочей геометрией.

    Не прошло по RR — алерт всё равно уходит (уровень есть уровень, читатель решает сам), но
    сделка не регистрируется: трекер не должен вести то, что курс торговать не велит.
    """
    try:
        from hunt_core.track.tracker import has_active_signal, register_signal_open

        if has_active_signal(state, symbol=sym, direction=z["direction"]):
            # Трекер ключуется SYMBOL:direction, поэтому перезакуп и добор физически не могут быть
            # двумя лонгами одновременно — это и есть причина, по которой модуль существует. Но
            # молчаливый выход делал невозможной сверку «сколько алертов ушло» с «сколько сделок
            # ведётся»: алерт выглядел как готовая к работе сделка, а её никто не завёл.
            LOG.info("zone_watch_handoff_skipped_occupied", symbol=sym, kind=z["kind"],
                     direction=z["direction"])
            return "occupied"
        tps = list(z.get("targets") or [])
        entry_lo, entry_hi = _entry_band(z)
        rr = _rr_worst_fill(
            direction=z["direction"], entry_lo=entry_lo, entry_hi=entry_hi,
            stop=stop, tp1=tps[0] if tps else None,
        )
        floor = float(getattr(cfg, "min_rr", 2.0) or 2.0)
        if rr is None or rr < floor:
            LOG.info(
                "zone_watch_handoff_skipped_rr", symbol=sym, kind=z["kind"],
                direction=z["direction"], rr=rr, floor=floor,
            )
            # ДВЕ разные причины, и читателю они говорят разное: «цели за зоной вообще нет»
            # (R:R не из чего считать) против «цель есть, но отношение ниже пола». Один код на
            # обе печатал бы «нет цели» под уже напечатанным списком целей.
            return "no_target" if rr is None else "rr_below_floor"
        setup = {
            "entry_zone": [entry_lo, entry_hi],
            "rr": rr,
            "stop_loss": stop,
            "tp1": tps[0] if len(tps) > 0 else None,
            "tp2": tps[1] if len(tps) > 1 else None,
            "tp3": tps[2] if len(tps) > 2 else None,
            "direction": z["direction"],
            "phase": f"zone_{z['kind']}",
            # Price IS inside the zone at this point — a real fill, not a pending limit (the ARMED
            # tier exists for the not-yet-reached case and would mis-model this one).
            "delivery_tier": "triggered",
            # Анонсом этой сделки служит сам алерт «🎯 ЦЕНА В ЗОНЕ», который тик отправляет тем
            # же проходом (`_followup(..., "announced": True)`). Без флага
            # `_cycle_reconcile._deliver_followup` резал бы ВСЕ последующие SL/TP/закрытие —
            # то есть вотчер заводил бы сделку, о судьбе которой канал не узнаёт никогда.
            "telegram_sent": True,
        }
        register_signal_open(
            state,
            symbol=sym,
            direction=z["direction"],
            price=price,
            setup=setup,
            lifecycle={},
            now=now,
        )
        LOG.info("zone_watch_handoff", symbol=sym, kind=z["kind"], direction=z["direction"], stop=stop)
        return "tracked"
    except Exception:  # noqa: BLE001 — a tracking handoff must never break the tick
        LOG.exception("zone_watch_handoff_failed", symbol=sym)
        return "failed"


def evaluate_zone_watch(
    state: dict[str, Any],
    *,
    native: NativeAnalystView,
    now: datetime,
    cfg: PrizrakConfig | None = None,
) -> list[HuntFollowUp]:
    """Approach/entry alerts for this symbol's actionable map zones (see module docstring).

    Args:
        state: The shared tracker state (zone memory lives under ``state["zone_watch"][SYMBOL]``).
        native: The typed native view — reads ``prizrak.setups`` + ``view.last_price``.
        now: Tick timestamp.
        cfg: PRIZRAK config (stop buffer); loaded when omitted.

    Returns:
        Zero or more :class:`HuntFollowUp` events (``zone_approach`` / ``zone_entry``) for the
        caller to deliver through the normal follow-up pipeline. Empty when disabled, price is
        unknown, or no zone changed state this tick.
    """
    if not _ENABLED:
        return []
    price = float(native.view.last_price or 0)
    if price <= 0:
        return []
    _setups = native.prizrak.setups
    zones = _actionable_zones(_setups if isinstance(_setups, dict) else {})
    sym = _compact(native.view.symbol)
    book = state.setdefault("zone_watch", {})
    stored = book.get(sym) or []
    if not zones:
        # The map produced nothing actionable — drop the memory so a later zone starts clean.
        book.pop(sym, None)
        return []

    if cfg is None:
        from hunt_core.prizrak.config import PrizrakConfig as _Cfg

        cfg = _Cfg.load()
    buf = float(cfg.stop_buffer_pct)

    # COLD START: with no memory for this symbol we have observed no TRANSITION — price may have been
    # sitting in that zone for days. Seed the state silently and alert from the next tick on. Without
    # this every restart fired one alert per symbol already resting in/near a zone (measured live:
    # a 9-message burst across 7 pinned symbols in 15s), which is exactly the "stale state announced
    # as a fresh event" defect the one-shot flags exist to prevent.
    seeding = not stored

    out: list[HuntFollowUp] = []
    fresh: list[dict[str, Any]] = []
    for z in zones:
        prev = _find_stored(stored, z)
        rec: dict[str, Any] = {
            **z,
            "approached_at": prev.get("approached_at"),
            "entered_at": prev.get("entered_at"),
        }
        dist = _dist_pct(price, z["lo"], z["hi"])
        stop = _stop_for(z["lo"], z["hi"], buffer_frac=buf, direction=z["direction"])
        # ТА ЖЕ логика, что и на холодном старте, но ПОЗОННО. `seeding` был на весь символ, поэтому
        # зона, которой у нас ещё нет в памяти, при непустом символе алертила сразу — хотя перехода
        # внутрь никто не наблюдал. А карта дрожит и зона МИГАЕТ: пропала на тик, вернулась — и это
        # засчитывалось как свежий вход. Измерено на живом SOL 2026-07-25: zone_entry «перезакуп»
        # ушёл в чат дважды (14:16 и 14:25), zone_approach «шорт» — тоже дважды (14:12 и 14:19).
        # Алертим только НАБЛЮДАЕМЫЙ переход: незнакомая зона, в которой цена уже стоит, засеивается.
        if not seeding and not prev and dist <= _APPROACH_PCT:
            rec["approached_at"] = now.isoformat()
            if dist == 0.0:
                rec["entered_at"] = now.isoformat()
            fresh.append(rec)
            continue
        if seeding:
            # Record where price stands now, announce nothing. A zone price is already in/near counts
            # as already-fired, so it only re-alerts after price leaves (>_RESET_PCT) and comes back.
            if dist == 0.0:
                rec["entered_at"] = now.isoformat()
                rec["approached_at"] = now.isoformat()
            elif dist <= _APPROACH_PCT:
                rec["approached_at"] = now.isoformat()
            fresh.append(rec)
            continue
        if dist == 0.0:
            if not rec["entered_at"]:
                rec["entered_at"] = now.isoformat()
                ev = _followup("zone_entry", sym, z, price=price, stop=stop, dist=0.0, now=now)
                # Исход передачи проставляется в УЖЕ созданное событие: сообщение обязано сказать
                # читателю, ведёт ли бот эту сделку дальше, — иначе «вход по факту касания» звучит
                # как сопровождаемая сделка, а SL/TP-follow-up не придёт никогда.
                ev.payload["tracking"] = _handoff(
                    state, sym, z, price=price, stop=stop, now=now, cfg=cfg
                )
                out.append(ev)
        elif dist > _RESET_PCT:
            # Genuinely left the area — re-arm both alerts for the next visit.
            rec["approached_at"] = None
            rec["entered_at"] = None
        elif dist <= _APPROACH_PCT and not rec["approached_at"]:
            rec["approached_at"] = now.isoformat()
            out.append(_followup("zone_approach", sym, z, price=price, stop=stop, dist=dist, now=now))
        fresh.append(rec)
    book[sym] = fresh
    return out


__all__ = ["evaluate_zone_watch"]
