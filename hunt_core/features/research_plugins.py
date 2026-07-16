"""Research feature plugins: polars-ols, polars-trading, polars-ds (core deps)."""
from __future__ import annotations



import math
from typing import Any

import polars as pl
import polars_ds
import polars_ols
import polars_ols.least_squares as polars_ols_ls
try:
    import polars_trading as _polars_trading
    _POLARS_TRADING_AVAILABLE = True
except ImportError:
    _polars_trading = None  # type: ignore[assignment]
    _POLARS_TRADING_AVAILABLE = False

import structlog

from hunt_core.errors import DEFENSIVE_EXC

LOG = structlog.get_logger("hunt_core.features.research_plugins")

_OLS_WINDOW = 20
_ENTROPY_WINDOW = 50
# polars-ds ks_2samp returns statistic=0 / threshold=NaN when either sample has
# fewer than 30 finite values, so each half must be >= 30 (audit H canary: the
# old 50/25 split tripped the _KS_MIN_SAMPLES guard and the detector never fired).
_KS_VOLUME_WINDOW = 64
_KS_HALF = 32
_KS_MIN_SAMPLES = 30


def polars_ols_available() -> bool:
    return True


def polars_trading_available() -> bool:
    return _POLARS_TRADING_AVAILABLE


def polars_ds_available() -> bool:
    return True


def _last_finite(df: pl.DataFrame, column: str) -> float | None:
    if df.is_empty() or column not in df.columns:
        return None
    series = df[column].drop_nulls()
    if series.is_empty():
        return None
    try:
        val = float(series[-1])
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def add_ols_trend_features(df: pl.DataFrame, *, window: int = _OLS_WINDOW) -> pl.DataFrame:
    """Rolling OLS on close → ``trend_slope_20`` (pct/bar) and ``residual_vol``."""
    if df.is_empty() or "close" not in df.columns:
        return df

    index_expr = pl.int_range(0, pl.len()).cast(pl.Float64)
    rolling_kwargs = polars_ols_ls.RollingKwargs(
        window_size=window,
        min_periods=window,
        use_woodbury=None,
        alpha=None,
        null_policy="drop",
    )
    coef_struct = polars_ols.compute_rolling_least_squares(
        pl.col("close"),
        index_expr,
        add_intercept=True,
        mode="coefficients",
        rolling_kwargs=rolling_kwargs,
    )
    resid_series = polars_ols.compute_rolling_least_squares(
        pl.col("close"),
        index_expr,
        add_intercept=True,
        mode="residuals",
        rolling_kwargs=rolling_kwargs,
    )
    work = df.with_columns(coef_struct.alias("_ols_coef"), resid_series.alias("_ols_resid"))
    work = work.with_columns(pl.col("_ols_coef").struct.field("literal").alias("_slope_raw"))
    return work.with_columns(
        [
            (pl.col("_slope_raw") / pl.col("close") * 100.0)
            .fill_nan(0.0)
            .fill_null(0.0)
            .alias("trend_slope_20"),
            pl.col("_ols_resid")
            .rolling_std(window_size=window, min_samples=window)
            .alias("residual_vol"),
        ]
    ).drop("_ols_coef", "_ols_resid", "_slope_raw")


def _polars_trading_sharpe_expr(*, window: int) -> pl.Expr | None:
    pt = _polars_trading
    for attr in ("rolling_sharpe", "sharpe_ratio"):
        fn = getattr(pt, attr, None)
        if callable(fn):
            try:
                out = fn(pl.col("close"), window=window)
                if isinstance(out, pl.Expr):
                    return out.alias("sharpe_20")
            except DEFENSIVE_EXC:
                continue
    metrics = getattr(pt, "metrics", None)
    if metrics is not None:
        rs = getattr(metrics, "rolling_sharpe", None)
        if callable(rs):
            try:
                ret = pl.col("close").pct_change()
                return rs(ret, window=window).alias("sharpe_20")
            except DEFENSIVE_EXC:
                pass
    return None


def _polars_trading_drawdown_expr() -> pl.Expr | None:
    pt = _polars_trading
    for attr in ("current_drawdown", "drawdown"):
        fn = getattr(pt, attr, None)
        if callable(fn):
            try:
                out = fn(pl.col("close"))
                if isinstance(out, pl.Expr):
                    return out.alias("current_drawdown")
            except DEFENSIVE_EXC:
                continue
    metrics = getattr(pt, "metrics", None)
    if metrics is not None:
        dd = getattr(metrics, "current_drawdown", None)
        if callable(dd):
            try:
                return dd(pl.col("close")).alias("current_drawdown")
            except DEFENSIVE_EXC:
                pass
    return None


def add_polars_trading_features(df: pl.DataFrame, *, window: int = _OLS_WINDOW) -> pl.DataFrame:
    """Add ``sharpe_20`` and ``current_drawdown`` via polars-trading API."""
    if df.is_empty() or "close" not in df.columns:
        return df
    sharpe_expr = _polars_trading_sharpe_expr(window=window)
    dd_expr = _polars_trading_drawdown_expr()
    if sharpe_expr is None:
        sharpe_expr = (
            pl.col("close")
            .pct_change()
            .rolling_mean(window_size=window, min_samples=window)
            / pl.col("close").pct_change().rolling_std(window_size=window, min_samples=window)
        ).alias("sharpe_20")
    if dd_expr is None:
        dd_expr = (pl.col("close") / pl.col("close").cum_max() - 1.0).alias("current_drawdown")
    work = df.with_columns([sharpe_expr, dd_expr])
    return work.with_columns(
        [
            pl.when(pl.col("sharpe_20").is_nan()).then(None).otherwise(pl.col("sharpe_20")).alias("sharpe_20"),
            pl.when(pl.col("current_drawdown").is_nan())
            .then(None)
            .otherwise(pl.col("current_drawdown"))
            .alias("current_drawdown"),
        ]
    )


def enrich_research_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Apply OLS trend + trading metrics when columns are not already present."""
    if df.is_empty():
        return df
    work = df
    if "trend_slope_20" not in work.columns:
        work = add_ols_trend_features(work)
    if "sharpe_20" not in work.columns or "current_drawdown" not in work.columns:
        work = add_polars_trading_features(work)
    return work


def research_snapshot_fields(df: Any) -> dict[str, float | None]:
    """Latest scalar research columns for TF snapshots / setup dicts."""
    if df is None or getattr(df, "is_empty", lambda: True)():
        return {}
    if not isinstance(df, pl.DataFrame):
        return {}
    work = enrich_research_columns(df)
    out: dict[str, float | None] = {}
    for key in ("trend_slope_20", "residual_vol", "sharpe_20", "current_drawdown"):
        val = _last_finite(work, key)
        if val is not None:
            out[key] = round(val, 6)
    return out


def compute_return_entropy_50(df: pl.DataFrame) -> float | None:
    """Rolling return entropy over the last 50 bars (polars-ds)."""
    if df.is_empty() or "close" not in df.columns or df.height < _ENTROPY_WINDOW:
        return None
    rets = df["close"].pct_change().tail(_ENTROPY_WINDOW).drop_nulls()
    if rets.len() < 8:
        return None
    tmp = pl.DataFrame({"ret": rets})
    bins = (pl.col("ret") * 1000.0).round(0).cast(pl.Int64)
    ent = tmp.with_columns(bins.alias("bin")).select(
        polars_ds.query_entropy("bin").alias("return_entropy_50")
    ).item()
    if ent is not None and math.isfinite(float(ent)):
        return round(float(ent), 6)
    return None


def detect_volume_regime_break(df: pl.DataFrame, *, window: int = _KS_VOLUME_WINDOW) -> bool:
    """Two-sample KS on recent vs prior volume halves → regime break flag."""
    if df.is_empty() or "volume" not in df.columns or df.height < window:
        return False
    vol = df["volume"].tail(window).cast(pl.Float64)
    if vol.len() < window:
        return False
    v1 = vol.head(_KS_HALF)
    v2 = vol.tail(_KS_HALF)
    if v1.len() < _KS_MIN_SAMPLES or v2.len() < _KS_MIN_SAMPLES:
        return False
    ks_row = pl.DataFrame({"a": v1, "b": v2}).select(
        polars_ds.ks_2samp("a", "b").alias("ks")
    )
    if ks_row.is_empty():
        return False
    ks = ks_row.item(0, 0)
    if isinstance(ks, dict):
        # polars-ds names the second struct field "pvalue" but it is the KS
        # REJECTION THRESHOLD (c(alpha)*sqrt(2/n)), not a p-value: reject the
        # null (= regime break) when statistic > threshold. The old
        # `pvalue <= 0.05` reading compared the threshold itself against 0.05,
        # which is false for any realistic n — the detector could never fire.
        stat = float(ks.get("statistic") or 0.0)
        threshold = float(ks.get("pvalue") or float("nan"))
        return math.isfinite(threshold) and stat > threshold
    return False


def symbol_regime_features(df: pl.DataFrame) -> dict[str, Any]:
    """Per-symbol polars-ds regime features for collect / scoring."""
    if df.is_empty():
        return {}
    out: dict[str, Any] = {}
    ent = compute_return_entropy_50(df)
    if ent is not None:
        out["return_entropy_50"] = ent
    if detect_volume_regime_break(df):
        out["volume_regime_break"] = True
    return out


__all__ = [
    "add_ols_trend_features",
    "add_polars_trading_features",
    "compute_return_entropy_50",
    "detect_volume_regime_break",
    "enrich_research_columns",
    "polars_ds_available",
    "polars_ols_available",
    "polars_trading_available",
    "research_snapshot_fields",
    "symbol_regime_features",
]
