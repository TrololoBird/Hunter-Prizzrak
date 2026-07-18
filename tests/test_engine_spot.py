"""SpotEngine consumer surface (E6b) — spot_enrichments fail-loud over populated planes.

Follows the multi-engine test pattern: construct the engine, populate SymbolState planes + a stub
trades cache by hand, and assert the read logic (no network)."""
from __future__ import annotations

import time
from typing import Any

from hunt_core.engine.spot import SpotEngine
from hunt_core.engine.state import PlaneStamp, Source


class _StubEx:
    def __init__(self, trades: dict[str, Any]) -> None:
        self.trades = trades


def _fresh(now: int, bound_ms: int = 10_000) -> PlaneStamp:
    return PlaneStamp(Source.WS, now, now, bound_ms)


def _engine_with_state(symbol: str, *, ticker: Any, frame: Any, trades: Any) -> SpotEngine:
    eng = SpotEngine([symbol])
    now = int(time.time() * 1000)
    st = eng._state(symbol)
    if ticker is not None:
        st.put_value("ticker", ticker, _fresh(now))
    if frame is not None:
        st.seed_frame("spot_1m", frame, _fresh(now, 80_000))
    if trades is not None:
        st.stamp_only("trades", _fresh(now, 60_000))
    eng._ex = _StubEx({symbol: trades or []})
    return eng


def test_spot_enrichments_full_payload() -> None:
    ticker = {"last": 100.0, "bid": 99.0, "ask": 101.0, "quoteVolume": 5000.0, "timestamp": 1}
    frame = [[0, 1, 1, 1, 100.0, 1], [60_000, 1, 1, 1, 101.0, 1]]  # prev 100 → forming 101 = +1%
    trades = [
        {"side": "buy", "price": 100.0, "amount": 3.0},
        {"side": "sell", "price": 100.0, "amount": 1.0},
    ]
    eng = _engine_with_state("BTC/USDT", ticker=ticker, frame=frame, trades=trades)
    out = eng.spot_enrichments("BTC/USDT", futures_mid=100.5)
    assert out == {
        "spot_futures_spread_bps": 50.0,  # perp 0.5% above spot mid(100)
        "spot_quote_volume_24h": 5000.0,
        "spot_lead_return_1m": 1.0,
        "spot_taker_delta_usd": 200.0,
        "spot_taker_buy_ratio": 0.75,
    }


def test_spot_enrichments_empty_when_ticker_absent() -> None:
    eng = _engine_with_state("BTC/USDT", ticker=None, frame=None, trades=None)
    assert eng.spot_enrichments("BTC/USDT", futures_mid=100.0) == {}


def test_spot_enrichments_empty_when_ticker_stale() -> None:
    eng = SpotEngine(["BTC/USDT"])
    now = int(time.time() * 1000)
    eng._state("BTC/USDT").put_value(
        "ticker", {"last": 100.0}, PlaneStamp(Source.WS, now - 10_000_000, now, 10_000)
    )
    eng._ex = _StubEx({})
    assert eng.spot_enrichments("BTC/USDT", futures_mid=100.0) == {}  # stale → нет данных


def test_spot_enrichments_omits_none_fields() -> None:
    # No futures_mid → spread None (omitted); no frame → lead omitted; no fresh trades → taker omitted.
    ticker = {"last": 100.0, "bid": 99.0, "ask": 101.0, "quoteVolume": 7000.0}
    eng = SpotEngine(["BTC/USDT"])
    now = int(time.time() * 1000)
    eng._state("BTC/USDT").put_value("ticker", ticker, _fresh(now))
    eng._ex = _StubEx({})
    out = eng.spot_enrichments("BTC/USDT", futures_mid=None)
    assert out == {"spot_quote_volume_24h": 7000.0}  # only the field with data


def test_spot_enrichments_untracked_symbol_is_empty() -> None:
    eng = SpotEngine(["BTC/USDT"])
    eng._ex = _StubEx({})
    assert eng.spot_enrichments("ETH/USDT", futures_mid=100.0) == {}
