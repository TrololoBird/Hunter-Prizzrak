"""Forward-liq leverage weights must track leverage, not array position.

leverage_tiers_from_brackets returns DESCENDING tiers, but _DEFAULT_LEVERAGE_WEIGHTS
(0.35→0.15) is built ASCENDING ("more retail OI at lower leverage"). The old
positional weights[i % n] put 0.35 on the HIGHEST leverage on the bracket path
(inverted intent) and wrapped big weights onto 5th+ tiers. Now each tier gets the
weight for its ascending-leverage rank, so the result is identical whether tiers
arrive ascending or descending — the lowest-leverage band always carries 0.35.
"""
from __future__ import annotations

from hunt_core.maps.liquidation import entry_anchored_forward_zones

_WEIGHTS = (0.35, 0.30, 0.20, 0.15)
# Two bars, one ΔOI>0 event at a clean price; long_liq = 100·(1−1/lev) lands each
# tier in its own bucket (price_min=80, bucket_size=1): 10×→90(b10), 25×→96(b16),
# 50×→98(b18), 100×→99(b19). Long notional in a bucket ∝ that tier's weight.
_OI_BARS = [
    {"oi": 1_000.0, "high": 100.0, "low": 100.0, "close": 100.0},
    {"oi": 2_000.0, "high": 100.0, "low": 100.0, "close": 100.0},
]


_N_BUCKETS = 400
_BUCKET_SIZE = (2.0 * 20.0) / _N_BUCKETS  # span=20 → 0.1, fine enough to separate tiers


def _weight_by_lev(tiers: tuple[int, ...]) -> dict[int, float]:
    cm = entry_anchored_forward_zones(
        _OI_BARS,
        current_price=100.0,
        n_buckets=_N_BUCKETS,
        price_range_pct=20.0,
        leverage_tiers=tiers,
        maintenance_margin_rates=None,
        leverage_weights=_WEIGHTS,
    )
    out: dict[int, float] = {}
    for lev in tiers:
        long_liq = 100.0 * (1.0 - 1.0 / lev)
        b = int((long_liq - 80.0) / _BUCKET_SIZE)
        out[lev] = cm.get(b, {}).get("long", 0.0)
    return out


def test_lowest_leverage_gets_largest_weight_ascending() -> None:
    w = _weight_by_lev((10, 25, 50, 100))
    assert w[10] > w[25] > w[50] > w[100]  # 0.35 → 10×


def test_order_invariant_descending_matches_ascending() -> None:
    # The bracket path feeds descending tiers; per-band weights must be identical.
    asc = _weight_by_lev((10, 25, 50, 100))
    desc = _weight_by_lev((100, 50, 25, 10))
    assert asc == desc
    assert desc[10] > desc[100]  # NOT the old inverted bug


def test_more_tiers_than_weights_clamps_high_leverage() -> None:
    # 6 tiers, 4 weights: extra high-leverage tiers clamp to the smallest weight,
    # never wrap a big weight back on (the old i%n bug).
    w = _weight_by_lev((10, 20, 25, 50, 75, 100))
    assert w[10] >= w[20] >= w[25] >= w[50] >= w[75] >= w[100]
    assert w[75] == w[100]  # both clamped to the smallest weight
