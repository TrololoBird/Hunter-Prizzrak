"""The stale-HTF cache trap: a frozen frame that locks a symbol out permanently.

Live (2026-07-16): the ENTIRE pinned watch universe — BTC, ETH, SOL, XRP, XAU, XAG,
PAXG and 11 more — stopped producing signals ~40 min after start and never recovered.
Four hours in, every tick for those symbols was rejected with `klines.1h.stale`, while
the bot reported errors=0 and looked healthy.

The trap is a cycle:
  * hot-tier symbols don't fetch 1h/4h directly — resolve_kline_map derives them by
    resampling one 1m pull, and 1500 1m bars can only build ~25 1h bars against the
    ~380 required, so the derived HTF frame comes back thin;
  * tick_assembly then restores the thin TF from `kline_map()`, which — unlike its
    sibling `get_kline_frame()` — applies NO age check, so a frame frozen hours ago is
    handed back as if fresh, AND `fetch_errors` is cleared for that TF (a cache hit
    laundered into "the fetch succeeded" — this is why errors stayed at 0);
  * the downstream staleness guard rejects the symbol on that stale frame;
  * `repair_kline_map_gaps`, the one path that would fetch REAL 1h bars and refresh
    the cache, is skipped whenever `has_delta_ready()` is True — and that only checked
    whether frames EXIST with enough bars, never whether they are still fresh.

So the guard rejected the tick before the code that would have cured the staleness
could run, and the cure was gated on a readiness flag that staleness never cleared.
Nothing self-heals; only a restart clears it, and then it re-arms hours later.
"""

from __future__ import annotations

import time

import polars as pl

from hunt_core.data.frame_cache import (
    _KLINE_FRAME_MAX_AGE_S,
    SymbolFrameCache,
)


def _frame(n: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "time": [1_752_600_000_000 + i * 60_000 for i in range(n)],
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "volume": [1.0] * n,
        }
    )


def _bootstrapped_cache() -> SymbolFrameCache:
    """A cache seeded exactly as a warm hot-tier symbol's would be."""
    cache = SymbolFrameCache()
    cache.seed_klines(
        "BTCUSDT",
        {"1m": _frame(60), "5m": _frame(48), "15m": _frame(96), "1h": _frame(48), "4h": _frame(24)},
    )
    cache.seed_prepared("BTCUSDT", object())
    return cache


def _age_frames(cache: SymbolFrameCache, symbol: str, seconds: float) -> None:
    """Backdate every seeded frame's timestamp by `seconds` (no sleeping)."""
    for tf in list(cache._frame_ts.get(symbol.upper()) or {}):
        cache._frame_ts[symbol.upper()][tf] -= seconds


def test_fresh_cache_is_ready() -> None:
    cache = _bootstrapped_cache()
    assert cache.is_ready("BTCUSDT") is True
    assert cache.has_delta_ready("BTCUSDT") is True


def test_stale_htf_clears_readiness_so_gap_repair_can_run() -> None:
    """THE fix: readiness must expire, or the HTF repair path is skipped forever.

    tick_assembly only runs repair_kline_map_gaps when `not hot_tier or not
    cache_delta_ready`. With readiness pinned True by mere presence, a hot symbol whose
    1h froze hours ago never repairs — which is exactly how 18 symbols went dark.
    """
    cache = _bootstrapped_cache()
    # 1h frames expire at 2h; age everything past that.
    _age_frames(cache, "BTCUSDT", _KLINE_FRAME_MAX_AGE_S["1h"] + 60)
    assert cache.is_ready("BTCUSDT") is False, (
        "a symbol whose HTF frames are all stale must NOT report ready — readiness "
        "gates the only path that refetches them"
    )
    assert cache.has_delta_ready("BTCUSDT") is False


def test_kline_map_does_not_hand_back_expired_frames() -> None:
    """kline_map must age-gate like get_kline_frame, or the cache launders stale data.

    tick_assembly restores TFs from kline_map() AND drops their fetch_errors. An
    unguarded accessor therefore turns a dead frame into a silent "successful fetch".
    """
    cache = _bootstrapped_cache()
    _age_frames(cache, "BTCUSDT", _KLINE_FRAME_MAX_AGE_S["4h"] + 60)
    out = cache.kline_map("BTCUSDT")
    assert "1h" not in out, "expired 1h frame must not be served as a restore candidate"
    assert "4h" not in out, "expired 4h frame must not be served either"


def test_kline_map_keeps_frames_that_are_still_within_their_tf_bound() -> None:
    """Per-TF bounds, not one global one: a 3h-old 4h frame is fine, a 3h-old 1h is not."""
    cache = _bootstrapped_cache()
    _age_frames(cache, "BTCUSDT", 3 * 3600)  # 3h: past 1h's 2h bound, inside 4h's 8h
    out = cache.kline_map("BTCUSDT")
    assert "1h" not in out
    assert "4h" in out


def test_the_two_accessors_agree() -> None:
    """kline_map and get_kline_frame must not disagree about what is expired.

    The trap existed because they did: one enforced the bound, the other ignored it,
    and the caller that mattered used the one that ignored it.
    """
    cache = _bootstrapped_cache()
    _age_frames(cache, "BTCUSDT", _KLINE_FRAME_MAX_AGE_S["1h"] + 60)
    assert cache.get_kline_frame("BTCUSDT", "1h") is None
    assert "1h" not in cache.kline_map("BTCUSDT")


def test_legacy_frames_without_a_timestamp_still_resolve() -> None:
    """seed paths that predate _frame_ts must not be dropped wholesale."""
    cache = SymbolFrameCache()
    cache._frames["BTCUSDT"] = {"1h": _frame(48)}  # no _frame_ts entry
    assert "1h" in cache.kline_map("BTCUSDT")
    assert cache.get_kline_frame("BTCUSDT", "1h") is not None


def test_seeding_refreshes_an_expired_frame() -> None:
    """The recovery leg: once a real fetch lands, the symbol must come back."""
    cache = _bootstrapped_cache()
    _age_frames(cache, "BTCUSDT", _KLINE_FRAME_MAX_AGE_S["1h"] + 60)
    assert cache.is_ready("BTCUSDT") is False
    cache.seed_klines(
        "BTCUSDT",
        {"1m": _frame(60), "5m": _frame(48), "15m": _frame(96), "1h": _frame(48), "4h": _frame(24)},
    )
    assert cache.is_ready("BTCUSDT") is True
    assert "1h" in cache.kline_map("BTCUSDT")
    assert time.monotonic() > 0  # sanity: no sleeping in this suite
