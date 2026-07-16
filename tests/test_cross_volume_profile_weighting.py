"""Regression test: cross-exchange VP must weight venues LINEARLY by volume.

The merge multiplied each venue's per-bar volume by that venue's OWN total volume
(``pl.col("volume") * pl.lit(qv)``) before concatenating, then divided the merged
frame by ``sum(weights)``. That division is a constant across every row, so it
cancels out of the histogram entirely — the surviving effect was that a venue's
influence on the merged profile scaled as qv² instead of qv.

Squaring is monotonic, so it never reorders two venues on its own; the damage
shows up when several smaller venues AGREE on a price shelf that a single larger
venue disagrees with. Linearly their volumes add up and win; squared, the big
venue's qv² buries them and the "cross-exchange" POC collapses onto Binance.
"""
from __future__ import annotations

import asyncio
from typing import Any

import polars as pl
import pytest

from hunt_core.market.cross import CrossExchangeConfig, fetch_cross_exchange_volume_profile


def _bars(price: float, volume: float, n: int = 10) -> pl.DataFrame:
    """n identical bars parked on one price shelf with a fixed per-bar volume."""
    return pl.DataFrame(
        {
            "open_time": list(range(n)),
            "open": [price] * n,
            "high": [price + 0.5] * n,
            "low": [price - 0.5] * n,
            "close": [price] * n,
            "volume": [volume] * n,
        }
    )


class _FakeVenue:
    def __init__(self, ex_id: str, df: pl.DataFrame) -> None:
        self.id = ex_id
        self._df = df
        self.has = {"fetchOHLCV": True}

    async def fetch_ohlcv(self, _sym: str, *, timeframe: str, limit: int) -> list[list[float]]:
        return self._df.rows()


class _VpClient:
    """Binance alone on the 100-shelf; two secondaries agreeing on the 200-shelf.

    Totals over the 10-bar lookback:
      binance 10 x 10.0 = 100   at price 100
      bybit   10 x  6.0 =  60   at price 200
      bitget  10 x  6.0 =  60   at price 200

    Linear:     shelf 200 = 60 + 60 = 120  >  shelf 100 = 100        → POC 200
    Quadratic:  shelf 200 = 60² + 60² = 7_200  <  shelf 100 = 10_000 → POC 100
    """

    def __init__(self) -> None:
        self._frames = {
            "binance": _bars(100.0, 10.0),
            "bybit": _bars(200.0, 6.0),
            "bitget": _bars(200.0, 6.0),
        }
        self.rest_gate = self

    def _bin_sym(self, symbol: str) -> str:
        return symbol.upper()

    async def fetch_klines(self, _sym: str, _interval: str, *, limit: int) -> pl.DataFrame:
        return self._frames["binance"]

    async def _secondary_ccxt_symbol(self, name: str, _sym: str) -> str | None:
        return "BTC/USDT:USDT" if name in self._frames else None

    async def _get_secondary(self, name: str) -> Any:
        return _FakeVenue(name, self._frames[name]) if name in self._frames else None

    async def invoke_secondary(self, _name, _ex, factory, *, context) -> Any:  # noqa: ANN001
        return await factory()


def _run() -> dict[str, Any]:
    return asyncio.run(
        fetch_cross_exchange_volume_profile(
            _VpClient(),
            "BTCUSDT",
            "1h",
            cfg=CrossExchangeConfig(enabled=True, exchanges=("bybit", "bitget")),
            lookback=10,
            buckets=24,
        )
    )


def test_cross_vp_reports_true_per_venue_volume_weights() -> None:
    out = _run()
    assert out["venues"] == 3
    assert out["per_exchange"]["binance"]["weight"] == pytest.approx(100.0)
    assert out["per_exchange"]["bybit"]["weight"] == pytest.approx(60.0)
    assert out["per_exchange"]["bitget"]["weight"] == pytest.approx(60.0)


def test_cross_vp_agreeing_secondaries_outweigh_a_larger_binance() -> None:
    """120 units of agreeing secondary volume must beat Binance's 100.

    Under the old qv² weighting Binance's 100² = 10_000 buried the secondaries'
    60² + 60² = 7_200 and the POC snapped back to the Binance shelf — the
    "cross-exchange" profile was Binance's profile wearing a cross-exchange name.
    """
    out = _run()
    assert out["poc"] == pytest.approx(200.0, abs=5.0)


def test_cross_vp_per_exchange_pocs_are_unweighted() -> None:
    """Each venue's own POC is computed from its own bars — weighting is merge-only."""
    out = _run()
    assert out["per_exchange"]["binance"]["poc"] == pytest.approx(100.0, abs=5.0)
    assert out["per_exchange"]["bybit"]["poc"] == pytest.approx(200.0, abs=5.0)
    assert out["source"] == "cross_exchange"
