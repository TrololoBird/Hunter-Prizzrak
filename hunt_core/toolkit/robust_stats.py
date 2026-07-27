"""Strategy-neutral robust statistics (extracted from scanner.detect.calibrate). Polars-native.

No numpy: median/MAD/std/quantile/OLS-slope are Polars Series expressions. Parity with the old
numpy version is preserved exactly — ``std(ddof=0)`` matches ``np.std`` (population), and
``quantile(interpolation="linear")`` matches ``np.quantile`` (Polars would otherwise default to
"nearest").
"""
from __future__ import annotations

import math
from typing import cast

import polars as pl

MIN_N_DEFAULT = 30
# Асимптотический множитель MAD→σ: 1/Φ⁻¹(3/4). Точное значение 1.4826022185056020.
_MAD_TO_SIGMA_ASYMPTOTIC = 1.4826022185056020
_DEFAULT_MAD_EPSILON = 1e-6
_DEFAULT_ROBUST_Z_CLIP = 12.0


def mad_to_sigma(n: int) -> float:
    """Множитель MAD→σ, поправленный на КОНЕЧНУЮ выборку.

    ⚠ Асимптотическая константа 1.4826 на коротком окне **занижает σ**, а значит
    **завышает каждый z-скор** — систематическое смещение в сторону ложных срабатываний,
    а не шум. Замер по формуле ниже (доля, на которую прежняя оценка σ была меньше верной):

        n=10  → C_n=1.6201, прежняя σ была меньше верной на **8.49%**
        n=20  → C_n=1.5448, на 4.03%
        n=30  → C_n=1.5227, на **2.63%**   ← это наш ``MIN_N_DEFAULT``
        n=100 → C_n=1.4941, на 0.77%
        n=500 → C_n=1.4849, на 0.15%

    То есть порог, откалиброванный на длинном окне, на окне длиной 30 срабатывал так,
    как будто он на 2.6% ниже. Раньше здесь стояла одна константа на любое n.

    Формула — предсказание Акиншина для несмещённого множителя:
    ``C_n = 1 / (Φ⁻¹(3/4) · (1 + A_n))``, ``A_n = −0.76213/n − 0.86413/n²``.
    На малых n она отходит от табличных значений симуляции Park–Kim–Wang не более чем на
    0.3% (n=10: 1.6201 против 1.6247), что на порядок меньше исправляемого смещения.

    Args:
        n: Число наблюдений в выборке.

    Returns:
        Множитель, на который надо умножить MAD, чтобы получить оценку σ.
    """
    if n < 2:
        return _MAD_TO_SIGMA_ASYMPTOTIC
    a_n = -0.76213 / n - 0.86413 / (n * n)
    return 1.0 / (0.6744897501960817 * (1.0 + a_n))

# Polars types the aggregate return as a wide union covering temporal series
# (``Decimal | date | time | timedelta | ...``). Every caller here passes a NUMERIC series —
# that is this module's contract, stated in the docstring above — so the cast is a runtime
# no-op that records the invariant instead of suppressing the check with ``type: ignore``.
# Needed once ``hunt_core.toolkit.*`` stopped being excluded from mypy (2026-07-26).


def _median(s: pl.Series) -> float:
    v = s.median()
    return float(cast(float, v)) if v is not None else 0.0


def _std_pop(s: pl.Series) -> float:
    v = s.std(ddof=0)  # population std, matching np.std's default
    return float(cast(float, v)) if v is not None else 0.0


def _mad(s: pl.Series, median: float) -> float:
    v = (s - median).abs().median()
    return float(cast(float, v)) if v is not None else 0.0


def _robust_scale(arr: pl.Series, *, mad_epsilon: float) -> float:
    """Робастная оценка σ. Множитель берётся ПО ДЛИНЕ выборки (см. ``mad_to_sigma``).

    ⚠ Ветка отката на ``std`` меняет КЛАСС оценки: у медианы и MAD точка слома 50%, у
    среднего и std — 0%, то есть одно значение способно увести оценку куда угодно. Откат
    случается, когда MAD = 0, а это не экзотика: MAD обращается в ноль, когда **больше
    половины наблюдений совпадают с медианой**, — штатное состояние для кванованной тиком
    цены на неликвиде и для разреженных счётчиков (касания, события ликвидаций).
    Откат оставлен (он всё же не фабрикует число, а даёт какой-то разброс), но помечен:
    правильная замена — Qn Руссо-Кру (эффективность 82% против 37% у MAD, точка слома та же
    50%, и он настраивается против вырождения на связках). Отдельной задачей.
    """
    median = _median(arr)
    mad = _mad(arr, median)
    scale = max(mad_to_sigma(arr.len()) * mad, mad_epsilon)
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
    scale = mad_to_sigma(n) * mad  # поправка на конечную выборку — та же, что в `_robust_scale`
    if scale <= 0.0:
        scale = _std_pop(arr)
    if scale <= 0.0:
        return None  # constant series — slope is zero but uninformative; caller should abstain
    return slope / scale


__all__ = ["MIN_N_DEFAULT", "mad_to_sigma", "ols_slope", "quantile", "robust_z"]
