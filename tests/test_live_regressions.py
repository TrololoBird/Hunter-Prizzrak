"""Regressions caught by the live run that the unit suite missed.

1. The G-26 Pydantic conversion left three dataclasses-API calls on HuntCandidate
   (dataclasses.replace + asdict), which threw at runtime in run_scan's enrich/serialise
   path — never exercised by a test. Pin the Pydantic equivalents.
2. btc_beta_1h called polars_ols with the pre-API-drift signature (features=[...] + Series
   + get_column), which raised every tick and silently degraded beta to None.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.snapshot import btc_beta_1h
from hunt_core.scanner.prescan import HuntCandidate, enrich_candidates_with_percentile_ranks


def _candidate(symbol: str, change: float) -> HuntCandidate:
    return HuntCandidate(
        symbol=symbol, hunt_score=50.0, watch_bias="both", flags=(), reasons=(),
        last_price=1.0, change_24h_pct=change, quote_volume=1e7,
        range_pct_24h=10.0, pos_in_range=0.5,
    )


def test_enrich_candidates_uses_model_copy_not_dataclasses_replace() -> None:
    # 11 candidates so the single top mover lands at pctile 10/11 ≈ 0.91 ≥ 0.9.
    cands = [_candidate(f"C{i}", float(i)) for i in range(10)] + [_candidate("TOP", 999.0)]
    out = enrich_candidates_with_percentile_ranks(cands)
    assert len(out) == 11
    top = next(c for c in out if c.symbol == "TOP")
    assert "top_decile_move" in top.flags  # model_copy actually applied the flag
    assert any(r.startswith("move_pctile=") for c in out for r in c.reasons)
    # originals untouched (frozen model_copy, not in-place mutation).
    assert cands[0].reasons == ()


def test_hunt_candidate_serialises_via_model_dump() -> None:
    # This is the run_scan watchlist/hot_funnel JSON path that used asdict().
    c = _candidate("SOLUSDT", 12.0)
    d = c.model_dump()
    assert d["symbol"] == "SOLUSDT" and d["change_24h_pct"] == 12.0
    assert isinstance(d["flags"], tuple)  # same shape asdict produced


def test_btc_beta_1h_returns_real_beta() -> None:
    # sym ≈ 1.5 × BTC returns → beta near 1.5, definitely not None (the failure mode).
    import math
    btc_close = [100.0 * (1.0 + 0.01 * math.sin(i)) for i in range(60)]
    sym_close = [100.0 * (1.0 + 0.015 * math.sin(i)) for i in range(60)]
    btc = pl.DataFrame({"close": btc_close})
    sym = pl.DataFrame({"close": sym_close})
    beta = btc_beta_1h(sym, btc, lookback=48)
    assert beta is not None
    assert 1.0 < beta < 2.0
