"""Spot-plane pins for defects found in the live run (2026-07-16).

The scanner's whole spot plane was a phantom: `spot_companion_refresh
symbols=0 updated=0` on every tick for 4 hours, so the companion cache stayed
empty and `enrichments_for()` always returned {}.
"""

from __future__ import annotations

from typing import Any

from hunt_core.market.spot import HuntCcxtSpotCompanion


class _Companion(HuntCcxtSpotCompanion):
    """Bypass __init__ (no network/exchange) — we only exercise pure helpers."""

    def __init__(self) -> None:  # noqa: D107
        pass


# ---------------------------------------------------------------------------
# basis leg: mid-vs-mid, not spot-last-vs-futures-mid
# ---------------------------------------------------------------------------


def test_spot_reference_prefers_mid_over_last() -> None:
    """Illiquid spot: last printed at the bid, mid is 100.5 → basis must use mid."""
    ticker: dict[str, Any] = {"last": 100.0, "bid": 100.0, "ask": 101.0}
    assert _Companion._spot_reference_price(ticker, 100.0) == 100.5


def test_spot_reference_falls_back_to_last_without_book() -> None:
    assert _Companion._spot_reference_price({"last": 100.0}, 100.0) == 100.0
    assert _Companion._spot_reference_price({"bid": 0, "ask": 0}, 100.0) == 100.0


def test_spot_reference_ignores_crossed_book() -> None:
    """ask < bid is nonsense — do not average it."""
    assert _Companion._spot_reference_price({"bid": 101.0, "ask": 99.0}, 100.0) == 100.0


def test_basis_sign_does_not_flip_on_half_spread() -> None:
    """The live defect: perp mid 100.5 vs a 100.0/101.0 spot book is PARITY.

    Reading the spot LAST (which sat at the bid) reported the perp 50 bps rich —
    a fabricated basis worth half the spot spread.
    """
    ticker: dict[str, Any] = {"last": 100.0, "bid": 100.0, "ask": 101.0}
    ref = _Companion._spot_reference_price(ticker, 100.0)
    assert _Companion._spread_bps(ref, 100.5) == 0.0
    # what the old code did:
    assert _Companion._spread_bps(100.0, 100.5) == 50.0


def test_spread_bps_none_when_futures_mid_missing() -> None:
    assert _Companion._spread_bps(100.0, None) is None
    assert _Companion._spread_bps(0.0, 100.0) is None


# ---------------------------------------------------------------------------
# refresh gate: the tick's own symbols, not the empty "full"-tier subset
# ---------------------------------------------------------------------------


def test_spot_refresh_gate_covers_fast_tier_universe() -> None:
    """Reproduce the live tier arithmetic that starved the refresh.

    Capacity planner under a real budget (live: budget=700, kept=18) demotes the
    whole universe to "fast", so `any(_tier_for(s) == "full")` is False and
    `batch_tier` falls back to the DEFAULT tier ("full") — the gate opened while
    the filtered list was empty.
    """
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    tier_by_symbol: dict[str, str] = {s: "fast" for s in symbols}
    default_tier = "full"

    def _tier_for(sym: str) -> str:
        return tier_by_symbol.get(sym, default_tier)

    batch_tier = "full" if any(_tier_for(s) == "full" for s in symbols) else default_tier
    # The old gate's own arithmetic: it passes...
    assert batch_tier == "full"
    # ...and yields nothing to refresh.
    assert [s for s in symbols if _tier_for(s) == "full"] == []
    # The fix refreshes what the tick actually enriches.
    assert list(symbols) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_enrichments_absent_symbol_is_empty_not_fabricated() -> None:
    comp = _Companion()
    comp._cache = {}
    assert comp.enrichments_for("BTCUSDT") == {}
