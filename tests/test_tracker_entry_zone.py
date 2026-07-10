"""Deep (prizrak) plans carry entry_lo/entry_hi but no ``entry_zone`` key.

register_signal_open must anchor the signal to the plan's entry, not to spot:
anchoring to spot leaves stop/TP on the plan's own entry, and a losing exit then
reads back as a TP hit.
"""
from __future__ import annotations

from datetime import UTC, datetime

from hunt_core.track.tracker import register_signal_open


def _deep_short_plan() -> dict[str, float | str]:
    """Shape of a level_core short plan: entry band around the catalyst,
    stop 2% above it, structural targets descending below it."""
    return {
        "direction": "short",
        "phase": "level_core_short",
        "entry_lo": 2614.75,
        "entry_hi": 2625.23,
        "stop_loss": 2672.4,
        "tp1": 2416.4775,
        "tp2": 2250.91,
        "tp3": 2218.83,
        "risk_reward": 1.9,
    }


def _open(setup: dict, *, price: float, direction: str = "short") -> dict:
    state: dict = {}
    register_signal_open(
        state,
        symbol="ETHUSDT",
        direction=direction,
        price=price,
        setup=setup,
        lifecycle={},
        now=datetime.now(UTC),
    )
    signals = state.get("signals") or {}
    assert len(signals) == 1, signals
    return next(iter(signals.values()))


def test_deep_plan_without_entry_zone_keeps_plan_entry() -> None:
    """Spot sits far below the plan's entry band; the signal must not snap to spot."""
    sig = _open(_deep_short_plan(), price=1736.725)

    assert sig["entry_lo"] == 2614.75
    assert sig["entry_hi"] == 2625.23
    snapshot = sig["delivered_levels_snapshot"]
    assert snapshot["entry_lo"] == 2614.75
    assert snapshot["entry_hi"] == 2625.23


def test_short_signal_geometry_is_consistent() -> None:
    sig = _open(_deep_short_plan(), price=1736.725)

    entry_lo, entry_hi = sig["entry_lo"], sig["entry_hi"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]

    assert sig["stop_loss"] > entry_hi, "short stop must sit above the entry band"
    assert tp1 < entry_lo, "short tp1 must sit below the entry band"
    assert tp1 > tp2 > tp3, "short targets must descend away from entry"


def test_explicit_entry_zone_still_wins() -> None:
    """The manipulation path passes entry_zone explicitly — it must not be overridden."""
    setup = dict(_deep_short_plan())
    setup["entry_zone"] = [2600.0, 2610.0]

    sig = _open(setup, price=1736.725)

    assert [sig["entry_lo"], sig["entry_hi"]] == [2600.0, 2610.0]


def test_plan_without_any_entry_falls_back_to_price() -> None:
    """A setup carrying no entry at all keeps the historical point-entry behaviour."""
    setup = {k: v for k, v in _deep_short_plan().items() if k not in {"entry_lo", "entry_hi"}}

    sig = _open(setup, price=1736.725)

    assert sig["entry_lo"] == 1736.725
    assert sig["entry_hi"] == 1736.725
