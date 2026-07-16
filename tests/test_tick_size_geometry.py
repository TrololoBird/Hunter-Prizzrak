"""Tick-size quantization of signal geometry + adaptive price render.

Binance USDⓈ-M ticks range 1e-8 (1000SATSUSDT/DOGSUSDT) to 1.0 (YFIUSDT).
Covers: BTC-like (tick 0.1), meme (1e-7), regular alt (1e-4); conservative
rounding sides (stop away from entry, TP toward entry); fmt_price on the
sub-milli-dollar tail that a flat .6f used to collapse.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hunt_core.deliver._labels import fmt_price
from hunt_core.market import tick_registry
from hunt_core.market.tick_registry import (
    quantize_conservative,
    quantize_price,
    quantize_to_tick,
    register_ticks_from_markets,
    set_tick_sizes,
    tick_size_for,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Never leak test ticks into other tests (module-level registry)."""
    monkeypatch.setattr(tick_registry, "_TICKS", dict(tick_registry._TICKS))


# ── quantize_to_tick ──────────────────────────────────────────────────────────

def test_btc_like_tick_01() -> None:
    assert quantize_to_tick(51234.53, 0.1, mode="nearest") == 51234.5
    assert quantize_to_tick(51234.57, 0.1, mode="floor") == 51234.5
    assert quantize_to_tick(51234.53, 0.1, mode="ceil") == 51234.6


def test_meme_tick_1e7_no_float_dust() -> None:
    # 3.512345e-5 on a 1e-7 grid — Decimal path must not leave 3.5100000000001e-05
    assert quantize_to_tick(3.512345e-05, 1e-07, mode="floor") == 3.51e-05
    assert quantize_to_tick(3.512345e-05, 1e-07, mode="ceil") == 3.52e-05


def test_alt_tick_1e4() -> None:
    assert quantize_to_tick(1.234567, 0.0001, mode="nearest") == 1.2346
    assert quantize_to_tick(1.23451, 0.0001, mode="floor") == 1.2345


def test_on_grid_price_is_unchanged_any_mode() -> None:
    for mode in ("nearest", "floor", "ceil"):
        assert quantize_to_tick(51234.5, 0.1, mode=mode) == 51234.5
        assert quantize_to_tick(3.5e-05, 1e-07, mode=mode) == 3.5e-05


def test_non_positive_passthrough() -> None:
    assert quantize_to_tick(0.0, 0.1) == 0.0
    assert quantize_to_tick(100.0, 0.0) == 100.0


# ── registry + fallback ───────────────────────────────────────────────────────

def test_registry_from_ccxt_markets_filters_linear_usdt_swaps() -> None:
    markets = [
        {  # linear USDT perp → registered
            "id": "TSTUSDT", "symbol": "TST/USDT:USDT", "type": "swap",
            "spot": False, "settle": "USDT", "precision": {"price": 1e-07},
        },
        {  # spot row → skipped
            "id": "TSTUSDT", "symbol": "TST/USDT", "type": "spot",
            "spot": True, "settle": None, "precision": {"price": 1e-08},
        },
        {  # coin-margined → skipped
            "id": "TSTUSD_PERP", "symbol": "TST/USD:TST", "type": "swap",
            "spot": False, "settle": "TST", "precision": {"price": 0.01},
        },
        {"id": "BADUSDT", "symbol": "BAD/USDT:USDT", "type": "swap",
         "spot": False, "settle": "USDT", "precision": {"price": None}},
    ]
    assert register_ticks_from_markets(markets) == 1
    assert tick_size_for("TSTUSDT") == 1e-07
    assert tick_size_for("TST/USDT:USDT") == 1e-07  # unified alias resolves too
    assert tick_size_for("BADUSDT") is None


def test_quantize_price_fallback_round8_when_tick_unknown() -> None:
    assert quantize_price(0.123456789123, "NOSUCHUSDT") == 0.12345679
    assert quantize_price(None, "NOSUCHUSDT") is None


# ── conservative sides ────────────────────────────────────────────────────────

def test_long_stop_rounds_away_tp_rounds_closer() -> None:
    set_tick_sizes({"MEMUSDT": 1e-07})
    entry = 3.5e-05
    stop_raw = 3.39514e-05   # below entry
    tp_raw = 3.81236e-05     # above entry
    stop = quantize_conservative(stop_raw, "MEMUSDT", direction="long")
    tp = quantize_conservative(tp_raw, "MEMUSDT", direction="long")
    assert stop == 3.39e-05 and stop <= stop_raw       # further from entry
    assert tp == 3.81e-05 and tp <= tp_raw             # closer to entry
    assert stop < entry < tp


def test_short_stop_rounds_away_tp_rounds_closer() -> None:
    set_tick_sizes({"BTCXUSDT": 0.1})
    entry = 51000.0
    stop_raw = 51530.04   # above entry
    tp_raw = 50120.03     # below entry
    stop = quantize_conservative(stop_raw, "BTCXUSDT", direction="short")
    tp = quantize_conservative(tp_raw, "BTCXUSDT", direction="short")
    assert stop == 51530.1 and stop >= stop_raw        # further from entry
    assert tp == 50120.1 and tp >= tp_raw              # closer to entry
    assert tp < entry < stop


# ── register_signal_open snaps tracked geometry onto the grid ────────────────

def test_register_signal_open_quantizes_levels() -> None:
    from hunt_core.track.tracker import register_signal_open

    set_tick_sizes({"MEMEXUSDT": 1e-07})
    state: dict = {"signals": {}, "followup_sent": {}}
    setup = {
        "entry_zone": [3.500049e-05, 3.550051e-05],
        "stop_loss": 3.291234e-05,
        "tp1": 3.812345e-05,
        "tp2": 4.123456e-05,
        "tp3": None,
        "risk_reward": 1.5,
    }
    register_signal_open(
        state,
        symbol="MEMEXUSDT",
        direction="long",
        price=3.52e-05,
        setup=setup,
        lifecycle=None,
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )
    sig = state["signals"]["MEMEXUSDT:long"]
    assert sig["entry_lo"] == 3.5e-05          # nearest
    assert sig["entry_hi"] == 3.55e-05         # nearest
    assert sig["stop_loss"] == 3.29e-05        # long stop → floor (further)
    assert sig["tp1"] == 3.81e-05              # long TP → floor (closer)
    assert sig["tp2"] == 4.12e-05
    assert sig["tp3"] is None
    assert sig["entry_zone"] == [sig["entry_lo"], sig["entry_hi"]]
    snap = sig["delivered_levels_snapshot"]
    assert snap["sl"] == sig["stop_loss"] and snap["tp1"] == sig["tp1"]
    # caller's setup dict must not be mutated
    assert setup["stop_loss"] == 3.291234e-05


def test_breakeven_buffer_survives_tick_grid() -> None:
    """round(x, 6) used to erase a 0.15% BE buffer on 3.5e-5 prices entirely."""
    from hunt_core.track._trailing import apply_tp1_breakeven_trail

    set_tick_sizes({"SATSXUSDT": 1e-08})
    entry = 3.5e-05
    active = {
        "entry_lo": 3.45e-05,
        "entry_hi": entry,
        "extreme_hi": 3.9e-05,
        "stop_loss": 3.2e-05,
        "original_stop_loss": 3.2e-05,
    }
    assert apply_tp1_breakeven_trail(active, direction="long", symbol="SATSXUSDT")
    # the lock must actually clear entry — with round(,6) it snapped back to
    # 3.5e-05 == entry and the guard rejected it forever
    assert active["stop_loss"] > entry


# ── fmt_price adaptive tail ───────────────────────────────────────────────────

def test_fmt_price_distinct_levels_stay_distinct_sub_1e4() -> None:
    lo, hi = 3.51e-05, 3.56e-05  # 1000SATS-scale entry band
    assert fmt_price(lo) != fmt_price(hi)
    assert fmt_price(lo) == "0.00003510"


def test_fmt_price_magnitude_branches() -> None:
    assert fmt_price(63937.75) == "63937.8"       # BTC perp tick 0.1
    assert fmt_price(2345.678) == "2345.68"
    assert fmt_price(0.004567891) == "0.004568"
    assert fmt_price(0.000234567) == "0.0002346"  # NEIRO-scale, 7dp
    assert fmt_price(None) == "—"
