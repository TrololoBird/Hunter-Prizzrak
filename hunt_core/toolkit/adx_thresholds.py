"""Canonical ADX thresholds — single source for Hunter interpretation."""
from __future__ import annotations



# Wilder-style regime bands (aligned across MTF, panel, prepare, forecast).
ADX_RANGE_MAX = 20.0  # below → ranging / neutral bias filter
ADX_BIAS_MIN = 20.0  # minimum ADX to assert directional EMA bias
ADX_TREND_MIN = 25.0  # established trend (labels, scoring)
ADX_STRONG_MIN = 50.0  # strong trend emphasis
ADX_PANEL_NEUTRAL = 20.0  # ADX+DI panel vote neutral below this

# Meme-perp hard gates (mtf_policy vetoes — stricter than pinned anchors).
ADX_MEME_TREND_MIN = 30.0
ADX_MEME_RANGE_MAX = 15.0

__all__ = [
    "ADX_BIAS_MIN",
    "ADX_MEME_RANGE_MAX",
    "ADX_MEME_TREND_MIN",
    "ADX_PANEL_NEUTRAL",
    "ADX_RANGE_MAX",
    "ADX_STRONG_MIN",
    "ADX_TREND_MIN",
]
