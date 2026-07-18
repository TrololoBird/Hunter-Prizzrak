"""Pure derived funding stats (E4a) — trend/zscore/recent-extreme over settled funding history.

Semantics pinned against the old client (market/client.py:1435-1491); fail-loud (missing rate/time
skipped, never a fabricated 0.0)."""
from __future__ import annotations

from typing import Any

from hunt_core.engine.funding_stats import funding_recent_extreme, funding_trend, funding_zscore


def _rec(rate: Any, ts: int = 0) -> dict[str, Any]:
    return {"fundingRate": rate, "timestamp": ts}


def test_trend_rising_falling_flat_and_none() -> None:
    assert funding_trend([_rec(1), _rec(2), _rec(3), _rec(4)]) == "rising"
    assert funding_trend([_rec(4), _rec(3), _rec(2), _rec(1)]) == "falling"
    assert funding_trend([_rec(1), _rec(2), _rec(1), _rec(2)]) == "flat"
    assert funding_trend([_rec(1), _rec(2)]) is None  # < min_records (3) → None, not "flat"


def test_trend_uses_last_window_only() -> None:
    # older declining values ignored; last 4 are strictly rising
    recs = [_rec(9), _rec(8), _rec(7), _rec(1), _rec(2), _rec(3), _rec(4)]
    assert funding_trend(recs) == "rising"


def test_zscore_latest_vs_history() -> None:
    recs = [_rec(x) for x in (1.0, 1.0, 1.0, 1.0, 1.0, 5.0)]
    z = funding_zscore(recs)
    assert z is not None and z > 2.0  # latest 5 is a strong positive outlier


def test_zscore_none_under_min_and_zero_on_degenerate() -> None:
    assert funding_zscore([_rec(1.0)] * 5) is None  # < 6 records
    assert funding_zscore([_rec(2.0)] * 6) == 0.0  # zero variance → 0.0 (latest at mean)


def test_zscore_skips_nonfinite() -> None:
    recs = [_rec(1.0), _rec("bad"), _rec(1.0), _rec(1.0), _rec(1.0), _rec(1.0), _rec(5.0)]
    z = funding_zscore(recs)  # "bad" skipped → 6 finite rates
    assert z is not None and z > 2.0


def test_recent_extreme_picks_largest_magnitude_in_window() -> None:
    now = 100 * 3_600_000  # 100h in ms
    recs = [
        _rec(0.01, ts=(100 - 1) * 3_600_000),   # 1h old, +0.01
        _rec(-0.05, ts=(100 - 10) * 3_600_000),  # 10h old, -0.05 (largest |.|)
        _rec(0.09, ts=(100 - 60) * 3_600_000),   # 60h old → outside 48h window
    ]
    out = funding_recent_extreme(recs, now_ms=now, max_age_hours=48.0)
    assert out is not None
    rate, age_h = out
    assert rate == -0.05 and round(age_h) == 10  # 0.09 excluded (too old), -0.05 wins on magnitude


def test_recent_extreme_none_when_empty_or_all_old() -> None:
    now = 100 * 3_600_000
    assert funding_recent_extreme([], now_ms=now) is None
    old = [_rec(0.5, ts=(100 - 100) * 3_600_000)]  # 100h old > 48h
    assert funding_recent_extreme(old, now_ms=now, max_age_hours=48.0) is None


def test_recent_extreme_skips_missing_rate_not_fabricate_zero() -> None:
    now = 100 * 3_600_000
    recs = [_rec(None, ts=(100 - 1) * 3_600_000)]  # missing rate → skipped (old client faked 0.0)
    assert funding_recent_extreme(recs, now_ms=now) is None
