"""Central polars_ta / polars_ta.tdx backend for hunt indicator pipeline.

Core deps (polars_ta, polars-ols, polars-ds, polars-trading) are required at startup
via ``hunt_core.bootstrap.require_feature_stack``. Scale normalization (0..1 → 0..100)
is applied for oscillators that need it.
"""
from __future__ import annotations



from collections.abc import Callable
from typing import Any, cast

import polars as pl
import polars_ta.ta as plta
import polars_ta.tdx as ptdx
import polars_ta.wq as wq
import structlog

from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.research_plugins import (
    add_polars_trading_features,
    polars_trading_available,
)

from .shared import clean_non_finite, materialize_series

LOG = structlog.get_logger("hunt_core.features.polars_ta_bridge")
_BRIDGE_SKIPS: set[str] = set()

BROKEN_PLTA_FUNCTIONS: frozenset[str] = frozenset({"SMA", "WMA", "KAMA", "LINEARREG"})


def polars_ta_available() -> bool:
    return True


def _log_skip(name: str, exc: BaseException) -> None:
    if name not in _BRIDGE_SKIPS:
        _BRIDGE_SKIPS.add(name)
        LOG.info("polars_ta bridge skip", indicator=name, error=str(exc))


def _normalize_percent_scale(series: pl.Series, *, name: str) -> pl.Series:
    numeric = series.cast(pl.Float64, strict=False)
    finite = numeric.drop_nulls()
    if finite.is_empty():
        return numeric.rename(name)
    try:
        max_val = finite.max()
        min_val = finite.min()
        overall_max = float(cast("Any", max_val)) if max_val is not None else 0.0
        overall_min = float(cast("Any", min_val)) if min_val is not None else 0.0
    except (TypeError, ValueError):
        return numeric.rename(name)
    if overall_max <= 1.5 and overall_min >= -0.01:
        return (numeric * 100.0).rename(name)
    return numeric.rename(name)


def _select_series(df: pl.DataFrame, expr: pl.Expr | pl.Series, *, name: str) -> pl.Series:
    return materialize_series(expr, df=df, name=name)


def _maybe_percent(series: pl.Series, *, name: str) -> pl.Series:
    return _normalize_percent_scale(series, name=name)


def _clean(series: pl.Series, *, fill: float) -> pl.Series:
    return clean_non_finite(series, fill=fill)


def _series_from_expr(
    df: pl.DataFrame,
    expr: pl.Expr,
    *,
    name: str,
    fill: float | None = None,
    percent: bool = False,
    clip: tuple[float, float] | None = None,
) -> pl.Series:
    raw = _select_series(df, expr, name=name)
    if percent:
        raw = _maybe_percent(raw, name=name)
    if fill is not None:
        raw = _clean(raw, fill=fill)
    if clip is not None:
        raw = raw.clip(clip[0], clip[1])
    return raw


def _try_scalar_expr(
    df: pl.DataFrame,
    *,
    name: str,
    builder: Callable[[], pl.Expr],
    fill: float = 0.0,
    percent: bool = False,
    clip: tuple[float, float] | None = None,
    skip_prefix: str = "plta",
) -> pl.Series | None:
    try:
        return _series_from_expr(
            df, builder(), name=name, fill=fill, percent=percent, clip=clip
        ).alias(name)
    except DEFENSIVE_EXC as exc:
        _log_skip(f"{skip_prefix}_{name}", exc)
        return None


def _struct_field_series(
    df: pl.DataFrame,
    struct_expr: pl.Expr,
    field: str,
    *,
    name: str,
) -> pl.Series:
    result = df.select(struct_expr)
    sc = result.get_column(result.columns[0])
    return _select_series(df, sc.struct.field(field), name=name)


def _struct_tuple(
    df: pl.DataFrame,
    struct_expr: pl.Expr,
    *fields: tuple[str, str],
) -> tuple[pl.Series, ...]:
    return tuple(
        _struct_field_series(df, struct_expr, src, name=dest) for src, dest in fields
    )


def _ohlc() -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    return pl.col("high"), pl.col("low"), pl.col("close")


def adx_from_polars_ta(
    df: pl.DataFrame,
    period: int = 14,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """ADX + DI via polars_ta.tdx (0..1 backend → 0..100)."""
    high, low, close = _ohlc()
    n = int(period)
    adx = _series_from_expr(
        df, ptdx.ADX(high, low, close, N=n), name=f"adx{period}", fill=0.0, percent=True, clip=(0.0, 100.0)
    )
    plus_di = _series_from_expr(
        df,
        ptdx.PLUS_DI(high, low, close, N=n),
        name=f"plus_di{period}",
        fill=0.0,
        percent=True,
        clip=(0.0, 100.0),
    )
    minus_di = _series_from_expr(
        df,
        ptdx.MINUS_DI(high, low, close, N=n),
        name=f"minus_di{period}",
        fill=0.0,
        percent=True,
        clip=(0.0, 100.0),
    )
    return adx, plus_di, minus_di


def cci_from_polars_ta(df: pl.DataFrame, period: int = 20) -> pl.Series:
    high, low, close = _ohlc()
    return _series_from_expr(
        df, ptdx.CCI(high, low, close, N=int(period)), name=f"cci{period}", fill=0.0
    )


def mfi_from_polars_ta(df: pl.DataFrame, period: int = 14) -> pl.Series:
    high, low, close = _ohlc()
    return _series_from_expr(
        df,
        ptdx.MFI(close, high, low, pl.col("volume"), N=int(period)),
        name=f"mfi{period}",
        fill=50.0,
        percent=True,
        clip=(0.0, 100.0),
    )


def willr_from_polars_ta(df: pl.DataFrame, period: int = 14) -> pl.Series:
    high, low, close = _ohlc()
    raw = _series_from_expr(
        df,
        plta.WILLR(high, low, close, timeperiod=int(period)),
        name=f"willr{period}",
        percent=True,
    )
    raw_max = raw.max()
    raw_min = raw.min()
    if raw_max is not None and float(cast("Any", raw_max)) <= 0.0 and raw_min is not None and float(cast("Any", raw_min)) >= -1.5:
        raw = raw * 100.0
    return _clean(raw, fill=-50.0).clip(-100.0, 0.0)


_EXTENDED_BUILDERS: list[tuple[str, str, Callable[[], pl.Expr], float, bool, tuple[float, float] | None]] = [
    ("plta", "mom10", lambda: plta.MOM(pl.col("close"), timeperiod=10), 0.0, False, None),
    ("plta", "trix14", lambda: plta.TRIX(pl.col("close"), timeperiod=14), 0.0, False, None),
    ("plta", "ppo12_26", lambda: plta.PPO(pl.col("close"), fastperiod=12, slowperiod=26), 0.0, False, None),
    ("plta", "rsv14", lambda: plta.RSV(*_ohlc(), timeperiod=14), 0.0, True, (0.0, 100.0)),
    ("plta", "rocp10", lambda: plta.ROCP(pl.col("close"), timeperiod=10), 0.0, False, None),
    ("plta", "rocr10", lambda: plta.ROCR(pl.col("close"), timeperiod=10), 0.0, False, None),
    ("plta", "ad_line", lambda: plta.AD(*_ohlc(), pl.col("volume")), 0.0, False, None),
    (
        "plta",
        "adosc_3_10",
        lambda: plta.ADOSC(*_ohlc(), pl.col("volume"), fastperiod=3, slowperiod=10),
        0.0,
        False,
        None,
    ),
    ("plta", "rma14", lambda: plta.RMA(pl.col("close"), timeperiod=14), 0.0, False, None),
    ("plta", "trange14", lambda: plta.TRANGE(*_ohlc()), 0.0, False, None),
    ("tdx", "mtm12", lambda: ptdx.MTM(pl.col("close"), N=12), 0.0, False, None),
    ("tdx", "psy12", lambda: ptdx.PSY(pl.col("close"), N=12), 50.0, True, (0.0, 100.0)),
    ("tdx", "dpo20", lambda: ptdx.DPO(pl.col("close"), N=20), 0.0, False, None),
    ("tdx", "bias6", lambda: ptdx.BIAS(pl.col("close"), N=6), 0.0, False, None),
    ("tdx", "emv14", lambda: ptdx.EMV(pl.col("high"), pl.col("low"), pl.col("volume"), N=14), 0.0, False, None),
    ("tdx", "tdx_boll_mid", lambda: ptdx.BOLL_M(pl.col("close"), M=20, N=2), 0.0, False, None),
    ("tdx", "tdx_boll_upper", lambda: ptdx.BOLL(pl.col("close"), M=20, N=2), 0.0, False, None),
]


def polars_ta_extended_exprs(df: pl.DataFrame) -> list[pl.Series]:
    """Extra polars_ta.ta / tdx columns for pinned deep analysis."""
    out: list[pl.Series] = []
    for prefix, name, builder, fill, percent, clip in _EXTENDED_BUILDERS:
        expr = _try_scalar_expr(
            df,
            name=name,
            builder=builder,
            fill=fill,
            percent=percent,
            clip=clip,
            skip_prefix=prefix,
        )
        if expr is not None:
            out.append(expr)

    kdj_exprs = _kdj_exprs(df)
    if kdj_exprs:
        out.extend(kdj_exprs)
    return out


def _kdj_exprs(df: pl.DataFrame) -> list[pl.Series]:
    if df.is_empty():
        return []
    try:
        high, low, close = _ohlc()
        result = df.select(ptdx.KDJ(high, low, close, N=9, M1=3, M2=3))
        sc = result.get_column(result.columns[0])
        k_raw = materialize_series(sc.struct.field("K"), df=df, name="kdj_k")
        d_raw = materialize_series(sc.struct.field("D"), df=df, name="kdj_d")
        k = _maybe_percent(_clean(k_raw, fill=50.0), name="kdj_k14")
        d = _maybe_percent(_clean(d_raw, fill=50.0), name="kdj_d14")
        try:
            j_raw = materialize_series(sc.struct.field("J"), df=df, name="kdj_j")
        except (KeyError, ValueError, TypeError):
            j_raw = materialize_series(3.0 * k_raw - 2.0 * d_raw, df=df, name="kdj_j")
        j = _clean(j_raw, fill=50.0)
        return [
            k.clip(0.0, 100.0).alias("kdj_k14"),
            d.clip(0.0, 100.0).alias("kdj_d14"),
            j.alias("kdj_j14"),
        ]
    except DEFENSIVE_EXC as exc:
        _log_skip("kdj_tdx", exc)
        return []


def polars_wq_exprs(df: pl.DataFrame) -> list[pl.Series]:
    """WorldQuant-style context features for pinned deep analysis."""
    close = pl.col("close")
    volume = pl.col("volume")
    out: list[pl.Series] = []
    for skip, name, builder, fill, clip in (
        ("wq_ts_rank", "wq_ts_rank_close20", lambda: wq.ts_rank(close, 20), 0.5, (0.0, 1.0)),
        ("wq_ts_corr", "wq_ts_corr_close_vol20", lambda: wq.ts_corr(close, volume, 20), 0.0, (-1.0, 1.0)),
    ):
        try:
            raw = _select_series(df, builder(), name=name)
            out.append(_clean(raw, fill=fill).clip(clip[0], clip[1]).alias(name))
        except DEFENSIVE_EXC as exc:
            _log_skip(skip, exc)
    if "rsi14" in df.columns:
        try:
            delta = _select_series(df, wq.ts_delta(pl.col("rsi14"), 5), name="wq_ts_delta_rsi5")
            out.append(_clean(delta, fill=0.0).alias("wq_ts_delta_rsi5"))
        except DEFENSIVE_EXC as exc:
            _log_skip("wq_ts_delta_rsi", exc)
    return out


def ema_series(df: pl.DataFrame, period: int) -> pl.Series:
    return _series_from_expr(
        df, plta.EMA(pl.col("close"), timeperiod=int(period)), name=f"ema{period}"
    )


def rsi_series(df: pl.DataFrame, period: int = 14) -> pl.Series:
    # clip to the oscillator's mathematical bound: a 1.0 backend value → 100.0 can
    # carry float noise (100.0000001) that otherwise trips the [0,100] range-defect
    # and rejects the whole symbol. Matches adx_from_polars_ta / mfi_from_polars_ta.
    return _series_from_expr(
        df,
        plta.RSI(pl.col("close"), timeperiod=int(period)),
        name=f"rsi{period}",
        percent=True,
        clip=(0.0, 100.0),
    )


def atr_series(df: pl.DataFrame, period: int = 14) -> pl.Series:
    high, low, close = _ohlc()
    return _series_from_expr(
        df, plta.ATR(high, low, close, timeperiod=int(period)), name=f"atr{period}"
    )


def natr_series(df: pl.DataFrame, period: int = 14) -> pl.Series:
    high, low, close = _ohlc()
    return _series_from_expr(
        df, plta.NATR(high, low, close, timeperiod=int(period)), name=f"natr{period}"
    )


def roc_series(df: pl.DataFrame, period: int = 10) -> pl.Series:
    return _series_from_expr(
        df, plta.ROC(pl.col("close"), timeperiod=int(period)), name=f"roc{period}"
    )


def macd_series(
    df: pl.DataFrame,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pl.Series, ...]:
    struct_expr = plta.MACD(
        pl.col("close"),
        fastperiod=int(fast),
        slowperiod=int(slow),
        signalperiod=int(signal),
    )
    return _struct_tuple(
        df,
        struct_expr,
        ("macd", "macd_line"),
        ("macdsignal", "macd_signal"),
        ("macdhist", "macd_hist"),
    )


def stochastic_series(
    df: pl.DataFrame,
    *,
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> tuple[pl.Series, pl.Series]:
    struct_expr = plta.STOCHF(
        *_ohlc(),
        fastk_period=int(period),
        fastd_period=int(smooth_d),
    )
    k_raw, d_raw = _struct_tuple(
        df,
        struct_expr,
        ("fastk", "stoch_k14"),
        ("fastd", "stoch_d14"),
    )
    # clip to [0,100]: a 1.0 backend extreme → 100.0 can carry float noise that
    # trips the stoch_k14/stoch_d14 range-defect and rejects the whole symbol
    # (notably deep oversold/overbought = prime capitulation/exhaustion setups).
    k = _clean(_maybe_percent(k_raw, name="stoch_k14"), fill=50.0).clip(0.0, 100.0)
    d = _clean(_maybe_percent(d_raw, name="stoch_d14"), fill=50.0).clip(0.0, 100.0)
    return k, d


def bbands_series(
    df: pl.DataFrame,
    *,
    period: int = 20,
    nbdev: float = 2.0,
) -> tuple[pl.Series, ...]:
    struct_expr = plta.BBANDS(
        pl.col("close"), timeperiod=int(period), nbdevup=float(nbdev), nbdevdn=float(nbdev)
    )
    return _struct_tuple(
        df,
        struct_expr,
        ("upperband", "bb_upper"),
        ("middleband", "bb_mid"),
        ("lowerband", "bb_lower"),
    )


def aroon_series(df: pl.DataFrame, *, period: int = 14) -> tuple[pl.Series, pl.Series, pl.Series]:
    up, down = _struct_tuple(
        df,
        plta.AROON(pl.col("high"), pl.col("low"), timeperiod=int(period)),
        ("aroonup", f"aroon_up{period}"),
        ("aroondown", f"aroon_down{period}"),
    )
    osc = materialize_series(up - down, df=df, name=f"aroon_osc{period}")
    return up, down, osc


def obv_series(df: pl.DataFrame) -> pl.Series:
    return _series_from_expr(df, plta.OBV(pl.col("close"), pl.col("volume")), name="obv")


def polars_trading_sharpe_drawdown(
    df: pl.DataFrame,
    *,
    window: int = 20,
) -> pl.DataFrame:
    """``sharpe_20`` + ``current_drawdown`` via polars-trading."""
    return add_polars_trading_features(df, window=window)


__all__ = [
    "BROKEN_PLTA_FUNCTIONS",
    "adx_from_polars_ta",
    "aroon_series",
    "atr_series",
    "bbands_series",
    "cci_from_polars_ta",
    "ema_series",
    "macd_series",
    "mfi_from_polars_ta",
    "natr_series",
    "obv_series",
    "polars_ta_available",
    "polars_trading_available",
    "polars_trading_sharpe_drawdown",
    "polars_ta_extended_exprs",
    "polars_wq_exprs",
    "roc_series",
    "rsi_series",
    "stochastic_series",
    "willr_from_polars_ta",
]
