"""build_market_view — the native MarketView assembly (ADR-0004 S2/S6), fail-loud read-through.

Fakes the engine surface (no network): a MarketSnapshot of fresh planes + a stub exchange with the
trades/liquidations read-through caches. Klines conversion is covered by the toolkit tests; here
timeframes=() isolates the plane→field mapping + price resolution + fail-loud None."""
from __future__ import annotations

import time
from typing import Any

from hunt_core.engine.state import MarketSnapshot, Plane, Source
from hunt_core.view.build import build_market_view

_NOW = int(time.time() * 1000)


def _plane(name: str, value: Any) -> Plane[Any]:
    return Plane(name, value, Source.WS, _NOW, _NOW, 10_000_000)


class _StubExchange:
    def __init__(self, trades: Any = None, liquidations: Any = None) -> None:
        self.trades = {"BTC/USDT:USDT": trades} if trades else {}
        self.liquidations = liquidations or []


class _StubEngine:
    def __init__(self, snap: MarketSnapshot, exchange: _StubExchange) -> None:
        self._snap = snap
        self.exchange = exchange

    def snapshot(self, symbol: str, required: Any) -> MarketSnapshot:
        return self._snap

    def plane_ages(self, symbol: str) -> dict[str, float]:
        return {"book": 0.4, "mark": 1.0}


class _StubMulti:
    def __init__(self, snap: MarketSnapshot, exchange: _StubExchange) -> None:
        self.primary = _StubEngine(snap, exchange)

    def snapshot(self, symbol: str, required: Any) -> MarketSnapshot:
        return self.primary.snapshot(symbol, required)

    def cross_funding(self, s: str) -> dict[str, float | None]:
        return {"binance": 0.0001, "okx": 0.0002, "bybit": None}

    def cross_open_interest(self, s: str) -> dict[str, float | None]:
        return {"binance": 1000.0}

    def cross_long_short(self, s: str) -> dict[str, float | None]:
        return {"binance": 1.5}

    def cross_liquidation_notional(self, s: str) -> dict[str, dict[str, float] | None]:
        return {"binance": None}


def _snap(planes: dict[str, Any], not_ready: tuple[str, ...] = ()) -> MarketSnapshot:
    return MarketSnapshot("BTC/USDT:USDT", _NOW, {k: _plane(k, v) for k, v in planes.items()}, not_ready)


def test_build_maps_planes_to_typed_fields() -> None:
    snap = _snap({
        "ticker": {"last": 64000.0, "quoteVolume": 1_234_567.0},
        "mark": {"markPrice": 64010.0, "indexPrice": 64005.0},
        "book": {"bids": [[63999.0, 5.0]], "asks": [[64001.0, 3.0]]},
        "funding": 0.0001,
        "oi": 1_000_000.0,
    })
    trades = [
        {"side": "buy", "price": 64000.0, "amount": 2.0, "timestamp": _NOW - 1000},
        {"side": "sell", "price": 64000.0, "amount": 1.0, "timestamp": _NOW - 500},
    ]
    multi = _StubMulti(snap, _StubExchange(trades=trades))
    view = build_market_view(multi, "BTC/USDT:USDT", timeframes=(), now_ms=_NOW)  # type: ignore[arg-type]
    assert view is not None
    assert view.last_price == 64000.0 and view.price_source == "ticker"
    assert view.quote_volume_24h == 1_234_567.0  # futures 24h quote-vol off the ticker plane
    assert view.derivs.mark == 64010.0 and view.derivs.funding == 0.0001 and view.derivs.oi == 1_000_000.0
    assert view.book.bid == 63999.0 and view.book.ask == 64001.0
    assert view.book.depth_imbalance is not None  # from book-math
    assert view.orderflow.cvd_1m == 64000.0  # (2-1)*64000 net buy notional
    assert view.orderflow.buy_ratio_60s is not None
    assert view.cross.funding == {"binance": 0.0001, "okx": 0.0002, "bybit": None}
    assert view.derivs.funding_zscore is None  # deferred to features/ (needs history)


def test_build_falls_back_to_mark_price() -> None:
    snap = _snap({"mark": {"markPrice": 64010.0, "indexPrice": 64005.0}})  # no ticker
    view = build_market_view(_StubMulti(snap, _StubExchange()), "BTC/USDT:USDT", timeframes=(), now_ms=_NOW)  # type: ignore[arg-type]
    assert view is not None and view.last_price == 64010.0 and view.price_source == "mark"
    assert view.quote_volume_24h is None  # no ticker plane → no 24h quote-vol (fail-loud, not 0)


def test_build_returns_none_when_no_price() -> None:
    # no ticker, no mark → cannot resolve a price → no view (never fabricated)
    view = build_market_view(_StubMulti(_snap({"funding": 0.0001}), _StubExchange()), "BTC/USDT:USDT", timeframes=(), now_ms=_NOW)  # type: ignore[arg-type]
    assert view is None


def test_build_absent_planes_are_none_not_fabricated() -> None:
    snap = _snap({"ticker": {"last": 64000.0}}, not_ready=("mark: absent", "book: absent"))
    view = build_market_view(_StubMulti(snap, _StubExchange()), "BTC/USDT:USDT", timeframes=(), now_ms=_NOW)  # type: ignore[arg-type]
    assert view is not None
    assert view.derivs.mark is None and view.derivs.funding is None  # absent → None (I-6)
    assert view.book.bid is None and view.orderflow.cvd_1m is None
    assert "mark: absent" in view.not_ready
