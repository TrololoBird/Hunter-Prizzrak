"""Zone watcher — approach/entry alerts for the PRIZRAK map zones (prizrak/zone_watch.py).

These pin the behaviours that make the feature safe to run against a LIVE Telegram chat:

* a **cold start announces nothing** — with no memory there is no observed transition, so a restart
  cannot burst-alert every symbol already resting in a zone (that regression was measured live);
* an approach alerts **once**, and survives the per-tick JITTER of a recomputed map (the spam risk —
  coordinate-keyed identity would re-alert every 60s forever);
* entering a zone alerts once and hands the trade to the tracker (stop за структуру + цели), so
  SL/TP follow-ups take over;
* the handoff never clobbers an already-open (gated, higher-confidence) signal;
* flags re-arm only after price genuinely leaves;
* no map zones ⇒ no alerts and no stale memory (I-6).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from _deep_fixtures import make_native
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.zone_watch import evaluate_zone_watch

_CFG = PrizrakConfig.load()
_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _setups(
    *,
    perezakup: dict[str, Any] | None = None,
    dobor: list[dict[str, Any]] | None = None,
    short: list[dict[str, Any]] | None = None,
    long_targets: list[float] | None = None,
    short_targets: list[float] | None = None,
) -> dict[str, Any]:
    hz: dict[str, Any] = {"tf": "4h"}
    if perezakup is not None:
        hz["perezakup"] = perezakup
    if dobor is not None:
        hz["dobor"] = dobor
    if short is not None:
        hz["short"] = short
    if long_targets is not None:
        hz["long_targets"] = long_targets
    if short_targets is not None:
        hz["short_targets"] = short_targets
    return {"horizons": {"local": hz}, "price": 0.0, "bias": ""}


def _zone(lo: float, hi: float, *, poc: float | None = None) -> dict[str, Any]:
    return {"lo": lo, "hi": hi, "poc": poc, "entry": poc if poc is not None else hi, "by_fact": False}


def _run(state: dict[str, Any], *, price: float, setups: dict[str, Any]) -> list[Any]:
    nav = make_native(symbol="BCHUSDT", price=price, setups=setups)
    return evaluate_zone_watch(state, native=nav, now=_NOW, cfg=_CFG)


def _seed(state: dict[str, Any], *, setups: dict[str, Any], price: float = 1e9) -> None:
    """Consume the silent cold-start pass so a test can exercise real transitions.

    ``price`` defaults far above every fixture zone, i.e. "price is nowhere near" — the seed then
    records no flags and the next call behaves as a genuine first approach.
    """
    assert _run(state, price=price, setups=setups) == [], "cold start must announce nothing"


def test_cold_start_seeds_silently() -> None:
    """★ No memory ⇒ no observed transition ⇒ NO alert, even standing inside the zone.

    Regression: a restart fired one alert per symbol already resting in/near a zone (live: a
    9-message burst across 7 pinned symbols). State is seeded instead, and alerting starts next tick.
    """
    setups = _setups(perezakup=_zone(190.0, 200.0, poc=196.0), long_targets=[240.0])
    inside: dict[str, Any] = {}
    assert _run(inside, price=195.0, setups=setups) == []  # standing INSIDE → silent
    assert inside["zone_watch"]["BCHUSDT"], "state must still be recorded"
    assert (inside.get("signals") or {}) == {}, "no silent handoff on a cold start either"

    near: dict[str, Any] = {}
    assert _run(near, price=201.4, setups=setups) == []  # standing in the approach band → silent

    # …and the seeded zone does not re-announce while price merely loiters.
    assert _run(inside, price=195.5, setups=setups) == []


def test_approach_alerts_once_and_survives_map_jitter() -> None:
    """★ The anti-spam pin: one approach alert, and a jittered re-computation must NOT re-alert."""
    state: dict[str, Any] = {}
    setups = _setups(perezakup=_zone(190.0, 200.0, poc=196.0), long_targets=[240.0, 260.0])
    _seed(state, setups=setups)
    out = _run(state, price=201.4, setups=setups)  # ~0.7% above hi → inside the approach band
    assert len(out) == 1 and out[0].event == "zone_approach"
    assert out[0].payload["stop_loss"] < 190.0  # стоп за структуру, ниже низа зоны

    # Next tick: same zone, edges drifted a little (the map is recomputed every tick).
    jittered = _setups(perezakup=_zone(190.3, 200.4, poc=196.4), long_targets=[240.0, 260.0])
    assert _run(state, price=201.3, setups=jittered) == []


def test_entry_alert_hands_off_to_tracker_with_stop_and_targets() -> None:
    """Entering the zone alerts once and registers a tracked trade (стоп за структуру + цели)."""
    state: dict[str, Any] = {}
    setups = _setups(perezakup=_zone(190.0, 200.0, poc=196.0), long_targets=[240.0, 260.0, 280.0])
    _seed(state, setups=setups)
    out = _run(state, price=195.0, setups=setups)
    assert len(out) == 1 and out[0].event == "zone_entry"

    sig = (state.get("signals") or {}).get("BCHUSDT:long")
    assert isinstance(sig, dict), "entry must hand off to the tracker for SL/TP follow-ups"
    assert float(sig["stop_loss"]) < 190.0
    assert float(sig["tp1"]) == 240.0
    # A second tick inside the same zone must not re-alert.
    assert _run(state, price=195.5, setups=setups) == []


def test_handoff_never_clobbers_an_open_signal() -> None:
    """A gated emitted signal owns SYMBOL:direction — the zone handoff must not overwrite it."""
    from hunt_core.track.tracker import register_signal_open

    state: dict[str, Any] = {}
    register_signal_open(
        state,
        symbol="BCHUSDT",
        direction="long",
        price=210.0,
        setup={"entry_zone": [208.0, 212.0], "stop_loss": 200.0, "tp1": 260.0},
        lifecycle={},
        now=_NOW,
    )
    before = dict((state["signals"] or {})["BCHUSDT:long"])
    zs = _setups(perezakup=_zone(190.0, 200.0, poc=196.0))
    _seed(state, setups=zs)
    _run(state, price=195.0, setups=zs)
    after = (state["signals"] or {})["BCHUSDT:long"]
    assert after["entry_lo"] == before["entry_lo"] and after["entry_hi"] == before["entry_hi"]


def test_flags_rearm_only_after_price_leaves() -> None:
    """Approach re-alerts on a genuine second visit, not while price loiters near the zone."""
    state: dict[str, Any] = {}
    setups = _setups(perezakup=_zone(190.0, 200.0, poc=196.0))
    _seed(state, setups=setups)
    assert len(_run(state, price=201.4, setups=setups)) == 1
    assert _run(state, price=201.6, setups=setups) == []      # still loitering → silent
    assert _run(state, price=215.0, setups=setups) == []      # >3% away → re-arms, no alert itself
    assert len(_run(state, price=201.4, setups=setups)) == 1  # genuine second approach → alerts


def test_no_zones_means_no_alerts_and_no_stale_memory() -> None:
    """I-6: an empty map produces nothing and forgets the symbol (no orphan alerts later)."""
    state: dict[str, Any] = {}
    zs = _setups(perezakup=_zone(190.0, 200.0, poc=196.0))
    _seed(state, setups=zs)
    assert len(_run(state, price=201.4, setups=zs)) == 1
    assert state["zone_watch"]["BCHUSDT"]
    assert _run(state, price=201.4, setups={"horizons": {}, "price": 0.0, "bias": ""}) == []
    assert "BCHUSDT" not in state.get("zone_watch", {})


def test_short_zone_stop_sits_above_the_zone() -> None:
    """A 🔴 шорт zone's стоп за структуру goes ABOVE its high (mirror of the long case)."""
    state: dict[str, Any] = {}
    setups = _setups(short=[_zone(230.0, 240.0)], short_targets=[200.0, 190.0])
    _seed(state, setups=setups, price=1.0)  # far BELOW a short zone → nowhere near
    out = _run(state, price=228.0, setups=setups)  # ~0.9% below lo → approach band
    assert len(out) == 1 and out[0].event == "zone_approach"
    assert out[0].direction == "short"
    assert out[0].payload["stop_loss"] > 240.0
