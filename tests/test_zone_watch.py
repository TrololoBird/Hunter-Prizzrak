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


def test_stop_buffer_is_a_fraction_and_rejects_percent_scale() -> None:
    """★ Имя `buffer_pct` было ложью: поле хранит ДОЛЮ (0.02 == 2%), и подстановка «2.0» молча
    давала лонговый стоп `lo * (1 - 2.0)` — ОТРИЦАТЕЛЬНУЮ цену, а шортовый — втрое выше уровня.
    Конфиг защищён границами поля, вызов из кода не был — теперь не пройдёт и он.
    """
    import pytest

    from hunt_core.prizrak.zone_watch import _stop_for

    assert _stop_for(100.0, 110.0, buffer_frac=0.02, direction="long") == pytest.approx(98.0)
    assert _stop_for(100.0, 110.0, buffer_frac=0.02, direction="short") == pytest.approx(112.2)
    for bad in (2.0, 100.0, 0.0, -0.02):
        with pytest.raises(ValueError):
            _stop_for(100.0, 110.0, buffer_frac=bad, direction="long")
    # реальное значение конфига обязано быть долей и проходить
    assert 0.0 < float(_CFG.stop_buffer_pct) < 0.5


def test_rr_floor_blocks_a_handoff_with_broken_geometry() -> None:
    """★ Живой дефект SOL 2026-07-25: вотчер заводил РЕАЛЬНЫЕ сделки в обход дисциплины RR.

    Зона 69.32–74.35 (7.26% ширины), стоп за структуру 67.93, tp1 75.41. От НИЗА полосы RR 1:4.38,
    от ВЕРХА — 1:0.17 при требовании курса 1:3. Путь эмиссии считает RR по худшему заливу
    (orchestrator._rr_conservative) и режет по cfg.min_rr; вотчер не делал ни того, ни другого.
    Алерт при этом обязан уйти — «уровень есть уровень», решает читатель.
    """
    from hunt_core.prizrak.zone_watch import _entry_band, _rr_worst_fill

    assert _rr_worst_fill(direction="long", entry_lo=69.32, entry_hi=74.35,
                          stop=67.93, tp1=75.41) == 0.17
    assert float(_CFG.min_rr) > 0.17, "такая геометрия обязана не проходить пол"

    state: dict[str, Any] = {}
    z = _zone(69.32, 74.35, poc=73.10)
    setups = _setups(perezakup=z, long_targets=[75.41])
    _seed(state, setups=setups)
    out = _run(state, price=73.5, setups=setups)
    assert [e.event for e in out] == ["zone_entry"], "алерт уходит"
    assert (state.get("signals") or {}) == {}, "но сделка НЕ регистрируется"

    # ТВХ якорится на ПОК, а не на всю зону (стр.30) — полоса сузилась с 7.26% до 1.71%
    lo, hi = _entry_band({"lo": 69.32, "hi": 74.35, "poc": 73.10, "direction": "long"})
    assert (lo, hi) == (73.10, 74.35)


def test_a_zone_that_flickers_back_does_not_re_alert() -> None:
    """★ Живой дефект SOL: zone_entry «перезакуп» ушёл в чат дважды (14:16 и 14:25).

    `seeding` был на весь СИМВОЛ, поэтому зона, которой нет в памяти, при непустом символе алертила
    сразу. Карта дрожит и зона мигает: пропала на тик — вернулась — засчиталось как свежий вход.
    Алерт обязан соответствовать НАБЛЮДАЕМОМУ переходу.
    """
    state: dict[str, Any] = {}
    pk = _zone(190.0, 200.0, poc=196.0)
    both = _setups(perezakup=pk, short=[_zone(230.0, 240.0)], long_targets=[260.0, 280.0])
    _seed(state, setups=both)                                   # цена далеко, засеяно молча
    first = _run(state, price=195.0, setups=both)               # НАСТОЯЩИЙ вход — алертит
    assert [e.event for e in first] == ["zone_entry"]

    only_short = _setups(short=[_zone(230.0, 240.0)], short_targets=[200.0])
    assert _run(state, price=195.0, setups=only_short) == []     # перезакуп «мигнул» — исчез
    # …и вернулся, цена всё это время внутри него: НОВОГО перехода не было ⇒ молчим
    assert _run(state, price=195.5, setups=both) == []
