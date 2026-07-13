"""_cluster_levels must be order-agnostic.

The support path feeds DESCENDING levels; the resistance path feeds ASCENDING.
With signed gaps, ascending input produced all-negative gaps → no split ever
fired → resistance «зона N» rows silently never rendered. Absolute gaps fix it:
a set of levels with a clear wide gap must split into two clusters regardless of
input order.
"""
from __future__ import annotations

from hunt_core.deliver.confluence_grid import _cluster_levels

# Five levels: a tight trio near 100 and a tight pair near 130 (~27% gap).
_ASC = [100.0, 101.0, 102.0, 130.0, 131.0]
_DESC = [131.0, 130.0, 102.0, 101.0, 100.0]


def test_ascending_input_splits_into_two_clusters() -> None:
    clusters = _cluster_levels(_ASC, pct_gap=2.5)
    assert len(clusters) == 2
    assert sorted(min(c) for c in clusters) == [100.0, 130.0]


def test_descending_input_still_splits() -> None:
    clusters = _cluster_levels(_DESC, pct_gap=2.5)
    assert len(clusters) == 2


def test_both_orders_agree_on_cluster_membership() -> None:
    asc = {frozenset(c) for c in _cluster_levels(_ASC, pct_gap=2.5)}
    desc = {frozenset(c) for c in _cluster_levels(_DESC, pct_gap=2.5)}
    assert asc == desc


def test_tight_ladder_stays_single_cluster() -> None:
    # No wide gap → one cluster (both orders).
    tight = [100.0, 100.5, 101.0, 101.5, 102.0]
    assert len(_cluster_levels(tight, pct_gap=2.5)) == 1
