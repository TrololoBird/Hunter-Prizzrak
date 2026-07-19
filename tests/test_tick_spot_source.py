"""ADR-0004 S8 first cut — the tick's spot-enrichment source selection (market.spot_*).

`_resolve_spot_extra` picks the engine SpotEngine when the coexistence engine is live, else the
legacy companion, else None. This is a display-only leaf (the 4 market.spot_* keys are read only by
format_telegram render lines), gated so OFF is byte-identical. These tests pin the branch + the
None-safe (I-6) fallback so the seam can't silently fabricate a spot field.
"""
from __future__ import annotations

import pytest

from hunt_core.runtime import tick_assembly as ta
from hunt_core.runtime.tick_state import live_spot_engine, set_live_spot_engine


class _StubSpotEngine:
    def __init__(self, out: dict[str, float]) -> None:
        self.out = out
        self.last_call: tuple[str, float] | None = None

    def spot_enrichments(self, symbol: str, *, futures_mid: float | None = None) -> dict[str, float]:
        self.last_call = (symbol, futures_mid)  # type: ignore[assignment]
        return self.out


class _StubCompanion:
    def __init__(self, out: dict[str, float]) -> None:
        self.out = out

    def enrichments_for(self, symbol: str, *, max_age_seconds: float = 120.0) -> dict[str, float]:
        return self.out


@pytest.fixture(autouse=True)
def _reset_engine():
    set_live_spot_engine(None)
    yield
    set_live_spot_engine(None)  # never leak the global into other tests (byte-identical default)


def test_engine_wins_when_live_and_receives_futures_mid():
    engine = _StubSpotEngine({"spot_futures_spread_bps": 1.2, "spot_quote_volume_24h": 9.0})
    companion = _StubCompanion({"spot_futures_spread_bps": 99.0})  # must be ignored
    set_live_spot_engine(engine)
    out = ta._resolve_spot_extra("BTC/USDT:USDT", 50000.0, companion)
    assert out == {"spot_futures_spread_bps": 1.2, "spot_quote_volume_24h": 9.0}
    assert engine.last_call == ("BTC/USDT:USDT", 50000.0)  # futures_mid threaded through


def test_falls_back_to_companion_when_engine_absent():
    companion = _StubCompanion({"spot_taker_buy_ratio": 0.55})
    out = ta._resolve_spot_extra("ETH/USDT:USDT", 3000.0, companion)
    assert out == {"spot_taker_buy_ratio": 0.55}  # byte-identical to pre-S8 (OFF path)


def test_none_when_neither_source_present():
    assert ta._resolve_spot_extra("XRP/USDT:USDT", 0.5, None) is None  # нет данных, not fabricated


def test_engine_empty_dict_is_passed_through_not_fabricated():
    """A no-spot / stale symbol → the engine returns {}; the seam must pass {} (no spot keys),
    never invent a companion value or a 0.0 field (I-6)."""
    set_live_spot_engine(_StubSpotEngine({}))
    companion = _StubCompanion({"spot_futures_spread_bps": 42.0})  # must NOT leak in
    assert ta._resolve_spot_extra("XAU/USDT:USDT", 2000.0, companion) == {}


def test_global_defaults_to_none():
    assert live_spot_engine() is None  # OFF-by-default: no engine ⇒ legacy path
