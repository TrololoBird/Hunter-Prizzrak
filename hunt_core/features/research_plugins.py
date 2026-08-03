"""Research feature plugins: polars-ols, polars-ds (core deps).

⚠ ПУТЬ ЧЕРЕЗ `polars-trading` УДАЛЁН 2026-08-03, И ОН НЕ ИСПОЛНЯЛСЯ НИ РАЗУ.
Здесь стоял `try: import polars_trading … except ImportError: _POLARS_TRADING_AVAILABLE
= False`, а сам пакет **не был объявлен ни в `pyproject.toml`, ни в `uv.lock`** и не стоял
в `.venv`. То есть флаг был константой False, а две функции-зонда
(`_polars_trading_sharpe_expr`, `_polars_trading_drawdown_expr`) — недостижимым кодом,
маскирующимся под опциональную оптимизацию.

Почему удалено, а не подключено: у результата **нет читателя**. Колонки `sharpe_20` и
`current_drawdown` не читает ни один модуль дерева (единственное упоминание вне этого
файла было в докстроке), а оба входа в расчёт — дохлые корни, оба уже перечислены в
`docs/audit/dead-symbols-2026-07-26.txt`: `polars_ta_bridge::polars_trading_sharpe_drawdown`
(строка 65) и `research_snapshot_fields` (строка 77). Подключать зависимость ради колонок,
которых никто не запрашивает, значило бы менять сироту «объявлено, не исполняется» на
сироту «исполняется, не читается».

Заодно ушли два проглатывающих обработчика (`except DEFENSIVE_EXC: continue` / `pass`
вокруг зондов `getattr`) — из тех 96 типизированных, которых ruff S110/S112 не видит.
Молчаливой деградации здесь больше нет, потому что нет и самой развилки: расчёт
sharpe/drawdown теперь безусловный и один.
"""
from __future__ import annotations



import math
from typing import Any

import polars as pl
import polars_ds
import polars_ols
import polars_ols.least_squares as polars_ols_ls
import structlog

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


def add_sharpe_drawdown_features(df: pl.DataFrame, *, window: int = _OLS_WINDOW) -> pl.DataFrame:
    """Добавить ``sharpe_20`` и ``current_drawdown`` — чистый Polars, без внешней библиотеки.

    ⚠ ПЕРЕИМЕНОВАНА ИЗ ``add_polars_trading_features`` 2026-08-03. Прежнее имя стало
    ложью в тот момент, когда из файла ушёл `polars_trading`: функция никогда и не
    вызывала библиотеку (пакет не был установлен), а после удаления зондов не может
    в принципе. Имя, обещающее источник, которого нет, — это тот же класс «name-lie»,
    который ловит `phantom-key-auditor`.

    Обе величины считаются ровно так, как считались всегда на практике: рассчитывались
    именно эти выражения, потому что зонды библиотеки возвращали ``None`` на каждом вызове.
    Числа НЕ меняются — меняется только то, что развилки больше нет.
    """
    if df.is_empty() or "close" not in df.columns:
        return df
    returns = pl.col("close").pct_change()
    sharpe_expr = (
        returns.rolling_mean(window_size=window, min_samples=window)
        / returns.rolling_std(window_size=window, min_samples=window)
    ).alias("sharpe_20")
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
        work = add_sharpe_drawdown_features(work)
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
    "add_sharpe_drawdown_features",
    "compute_return_entropy_50",
    "detect_volume_regime_break",
    "enrich_research_columns",
    "polars_ds_available",
    "polars_ols_available",
    "research_snapshot_fields",
    "symbol_regime_features",
]
