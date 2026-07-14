"""Liquidation-map leverage weighting: OI-share × liquidation-propensity (SLICE 1a).

Realized liquidations skew to HIGH leverage (Cheng et al. 2021, ~60× mean
effective leverage of liquidated positions), which the pure OI weighting
under-represents. `_leverage_propensity_weights` lifts high-leverage tiers via a
mass-preserving propensity ∝ leverage^exp; exp=0 reproduces the old weighting.
"""
from __future__ import annotations

import math

from hunt_core.maps.config import _DEFAULT_LEVERAGE_WEIGHTS
from hunt_core.maps.liquidation import _leverage_propensity_weights

_TIERS = (10, 25, 50, 100)


def test_exp_zero_is_backward_compatible() -> None:
    w = _leverage_propensity_weights(_TIERS, _DEFAULT_LEVERAGE_WEIGHTS, propensity_exp=0.0)
    assert w == {10: 0.35, 25: 0.30, 50: 0.20, 100: 0.15}


def test_propensity_lifts_high_leverage_and_is_monotonic() -> None:
    w = _leverage_propensity_weights(_TIERS, _DEFAULT_LEVERAGE_WEIGHTS, propensity_exp=1.0)
    # Old curve was high→low (0.35 on 10×); propensity inverts it — 100× now dominant.
    assert w[100] > w[50] > w[25] > w[10]
    assert w[100] > _DEFAULT_LEVERAGE_WEIGHTS[-1]  # 100× lifted above its OI weight (0.15)
    assert w[10] < _DEFAULT_LEVERAGE_WEIGHTS[0]     # 10× reduced below its OI weight (0.35)


def test_mass_preserved_so_notional_scale_is_kept() -> None:
    base_sum = sum(_DEFAULT_LEVERAGE_WEIGHTS)
    for exp in (0.5, 1.0, 1.5):
        w = _leverage_propensity_weights(_TIERS, _DEFAULT_LEVERAGE_WEIGHTS, propensity_exp=exp)
        assert math.isclose(sum(w.values()), base_sum, rel_tol=1e-9)


def test_descending_tiers_ranked_correctly() -> None:
    # Real-bracket path can pass DESCENDING tiers; weight is by ascending RANK, not
    # position — the highest leverage must still get the highest propensity weight.
    w = _leverage_propensity_weights((100, 50, 25, 10), _DEFAULT_LEVERAGE_WEIGHTS, propensity_exp=1.0)
    assert w[100] == max(w.values())
    assert w[10] == min(w.values())
