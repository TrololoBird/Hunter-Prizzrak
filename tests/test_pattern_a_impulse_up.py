"""G-7: Pattern A opens on an aggressive PUMP UP (method Type 1: pump → absorption →
bokovik → sweep → break), NOT a down impulse. The old code seeded on a DOWN impulse,
inverting the whole formation into a falling-knife V-recovery long.
"""
from __future__ import annotations

from hunt_core.scanner.detect.patterns import _advance_pattern_a
from hunt_core.scanner.detect.events import ohlcv_to_df
from hunt_core.scanner.detect.state import new_symbol_state


def _flat(n: int, price: float = 100.0, start_t: int = 0):
    return [[start_t + i, price, price + 0.5, price - 0.5, price, 10.0] for i in range(n)]


def _df(rows):
    return ohlcv_to_df(rows)


def test_pump_up_seeds_pattern_a() -> None:
    rows = _flat(40)
    rows.append([40, 100.0, 118.0, 100.0, 118.0, 60.0])  # aggressive green pump
    meso = _df(rows)
    macro = _df(_flat(120))
    state, setup = _advance_pattern_a(
        macro, meso, "4h", None, new_symbol_state(), now_ms=1_000.0,
    )
    assert setup is None  # only stage 0 → stage 1, not a completed setup
    assert state.get("pattern") == "A"
    assert state.get("stage") == 1


def test_dump_does_not_seed_pattern_a() -> None:
    # A pure aggressive DOWN candle must NOT open Pattern A anymore (it used to).
    rows = _flat(40)
    rows.append([40, 100.0, 100.0, 82.0, 82.0, 60.0])  # aggressive red dump
    meso = _df(rows)
    macro = _df(_flat(120))
    state, setup = _advance_pattern_a(
        macro, meso, "4h", None, new_symbol_state(), now_ms=1_000.0,
    )
    assert setup is None
    assert state.get("pattern") != "A"
