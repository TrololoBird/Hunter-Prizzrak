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

**Anti-spam is the core design concern.** The map is recomputed every tick and zone edges JITTER, so a
coordinate-keyed identity would mint a "new" zone every tick and re-alert forever. Zones are therefore
matched to remembered ones by **anchor proximity** (``_MATCH_TOL_PCT``), each alert is one-shot, and the
approach flag only re-arms after price has left by ``_RESET_PCT``. A zone the map stops producing is
simply dropped (no orphan alerts).
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


def _stop_for(lo: float, hi: float, *, direction: str, buffer_pct: float) -> float:
    """Стоп за структуру с запасом (курс стр.33) — behind the zone's far edge, not inside it."""
    return lo * (1.0 - buffer_pct) if direction == "long" else hi * (1.0 + buffer_pct)


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


def _handoff(
    state: dict[str, Any], sym: str, z: dict[str, Any], *, price: float, stop: float, now: datetime
) -> None:
    """Price entered the zone → register it as a real tracked trade so SL/TP follow-ups take over.

    Never clobbers an already-open signal for that direction: a gated emitted setup is the
    higher-confidence object, and ``register_signal_open`` would overwrite it under the same key.
    """
    try:
        from hunt_core.track.tracker import has_active_signal, register_signal_open

        if has_active_signal(state, symbol=sym, direction=z["direction"]):
            return
        tps = list(z.get("targets") or [])
        setup = {
            "entry_zone": [z["lo"], z["hi"]],
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
        stop = _stop_for(z["lo"], z["hi"], direction=z["direction"], buffer_pct=buf)
        if dist == 0.0:
            if not rec["entered_at"]:
                rec["entered_at"] = now.isoformat()
                out.append(_followup("zone_entry", sym, z, price=price, stop=stop, dist=0.0, now=now))
                _handoff(state, sym, z, price=price, stop=stop, now=now)
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
