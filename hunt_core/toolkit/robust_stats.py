"""Strategy-neutral robust statistics (extracted from scanner.detect.calibrate)."""
from __future__ import annotations

import numpy as np
import polars as pl

MIN_N_DEFAULT = 30
_MAD_TO_SIGMA = 1.4826
_DEFAULT_MAD_EPSILON = 1e-6
_DEFAULT_ROBUST_Z_CLIP = 12.0


def _robust_scale(arr: np.ndarray, *, mad_epsilon: float) -> float:
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    scale = max(_MAD_TO_SIGMA * mad, mad_epsilon)
    if scale <= mad_epsilon:
        std = float(np.std(arr))
        if std <= mad_epsilon:
            return mad_epsilon
        return max(std, mad_epsilon)
    return scale


def _clip_z(z: float, *, clip: float) -> float:
    if not np.isfinite(z):
        return 0.0
    return float(max(-clip, min(clip, z)))


def _clean(series: pl.Series | None) -> np.ndarray:
    if series is None or series.len() == 0:
        return np.empty(0, dtype=np.float64)
    arr = series.cast(pl.Float64, strict=False).to_numpy()
    return arr[np.isfinite(arr)]


def robust_z(
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
    mad_epsilon: float = _DEFAULT_MAD_EPSILON,
    clip: float = _DEFAULT_ROBUST_Z_CLIP,
) -> float | None:
    arr = _clean(series)
    if arr.size < min_n:
        return None
    last = float(arr[-1])
    scale = _robust_scale(arr, mad_epsilon=mad_epsilon)
    if scale <= mad_epsilon and float(np.std(arr)) <= mad_epsilon:
        return None  # constant series — no distribution to score against; caller should abstain
    return _clip_z((last - float(np.median(arr))) / scale, clip=clip)


def quantile(series: pl.Series | None, q: float, *, min_n: int = MIN_N_DEFAULT) -> float | None:
    arr = _clean(series)
    if arr.size < min_n:
        return None
    q = min(1.0, max(0.0, float(q)))
    return float(np.quantile(arr, q))


def ols_slope(
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
    normalize: bool = True,
) -> float | None:
    arr = _clean(series)
    if arr.size < min_n:
        return None
    x = np.arange(arr.size, dtype=np.float64)
    x_mean = x.mean()
    var_x = float(np.sum((x - x_mean) ** 2))
    if var_x <= 0.0:
        return 0.0
    y_mean = float(arr.mean())
    slope = float(np.sum((x - x_mean) * (arr - y_mean)) / var_x)
    if not normalize:
        return slope
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    scale = _MAD_TO_SIGMA * mad
    if scale <= 0.0:
        scale = float(np.std(arr))
    if scale <= 0.0:
        return None  # constant series — slope is zero but uninformative; caller should abstain
    return slope / scale


__all__ = ["MIN_N_DEFAULT", "ols_slope", "quantile", "robust_z"]
