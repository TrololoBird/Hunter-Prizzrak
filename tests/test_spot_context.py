"""Spot companion context path — metrics, weekly ladder, deep-panel render.

Task E (spot-CCXT audit): the spot fields were produced but consumed by nobody
(orphan telemetry). These tests pin the now-live path: SpotMetrics carries the
24h spot quote volume, the weekly full-history ladder mirrors Prizrak's macro
spot levels, and the deep panel renders all of it.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from hunt_core.market.spot import HuntCcxtSpotCompanion, SpotMetrics
from hunt_core.prizrak.format_telegram import _spot_context_text
from hunt_core.prizrak.structure import spot_weekly_ladder


def _companion_no_network() -> HuntCcxtSpotCompanion:
    """Instance without touching the network / creating a ccxt exchange."""
    comp = object.__new__(HuntCcxtSpotCompanion)
    comp._cache = {}
    comp._weekly_cache = {}
    return comp


# ---------------------------------------------------------------------------
# SpotMetrics / enrichments
# ---------------------------------------------------------------------------


def test_enrichments_include_quote_volume() -> None:
    comp = _companion_no_network()
    comp._cache["BTCUSDT"] = SpotMetrics(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        spot_lead_return_1m=0.12,
        spot_futures_spread_bps=4.5,
        spot_quote_volume_24h=1_234_567.0,
        fetched_at=time.monotonic(),
    )
    payload = comp.enrichments_for("BTCUSDT")
    assert payload["spot_lead_return_1m"] == 0.12
    assert payload["spot_futures_spread_bps"] == 4.5
    assert payload["spot_quote_volume_24h"] == 1_234_567.0


def test_enrichments_zero_volume_is_valid_data() -> None:
    """0.0 spot volume is a real observation (dead spot market), not absence."""
    comp = _companion_no_network()
    comp._cache["XUSDT"] = SpotMetrics(
        symbol="XUSDT",
        spot_price=1.0,
        spot_lead_return_1m=None,
        spot_futures_spread_bps=None,
        spot_quote_volume_24h=0.0,
        fetched_at=time.monotonic(),
    )
    assert comp.enrichments_for("XUSDT") == {"spot_quote_volume_24h": 0.0}


def test_enrichments_stale_cache_empty() -> None:
    comp = _companion_no_network()
    comp._cache["BTCUSDT"] = SpotMetrics(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        spot_lead_return_1m=0.1,
        spot_futures_spread_bps=1.0,
        spot_quote_volume_24h=1.0,
        fetched_at=time.monotonic() - 999.0,
    )
    assert comp.enrichments_for("BTCUSDT", max_age_seconds=120.0) == {}


# ---------------------------------------------------------------------------
# Weekly ladder
# ---------------------------------------------------------------------------


def _bar(ts: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> list[float]:
    return [float(ts), o, h, l, c, v]


def _weekly_with_pivots() -> list[list[float]]:
    """Synthetic weekly series: a swing low near 50, swing high near 100, low near 60."""
    px = [80, 74, 66, 58, 50, 58, 66, 78, 88, 96, 100, 94, 84, 74, 66, 60, 66, 72, 76, 74]
    bars = []
    for i, p in enumerate(px):
        bars.append(_bar(i, p, p + 2.0, p - 2.0, p + (1.0 if i % 2 else -1.0)))
    return bars


def test_spot_weekly_ladder_splits_below_above() -> None:
    ladder = spot_weekly_ladder(_weekly_with_pivots(), price=70.0)
    assert ladder["source"] == "spot_1w"
    assert ladder["bars_used"] == 20
    below = [float(lv["price"]) for lv in ladder["below"]]
    above = [float(lv["price"]) for lv in ladder["above"]]
    assert below and above
    assert all(p < 70.0 for p in below)
    assert all(p >= 70.0 for p in above)
    # Ordered by distance from price.
    assert below == sorted(below, key=lambda p: 70.0 - p)
    assert above == sorted(above, key=lambda p: p - 70.0)
    # The deep swing low (~48, low-2) and swing high (~102, high+2) are present.
    assert any(abs(p - 48.0) < 4.0 for p in below)
    assert any(abs(p - 102.0) < 4.0 for p in above)


def test_spot_weekly_ladder_merges_nearby_pivots() -> None:
    # Two visits to the same low → one level with touches=2, not two levels.
    px = [80, 70, 60, 50, 60, 70, 80, 70, 60, 50.3, 60, 70, 80, 82, 84]
    bars = [_bar(i, p, p + 1.0, p - 1.0, p) for i, p in enumerate(px)]
    ladder = spot_weekly_ladder(bars, price=75.0, merge_tol_pct=1.5)
    lows = [lv for lv in ladder["below"] if float(lv["price"]) < 52.0]
    assert len(lows) == 1
    assert int(lows[0]["touches"]) == 2


def test_spot_weekly_ladder_degenerate_inputs() -> None:
    assert spot_weekly_ladder([], price=10.0)["below"] == []
    assert spot_weekly_ladder(_weekly_with_pivots(), price=0.0)["below"] == []
    short = _weekly_with_pivots()[:5]
    out = spot_weekly_ladder(short, price=70.0)
    assert out["below"] == [] and out["above"] == []


# ---------------------------------------------------------------------------
# fetch_weekly_ohlcv cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_weekly_ohlcv_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    comp = _companion_no_network()
    comp._lock = asyncio.Lock()
    comp._markets_loaded = True

    calls = {"n": 0}
    raw = [[float(i) * 604_800_000.0, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(40)]

    async def _fake_fetch(factory: Any, **_kw: Any) -> Any:
        calls["n"] += 1
        return raw

    monkeypatch.setattr(comp, "_spot_fetch", _fake_fetch)
    monkeypatch.setattr(
        "hunt_core.market.factory.drop_unclosed_ohlcv_tail",
        lambda rows, tf, *, exchange, now_ms=None: rows[:-1],
    )
    monkeypatch.setattr(
        "hunt_core.market.spot.to_ccxt_symbol", lambda sym, exchange=None: "BTC/USDT"
    )
    comp._ex = object()  # only passed through to the patched helpers

    bars1 = await comp.fetch_weekly_ohlcv("BTCUSDT")
    bars2 = await comp.fetch_weekly_ohlcv("BTCUSDT")
    assert calls["n"] == 1, "second call must be served from the cache"
    assert bars1 is not None and bars2 is not None
    assert len(bars1) == 39, "unclosed weekly tail must be dropped"
    assert bars1 == bars2


# ---------------------------------------------------------------------------
# Deep-panel render
# ---------------------------------------------------------------------------


def test_spot_context_text_full() -> None:
    row = {
        "market": {
            "spot_futures_spread_bps": 6.4,
            "spot_quote_volume_24h": 500_000_000.0,
            "vol_24h_m": 2_000.0,  # $2000M futures
        },
        "spot_weekly_ladder": {
            "below": [{"price": 0.067, "touches": 8}, {"price": 0.052, "touches": 1}],
            "above": [{"price": 0.102, "touches": 2}],
            "bars_used": 300,
            "source": "spot_1w",
        },
    }
    txt = _spot_context_text(row)
    assert txt is not None
    assert "Спот-контекст" in txt
    assert "+6.4 bps" in txt and "перп дороже спота" in txt
    assert "0.25" in txt  # 500M spot / 2000M fut
    assert "ladder 1w" in txt
    assert "×8" in txt  # touch count surfaces as strength
    assert "0.052" in txt and "×1" not in txt  # single touch not annotated


def test_spot_context_text_absent_data_renders_nothing() -> None:
    assert _spot_context_text({}) is None
    assert _spot_context_text({"market": {"basis_pct": 0.1}}) is None


def test_spot_context_text_volume_only() -> None:
    row = {"market": {"spot_quote_volume_24h": 0.0, "vol_24h_m": 100.0}}
    txt = _spot_context_text(row)
    assert txt is not None and "0.00" in txt
