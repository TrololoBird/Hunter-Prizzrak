"""Depth bins must never render a bid above the reference price, or an ask below it."""

from __future__ import annotations

from hunt_core.maps.orderbook import merge_full_depth_bins

REF = 64452.10
BUCKETS = 20
RANGE_PCT = 5.0
EPS = 1e-6


def _bins(per_exchange: dict) -> dict:
    return merge_full_depth_bins(
        per_exchange, current_price=REF, n_buckets=BUCKETS, price_range_pct=RANGE_PCT
    )


def _assert_sides_sane(out: dict) -> None:
    for b in out["bid_bins"]:
        assert b["price_lo"] < REF + EPS, f"bid bin {b} starts at/above reference {REF}"
    for a in out["ask_bins"]:
        assert a["price_hi"] > REF - EPS, f"ask bin {a} ends at/below reference {REF}"
    if out["bid_bins"] and out["ask_bins"]:
        assert out["bid_bins"][0]["price_lo"] < out["ask_bins"][0]["price_hi"]


def test_clean_book_keeps_bids_below_and_asks_above() -> None:
    per_ex = {
        "binance": {
            "bids": [(REF - k * 5.0, 2.0) for k in range(80)],
            "asks": [(REF + 1.0 + k * 5.0, 2.0) for k in range(80)],
            "bid_price": REF,
        }
    }
    _assert_sides_sane(_bins(per_ex))


def test_level_resting_exactly_on_reference_does_not_flip_sides() -> None:
    """The reference price lands on a bucket edge; float error used to push it over."""
    per_ex = {"a": {"bids": [(REF, 2.0)], "asks": [(REF, 2.0)], "bid_price": REF}}
    out = _bins(per_ex)
    _assert_sides_sane(out)
    assert all(b["price_hi"] <= REF + EPS for b in out["bid_bins"])
    assert all(a["price_lo"] >= REF - EPS for a in out["ask_bins"])


def test_cross_venue_desync_does_not_render_a_crossed_book() -> None:
    """One venue quoting off-reference must not produce a bid wall above an ask wall."""
    per_ex = {
        "binance": {
            "bids": [(REF - k * 2.0, 40.0) for k in range(80)],
            "asks": [(REF + 1.0 + k * 2.0, 1.0) for k in range(80)],
            "bid_price": REF,
        },
        # lagging high: its bids sit above the reference
        "bybit": {
            "bids": [(REF + 60.0 - k * 2.0, 40.0) for k in range(20)],
            "asks": [(REF + 62.0 + k * 2.0, 1.0) for k in range(20)],
            "bid_price": REF + 60.0,
        },
        # lagging low: its asks sit below the reference
        "okx": {
            "bids": [(REF - 70.0 - k * 2.0, 1.0) for k in range(20)],
            "asks": [(REF - 68.0 + k * 2.0, 40.0) for k in range(20)],
            "bid_price": REF - 70.0,
        },
    }
    _assert_sides_sane(_bins(per_ex))


def test_bins_expose_their_band_not_just_a_centre() -> None:
    per_ex = {"a": {"bids": [(REF - 10.0, 2.0)], "asks": [(REF + 10.0, 2.0)], "bid_price": REF}}
    out = _bins(per_ex)
    for b in out["bid_bins"] + out["ask_bins"]:
        assert b["price_lo"] < b["price_center"] < b["price_hi"]
