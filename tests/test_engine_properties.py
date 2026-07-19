"""Property-based proof of the engine's I-5 (no-lookahead) / I-6 (fail-loud) invariants.

Converts the two invariants from prose + review into executable, adversarial proof over the engine's
pure modules (the reliability standard the rest of the engine is held to). Hypothesis is already a
dev dependency. These hunt the project's signature defect family mechanically: a fabricated
``0.0``/``0.5`` where data is missing (I-6), and a forming-candle leak (I-5).
"""
from __future__ import annotations

import math
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from hunt_core.engine.freshness import closed_bars, newest_closed
from hunt_core.engine.funding_stats import funding_recent_extreme, funding_trend, funding_zscore
from hunt_core.engine.liquidations import liquidation_notional
from hunt_core.engine.orderflow import taker_flow

_finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12)
_pos = st.floats(min_value=1e-6, max_value=1e9, allow_nan=False, allow_infinity=False)
_maybe = st.one_of(st.none(), _finite, st.just(float("nan")), st.just(float("inf")), st.text(max_size=3))
_bar = st.lists(_finite, min_size=6, max_size=6)


def _liq_event() -> st.SearchStrategy[dict[str, Any]]:
    return st.fixed_dictionaries(
        {
            "contracts": _maybe,
            "price": _maybe,
            "contractSize": st.one_of(st.none(), _pos),
            "side": st.sampled_from(["buy", "sell", None, "unknown"]),
        }
    )


# --- I-6: liquidation notional never fabricates ---


@given(st.lists(_liq_event(), max_size=40))
def test_liq_notional_finite_and_split_bounded(events: list[dict[str, Any]]) -> None:
    out = liquidation_notional(events, contract_size=1.0)
    assert set(out) == {"long", "short", "total"}
    assert all(math.isfinite(v) for v in out.values())  # never NaN/inf (I-6)
    # long/short are side-attributions of the counted total; each is one signed subset
    assert math.isclose(out["long"] + out["short"], out["total"], rel_tol=1e-9) or (
        out["long"] + out["short"] <= out["total"] + 1e-6
    )


def test_liq_notional_empty_is_zero_not_absent() -> None:
    assert liquidation_notional([]) == {"long": 0.0, "short": 0.0, "total": 0.0}
    assert liquidation_notional(None) == {"long": 0.0, "short": 0.0, "total": 0.0}


@given(st.sampled_from([float("nan"), float("inf"), None, "x"]))
def test_liq_notional_skips_uncomputable_never_fabricates(bad: Any) -> None:
    # a non-computable contracts/price contributes NOTHING (skipped) — not a fabricated 0-notional row
    ev = {"contracts": bad, "price": 100.0, "contractSize": 1.0, "side": "sell"}
    assert liquidation_notional([ev], contract_size=1.0) == {"long": 0.0, "short": 0.0, "total": 0.0}


# --- I-6: taker flow ratios are None when empty (never a fabricated 0.5) ---


def _trade() -> st.SearchStrategy[dict[str, Any]]:
    return st.fixed_dictionaries(
        {
            "side": st.sampled_from(["buy", "sell", None]),
            "price": st.one_of(_pos, st.none()),
            "amount": st.one_of(_pos, st.none()),
            "timestamp": st.integers(min_value=0, max_value=10**13),
        }
    )


@given(st.lists(_trade(), max_size=40))
def test_taker_flow_ratio_none_iff_no_counted_trade(trades: list[dict[str, Any]]) -> None:
    out = taker_flow(trades)
    counted = out["count"]
    assert (out["buy_ratio"] is None) == (counted == 0)  # I-6: no 0.5 out of thin air
    assert (out["delta_ratio"] is None) == (counted == 0)
    assert math.isclose(out["delta"], out["buy_notional"] - out["sell_notional"], rel_tol=1e-9)
    if out["buy_ratio"] is not None:
        assert 0.0 <= out["buy_ratio"] <= 1.0


@given(st.lists(_trade(), max_size=40), st.integers(min_value=1, max_value=10**13))
def test_taker_flow_window_subsets(trades: list[dict[str, Any]], now: int) -> None:
    windowed = taker_flow(trades, window_ms=30_000, now_ms=now)["count"]
    full = taker_flow(trades)["count"]
    assert windowed <= full  # a time window can only drop trades, never invent them


# --- I-6: funding stats fail loud ---


@given(st.lists(st.fixed_dictionaries({"fundingRate": _maybe, "timestamp": st.integers(0, 10**13)}), max_size=40))
def test_funding_zscore_none_below_min_else_finite(records: list[dict[str, Any]]) -> None:
    finite_rates = sum(1 for r in records if isinstance(r["fundingRate"], (int, float)) and math.isfinite(r["fundingRate"]))
    z = funding_zscore(records, min_records=6)
    if finite_rates < 6:
        assert z is None  # not a fabricated 0.0
    else:
        assert z is None or math.isfinite(z)  # never NaN (scipy.stats.zscore would fail this)


@given(st.lists(st.fixed_dictionaries({"fundingRate": _maybe, "timestamp": st.integers(0, 10**13)}), max_size=40))
def test_funding_trend_domain(records: list[dict[str, Any]]) -> None:
    assert funding_trend(records) in (None, "rising", "falling", "flat")


@given(
    st.lists(st.fixed_dictionaries({"fundingRate": _maybe, "timestamp": st.integers(0, 10**13)}), max_size=40),
    st.integers(min_value=0, max_value=10**13),
)
def test_funding_recent_extreme_from_input_or_none(records: list[dict[str, Any]], now: int) -> None:
    out = funding_recent_extreme(records, now_ms=now, max_age_hours=48.0)
    if out is not None:
        rate, age_h = out
        assert math.isfinite(rate) and age_h >= 0.0


# --- I-5: the forming candle is always dropped ---


@given(st.lists(_bar, max_size=40))
def test_closed_bars_drops_exactly_the_forming_tail(cache: list[list[float]]) -> None:
    assert len(closed_bars(cache)) == max(0, len(cache) - 1)  # forming tail dropped
    nc = newest_closed(cache)
    if len(cache) >= 2:
        assert nc is cache[-2]  # newest CLOSED is [-2], never the forming [-1]
    else:
        assert nc is None  # only a forming bar → no closed bar (never fabricated)


@given(st.lists(_bar, min_size=1, max_size=40), _bar)
def test_appending_forming_bar_never_alters_earlier_closed(cache: list[list[float]], forming: list[float]) -> None:
    # metamorphic (I-5): adding a live/forming bar only exposes the previously-forming bar as closed;
    # it never changes any earlier bar. This is the exact class the idx=-2 regressions violated.
    extended = closed_bars([*cache, forming])
    assert extended == cache  # [:-1] of (cache+[forming]) is exactly cache
    assert extended[:-1] == closed_bars(cache)  # earlier closed bars unchanged
