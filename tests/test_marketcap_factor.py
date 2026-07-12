"""Market-cap доп-фактор (Павел М., prizrak_marketcap_factor).

Course rule: the cap chart is a calibration factor for true-value vs price divergence,
NOT a gate. These tests pin the bounded, non-gating behaviour: neutral when disabled or
unavailable; confirm-bonus when the cap trend agrees with the trade; diverge-penalty when
the cap trend opposes; and supply-stability detection from price-vs-cap % drift.
"""

from __future__ import annotations

import math

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.marketcap import compute_marketcap_factor

# A zigzag with real swing structure (a pure monotone ramp has no interior pivots, so the
# fractal detector reads it as neutral). Period 16 > 2×lookback_pivot(5); the drift makes
# each swing high/low ascend (bull) or descend (bear).
_N = 64
_PERIOD = 16
_AMP = 8.0
_DRIFT = 2.0


def _shape(trend: str) -> list[float]:
    """Normalised zigzag path in 'price-like' units, oscillating around a drifting mean."""
    sign = 1.0 if trend == "bull" else -1.0
    return [100.0 + sign * _DRIFT * i + _AMP * math.sin(2 * math.pi * i / _PERIOD) for i in range(_N)]


def _cfg(**over: object) -> PrizrakConfig:
    return PrizrakConfig(marketcap_enabled=True, **over)  # type: ignore[arg-type]


def _price(trend: str) -> list[list[float]]:
    """Raw CCXT rows with a clean trending swing structure (flat OHLC around the close)."""
    return [[i * 60_000, c, c + 0.1, c - 0.1, c, 10.0] for i, c in enumerate(_shape(trend))]


def _cap(trend: str, *, base: float = 1_000_000.0, scale: float = 1.0) -> list[list[float]]:
    """CoinGecko-shaped ``[[ts_ms, market_cap], ...]``. ``scale`` multiplies the % move
    relative to price: 1.0 ⇒ supply stable (same % path), >1 ⇒ supply moving."""
    return [[i * 60_000, base * (1.0 + (c - 100.0) / 100.0 * scale)] for i, c in enumerate(_shape(trend))]


def test_disabled_is_neutral() -> None:
    res = compute_marketcap_factor(_price("bull"), _cap("bull"), direction="long", cfg=PrizrakConfig())
    assert res["multiplier"] == 1.0
    assert "marketcap_disabled" in res["evidence"]


def test_unavailable_is_neutral() -> None:
    res = compute_marketcap_factor(_price("bull"), None, direction="long", cfg=_cfg())
    assert res["multiplier"] == 1.0
    assert "marketcap_unavailable" in res["evidence"]

    res_short = compute_marketcap_factor(_price("bull"), [[0, 1.0]], direction="long", cfg=_cfg())
    assert res_short["multiplier"] == 1.0


def test_confirming_cap_trend_adds_bonus() -> None:
    res = compute_marketcap_factor(_price("bull"), _cap("bull"), direction="long", cfg=_cfg())
    assert res["multiplier"] > 1.0
    assert res["cap_trend"] == "bull"
    assert any("marketcap_confirms_bull" in e for e in res["evidence"])


def test_opposing_cap_trend_penalises() -> None:
    # Price bullish but cap bearish → true value diverging from price (low-float pump risk).
    res = compute_marketcap_factor(_price("bull"), _cap("bear"), direction="long", cfg=_cfg())
    assert res["multiplier"] < 1.0
    assert res["cap_trend"] == "bear"
    assert any("marketcap_diverges" in e for e in res["evidence"])


def test_multiplier_is_bounded() -> None:
    hi = compute_marketcap_factor(
        _price("bull"), _cap("bull"), direction="long", cfg=_cfg(marketcap_confirm_bonus=0.15)
    )
    lo = compute_marketcap_factor(
        _price("bull"), _cap("bear"), direction="long", cfg=_cfg(marketcap_diverge_penalty=0.15)
    )
    assert 0.85 <= lo["multiplier"] <= hi["multiplier"] <= 1.15


def test_supply_stable_when_price_and_cap_move_together() -> None:
    # Identical % path ⇒ supply stable, 1:1 transfer valid.
    res = compute_marketcap_factor(_price("bull"), _cap("bull"), direction="long", cfg=_cfg())
    assert res["supply"] == "stable"
    assert any("supply_stable" in e for e in res["evidence"])


def test_supply_unstable_damps_confirm_bonus() -> None:
    # Cap moves ~4× faster than price in % terms ⇒ supply moving (unlock/burn) ⇒ bonus halved.
    stable = compute_marketcap_factor(_price("bull"), _cap("bull"), direction="long", cfg=_cfg())
    unstable = compute_marketcap_factor(_price("bull"), _cap("bull", scale=4.0), direction="long", cfg=_cfg())
    assert unstable["supply"] == "unstable"
    assert any("supply_unstable" in e for e in unstable["evidence"])
    # Both confirm bull, but the unstable one gets a smaller (damped) bonus.
    assert 1.0 < unstable["multiplier"] < stable["multiplier"]
