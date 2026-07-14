"""Normalized factor registry with per-symbol adaptive z-scores (§E.1)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import polars as pl

import structlog

LOG = structlog.get_logger(__name__)
@dataclass(frozen=True, slots=True)
class FactorSpec:
    name: str
    family: str
    direction: str  # bullish | bearish | neutral
    normalize: Callable[[float], float | None]


def _clamp11(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def rolling_z(series: pl.Series, *, window: int = 48) -> pl.Series:
    """Rolling z-score; returns NaN where std=0 (fail-loud upstream)."""
    if series.len() < window:
        return pl.Series([float("nan")] * series.len())
    mean = series.rolling_mean(window_size=window, min_samples=max(8, window // 4))
    std = series.rolling_std(window_size=window, min_samples=max(8, window // 4))
    z = (series - mean) / std
    return z.fill_nan(None)


def adaptive_z_last(raw: float | None, history: pl.Series | None, *, window: int = 48) -> float | None:
    """Map raw metric to [-1,1] via rolling z of symbol history."""
    if raw is None or not math.isfinite(float(raw)):
        return None
    if history is None or history.len() < 8:
        return None
    z_series = rolling_z(history, window=window)
    if z_series.is_empty():
        return None
    z = z_series[-1]
    if z is None or (isinstance(z, float) and not math.isfinite(z)):
        return None
    return _clamp11(float(z) / 3.0)


def factor_from_tf(tf: dict[str, Any], key: str, *, invert: bool = False) -> float | None:
    val = tf.get(key)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        LOG.debug("factor_from_tf float conversion failed key=%s", key, exc_info=True)
        return None
    if not math.isfinite(f):
        return None
    if invert:
        f = -f
    return f


def build_factor_panel(row: dict[str, Any]) -> dict[str, float | None]:
    """Extract normalized factors from a tick row (snapshot/tf/market)."""
    tf15 = (row.get("timeframes") or {}).get("15m") or {}
    tf1h = (row.get("timeframes") or {}).get("1h") or {}
    market = row.get("market") or {}
    panel: dict[str, float | None] = {}

    rsi = factor_from_tf(tf15, "rsi14")
    if rsi is not None:
        panel["momentum_rsi15"] = _clamp11((50.0 - rsi) / 50.0)

    adx = factor_from_tf(tf1h, "adx14")
    if adx is not None:
        panel["trend_adx1h"] = _clamp01(adx / 50.0)

    taker = market.get("taker_ratio")
    if taker is not None:
            try:
                tr = float(taker)
                if math.isfinite(tr):
                    panel["flow_taker"] = _clamp11((tr - 1.0) * 2.0)
            except (TypeError, ValueError):
                LOG.debug("taker_ratio float conversion failed", exc_info=True)
                pass

    oi_z = market.get("oi_z")
    if oi_z is not None:
            try:
                oz = float(oi_z)
                if math.isfinite(oz):
                    panel["deriv_oi_z"] = _clamp11(oz / 3.0)
            except (TypeError, ValueError):
                LOG.debug("oi_z float conversion failed", exc_info=True)
                pass

    funding = market.get("funding_pct")
    if funding is not None:
            try:
                fp = float(funding)
                if math.isfinite(fp):
                    # funding_pct is in PERCENTAGE POINTS (funding_rate*100, see
                    # snapshot.py). The 5000 factor was calibrated for the raw FRACTION
                    # (0.0001·5000=0.5), so on pp-scale input it over-scaled ×100 and
                    # saturated ±1 at any real funding. 50 restores the intended slope
                    # (0.01pp≈0.0001 fraction → 0.5).
                    panel["deriv_funding"] = _clamp11(fp * 50.0)
            except (TypeError, ValueError):
                LOG.debug("funding_pct float conversion failed", exc_info=True)
                pass

    cmf = factor_from_tf(tf15, "cmf20")
    if cmf is not None:
        panel["flow_cmf15"] = _clamp11(cmf)

    return panel


__all__ = [
    "FactorSpec",
    "adaptive_z_last",
    "build_factor_panel",
    "factor_from_tf",
    "rolling_z",
]
