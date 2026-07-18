"""Multi-venue cross-funding — fail-loud across venues (ADR-0002): a stale/absent venue reads None,
never a fabricated rate, so a divergence signal is only ever computed from fresh venues."""
from __future__ import annotations

import time

from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.state import PlaneStamp, Source, SymbolState


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
