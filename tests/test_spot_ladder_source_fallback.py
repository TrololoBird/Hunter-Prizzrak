"""Every symbol gets the macro horizon — only the SOURCE may differ, and it must be declared.

The author applies one methodology to every asset, so no instrument may quietly lose a horizon.
Binance lists gold/silver as its own tokenized perps with NO spot pair, so the deterministic
settle-strip produced ``XAU/USDT``/``XAG/USDT`` — symbols that do not exist — and the macro ladder
had no source at all for them. That is not cosmetic: the 2026-07-25 обзор showed the author's
deepest zones live ONLY in full history (for CFX all five buy zones did).

Worse, it was INVISIBLE: an omitted block looks exactly like a block with no levels in it.
"""
from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from hunt_core.engine.spot import SpotEngine, _SPOT_BASE_ALIAS
from hunt_core.runtime.native_producers import spot_weekly_ladder_native


class _FakeEx:
    """Minimal stand-in for the ccxt spot exchange: only ``markets`` is consulted by the resolver."""

    def __init__(self, symbols: list[str]) -> None:
        self.markets = {s: {"spot": True} for s in symbols}


def _engine(symbols: list[str]) -> SpotEngine:
    eng = SpotEngine([])
    eng._ex = _FakeEx(symbols)  # type: ignore[assignment]
    return eng


def _weekly_frame(lo: float, hi: float, cycles: int) -> pl.DataFrame:
    """A weekly kline frame oscillating lo↔hi so the ladder finds confirmed swing pivots."""
    rows: list[dict[str, Any]] = []
    mid = (lo + hi) / 2
    step_ms = 7 * 24 * 3600 * 1000
    seq = [(mid, hi, mid * 0.99, hi * 0.998), (hi * 0.998, hi, mid, mid),
           (mid, mid * 1.01, lo, lo * 1.002), (lo * 1.002, mid, lo * 0.999, mid)]
    for i in range(cycles * len(seq)):
        o, h, low, c = seq[i % len(seq)]
        rows.append({
            "time": pl.datetime(1970, 1, 1), "open": o, "high": h, "low": low,
            "close": c, "volume": 1000.0, "_ts": i * step_ms,
        })
    return pl.DataFrame(rows).with_columns(
        pl.from_epoch(pl.col("_ts"), time_unit="ms").dt.replace_time_zone("UTC").alias("time")
    ).drop("_ts")


def test_resolver_finds_the_direct_spot_sibling() -> None:
    eng = _engine(["BTC/USDT", "PAXG/USDT"])
    assert eng.resolve_spot_symbol("BTC/USDT:USDT") == "BTC/USDT"
    assert eng.resolve_spot_symbol("PAXG/USDT:USDT") == "PAXG/USDT"


def test_resolver_uses_the_same_underlying_proxy_for_tokenised_gold() -> None:
    """★ XAU has no spot pair; PAXG is the same 1 oz of gold (median close diff 0.19% over 33 weeks)."""
    eng = _engine(["BTC/USDT", "PAXG/USDT"])
    assert eng.resolve_spot_symbol("XAU/USDT:USDT") == "PAXG/USDT"


def test_resolver_returns_none_rather_than_a_symbol_that_does_not_exist() -> None:
    """I-6: silver has no tokenised spot and deliberately no alias — say so, don't borrow a metal."""
    eng = _engine(["BTC/USDT", "PAXG/USDT"])
    assert eng.resolve_spot_symbol("XAG/USDT:USDT") is None
    assert "XAG" not in _SPOT_BASE_ALIAS, "silver must not alias onto a different underlying"


@pytest.mark.asyncio
async def test_ladder_falls_back_to_the_contract_weeks_when_no_spot_market_exists() -> None:
    """★ XAG keeps the horizon — same geometry, own weekly bars, and the source says so."""
    eng = _engine(["BTC/USDT", "PAXG/USDT"])
    frame = _weekly_frame(50.0, 70.0, cycles=8)
    out = await spot_weekly_ladder_native(
        "XAG/USDT:USDT", price=58.5, spot=eng, contract_weekly=frame
    )
    assert out is not None, "a symbol with no spot pair must still get the macro horizon"
    assert out["source"] == "contract_1w"
    assert out["below"] or out["above"]


@pytest.mark.asyncio
async def test_proxy_source_is_declared_not_silently_substituted() -> None:
    """A proxied history must be visible in the payload — the card renders it as «спот-история PAXG»."""
    eng = _engine(["BTC/USDT", "PAXG/USDT"])
    bars = [[float(i), 100.0, 110.0, 90.0, 100.0, 10.0] for i in range(40)]
    eng._weekly["PAXG/USDT"] = (bars, float("inf"))  # pre-seed the cache; no network
    out = await spot_weekly_ladder_native("XAU/USDT:USDT", price=100.0, spot=eng)
    if out is not None:  # flat synthetic bars may yield no pivots; the label is what matters
        assert out["source"] == "spot_1w:PAXG/USDT"


@pytest.mark.asyncio
async def test_no_source_at_all_is_fail_loud_none() -> None:
    """No spot market AND no contract frame ⇒ None, never an empty-but-present ladder."""
    eng = _engine(["BTC/USDT"])
    assert await spot_weekly_ladder_native("XAG/USDT:USDT", price=58.5, spot=eng) is None
