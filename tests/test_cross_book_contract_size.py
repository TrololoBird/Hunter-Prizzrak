"""Cross-venue order books must be normalised to BASE units before merging.

CCXT does not normalise order-book sizes: fetch_order_book hands back the venue's raw
`sz`/`amount`, which is denominated in that venue's CONTRACT units. Those units differ —
OKX's BTC-USDT-SWAP is contractSize=0.01, Binance/Bitget/Bybit linear USDT are 1.0 — so
merging the raw numbers across venues silently multiplies OKX's notional by 100.

Measured live (2026-07-16, BTC): OKX's top-20 bids priced as base units came to $86.20M
against a true $0.86M, while its real best-bid depth was 6.58 BTC vs Binance's 188.1 BTC.
The smallest book in the merge rendered as the largest wall, and the deep-analysis card
published a "$279.4M · 100% от макс." bid band that was ~97% fictional — a phantom
support wall a reader would trade against.
"""

from __future__ import annotations

from hunt_core.market.cross import _venue_contract_size
from hunt_core.maps.orderbook import merge_full_depth_bins


class _Ex:
    def __init__(self, contract_size: object) -> None:
        self._cs = contract_size

    def market(self, _sym: str) -> dict[str, object]:
        return {"contractSize": self._cs}


class _NoMarkets:
    def market(self, _sym: str) -> dict[str, object]:
        raise ValueError("markets not loaded")


def test_okx_contract_size_is_read() -> None:
    assert _venue_contract_size(_Ex(0.01), "BTC/USDT:USDT") == 0.01


def test_linear_usdt_venues_are_one() -> None:
    assert _venue_contract_size(_Ex(1.0), "BTC/USDT:USDT") == 1.0
    assert _venue_contract_size(_Ex(1), "BTC/USDT:USDT") == 1.0


def test_unknown_contract_size_fails_open_to_one() -> None:
    """Unknown → face value. Guessing anything else would invent liquidity."""
    for bad in (None, "", "abc", 0, -1, float("nan"), float("inf")):
        assert _venue_contract_size(_Ex(bad), "BTC/USDT:USDT") == 1.0
    assert _venue_contract_size(_NoMarkets(), "BTC/USDT:USDT") == 1.0


def test_okx_scaled_depth_no_longer_dominates_the_merge() -> None:
    """The regression itself, at the scale it actually occurred.

    OKX carries ~6.6 BTC at the top of book against Binance's ~188 BTC. Unnormalised,
    OKX's 658 contracts read as 658 BTC and swamp the merge; normalised, Binance must
    dominate — as it does in reality.
    """
    price = 63894.9
    binance = {"bids": [(63866.0, 188.1)], "asks": [(63900.0, 150.0)]}
    # OKX sizes AFTER normalisation (what fetch_exchange_order_book now emits):
    okx_fixed = {"bids": [(63863.4, 657.91 * 0.01)], "asks": [(63901.0, 500.0 * 0.01)]}
    merged = merge_full_depth_bins(
        {"binance": binance, "okx": okx_fixed},
        current_price=price,
        n_buckets=8,
        price_range_pct=0.5,
    )
    bid_usd = sum(b["depth_usd"] for b in merged["bid_bins"])
    # 188.1 BTC + 6.58 BTC ~= 194.7 BTC ~= $12.4M — millions, not hundreds of millions.
    assert 10e6 < bid_usd < 15e6, f"merged bid depth should be ~$12M, got ${bid_usd/1e6:.1f}M"

    # And the un-normalised form is what used to happen — pinned so the contrast is
    # explicit rather than folklore.
    okx_raw = {"bids": [(63863.4, 657.91)], "asks": [(63901.0, 500.0)]}
    broken = merge_full_depth_bins(
        {"binance": binance, "okx": okx_raw},
        current_price=price,
        n_buckets=8,
        price_range_pct=0.5,
    )
    broken_usd = sum(b["depth_usd"] for b in broken["bid_bins"])
    assert broken_usd > 3 * bid_usd, "sanity: raw contracts really do inflate the merge"
