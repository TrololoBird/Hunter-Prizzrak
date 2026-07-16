"""Canonical volume-profile pinning — hand-computed POC/VAH/VAL on tiny synthetic bars.

Canon (Market Profile / TradingView VP): histogram of volume over price buckets;
POC = max-volume bucket (price = bucket centre); Value Area = smallest contiguous set
of buckets around the POC covering 70% of total volume, grown greedily by taking the
larger of the two adjacent buckets; VAH/VAL = top/bottom edges of the VA. Bar volume
is split EQUALLY across every bucket the bar's [low, high] touches (documented
DEF-DIFF vs TradingView's close-bin / overlap-proportional options — with OHLCV-only
data there is no intrabar shape to weight by).

Numerically validated against an independent numpy reference on research/dataset_v11
(ETHFI/ARB/1MBABYDOGE, 1h lb=48 + 4h lb=42, 60 buckets): histograms matched bucket-
for-bucket; the only divergences were nondeterministic tie-breaks, pinned here.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.volume_profile import volume_profile_levels
from hunt_core.maps.volume_profile import _hvn_lvn_nodes, _volume_histogram


def _df(rows: list[tuple[float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "volume": [r[2] for r in rows],
        }
    )


def test_hand_computed_poc_vah_val() -> None:
    """buckets=10 over [10, 20] → bucket width 1.0.

    hist: b2=51, b4=b5=16, all other buckets 1 (bar C spreads 10 equally over 10
    buckets; A adds 50 to b2; B adds 15 to b4 and b5). total=90.
    POC bucket 2 → price 12.5. VA target 63: start 51, add b3 (1, right>=left tie→up)
    =52, add b4 (16) =68 ≥ 63 → included {2,3,4} → VAL=12.0, VAH=15.0.
    """
    df = _df(
        [
            (12.9, 12.0, 50.0),  # A → bucket 2
            (15.9, 14.0, 30.0),  # B → buckets 4,5 (15 each)
            (20.0, 10.0, 10.0),  # C → all 10 buckets (1 each)
            (13.5, 13.1, 0.0),  # zero-volume pad (height >= 5 gate; dropped from hist)
            (17.5, 17.1, 0.0),
        ]
    )
    poc, vah, val = volume_profile_levels(df, buckets=10, value_area_pct=0.70)
    assert poc == 12.5
    assert val == 12.0
    assert vah == 15.0


def test_poc_tie_break_is_deterministic_and_centre_of_range() -> None:
    """One dominating bar equal-split over all 10 buckets → all buckets tie at 10.0.

    Canonical Market Profile tie rule: the tied bucket closest to the centre of the
    range; residual tie → lower bucket. Centre of 10 buckets is 4.5 → bucket 4 →
    POC 4.5. Was nondeterministic (group_by hash order picked any tied bucket).
    """
    df = _df([(20.0, 10.0, 100.0), (15.0, 14.9, 0.0), (15.0, 14.9, 0.0), (15.0, 14.9, 0.0), (15.0, 14.9, 0.0)])
    results = {volume_profile_levels(df, buckets=10, value_area_pct=0.70) for _ in range(20)}
    assert len(results) == 1, f"POC/VA must be deterministic, got {results}"
    poc, vah, val = results.pop()
    assert poc == 14.5  # bucket 4 (of tied 0..9, closest-to-centre, lower on tie)
    # VA from b4: ties expand up first (b5..b9 → acc 60), then left b3 → 70 ≥ 70.
    assert val == 13.0
    assert vah == 20.0


def test_zero_volume_window_has_no_profile() -> None:
    """All-zero volume → no POC (was a fabricated POC at the range LOW)."""
    df = _df([(10.0 + i, 9.0 + i, 0.0) for i in range(5)])
    assert volume_profile_levels(df, buckets=20) == (None, None, None)


def test_flat_window_collapses_to_price() -> None:
    df = _df([(10.0, 10.0, 5.0)] * 6)
    assert volume_profile_levels(df, buckets=20) == (10.0, 10.0, 10.0)


def test_value_area_covers_target_volume() -> None:
    """VA must contain >= 70% of total volume (checked against the histogram)."""
    rows = []
    px = 100.0
    for i in range(40):
        px += (1.3 if i % 3 else -1.9) * ((i % 5) + 1) / 4.0
        rows.append((px + 1.0 + (i % 4) * 0.3, px - 1.2 - (i % 3) * 0.2, 5.0 + (i % 7) * 2.0))
    df = _df(rows)
    buckets = 24
    poc, vah, val = volume_profile_levels(df, buckets=buckets, value_area_pct=0.70)
    assert poc is not None and vah is not None and val is not None
    assert val <= poc <= vah
    hist = _volume_histogram(df, lookback=None, buckets=buckets)
    total = sum(hist.values())
    lo_all = float(df["low"].min())
    hi_all = float(df["high"].max())
    size = (hi_all - lo_all) / buckets
    in_va = sum(
        v
        for b, v in hist.items()
        if val - 1e-9 <= lo_all + b * size and lo_all + (b + 1) * size <= vah + 1e-9
    )
    assert in_va >= total * 0.70 - 1e-9


def test_maps_histogram_guards_inverted_bar() -> None:
    """A corrupt hi<lo bar must bucket by min/max (parity with the original loop and
    features/volume_profile), not divide volume by a zero-or-negative bucket span."""
    df = pl.DataFrame(
        {
            "high": [9.0, 12.0, 13.0, 14.0, 15.0],  # first bar inverted (hi < lo)
            "low": [10.0, 11.0, 12.0, 13.0, 14.0],
            "volume": [5.0] * 5,
        }
    )
    hist = _volume_histogram(df, lookback=None, buckets=6)
    # Range comes from low.min/high.max = [10, 15], bucket width 5/6. The inverted bar
    # spans [9,10] → clipped to bucket 0 (5.0). (12,11)→b1,b2; (13,12)→b2,b3;
    # (14,13)→b3,b4; (15,14)→b4,b5 (2.5 each).
    assert hist == {0: 5.0, 1: 2.5, 2: 5.0, 3: 5.0, 4: 5.0, 5: 2.5}


def test_hvn_lvn_tie_break_is_deterministic() -> None:
    hist = {b: 10.0 for b in range(10)}  # all tied — nothing above/below the mean
    hvn, lvn = _hvn_lvn_nodes(hist, price_min=0.0, bucket_size=1.0)
    assert hvn == [] and lvn == []
    hist2 = {0: 1.0, 1: 1.0, 2: 1.0, 3: 50.0, 4: 50.0, 5: 50.0, 6: 50.0}
    hvn2, lvn2 = _hvn_lvn_nodes(hist2, price_min=0.0, bucket_size=1.0)
    # mean=203/7=29.0; HVN > 37.7 → tied 50s, top-3 = lowest buckets 3,4,5
    assert [n.price for n in hvn2] == [3.5, 4.5, 5.5]
    # LVN < 14.5 → tied 1.0s at buckets 0,1,2 in bucket order
    assert [n.price for n in lvn2] == [0.5, 1.5, 2.5]
