"""Strategy-neutral robust statistics (extracted from scanner.detect.calibrate). Polars-native.

No numpy: median/MAD/std/quantile/OLS-slope are Polars Series expressions. Parity with the old
numpy version is preserved exactly — ``std(ddof=0)`` matches ``np.std`` (population), and
``quantile(interpolation="linear")`` matches ``np.quantile`` (Polars would otherwise default to
"nearest").
"""
from __future__ import annotations

import math

import polars as pl

MIN_N_DEFAULT = 30
_MAD_TO_SIGMA = 1.4826
_DEFAULT_MAD_EPSILON = 1e-6
_DEFAULT_ROBUST_Z_CLIP = 12.0


def _median(s: pl.Series) -> float:
    v = s.median()
    return float(v) if v is not None else 0.0


def _std_pop(s: pl.Series) -> float:
    v = s.std(ddof=0)  # population std, matching np.std's default
    return float(v) if v is not None else 0.0


def _mad(s: pl.Series, median: float) -> float:
    v = (s - median).abs().median()
    return float(v) if v is not None else 0.0


def _robust_scale(arr: pl.Series, *, mad_epsilon: float) -> float:
    median = _median(arr)
    mad = _mad(arr, median)
    scale = max(_MAD_TO_SIGMA * mad, mad_epsilon)
    if scale <= mad_epsilon:
        std = _std_pop(arr)
        if std <= mad_epsilon:
            return mad_epsilon
        return max(std, mad_epsilon)
    return scale


def _clip_z(z: float, *, clip: float) -> float:
    if not math.isfinite(z):
        return 0.0
    return max(-clip, min(clip, z))


def _clean(series: pl.Series | None) -> pl.Series:
    """Finite Float64 values in order — drops null, NaN and ±inf (matches np.isfinite filter)."""
    if series is None or series.len() == 0:
        return pl.Series([], dtype=pl.Float64)
    s = series.cast(pl.Float64, strict=False)
    return s.filter(s.is_finite())


def robust_z(
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
    mad_epsilon: float = _DEFAULT_MAD_EPSILON,
    clip: float = _DEFAULT_ROBUST_Z_CLIP,
) -> float | None:
    arr = _clean(series)
    if arr.len() < min_n:
        return None
    last = float(arr[-1])
    scale = _robust_scale(arr, mad_epsilon=mad_epsilon)
    if scale <= mad_epsilon and _std_pop(arr) <= mad_epsilon:
        return None  # constant series — no distribution to score against; caller should abstain
    return _clip_z((last - _median(arr)) / scale, clip=clip)


def quantile(series: pl.Series | None, q: float, *, min_n: int = MIN_N_DEFAULT) -> float | None:
    arr = _clean(series)
    if arr.len() < min_n:
        return None
    q = min(1.0, max(0.0, float(q)))
    v = arr.quantile(q, interpolation="linear")
    return float(v) if v is not None else None


def ols_slope(
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
    normalize: bool = True,
) -> float | None:
    arr = _clean(series)
    n = arr.len()
    if n < min_n:
        return None
    x = pl.int_range(0, n, eager=True).cast(pl.Float64)
    x_mean = float(x.mean())  # type: ignore[arg-type]
    var_x = float(((x - x_mean) ** 2).sum())
    if var_x <= 0.0:
        return 0.0
    y_mean = float(arr.mean())  # type: ignore[arg-type]
    slope = float(((x - x_mean) * (arr - y_mean)).sum()) / var_x
    if not normalize:
        return slope
    median = _median(arr)
    mad = _mad(arr, median)
    scale = _MAD_TO_SIGMA * mad
    if scale <= 0.0:
        scale = _std_pop(arr)
    if scale <= 0.0:
        return None  # constant series — slope is zero but uninformative; caller should abstain
    return slope / scale


__all__ = ["MIN_N_DEFAULT", "ols_slope", "quantile", "robust_z"]
