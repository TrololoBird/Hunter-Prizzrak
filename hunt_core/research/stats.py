"""Small, dependency-light stats shared by the research harness.

AUC as the rank statistic P(feature higher on a win than on a loss), with tie
correction, computed without scipy/sklearn.
"""
from __future__ import annotations

import numpy as np


def rankdata(a: np.ndarray) -> np.ndarray:
    """Average ranks (1-based), ties averaged — scipy.rankdata equivalent."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1)
    sorted_a = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1
    return ranks


def auc(feature: np.ndarray, win: np.ndarray) -> float | None:
    """P(feature_i > feature_j | i win, j loss), ties=0.5. None if degenerate."""
    mask = ~np.isnan(feature)
    f, w = feature[mask], win[mask]
    pos, neg = f[w == 1], f[w == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    ranks = rankdata(np.concatenate([pos, neg]))
    r_pos = ranks[: len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


__all__ = ["auc", "rankdata"]
