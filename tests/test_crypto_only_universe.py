"""Scanner universe is crypto-only — tokenized equities/commodities are excluded.

Binance USDⓈ-M lists EQUITY (BABA, COIN, MSTR…), COMMODITY (NATGAS), INDEX and
PREMARKET perps beside real crypto. The manipulation method is a crypto pump/dump
edge, so apply_quality_gates must drop anything whose underlyingType != COIN, while
failing open when the type is unknown (so an API shape change can't empty the universe).
"""
from __future__ import annotations

from hunt_core.market.symbols import is_crypto_underlying, underlying_type_of
from hunt_core.scanner.prescan import apply_quality_gates


def _row(symbol: str, underlying: str) -> dict:
    return {
        "symbol": symbol, "underlying_type": underlying,
        "quote_volume": 5e8, "last_price": 100.0,
        "high_price": 105.0, "low_price": 98.0,
    }


def test_equity_and_commodity_rejected() -> None:
    for sym, ut in [("COINUSDT", "EQUITY"), ("BABAUSDT", "EQUITY"),
                    ("NATGASUSDT", "COMMODITY"), ("EWYUSDT", "KR_EQUITY")]:
        ok, reason = apply_quality_gates(_row(sym, ut))
        assert not ok and reason == "non_coin_underlying", (sym, ut, ok, reason)


def test_coin_and_unknown_pass() -> None:
    ok_coin, _ = apply_quality_gates(_row("BTCUSDT", "COIN"))
    assert ok_coin
    # Unknown/absent type fails open — must NOT be dropped as non-coin.
    ok_unknown, reason = apply_quality_gates(_row("NEWUSDT", ""))
    assert ok_unknown, reason


def test_symbol_helpers() -> None:
    assert underlying_type_of({"info": {"underlyingType": "equity"}}) == "EQUITY"
    assert underlying_type_of({}) == ""
    assert is_crypto_underlying({"info": {"underlyingType": "COIN"}})
    assert is_crypto_underlying({})  # unknown fails open
    assert not is_crypto_underlying({"info": {"underlyingType": "EQUITY"}})
