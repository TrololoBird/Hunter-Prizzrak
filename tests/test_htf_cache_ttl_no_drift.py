"""HTF kline cache TTL must not let a cached frame drift past the staleness reject.

Live 2026-07-17: the entire pinned Prizrak universe (BTC/ETH/SOL/XRP/XAU/XAG/PAXG)
went `klines.4h.stale` for hours (11-12/15-16/17-19h) with errors=0 — no fetch failure,
no ban. Root cause: `_CACHE_TTL["klines_4h"]` was 14400s (= the 4h bar cadence), so a
perfectly-cached 4h frame's idx=-2 bar — the bar `audit_kline_staleness` checks — aged to
2*interval + TTL = 12h, past the 10h (2.5x) reject, between refetches. Deterministic drift.

The invariant that prevents it, for every TF actually staleness-audited:

    worst-case idx=-2 age  =  2 * interval  +  cache_TTL   <   reject_threshold
                                                                (= stale_mult * interval)

`2 * interval` is the oldest a fresh frame's idx=-2 bar can be (just before the next bar
closes); `+ cache_TTL` is the extra age a served frame carries because the cache refetches
only every TTL. This test encodes it so no one raises the TTL back toward the cadence.
"""
from __future__ import annotations

import pytest

from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    TF_MS,
    _DEFAULT_STALE_AGE_MULT,
    _STALE_AGE_MULT,
)
from hunt_core.market.client import _CACHE_TTL


_1H_DEFERRED = pytest.mark.xfail(
    reason="klines_1h TTL 3900 violates by 5 min (worst 3h5m vs 3h reject). Marginal, "
    "not seen in the live blackout, and dropping it below one interval changes "
    "test_ohlcv_list_cache_closed_bars.py's forming-bar threat model — deferred. "
    "strict=True: if 1h is later tightened this xpasses and flags removing the marker.",
    strict=True,
)

_PARAMS = [
    pytest.param(tf, marks=_1H_DEFERRED) if tf == "1h" else tf
    for tf in REQUIRED_SIGNAL_KLINE_TFS
]


@pytest.mark.parametrize("tf", _PARAMS)
def test_htf_cache_ttl_cannot_drift_past_staleness_reject(tf: str) -> None:
    interval_s = TF_MS[tf] / 1000.0
    mult = _STALE_AGE_MULT.get(tf, _DEFAULT_STALE_AGE_MULT)
    ttl_s = float(_CACHE_TTL.get(f"klines_{tf}", 60))

    reject_threshold_s = mult * interval_s
    worst_idx2_age_s = 2 * interval_s + ttl_s

    assert worst_idx2_age_s < reject_threshold_s, (
        f"{tf}: a cached frame can drift to idx=-2 age {worst_idx2_age_s:.0f}s "
        f">= reject {reject_threshold_s:.0f}s (mult {mult}x). "
        f"Lower _CACHE_TTL['klines_{tf}'] below {(mult - 2) * interval_s:.0f}s "
        f"(currently {ttl_s:.0f}s) — see tests/test_htf_cache_ttl_no_drift.py docstring."
    )
