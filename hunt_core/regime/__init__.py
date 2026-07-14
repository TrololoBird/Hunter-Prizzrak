"""Market-regime package.

Live surface is ``hunt_core.regime.market_regime`` (cross-section survey +
calibrated params), imported directly by its consumers. The former ``regime.py``
facade and its per-symbol ``classifier.py`` (an unwired regime-veto engine with
no live consumer) were removed; recover from git if the regime-range veto is
ever wired.
"""
