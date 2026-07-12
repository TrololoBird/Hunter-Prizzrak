"""Pure (no-network) parts of the CoinGecko market-cap source: ticker normalisation,
response parsing, and disk-cache freshness. The network fetch itself is silent-fail and
exercised only when the factor is enabled live."""

from __future__ import annotations

import time

from hunt_core.prizrak import marketcap_source as ms


def test_base_ticker_strips_quote_and_settlement() -> None:
    assert ms._base_ticker("BTCUSDT") == "BTC"
    assert ms._base_ticker("BTC/USDT:USDT") == "BTC"
    assert ms._base_ticker("ondo/usdc") == "ONDO"
    assert ms._base_ticker("ETHUSD") == "ETH"
    # A bare ticker that happens to end in a quote-like string but IS the whole symbol.
    assert ms._base_ticker("USDT") == "USDT"


def test_parse_market_caps_filters_bad_points() -> None:
    payload = {"market_caps": [[1000, 5.0], [2000, None], [3000, 7.0], "junk", [4000]]}
    assert ms._parse_market_caps(payload) == [[1000.0, 5.0], [3000.0, 7.0]]
    assert ms._parse_market_caps({}) == []
    assert ms._parse_market_caps({"market_caps": "nope"}) == []


def test_cache_freshness() -> None:
    now_ms = time.time() * 1000
    assert ms._cache_fresh({"fetched_ms": now_ms}, ttl_s=3600) is True
    assert ms._cache_fresh({"fetched_ms": now_ms - 4000 * 1000}, ttl_s=3600) is False
    assert ms._cache_fresh({}, ttl_s=3600) is False


def test_cache_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ms, "MARKETCAP_CACHE", tmp_path)
    series = [[1000.0, 5.0], [2000.0, 6.0]]
    ms._write_cache("FOOUSDT", 90, series)
    entry = ms._read_cache("FOOUSDT")
    assert entry is not None
    assert entry["series"] == series
    assert entry["days"] == 90
    assert "fetched_ms" in entry


def test_stale_fallback() -> None:
    assert ms._stale(None) is None
    assert ms._stale({"series": [[1.0, 2.0]]}) == [[1.0, 2.0]]
    assert ms._stale({"nope": 1}) is None
