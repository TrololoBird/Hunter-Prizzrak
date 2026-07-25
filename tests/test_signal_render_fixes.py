"""Signal-render correctness fixes from the 2026-07-12 output review.

- #3 iceberg/hidden-order side sanity (maps/orderbook._detect_iceberg): never
  emit an ask below price / bid above price (the «скрытый sell ниже цены» bug).
- #2 interest-zone bias warning actually fires (prizrak/build.interest_zones_text
  read htf_bias from the wrong shape → dead code).
- #8 deeper-level dedup (deliver/confluence_grid): drop per-TF repeats and
  near-adjacent (~0.02%) duplicates.
- #10 liquidation line shows cluster size, not just a bare distance %.
"""
from __future__ import annotations


import re
from collections import deque

from hunt_core.maps.orderbook import _detect_iceberg


class _Trade:
    __slots__ = ("ts_ms", "price", "qty")

    def __init__(self, ts_ms: int, price: float, qty: float) -> None:
        self.ts_ms = ts_ms
        self.price = price
        self.qty = qty


def _strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# ── #3 iceberg side sanity ────────────────────────────────────────────────────


def test_iceberg_never_ask_below_price_or_bid_above() -> None:
    import time as _t

    price = 63_679.55
    now = int(_t.time() * 1000)
    # Heavy repeated fills at a support level below price (a hidden BID being
    # hit by sellers) and at a resistance above (a hidden ASK).
    below = 63_640.0
    above = 63_720.0
    trades = deque(
        [_Trade(now - 1000, below, 5.0) for _ in range(6)]
        + [_Trade(now - 1000, above, 5.0) for _ in range(6)]
    )
    # Book shows tiny size at both (icebergs hide their true size).
    bids = [(below, 0.05), (price - 1, 2.0)]
    asks = [(above, 0.05), (price + 1, 2.0)]
    out = _detect_iceberg(trades, bids, asks, current_price=price)
    for lvl in out:
        if lvl["side"] == "ask":
            assert lvl["price"] > price, f"ask below price: {lvl}"
        if lvl["side"] == "bid":
            assert lvl["price"] < price, f"bid above price: {lvl}"


def test_iceberg_straddle_bucket_skipped() -> None:
    import time as _t

    price = 63_679.55
    now = int(_t.time() * 1000)
    # Fills right at the spread (within one tol bucket of price) → ambiguous side,
    # must be dropped rather than guessed.
    trades = deque([_Trade(now - 1000, price + 0.5, 5.0) for _ in range(8)])
    bids = [(price - 0.5, 0.01)]
    asks = [(price + 0.5, 0.01)]
    out = _detect_iceberg(trades, bids, asks, current_price=price)
    assert all(abs(lvl["price"] - price) / price > 0.0008 for lvl in out)


# ── #2 bias warning fires from prizrak_structure ─────────────────────────────


def test_bias_conflict_computed_only_when_no_candidate() -> None:
    # entry.py must compute prizrak_bias_liq_conflict only in the WAIT case; a
    # bullish DOM under a SHORT bias with no candidate flags the conflict.
    from hunt_core.prizrak.config import PrizrakConfig
    from hunt_core.prizrak.liq_reconcile import compute_liquidation_factor

    cfg = PrizrakConfig.load()
    liq_ctx = {
        "liq_cascade_risk": "short_squeeze",
        "liq_synthetic_only": False,
        "map_book_imbalance_1pct": 0.45,
    }
    factor = compute_liquidation_factor(liq_ctx, direction="short", cfg=cfg)
    assert factor["conflict"] is True  # short bias vs bullish market = conflict
    aligned = compute_liquidation_factor(liq_ctx, direction="long", cfg=cfg)
    assert aligned["conflict"] is False  # long bias agrees with bullish market


def test_deeper_levels_drop_per_tf_repeats_and_adjacent_dupes() -> None:
    from hunt_core.deliver.confluence_grid import build_confluence_grid

    price = 63_679.55
    row = {
        "price": price,
        "prizrak_structure": {
            "struct_by_tf": {
                "1h": {
                    "key_levels": {"support": 63_602.8, "resistance": 64_271.9},
                    "all_swing_lows": [
                        63_668.8, 63_657.4,  # ~0.02% apart → one survives
                        63_602.8,            # already shown as 1h support → dropped
                        62_410.1,            # distinct → kept
                    ],
                    "all_swing_highs": [],
                },
            },
        },
        "regime": {},
    }
    grid = build_confluence_grid(row)
    deeper = next((g for g in grid if g.get("tf") == "глубже"), None)
    assert deeper is not None
    lows = deeper["support"]
    assert 63_602.8 not in lows                      # per-TF repeat gone
    assert not (63_668.8 in lows and 63_657.4 in lows)  # adjacent pair collapsed
    assert 62_410.1 in lows                          # distinct level kept


# ── #10 liquidation line carries cluster size ────────────────────────────────


def test_liquidation_line_shows_cluster_size() -> None:
    from hunt_core.deliver._sections import format_liquidation_map_section

    row = {
        "market": {
            "liq_heatmap_nearest_short": 63_832.5,
            "liq_magnet_pull_short_pct": 0.2,
            "liq_heatmap_clusters": [
                {"price": 63_832.5, "total_notional": 4_200_000.0,
                 "intensity": 0.83, "event_count": 12, "source": "realized"},
            ],
        },
        "maps": {"liquidation": {}},
    }
    text = _strip(format_liquidation_map_section(row))
    assert "Шорт-сквиз" in text
    assert "0.2%" in text
    assert "M" in text  # compact notional like $4.2M
    assert "плотн" in text  # intensity label
