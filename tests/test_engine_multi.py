"""Multi-venue cross-funding — fail-loud across venues (ADR-0002): a stale/absent venue reads None,
never a fabricated rate, so a divergence signal is only ever computed from fresh venues."""
from __future__ import annotations

import time

from hunt_core.engine.api import _resolve
from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.state import PlaneStamp, Source, SymbolState


class _ExWithLiq:
    """Minimal stand-in whose ``.liquidations`` models ccxt's flat, list-like ``ArrayCache``."""

    def __init__(self, liqs: object) -> None:
        self.liquidations = liqs


def test_resolve_liq_filters_flat_arraycache_by_symbol() -> None:
    # ccxt stores liquidations as ONE flat cache across symbols (None until the first !forceOrder),
    # NOT a per-symbol dict — a plain list models it and, like ArrayCache, has no .get (pins the fix).
    st = SymbolState("BTC/USDT:USDT")
    btc = {"symbol": "BTC/USDT:USDT", "contracts": 1.0, "price": 100.0, "side": "sell"}
    eth = {"symbol": "ETH/USDT:USDT", "contracts": 2.0, "price": 50.0, "side": "buy"}
    ex = _ExWithLiq([btc, eth])
    assert _resolve(ex, st, "BTC/USDT:USDT", "liq") == [btc]  # filtered to this symbol
    assert _resolve(_ExWithLiq(None), st, "BTC/USDT:USDT", "liq") is None  # pre-first-event
    assert _resolve(_ExWithLiq([]), st, "BTC/USDT:USDT", "liq") is None  # empty cache
    assert _resolve(ex, st, "SOL/USDT:USDT", "liq") is None  # no events for this symbol → None


def test_cross_funding_is_fail_loud_per_venue() -> None:
    me = MultiEngine(["BTC/USDT:USDT"], timeframes=("1m",), secondaries=("okx", "bybit"))
    now = int(time.time() * 1000)

    fresh = SymbolState("BTC/USDT:USDT")
    fresh.put_value("funding", 0.00012, PlaneStamp(Source.REST_SEED, now, now, 180_000))
    me._cross["okx"]["BTC/USDT:USDT"] = fresh

    stale = SymbolState("BTC/USDT:USDT")
    stale.put_value("funding", 0.09, PlaneStamp(Source.REST_SEED, now - 10_000_000, now, 180_000))
    me._cross["bybit"]["BTC/USDT:USDT"] = stale

    cf = me.cross_funding("BTC/USDT:USDT")
    assert cf["okx"] == 0.00012  # fresh → returned
    assert cf["bybit"] is None  # stale → None, not the stale 0.09
    assert cf["binance"] is None  # primary not started → absent, not fabricated


def test_cross_funding_absent_venue_is_none() -> None:
    me = MultiEngine(["BTC/USDT:USDT"], timeframes=("1m",), secondaries=("okx",))
    cf = me.cross_funding("BTC/USDT:USDT")
    assert cf == {"binance": None, "okx": None}  # nothing polled yet → all None, none invented


def test_cross_long_short_is_fail_loud_per_venue() -> None:
    me = MultiEngine(["BTC/USDT:USDT"], timeframes=("1m",), secondaries=("okx", "bybit"))
    now = int(time.time() * 1000)

    fresh = SymbolState("BTC/USDT:USDT")
    fresh.put_value("lsr", 1.8, PlaneStamp(Source.REST_SEED, now, now, 360_000))
    me._cross["okx"]["BTC/USDT:USDT"] = fresh

    stale = SymbolState("BTC/USDT:USDT")
    stale.put_value("lsr", 3.0, PlaneStamp(Source.REST_SEED, now - 10_000_000, now, 360_000))
    me._cross["bybit"]["BTC/USDT:USDT"] = stale

    ls = me.cross_long_short("BTC/USDT:USDT")
    assert ls["okx"] == 1.8  # fresh → returned
    assert ls["bybit"] is None  # stale → None, not the stale 3.0
    assert ls["binance"] is None  # primary not started → absent, not fabricated


def test_cross_liquidations_is_fail_loud_per_venue() -> None:
    me = MultiEngine(["BTC/USDT:USDT"], timeframes=("1m",), secondaries=("okx", "bybit"))
    now = int(time.time() * 1000)
    events = [{"contracts": 2.0, "price": 100.0, "contractSize": 0.01, "side": "sell"}]

    fresh = SymbolState("BTC/USDT:USDT")
    fresh.put_value("liq", events, PlaneStamp(Source.REST_SEED, now, now, 180_000))
    me._cross["okx"]["BTC/USDT:USDT"] = fresh

    stale = SymbolState("BTC/USDT:USDT")
    stale.put_value("liq", events, PlaneStamp(Source.REST_SEED, now - 10_000_000, now, 180_000))
    me._cross["bybit"]["BTC/USDT:USDT"] = stale

    liq = me.cross_liquidations("BTC/USDT:USDT")
    assert liq["okx"] == events  # fresh → the recent events
    assert liq["bybit"] is None  # stale → None
    assert liq["binance"] is None  # primary not started → None

    # Notional is computed from the event's own contractSize (payload baseValue/quoteValue ignored).
    notional = me.cross_liquidation_notional("BTC/USDT:USDT")
    assert notional["okx"] == {"long": 2.0, "short": 0.0, "total": 2.0}  # 2 * 0.01 * 100, sell=long
    assert notional["bybit"] is None
    assert notional["binance"] is None
