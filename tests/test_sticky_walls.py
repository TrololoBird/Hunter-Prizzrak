"""Sticky-wall detector: a sticky wall is a price-anchored level, not the book top.

Regression for the live BTC artifact «Sticky bid @ 64161.915 · Sticky ask @
64161.915 (30 samples each)»: the bucket straddling the spread exists in every
snapshot by definition, so it always hit min_samples and, sorted by distance,
always won. The detector must reject (1) wrong-side entries (bid above price,
ask below) and (2) buckets closer to price than one bucket width.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from hunt_core.maps.orderbook import _detect_sticky_walls

PRICE = 64_000.0


def _snap(bid_px: float, ask_px: float, extra_bid: float | None = None) -> dict[str, Any]:
    bids = [{"price": bid_px, "notional_usd": 100_000.0}]
    if extra_bid is not None:
        bids.append({"price": extra_bid, "notional_usd": 5_000_000.0})
    return {
        "bid_levels": bids,
        "ask_levels": [{"price": ask_px, "notional_usd": 120_000.0}],
    }


def test_book_top_is_not_sticky() -> None:
    # 30 identical snapshots of just the top of book hugging the price —
    # the exact degenerate input behind the live artifact.
    history: deque[dict[str, Any]] = deque(
        _snap(PRICE - 5, PRICE + 5) for _ in range(30)
    )
    sticky = _detect_sticky_walls(history, current_price=PRICE, min_samples=30)
    assert sticky == []


def test_anchored_wall_below_price_is_sticky_and_sided() -> None:
    # A big bid parked 0.5% below price in every snapshot IS sticky; the
    # spread-hugging top levels still are not.
    wall_px = PRICE * 0.995
    history: deque[dict[str, Any]] = deque(
        _snap(PRICE - 5, PRICE + 5, extra_bid=wall_px) for _ in range(30)
    )
    sticky = _detect_sticky_walls(history, current_price=PRICE, min_samples=30)
    assert len(sticky) == 1
    wall = sticky[0]
    assert wall["side"] == "bid"
    assert wall["price"] < PRICE
    assert wall["distance_pct"] >= 0.15  # at least one bucket width away
    assert wall["notional_usd"] == 5_000_000.0


def test_sides_never_share_a_price() -> None:
    # Even with symmetric anchored walls, a bid must resolve below price and an
    # ask above — identical bid/ask prices can never be reported again.
    history: deque[dict[str, Any]] = deque(
        {
            "bid_levels": [{"price": PRICE * 0.996, "notional_usd": 1e6}],
            "ask_levels": [{"price": PRICE * 1.004, "notional_usd": 1e6}],
        }
        for _ in range(30)
    )
    sticky = _detect_sticky_walls(history, current_price=PRICE, min_samples=30)
    sides = {s["side"]: s["price"] for s in sticky}
    assert set(sides) == {"bid", "ask"}
    assert sides["bid"] < PRICE < sides["ask"]
