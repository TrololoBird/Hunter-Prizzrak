"""Pure liquidation-notional derivation — computed contracts×contractSize×price, fail-loud.

The ccxt liquidation payload's ``baseValue``/``quoteValue`` are ``None`` on the WS streams, so the
engine computes notional itself; these pin the side-attribution and the fail-loud skips (no event
is ever counted as a fabricated zero-notional)."""
from __future__ import annotations

from typing import Any

from hunt_core.engine.liquidations import liquidation_notional, market_contract_size


def _ev(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"contracts": 2.0, "price": 100.0, "contractSize": 1.0, "side": "sell"}
    base.update(kw)
    return base


def test_side_attribution_sell_is_long_buy_is_short() -> None:
    # ccxt unifies side = the force-order side: SELL force-order liquidates a LONG, BUY a SHORT.
    out = liquidation_notional([_ev(side="sell"), _ev(side="buy", contracts=1.0)])
    assert out["long"] == 200.0  # 2 * 1 * 100
    assert out["short"] == 100.0  # 1 * 1 * 100
    assert out["total"] == 300.0


def test_event_contract_size_preferred_over_fallback() -> None:
    out = liquidation_notional([_ev(contractSize=0.01)], contract_size=1.0)
    assert out["total"] == 2.0  # 2 * 0.01 * 100 — the event's own size wins over the fallback


def test_contract_size_fallback_used_when_event_omits_it() -> None:
    ev = _ev()
    del ev["contractSize"]
    out = liquidation_notional([ev], contract_size=0.01)  # OKX-style non-unit contract
    assert out["total"] == 2.0  # 2 * 0.01 * 100


def test_missing_contracts_or_price_is_skipped_not_zero() -> None:
    ev = _ev()
    del ev["contracts"]
    out = liquidation_notional([ev, _ev(price=None)])
    assert out == {"long": 0.0, "short": 0.0, "total": 0.0}  # both uncomputable → skipped, not faked


def test_no_contract_size_anywhere_is_skipped() -> None:
    ev = _ev()
    del ev["contractSize"]
    out = liquidation_notional([ev])  # no fallback provided → cannot compute → skip fail-loud
    assert out["total"] == 0.0


def test_non_finite_values_are_skipped() -> None:
    out = liquidation_notional([_ev(price=float("nan")), _ev(contracts=float("inf"))])
    assert out["total"] == 0.0


def test_side_unknown_counts_total_but_not_attribution() -> None:
    out = liquidation_notional([_ev(side=None)])
    assert out["total"] == 200.0  # counted in total…
    assert out["long"] == 0.0 and out["short"] == 0.0  # …but not attributable to a side


def test_empty_and_none_are_all_zero() -> None:
    assert liquidation_notional([]) == {"long": 0.0, "short": 0.0, "total": 0.0}
    assert liquidation_notional(None) == {"long": 0.0, "short": 0.0, "total": 0.0}


def test_non_dict_events_are_ignored() -> None:
    out = liquidation_notional([None, "x", 5, _ev()])  # type: ignore[list-item]
    assert out["total"] == 200.0  # only the one real event counts


class _FakeExchange:
    def __init__(self, contract_size: Any) -> None:
        self._cs = contract_size

    def market(self, symbol: str) -> dict[str, Any]:
        if self._cs == "raise":
            raise KeyError(symbol)
        return {"contractSize": self._cs}


def test_market_contract_size_reads_positive_float() -> None:
    assert market_contract_size(_FakeExchange(0.01), "BTC/USDT:USDT") == 0.01


def test_market_contract_size_none_on_unknown_symbol() -> None:
    assert market_contract_size(_FakeExchange("raise"), "BTC/USDT:USDT") is None


def test_market_contract_size_none_on_nonpositive_or_missing() -> None:
    assert market_contract_size(_FakeExchange(0.0), "BTC/USDT:USDT") is None
    assert market_contract_size(_FakeExchange(None), "BTC/USDT:USDT") is None
