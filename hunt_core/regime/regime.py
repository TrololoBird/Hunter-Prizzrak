"""Market regime snapshot + classifier (P4 canonical API).

Implementation: ``regime/market_regime.py`` (cross-section survey) +
``regime/classifier.py`` (per-symbol structural regime). This module is the
stable import surface for gate/cycle consumers.
"""
from __future__ import annotations

from hunt_core.toolkit.adx_thresholds import (
    ADX_RANGE_MAX,
    ADX_STRONG_MIN,
    ADX_TREND_MIN,
)
from hunt_core.regime.classifier import (
    Regime,
    RegimeResult,
    classify_regime,
    regime_conflicts_direction,
)
from hunt_core.regime.market_regime import (
    HuntCalibratedParams,
    MarketRegimeSnapshot,
    active_params,
    apply_snapshot,
    calibrate_from_cross_section,
    compute_return_entropy_50,
    detect_volume_regime_break,
    last_snapshot,
    load_regime_file,
    refresh_market_regime,
    save_regime_file,
    symbol_regime_features,
)

__all__ = [
    "ADX_RANGE_MAX",
    "ADX_STRONG_MIN",
    "ADX_TREND_MIN",
    "HuntCalibratedParams",
    "MarketRegimeSnapshot",
    "Regime",
    "RegimeResult",
    "active_params",
    "apply_snapshot",
    "calibrate_from_cross_section",
    "classify_regime",
    "compute_return_entropy_50",
    "detect_volume_regime_break",
    "last_snapshot",
    "load_regime_file",
    "refresh_market_regime",
    "regime_conflicts_direction",
    "save_regime_file",
    "symbol_regime_features",
]
