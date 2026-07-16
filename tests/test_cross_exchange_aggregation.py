"""Regression tests for cross-exchange aggregation correctness.

Every test here pins a defect that shipped wrong intel to the Telegram card and
the persisted research dataset. The CCXT parser facts they encode were verified
against the installed ccxt 4.5.59 source and live public endpoints:

* ``bybit.parse_open_interest`` sets ``openInterestValue`` only for INVERSE
  markets; linear reports ``openInterestAmount`` in base coins.
* ``bitget.parse_open_interest`` hardcodes ``'openInterestValue': None``.
* ``okx.parse_open_interest`` sets ``openInterestValue`` from ``oiUsd``.
* ``Exchange.safe_open_interest`` rebuilds the dict from a fixed key mapping with
  no ``openInterest`` key — the old fallback was dead code.
* ``Exchange.safe_ticker``/``Ticker`` use ``markPrice``; ``mark`` does not exist.
* ``interval`` ("8h"/"1h"/"4h") is set by bybit/okx/bitget ``parse_funding_rate``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import ccxt.async_support as ccxt
import pytest

from hunt_core.deliver._sections import format_cross_exchange_section
from hunt_core.market.cross import (
    funding_consensus_from_normalized,
    merge_ws_cross_into_snapshot,
    normalize_funding_to_8h,
    normalized_funding_map,
    parse_funding_interval_hours,
    price_divergence_from_map,
)


# ── FIX 2 — funding interval normalization ──────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8h", 8.0),
        ("1h", 1.0),
        ("4h", 4.0),
        (4, 4.0),
        (None, None),
        ("", None),
        ("weird", None),
        ("0h", None),
        ("-4h", None),
        (True, None),
    ],
)
def test_parse_funding_interval_hours(raw: Any, expected: float | None) -> None:
    assert parse_funding_interval_hours(raw) == expected


def test_normalize_funding_to_8h_rescales_hourly_venue() -> None:
    # +0.0001 per 1h is +0.0008 per 8h — 8x the same number on an 8h venue.
    assert normalize_funding_to_8h(0.0001, 1.0) == pytest.approx(0.0008)
    assert normalize_funding_to_8h(0.0003, 8.0) == pytest.approx(0.0003)
    assert normalize_funding_to_8h(0.0002, 4.0) == pytest.approx(0.0004)


def test_normalize_funding_to_8h_unknown_interval_is_none_not_assumed_8h() -> None:
    assert normalize_funding_to_8h(0.0001, None) is None
    assert normalize_funding_to_8h(None, 8.0) is None


def test_consensus_uses_per_8h_units_not_raw_rates() -> None:
    """The reported concrete case: bybit 1h +0.0001 vs binance 8h +0.0003.

    Raw, bybit's +0.0001 fails the ``> 0.0001`` bull threshold and the verdict
    collapses to "neutral". Normalized it is +0.0008 — clearly bullish.
    """
    funding = {"binance": 0.0003, "bybit": 0.0001}
    intervals = {"binance": 8.0, "bybit": 1.0}

    normalized, unknown = normalized_funding_map(funding, intervals)
    assert normalized == pytest.approx({"binance": 0.0003, "bybit": 0.0008})
    assert unknown == []

    _spread, consensus = funding_consensus_from_normalized(normalized)
    assert consensus == "bull"

    # Guard the old behaviour explicitly: comparing raw rates says "neutral".
    _raw_spread, raw_consensus = funding_consensus_from_normalized(funding)
    assert raw_consensus == "neutral"


def test_spread_is_computed_in_comparable_units() -> None:
    normalized, _ = normalized_funding_map(
        {"binance": 0.0003, "bybit": 0.0001}, {"binance": 8.0, "bybit": 1.0}
    )
    spread, _consensus = funding_consensus_from_normalized(normalized)
    # 0.0008 - 0.0003, NOT the raw 0.0003 - 0.0001 = 0.0002.
    assert spread == pytest.approx(0.0005)


def test_unknown_interval_venue_is_excluded_and_named() -> None:
    normalized, unknown = normalized_funding_map(
        {"binance": 0.0003, "bybit": 0.0001, "bitget": 0.0009},
        {"binance": 8.0, "bybit": 1.0, "bitget": None},
    )
    assert "bitget" not in normalized
    assert unknown == ["bitget"]


def test_consensus_with_fewer_than_two_venues_is_unknown_not_neutral() -> None:
    spread, consensus = funding_consensus_from_normalized({"binance": 0.0003})
    assert consensus == "unknown"
    assert spread is None


def test_consensus_divergent_and_bear() -> None:
    _s, bear = funding_consensus_from_normalized({"a": -0.0004, "b": -0.0002})
    assert bear == "bear"
    _s2, div = funding_consensus_from_normalized({"a": 0.0009, "b": -0.0002})
    assert div == "divergent"


# ── FIX 3 — homogeneous price type ──────────────────────────────────────────


def test_price_divergence_needs_two_venues() -> None:
    assert price_divergence_from_map({"binance": 100.0}) is None
    assert price_divergence_from_map({}) is None
    assert price_divergence_from_map(None) is None


def test_price_divergence_percent_of_mean() -> None:
    div = price_divergence_from_map({"a": 99.0, "b": 101.0})
    assert div == pytest.approx(2.0)


def test_price_divergence_ignores_nonpositive() -> None:
    assert price_divergence_from_map({"a": 100.0, "b": 0.0}) is None


# ── FIX 8 — WS overlay recomputes consensus, not just spread ────────────────


def _rest_snapshot() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "funding": {"binance": 0.0003, "okx": 0.0004},
        "funding_interval_hours": {"binance": 8.0, "okx": 8.0},
        "funding_8h": {"binance": 0.0003, "okx": 0.0004},
        "funding_unknown_interval": [],
        "funding_spread": 0.0001,
        "funding_consensus": "bull",
        "mark_price": {"binance": 100.0, "okx": 100.1},
        "last_price": {},
        "listed": {"binance": "listed", "okx": "listed"},
        "oi_usd": {},
        "oi_total": None,
    }


def test_ws_overlay_recomputes_consensus_not_only_spread() -> None:
    """A WS rate flipping negative must flip the verdict off "bull"."""
    snap = _rest_snapshot()
    merged = merge_ws_cross_into_snapshot(snap, {"okx": {"fundingRate": -0.0009}})

    assert merged["funding"]["okx"] == pytest.approx(-0.0009)
    # Old bug: spread was recomputed but consensus stayed the stale REST "bull",
    # so the card printed a negative OKX rate under «Фандинг бычий на всех биржах».
    assert merged["funding_consensus"] != "bull"
    assert merged["funding_consensus"] == "divergent"
    assert merged["funding_spread"] == pytest.approx(0.0012)


def test_ws_overlay_normalizes_using_rest_interval_map() -> None:
    snap = _rest_snapshot()
    snap["funding_interval_hours"]["bybit"] = 1.0
    merged = merge_ws_cross_into_snapshot(snap, {"bybit": {"fundingRate": 0.0001}})
    assert merged["funding_8h"]["bybit"] == pytest.approx(0.0008)
    assert merged["funding_consensus"] == "bull"


def test_ws_overlay_marks_unknown_interval_venue() -> None:
    snap = _rest_snapshot()
    merged = merge_ws_cross_into_snapshot(snap, {"bybit": {"fundingRate": 0.0001}})
    assert "bybit" in merged["funding_unknown_interval"]
    assert "bybit" not in merged["funding_8h"]


def test_ws_overlay_recomputes_price_divergence_on_mark_basis() -> None:
    snap = _rest_snapshot()
    merged = merge_ws_cross_into_snapshot(snap, {"okx": {"markPrice": 101.0}})
    assert merged["price_divergence_basis"] == "mark"
    assert merged["price_divergence_pct"] == pytest.approx(0.995, abs=1e-2)


def test_ws_overlay_without_ws_returns_snapshot_untouched() -> None:
    snap = _rest_snapshot()
    assert merge_ws_cross_into_snapshot(snap, None) is snap


# ── FIX 1/4/5/6 — snapshot assembly against faked CCXT venues ───────────────


class _FakeSecondary:
    """Minimal stand-in reproducing each venue's real CCXT parser output."""

    _MARKET = {
        "symbol": "BTC/USDT:USDT",
        "id": "BTCUSDT",
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "type": "swap",
        "swap": True,
        "linear": True,
        "inverse": False,
        "contract": True,
        "active": True,
    }

    def __init__(self, ex_id: str, payloads: dict[str, Any]) -> None:
        self.id = ex_id
        self._payloads = payloads
        self.has = {
            "fetchFundingRate": True,
            "fetchOpenInterest": True,
            "fetchTicker": True,
        }
        self.markets = {"BTC/USDT:USDT": dict(self._MARKET)}

    def market(self, symbol: str) -> dict[str, Any]:
        if symbol in {"BTCUSDT", "BTC/USDT:USDT"}:
            return dict(self._MARKET)
        raise ccxt.BadSymbol(f"{self.id} does not have market symbol {symbol}")

    async def fetch_funding_rate(self, _sym: str) -> dict[str, Any]:
        return self._payloads["funding"]

    async def fetch_open_interest(self, _sym: str) -> dict[str, Any]:
        return self._payloads["oi"]

    async def fetch_ticker(self, _sym: str) -> dict[str, Any]:
        return self._payloads["ticker"]


def _bybit_linear_payloads() -> dict[str, Any]:
    # ccxt bybit: linear → openInterestValue None, amount in base coins.
    return {
        "funding": {"fundingRate": 0.0001, "interval": "1h"},
        "oi": {"openInterestAmount": 120_000.0, "openInterestValue": None},
        "ticker": {"markPrice": 50000.0, "last": 50001.0},
    }


def _okx_payloads() -> dict[str, Any]:
    # ccxt okx: openInterestValue = oiUsd; fetchTicker has NO markPrice.
    return {
        "funding": {"fundingRate": 0.0002, "interval": "8h"},
        "oi": {"openInterestAmount": 80_000.0, "openInterestValue": 4_000_000_000.0},
        "ticker": {"markPrice": None, "last": 50002.0},
    }


def _bitget_payloads() -> dict[str, Any]:
    # ccxt bitget: 'openInterestValue': None is hardcoded.
    return {
        "funding": {"fundingRate": 0.0003, "interval": "8h"},
        "oi": {"openInterestAmount": 30_000.0, "openInterestValue": None},
        "ticker": {"markPrice": 50000.0, "last": 50000.0},
    }


class _StubClient:
    """Drives the real ``fetch_cross_exchange_snapshot`` over fake venues."""

    def __init__(
        self,
        venues: dict[str, _FakeSecondary | None],
        *,
        premium: dict[str, dict[str, float]] | None = None,
        binance_oi: float | None = 200_000.0,
        binance_interval: float | None = 8.0,
    ) -> None:
        from hunt_core.market.client import HuntCcxtClient

        self._venues = venues
        self._premium = premium if premium is not None else {
            "BTCUSDT": {"mark_price": 50000.0, "last_funding_rate": 0.0004}
        }
        self._binance_oi = binance_oi
        self._binance_interval = binance_interval
        self._secondary_exchange_ids = {k: k for k in venues}
        self._secondary_funding_cache: dict[Any, Any] = {}
        self._secondary_oi_cache: dict[Any, Any] = {}
        self.fetch_cross_exchange_snapshot = (
            HuntCcxtClient.fetch_cross_exchange_snapshot.__get__(self)
        )
        for name in (
            "_secondary_listing",
            "_fetch_secondary_funding",
            "_fetch_secondary_oi",
            "_fetch_secondary_ticker",
            "_binance_oi_usd",
            "_binance_funding_interval_hours",
            "_secondary_ttl",
        ):
            setattr(self, name, getattr(HuntCcxtClient, name).__get__(self))
        self._cache_fresh = HuntCcxtClient._cache_fresh

    # -- surface the real client relies on --
    def _bin_sym(self, symbol: str) -> str:
        return symbol.upper()

    async def load_markets(self) -> None:
        return None

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        return self._premium

    async def fetch_funding_info_all(self) -> dict[str, dict[str, float | int]]:
        if self._binance_interval is None:
            return {}
        return {"BTCUSDT": {"funding_interval_hours": self._binance_interval}}

    async def fetch_open_interest(self, _sym: str) -> float | None:
        return self._binance_oi

    async def _get_secondary(self, name: str) -> Any:
        return self._venues.get(name)

    async def _secondary_call(self, _name, ex, factory, *, context, method) -> Any:  # noqa: ANN001
        return await factory()


def _snapshot(client: _StubClient) -> dict[str, Any]:
    return asyncio.run(client.fetch_cross_exchange_snapshot("BTCUSDT"))


def _all_venues() -> dict[str, _FakeSecondary | None]:
    return {
        "bybit": _FakeSecondary("bybit", _bybit_linear_payloads()),
        "okx": _FakeSecondary("okx", _okx_payloads()),
        "bitget": _FakeSecondary("bitget", _bitget_payloads()),
    }


def test_oi_total_sums_every_venue_not_just_okx() -> None:
    """The headline defect: OI Total printed OKX's $4B as a 4-venue total.

    Only OKX populates ``openInterestValue``, so reading it alone yielded a
    single-venue proxy. With per-venue USD notional and Binance included:
      binance 200,000 × 50000 = $10.0B
      bybit   120,000 × 50000 = $6.0B
      okx     openInterestValue = $4.0B
      bitget   30,000 × 50000 = $1.5B  → $21.5B
    """
    snap = _snapshot(_StubClient(_all_venues()))

    assert snap["oi_usd"]["binance"] == pytest.approx(10_000_000_000.0)
    assert snap["oi_usd"]["bybit"] == pytest.approx(6_000_000_000.0)
    assert snap["oi_usd"]["okx"] == pytest.approx(4_000_000_000.0)
    assert snap["oi_usd"]["bitget"] == pytest.approx(1_500_000_000.0)
    assert snap["oi_total"] == pytest.approx(21_500_000_000.0)
    assert snap["oi_venues"] == ["binance", "bitget", "bybit", "okx"]
    assert snap["oi_total_partial"] is False


def test_oi_base_amounts_are_preserved_for_the_dataset() -> None:
    snap = _snapshot(_StubClient(_all_venues()))
    assert snap["oi_base"]["bybit"] == pytest.approx(120_000.0)
    assert snap["oi_base"]["binance"] == pytest.approx(200_000.0)


def test_oi_total_is_partial_when_a_venue_cannot_be_converted() -> None:
    """No price for a base-coin-only venue → contribute nothing, flag partial."""
    venues = _all_venues()
    venues["bybit"] = _FakeSecondary(
        "bybit",
        {
            **_bybit_linear_payloads(),
            "ticker": {"markPrice": None, "last": None},
        },
    )
    snap = _snapshot(_StubClient(venues))

    assert "bybit" not in snap["oi_usd"]  # never fabricated
    assert snap["oi_base"]["bybit"] == pytest.approx(120_000.0)
    assert snap["oi_total_partial"] is True
    assert snap["oi_total"] == pytest.approx(15_500_000_000.0)


def test_oi_total_is_partial_when_a_venue_is_unavailable() -> None:
    venues = _all_venues()
    venues["bitget"] = None  # client init failed
    snap = _snapshot(_StubClient(venues))
    assert snap["listed"]["bitget"] == "unknown"
    assert snap["oi_total_partial"] is True


def test_oi_total_none_when_nothing_known() -> None:
    snap = _snapshot(_StubClient({}, premium={}, binance_oi=None))
    assert snap["oi_total"] is None


def test_markprice_is_used_not_the_phantom_mark_key() -> None:
    """``mark`` is not a CCXT ticker key — secondaries must contribute markPrice.

    bybit/bitget report markPrice 50000 while their last is 50001/50000. If the
    phantom ``mark`` key were still read, every secondary would fall through to
    ``last`` and be compared against Binance's MARK — measuring basis, not
    cross-venue divergence.
    """
    snap = _snapshot(_StubClient(_all_venues()))

    assert snap["mark_price"]["bybit"] == pytest.approx(50000.0)
    assert snap["mark_price"]["bitget"] == pytest.approx(50000.0)
    assert snap["last_price"]["bybit"] == pytest.approx(50001.0)
    # OKX fetchTicker genuinely has no markPrice → absent, not backfilled by last.
    assert "okx" not in snap["mark_price"]
    assert snap["last_price"]["okx"] == pytest.approx(50002.0)


def test_price_divergence_compares_one_homogeneous_price_type() -> None:
    snap = _snapshot(_StubClient(_all_venues()))
    assert snap["price_divergence_basis"] == "mark"
    # binance/bybit/bitget all mark 50000 → no divergence at all.
    assert snap["price_divergence_pct"] == pytest.approx(0.0)


def test_price_divergence_falls_back_to_last_but_stays_homogeneous() -> None:
    venues = {"okx": _FakeSecondary("okx", _okx_payloads())}
    snap = _snapshot(_StubClient(venues, premium={"BTCUSDT": {"last_funding_rate": 0.0004}}))
    # No binance mark and no okx mark → fewer than 2 marks; last-vs-last only.
    assert snap["mark_price"] == {}
    assert snap["price_divergence_basis"] is None
    assert snap["price_divergence_pct"] is None


def test_funding_is_normalized_in_the_snapshot() -> None:
    snap = _snapshot(_StubClient(_all_venues()))
    assert snap["funding"]["bybit"] == pytest.approx(0.0001)  # raw kept
    assert snap["funding_interval_hours"]["bybit"] == pytest.approx(1.0)
    assert snap["funding_8h"]["bybit"] == pytest.approx(0.0008)  # normalized
    assert snap["funding_consensus"] == "bull"


def test_missing_symbol_does_not_fabricate_binance_funding_zero() -> None:
    """Absent from the premium index = unknown, not a 0.0 rate.

    The old ``float(pr.get("last_funding_rate") or 0)`` put binance=0.0 into the
    rates list, forcing consensus to "neutral" and inflating the spread.
    """
    snap = _snapshot(_StubClient(_all_venues(), premium={}))

    assert "binance" not in snap["funding"]
    assert snap["funding_consensus"] == "bull"  # the three real venues agree
    assert snap["listed"]["binance"] == "not_listed"


def test_secondary_funding_absent_is_none_not_zero() -> None:
    venues = _all_venues()
    venues["bybit"] = _FakeSecondary(
        "bybit", {**_bybit_linear_payloads(), "funding": {"fundingRate": None}}
    )
    snap = _snapshot(_StubClient(venues))
    assert "bybit" not in snap["funding"]
    assert "bybit" not in snap["funding_8h"]


def test_zero_funding_rate_is_kept_as_real_data() -> None:
    """0.0 is a legitimate flat rate — only absence is unknown."""
    venues = _all_venues()
    venues["bybit"] = _FakeSecondary(
        "bybit",
        {**_bybit_linear_payloads(), "funding": {"fundingRate": 0.0, "interval": "8h"}},
    )
    snap = _snapshot(_StubClient(venues))
    assert snap["funding"]["bybit"] == 0.0
    assert snap["funding_8h"]["bybit"] == 0.0


def test_listed_is_tristate_unavailable_venue_is_unknown_not_delisted() -> None:
    """A failed venue client must not be reported as «не листится»."""
    venues = _all_venues()
    venues["okx"] = None  # _get_secondary returned None (permanent failure)
    snap = _snapshot(_StubClient(venues))

    assert snap["listed"]["okx"] == "unknown"
    assert snap["listed"]["bybit"] == "listed"
    assert snap["listed"]["binance"] == "listed"


def test_snapshot_is_stamped_with_fetched_at() -> None:
    snap = _snapshot(_StubClient(_all_venues()))
    assert isinstance(snap["fetched_at_ms"], float)
    assert snap["fetched_at_ms"] > 0


def test_binance_interval_unavailable_excludes_it_from_consensus() -> None:
    snap = _snapshot(_StubClient(_all_venues(), binance_interval=None))
    assert "binance" in snap["funding"]
    assert "binance" not in snap["funding_8h"]
    assert "binance" in snap["funding_unknown_interval"]


# ── The old expressions, pinned against the same payloads ───────────────────
#
# These reproduce the pre-fix one-liners verbatim to pin WHY they were wrong. If
# a future refactor reintroduces one of them, the tests above break and these
# document the exact mechanism.


def test_old_oi_expression_would_yield_an_okx_only_total() -> None:
    """``float(r.get("openInterestValue") or r.get("openInterest") or 0) or None``.

    Only OKX populates ``openInterestValue``, so the old expression silently
    reduced a 4-venue "OI Total" to a single-venue proxy — $4.0B instead of
    $21.5B, 5x under.
    """
    payloads = {
        "bybit": _bybit_linear_payloads()["oi"],
        "okx": _okx_payloads()["oi"],
        "bitget": _bitget_payloads()["oi"],
    }
    old_total = 0.0
    for r in payloads.values():
        old = float(r.get("openInterestValue") or r.get("openInterest") or 0) or None
        if old is not None:
            old_total += old

    assert old_total == pytest.approx(4_000_000_000.0)  # OKX alone
    assert _snapshot(_StubClient(_all_venues()))["oi_total"] == pytest.approx(21_500_000_000.0)


def test_openinterest_fallback_key_never_exists() -> None:
    """``safe_open_interest`` rebuilds the dict from a fixed key mapping.

    ``openInterest`` is not among its keys, so ``r.get("openInterest")`` was dead
    code that could never rescue the base-coin venues.
    """
    for payloads in (_bybit_linear_payloads(), _okx_payloads(), _bitget_payloads()):
        assert "openInterest" not in payloads["oi"]


def test_old_mark_expression_would_fall_through_to_last() -> None:
    """``float(t.get("mark") or t.get("last") or 0)`` — ``mark`` is a phantom key.

    Every secondary silently contributed its LAST trade while Binance contributed
    its MARK, so "price divergence" largely measured mark-vs-last basis.
    """
    ticker = _bybit_linear_payloads()["ticker"]
    old = float(ticker.get("mark") or ticker.get("last") or 0)
    assert old == 50001.0  # last, not the 50000.0 markPrice

    snap = _snapshot(_StubClient(_all_venues()))
    assert snap["mark_price"]["bybit"] == pytest.approx(50000.0)  # the real mark


# ── FIX 6 — negative results are not cached for the full TTL ────────────────


def test_negative_secondary_result_gets_a_short_ttl() -> None:
    from hunt_core.market.client import (
        _CACHE_TTL,
        _SECONDARY_NEGATIVE_CACHE_TTL_S,
        HuntCcxtClient,
    )

    ttl = HuntCcxtClient._secondary_ttl
    assert ttl({"oi_usd": None, "oi_base": None}, "secondary_oi") == _SECONDARY_NEGATIVE_CACHE_TTL_S
    assert ttl({"oi_usd": 1.0, "oi_base": None}, "secondary_oi") == float(_CACHE_TTL["secondary_oi"])
    assert _SECONDARY_NEGATIVE_CACHE_TTL_S < _CACHE_TTL["secondary_oi"]


# ── Card rendering ──────────────────────────────────────────────────────────


def test_card_does_not_claim_total_when_partial() -> None:
    snap = _snapshot(_StubClient(_all_venues()))
    snap["oi_total_partial"] = True
    out = format_cross_exchange_section(snap)
    assert "OI Total" not in out
    assert "частичный" in out


def test_card_names_the_venues_behind_the_oi_total() -> None:
    out = format_cross_exchange_section(_snapshot(_StubClient(_all_venues())))
    assert "OI Total" in out
    assert "$21.5B" in out
    assert "BNC+BGT+BYB+OKX" in out


def test_card_renders_unknown_listing_as_question_mark() -> None:
    venues = _all_venues()
    venues["okx"] = None
    out = format_cross_exchange_section(_snapshot(_StubClient(venues)))
    assert "OKX?" in out
    assert "OKX✗" not in out
    assert "биржа недоступна" in out


def test_card_shows_normalized_funding_with_native_interval_tag() -> None:
    out = format_cross_exchange_section(_snapshot(_StubClient(_all_venues())))
    assert "Funding (за 8ч)" in out
    # bybit raw +0.0001/1h renders as its per-8h equivalent, tagged with (1ч).
    assert "BYB +0.0800%(1ч)" in out
    assert "Фандинг бычий на всех биржах" in out


def test_card_flags_venues_excluded_for_unknown_interval() -> None:
    out = format_cross_exchange_section(_snapshot(_StubClient(_all_venues(), binance_interval=None)))
    assert "интервал фандинга неизвестен" in out


def test_card_labels_the_price_basis() -> None:
    out = format_cross_exchange_section(_snapshot(_StubClient(_all_venues())))
    assert "Цены (mark):" in out


def test_card_empty_for_empty_snapshot() -> None:
    assert format_cross_exchange_section({}) == ""
