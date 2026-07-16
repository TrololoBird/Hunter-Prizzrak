"""Canonical ADX thresholds — single source for Hunter interpretation."""
from __future__ import annotations



# Wilder-style regime bands (aligned across MTF, panel, prepare, forecast).
ADX_RANGE_MAX = 20.0  # below → ranging / neutral bias filter
ADX_BIAS_MIN = 20.0  # minimum ADX to assert directional EMA bias
ADX_TREND_MIN = 25.0  # established trend (labels, scoring)

__all__ = [
    "ADX_BIAS_MIN",
    "ADX_RANGE_MAX",
    "ADX_TREND_MIN",
]
