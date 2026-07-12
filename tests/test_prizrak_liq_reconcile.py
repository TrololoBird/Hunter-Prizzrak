"""bias ↔ liquidation/DOM reconciliation доп-фактор (WS-2M.2) + maps-config regressions.

Anchored on the documented ETH failure (`research/prizrak_corpus/prizrak_eth.razbor.md`):
structural bias was SHORT while the bot's own liq map said short-squeeze and DOM showed
buyers — and the squeeze/buyers were right. The factor must down-weight AND flag that case.
"""
from __future__ import annotations

from hunt_core.maps.config import MapsConfig, _DEFAULT_LEVERAGE_WEIGHTS
from hunt_core.maps.liquidation import _DEFAULT_LEVERAGE_TIERS
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.liq_reconcile import compute_liquidation_factor


def _cfg(*, enabled: bool = True) -> PrizrakConfig:
    c = PrizrakConfig.load().model_copy()
    c.liq_reconcile_enabled = enabled
    c.liq_dom_neutral_band = 0.15
    return c


# ── the factor ───────────────────────────────────────────────────────────────

def test_eth_failure_short_vs_squeeze_and_buyers_is_penalized_and_flagged() -> None:
    eth = {
        "liq_cascade_risk": "short_squeeze",
        "liq_synthetic_only": False,
        "map_book_imbalance_1pct": 0.222,
    }
    out = compute_liquidation_factor(eth, direction="short", cfg=_cfg())
    assert out["multiplier"] < 1.0
    assert out["conflict"] is True


def test_aligned_long_gets_bonus_no_flag() -> None:
    eth = {
        "liq_cascade_risk": "short_squeeze",
        "liq_synthetic_only": False,
        "map_book_imbalance_1pct": 0.222,
    }
    out = compute_liquidation_factor(eth, direction="long", cfg=_cfg())
    assert out["multiplier"] > 1.0
    assert out["conflict"] is False


def test_synthetic_only_cascade_does_not_drive_hard_flag() -> None:
    # A leverage-tier ESTIMATE must not veto/flag structure; weak DOM stays inside band.
    syn = {
        "liq_cascade_risk": "short_squeeze",
        "liq_synthetic_only": True,
        "map_book_imbalance_1pct": 0.05,
    }
    out = compute_liquidation_factor(syn, direction="short", cfg=_cfg())
    assert out["conflict"] is False
    assert out["multiplier"] == 1.0


def test_strong_dom_alone_can_flag_even_if_synthetic_liq() -> None:
    # DOM is real book data — a strong contradicting imbalance flags even without realized liq.
    ctx = {
        "liq_cascade_risk": "short_squeeze",
        "liq_synthetic_only": True,
        "map_book_imbalance_1pct": 0.40,  # >= 2× band → strong
    }
    out = compute_liquidation_factor(ctx, direction="short", cfg=_cfg())
    assert out["conflict"] is True
    assert out["multiplier"] < 1.0


def test_bounded_and_neutral_when_no_data_or_disabled() -> None:
    assert compute_liquidation_factor(None, direction="short", cfg=_cfg())["multiplier"] == 1.0
    assert compute_liquidation_factor({}, direction="short", cfg=_cfg())["multiplier"] == 1.0
    live = {"liq_cascade_risk": "long_flush", "liq_synthetic_only": False, "map_book_imbalance_1pct": -0.5}
    assert compute_liquidation_factor(live, direction="long", cfg=_cfg(enabled=False))["multiplier"] == 1.0
    # bounded envelope
    out = compute_liquidation_factor(live, direction="long", cfg=_cfg())
    assert 0.85 <= out["multiplier"] <= 1.15


# ── maps-config regressions (WS-2M.1 / 2M.4) ──────────────────────────────────

def test_vp_buckets_defaults_to_60_without_toml() -> None:
    # The from_defaults fallback used to silently revert to the coarse 24.
    assert MapsConfig.from_defaults({}).vp_buckets == 60
    assert MapsConfig().vp_buckets == 60


def test_leverage_weights_match_tier_ladder_length() -> None:
    # 5 weights for 4 tiers left the last weight as dead code; lengths must agree.
    assert len(_DEFAULT_LEVERAGE_WEIGHTS) == len(_DEFAULT_LEVERAGE_TIERS)
    assert _DEFAULT_LEVERAGE_TIERS == (10, 25, 50, 100)
