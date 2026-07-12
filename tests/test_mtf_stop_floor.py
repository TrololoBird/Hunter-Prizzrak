"""The MTF scenario stop must never hug the level it anchors to.

Root cause of the `pre`-phase stop-out cluster: ``stop = sup`` pinned the stop
onto the nearest support, which normally sits just under the price (that level is
*why* price is there). In data/signal_history.jsonl every delivered signal whose
nominal risk was below 0.5% closed stop_hit (26/26), a third of them with
mfe_pct == 0.0 — never a tick in favour.

These tests drive ``build_mtf_confluence`` itself, not a copy of its arithmetic.
"""
from __future__ import annotations

from hunt_core.confluence.mtf import build_mtf_confluence
from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

_BULL = {"rsi14": 60, "adx14": 25, "ema20": 101, "ema50": 100, "ema200": 99, "close": 102}
_BEAR = {"rsi14": 40, "adx14": 25, "ema20": 99, "ema50": 100, "ema200": 101, "close": 98}


def _tf(snap: dict, atr: float) -> dict:
    return {k: {**snap, "atr14": atr} for k in ("15m", "1h", "4h", "1d")}


def _row(support: float = 0.0, resistance: float = 0.0) -> dict:
    return {
        "structure": {
            "key_levels": {"support": support, "resistance": resistance},
            "liquidity_pools": {},
        }
    }


def _risk_pct(price: float, stop: float) -> float:
    return abs(price - stop) / price * 100.0


def test_long_stop_floor_rescues_support_just_below_price():
    # SPYUSDT, from signal_history: entry ~745.22, stop 743.514 -> risk 0.23%.
    price, atr, support = 745.216, 1.310, 743.514
    sc = build_mtf_confluence("SPYUSDT", _tf(_BULL, atr), price, row=_row(support=support)).long_scenario
    assert sc.stop < support, "stop must clear the level it anchors to"
    assert _risk_pct(price, sc.stop) >= long_min_sl_dist_pct("SPYUSDT")


def test_long_stop_floor_holds_when_support_sits_one_tick_below():
    # PAXGUSDT, from signal_history: entry 4163.77, stop 4163.76 -> risk 0.000%.
    price, support = 4163.77, 4163.76
    sc = build_mtf_confluence("PAXGUSDT", _tf(_BULL, 0.0), price, row=_row(support=support)).long_scenario
    assert _risk_pct(price, sc.stop) >= long_min_sl_dist_pct("PAXGUSDT")


def test_structural_stop_further_than_floor_is_preserved():
    """A genuinely structural stop must not be dragged in by the floor."""
    price, atr, support = 60_000.0, 900.0, 58_000.0
    sc = build_mtf_confluence("BTCUSDT", _tf(_BULL, atr), price, row=_row(support=support)).long_scenario
    assert sc.stop == support


def test_short_stop_floor_is_symmetric():
    price, resistance = 100.0, 100.001
    sc = build_mtf_confluence("FOOUSDT", _tf(_BEAR, 0.0), price, row=_row(resistance=resistance)).short_scenario
    assert sc.stop > resistance
    assert _risk_pct(price, sc.stop) >= short_min_sl_dist_pct("FOOUSDT")


def test_anchor_symbols_keep_their_tighter_floor():
    """BTC/ETH/XAU/XAG trade a tighter nominal floor than the 1.0% default."""
    assert long_min_sl_dist_pct("BTCUSDT") < long_min_sl_dist_pct("FOOUSDT")
    assert short_min_sl_dist_pct("ETHUSDT") < short_min_sl_dist_pct("FOOUSDT")
