"""Full-fidelity klines (ADR-0004 Phase 1.1) — engine/rest.py::fetch_klines_full.

ccxt's unified fetch_ohlcv/watch_ohlcv drop taker_buy_base_volume / quote_volume / num_trades (6-col),
which zero-filled the orderflow CVD/delta features. The engine's Binance kline planes now seed + WS-merge
via the raw fapiPublicGetKlines (12-element) so taker is REAL, never fabricated. These pin the parse
(12→11 element bar), the forming-bar drop (I-5), and fail-loud [] on failure / non-Binance / no market id.
"""
from __future__ import annotations

import asyncio

from hunt_core.engine import rest

_H1_MS = 3_600_000


def _raw(open_ms: int, *, taker_base: float = 6.0) -> list:
    """A raw /fapi/v1/klines 12-element row (strings, as Binance returns)."""
    return [
        open_ms, "1.0", "2.0", "0.5", "1.5", "10.0",  # 0-5 open_ms,o,h,l,c,v
        open_ms + _H1_MS - 1, "1500.0", 42,            # 6-8 close_ms, quote_vol, num_trades
        str(taker_base), "900.0", "0",                 # 9-11 taker_base, taker_quote, ignore
    ]


class _FakeBinance:
    def __init__(self, rows: list[list], *, has_method: bool = True, raise_it: bool = False) -> None:
        self._rows = rows
        self._raise = raise_it
        self.calls = 0
        if not has_method:
            # Simulate a non-Binance client: no fapiPublicGetKlines implicit method.
            self.fapiPublicGetKlines = None  # type: ignore[assignment]

    def parse_timeframe(self, tf: str) -> float:
        return {"1h": 3600.0, "1d": 86400.0}[tf]

    def market(self, symbol: str) -> dict:
        return {"id": symbol.split("/")[0] + "USDT"}

    async def fapiPublicGetKlines(self, params):  # noqa: N802 — ccxt implicit method name
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        return [list(r) for r in self._rows]


def _run(coro):
    return asyncio.run(coro)


def test_parses_full_fidelity_and_drops_forming_bar():
    now = 10 * _H1_MS
    # 3 closed bars + 1 forming (opens at `now`, not yet closed)
    rows = [_raw(7 * _H1_MS), _raw(8 * _H1_MS), _raw(9 * _H1_MS), _raw(now)]
    ex = _FakeBinance(rows)
    bars = _run(rest.fetch_klines_full(ex, "BTC/USDT:USDT", "1h", limit=10, now_ms=now + 1))
    assert len(bars) == 3, "forming bar must be dropped (I-5)"
    # 11-element bar carrying real taker_buy_base_volume at index 9
    assert all(len(b) == 11 for b in bars)
    assert bars[-1][0] == 9 * _H1_MS
    assert bars[-1][9] == 6.0  # real taker_buy_base, not a zero-fill
    assert bars[-1][7] == 1500.0  # quote volume
    assert bars[-1][8] == 42.0  # num trades


def test_market_id_override_skips_lookup():
    now = 10 * _H1_MS
    ex = _FakeBinance([_raw(8 * _H1_MS), _raw(now)])
    bars = _run(
        rest.fetch_klines_full(ex, "BTC/USDT:USDT", "1h", limit=5, market_id="BTCUSDT", now_ms=now + 1)
    )
    assert len(bars) == 1 and bars[0][0] == 8 * _H1_MS


def test_fail_loud_on_exception():
    ex = _FakeBinance([_raw(_H1_MS)], raise_it=True)
    assert _run(rest.fetch_klines_full(ex, "BTC/USDT:USDT", "1h", limit=5)) == []


def test_fail_loud_when_not_binance():
    ex = _FakeBinance([_raw(_H1_MS)], has_method=False)
    assert _run(rest.fetch_klines_full(ex, "BTC/USDT:USDT", "1h", limit=5)) == []
    assert ex.calls == 0


def test_short_rows_skipped_not_fabricated():
    now = 10 * _H1_MS
    good = _raw(8 * _H1_MS)
    short = [9 * _H1_MS, "1.0", "2.0", "0.5", "1.5", "10.0"]  # 6-element — no taker fields
    ex = _FakeBinance([short, good, _raw(now)])
    bars = _run(rest.fetch_klines_full(ex, "BTC/USDT:USDT", "1h", limit=10, now_ms=now + 1))
    # only the full 11-element closed bar survives; the 6-element row is skipped, never padded with 0s
    assert len(bars) == 1 and bars[0][0] == 8 * _H1_MS and bars[0][9] == 6.0
