"""Engine REST extensions (E1/E2) — mark/index/windowed OHLCV, funding history, futures-data series.

All fail-loud: a failed fetch returns [] (never a fabricated frame/series), the forming bar is
dropped (I-5), and a windowed fetch only serves fully-closed bars. Driven via asyncio.run to match
the sync engine test suite (no async-test infra needed)."""
from __future__ import annotations

import asyncio
from typing import Any

from hunt_core.engine import rest

_BASE = 1_000_000_000_000  # far-past ms so "now" always exceeds the window end


class _FakeExchange:
    def __init__(self, *, ohlcv: list[list[float]] | None = None, raise_ohlcv: bool = False) -> None:
        self._ohlcv = ohlcv or []
        self._raise = raise_ohlcv
        self.last_params: dict[str, Any] | None = None
        self.last_since: int | None = None

    def parse_timeframe(self, tf: str) -> int:
        return {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}[tf]

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, *, since: Any = None, limit: Any = None, params: Any = None
    ) -> list[list[float]]:
        if self._raise:
            raise RuntimeError("boom")
        self.last_params = params
        self.last_since = since
        return list(self._ohlcv)

    async def fetch_funding_rate_history(self, symbol: str, *, limit: int) -> list[dict[str, Any]]:
        return [{"timestamp": _BASE, "fundingRate": 0.0001}, {"timestamp": _BASE + 1, "fundingRate": -0.0002}]


def test_fetch_ohlcv_series_drops_forming_and_passes_price() -> None:
    bars = [[float(_BASE + i * 60_000), 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(4)]
    ex = _FakeExchange(ohlcv=bars)
    out = asyncio.run(rest.fetch_ohlcv_series(ex, "BTC/USDT:USDT", "1m", limit=100, price="mark"))
    assert len(out) == 3  # forming last bar dropped (I-5)
    assert out[-1][0] == float(_BASE + 2 * 60_000)
    assert ex.last_params == {"price": "mark"}  # price threaded to ccxt


def test_fetch_ohlcv_series_failure_is_empty_not_fabricated() -> None:
    ex = _FakeExchange(raise_ohlcv=True)
    assert asyncio.run(rest.fetch_ohlcv_series(ex, "BTC/USDT:USDT", "1d", limit=30)) == []


def test_fetch_ohlcv_between_keeps_only_closed_bars_in_window() -> None:
    # interval 60k; opens at 0,60k,120k,180k; window end 200k → keep open+60k ≤ 200k (0,60k,120k)
    bars = [[float(_BASE + i * 60_000), 1.0, 2.0, 0.5, 1.5, 1.0] for i in range(4)]
    ex = _FakeExchange(ohlcv=bars)
    out = asyncio.run(
        rest.fetch_ohlcv_between(ex, "BTC/USDT:USDT", "1m", start_ms=_BASE, end_ms=_BASE + 200_000)
    )
    assert [int(b[0]) for b in out] == [_BASE, _BASE + 60_000, _BASE + 120_000]
    assert ex.last_since == _BASE and ex.last_params == {"until": _BASE + 200_000}


def test_fetch_ohlcv_between_empty_on_no_data() -> None:
    assert asyncio.run(
        rest.fetch_ohlcv_between(_FakeExchange(ohlcv=[]), "BTC/USDT:USDT", "1m", start_ms=0, end_ms=1)
    ) == []


def test_fetch_funding_history_returns_records() -> None:
    rows = asyncio.run(rest.fetch_funding_history(_FakeExchange(), "BTC/USDT:USDT", limit=16))
    assert [r["fundingRate"] for r in rows] == [0.0001, -0.0002]


class _FutDataExchange:
    def __init__(self, rows: Any) -> None:
        self._rows = rows

    async def fapiDataGetOpenInterestHist(self, params: dict[str, Any]) -> Any:
        return self._rows


def test_fetch_futures_data_series_extracts_finite_floats() -> None:
    rows = [
        {"sumOpenInterest": "100.0"},
        {"sumOpenInterest": "bad"},  # unparseable → skipped
        {"sumOpenInterest": float("inf")},  # non-finite → skipped
        {"sumOpenInterest": 105.5},
    ]
    ex = _FutDataExchange(rows)
    out = asyncio.run(
        rest.fetch_futures_data_series(
            ex, "fapiDataGetOpenInterestHist", {"symbol": "BTCUSDT"}, "sumOpenInterest"
        )
    )
    assert out == [100.0, 105.5]  # bad/non-finite skipped, never fabricated


def test_fetch_futures_data_series_empty_on_unsupported() -> None:
    class _NoMethod:
        pass

    out = asyncio.run(
        rest.fetch_futures_data_series(_NoMethod(), "fapiDataGetOpenInterestHist", {}, "sumOpenInterest")
    )
    assert out == []
