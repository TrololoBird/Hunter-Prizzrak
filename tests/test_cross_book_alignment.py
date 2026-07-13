"""Cross-venue DOM time-alignment must never drop the PRIMARY (binance).

A live signal surfaced `⏱ исключены устаревшие: BIN, OKX, BIT` — i.e. binance,
the sole authoritative venue for this Binance-analytics bot, was being excluded
from its own DOM as "stale", leaving the order map built from secondaries only.
The alignment reference is now the primary's own snapshot, so binance is kept
even when it is the slower fetch; secondaries misaligned from it (either
direction) are the ones dropped. Only when binance is absent does the freshest
surviving secondary become the reference.
"""
from __future__ import annotations

from hunt_core.market.cross import _stale_venues_by_alignment

_STALE_MS = 750.0


def test_primary_kept_even_when_slowest() -> None:
    # binance 1.5s behind a fresh bybit — the exact live bug. binance MUST stay;
    # bybit is the misaligned one relative to the authoritative clock.
    stamped = {"binance": 1000.0, "bybit": 2500.0, "okx": 1100.0}
    excluded = _stale_venues_by_alignment(stamped, primary="binance", stale_ms=_STALE_MS)
    assert "binance" not in excluded
    assert "bybit" in excluded          # 1500ms from binance > 750
    assert "okx" not in excluded        # 100ms from binance, aligned


def test_secondary_ahead_of_primary_also_dropped() -> None:
    # Skew is bidirectional: a secondary 1s AHEAD of binance captured a later
    # market state and would still blur the merge.
    stamped = {"binance": 5000.0, "okx": 6200.0}
    excluded = _stale_venues_by_alignment(stamped, primary="binance", stale_ms=_STALE_MS)
    assert excluded == ["okx"]


def test_all_aligned_excludes_nothing() -> None:
    stamped = {"binance": 1000.0, "bybit": 1200.0, "okx": 900.0}
    assert _stale_venues_by_alignment(stamped, primary="binance", stale_ms=_STALE_MS) == []


def test_primary_absent_falls_back_to_freshest() -> None:
    # binance fetch failed (unstamped). Reference becomes the freshest secondary;
    # older ones beyond tolerance drop.
    stamped = {"bybit": 3000.0, "okx": 1500.0}
    excluded = _stale_venues_by_alignment(stamped, primary="binance", stale_ms=_STALE_MS)
    assert excluded == ["okx"]          # 1500ms behind bybit


def test_single_venue_never_excluded() -> None:
    assert _stale_venues_by_alignment({"binance": 1000.0}, primary="binance", stale_ms=_STALE_MS) == []
    assert _stale_venues_by_alignment({}, primary="binance", stale_ms=_STALE_MS) == []
