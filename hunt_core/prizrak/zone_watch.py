"""Zone watcher — turns the PRIZRAK zone MAP into watched limit setups with approach/entry alerts.

The deep card's zone map (``prizrak/setups.py``) is a DISPLAY product, and the tracker keys a signal
by ``SYMBOL:direction`` (``tracker._key``) — so it structurally cannot hold перезакуп AND добор as two
separate longs. This module is the layer in between:

* remembers the **actionable local zones** per symbol (перезакуп + добор + ближний шорт — the ones the
  author actually places limits on; снайпер/спот are context, not live limits);
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
_MAX_ZONES = int(os.getenv("HUNT_ZONE_MAX_PER_SYMBOL", "5") or 5)
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
    return {
        "kind": kind,
        "direction": direction,
        "lo": lo,
        "hi": hi,
        "anchor": anchor,
        "poc": float(poc) if isinstance(poc, (int, float)) else None,
        "by_fact": bool(z.get("by_fact")),
        "targets": tgts,
    }


def _actionable_zones(setups: dict[str, Any]) -> list[dict[str, Any]]:
    """The LOCAL horizon's live-limit zones: 🟢 перезакуп · 🟡 добор · 🔴 ближний шорт.

    Снайпер/спот horizons are deliberately excluded — they are deep context levels (often tens of
    percent away), not limits the author sits on; watching them would only generate noise.
    """
    _hz = (setups.get("horizons") or {}).get("local") if isinstance(setups, dict) else None
    hz = _hz if isinstance(_hz, dict) else {}
    if not hz:
        return []
    _lt = hz.get("long_targets")
    long_t = _lt if isinstance(_lt, list) else []
    _st = hz.get("short_targets")
    short_t = _st if isinstance(_st, list) else []

    out: list[dict[str, Any]] = []
    pk = hz.get("perezakup")
    if isinstance(pk, dict):
        rec = _mk_zone(pk, kind="перезакуп", direction="long", targets=long_t)
        if rec is not None:
            out.append(rec)
    for z in (hz.get("dobor") or [])[:2]:
        if isinstance(z, dict):
            rec = _mk_zone(z, kind="добор", direction="long", targets=long_t)
            if rec is not None:
                out.append(rec)
    for z in (hz.get("short") or [])[:2]:
        if isinstance(z, dict):
            rec = _mk_zone(z, kind="шорт", direction="short", targets=short_t)
            if rec is not None:
                out.append(rec)
    return out[:_MAX_ZONES]


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
) -> None:
    """Price entered the zone → register it as a real tracked trade so SL/TP follow-ups take over.

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
            return
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
            return
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
    except Exception:  # noqa: BLE001 — a tracking handoff must never break the tick
        LOG.exception("zone_watch_handoff_failed", symbol=sym)


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
                out.append(_followup("zone_entry", sym, z, price=price, stop=stop, dist=0.0, now=now))
                _handoff(state, sym, z, price=price, stop=stop, now=now, cfg=cfg)
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
